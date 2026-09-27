from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Iterable

from sqlalchemy import select, update

from app.core.config import Settings
from app.core.logging import task_progress
from app.db.models import DetectionRegion, Page, Project, Task, TaskItem
from app.db.session import Database
from app.providers.llm.base import (
    LLMError,
    LLMProfileConfig,
    LLMProtocolError,
    LLMRuntime,
    LLMSecretStoreError,
    TranslationInput,
    TranslationRegion,
    is_copied_japanese_text,
)
from app.providers.llm.secrets import SecretStore
from app.services.render_state import mark_page_render_outdated
from app.services.tasks import TaskBroadcaster, task_blocked_by_paused_batch, utc_now

logger = logging.getLogger(__name__)


def _safe_task_error(error: Exception, fallback: str) -> str:
    """Persist only bounded, known provider errors; never log arbitrary bodies."""
    if isinstance(error, LLMError):
        text = str(error).strip()
        return text[:500] if text else fallback
    return fallback


def aggregate_translation_status(statuses: Iterable[str]) -> str:
    values = list(statuses)
    if not values:
        return "pending"
    if "processing" in values:
        return "processing"
    if "outdated" in values:
        return "outdated"
    if "pending" in values:
        return "pending"
    successful = sum(value in {"completed", "manual"} for value in values)
    failed = values.count("failed")
    if failed and successful:
        return "completed_with_warnings"
    if failed:
        return "failed"
    return "completed"


async def refresh_page_translation_status(session, page_id: str) -> str:
    statuses = list(
        (
            await session.scalars(
                select(DetectionRegion.translation_status).where(DetectionRegion.page_id == page_id)
            )
        ).all()
    )
    value = aggregate_translation_status(statuses)
    await session.execute(update(Page).where(Page.id == page_id).values(translation_status=value))
    return value


def is_untranslated_provider_region(region: DetectionRegion, project: Project) -> bool:
    return (
        region.translation_status == "completed"
        and region.target_text_origin == "provider"
        and region.sfx_strategy is None
        and is_copied_japanese_text(
            region.source_text or "",
            region.target_text,
            project.source_language,
            project.target_language,
        )
    )


def deterministic_reading_order(
    regions: list[DetectionRegion], language: str
) -> list[DetectionRegion]:
    """Cluster by vertical position and sort within a line by script direction."""
    if not regions:
        return []
    average_height = sum(max(1.0, region.y2 - region.y1) for region in regions) / len(regions)
    threshold = max(8.0, average_height * 0.65)
    lines: list[list[DetectionRegion]] = []
    for region in sorted(regions, key=lambda item: (item.y1, item.x1, item.id)):
        line = next((item for item in lines if abs(item[0].y1 - region.y1) <= threshold), None)
        if line is None:
            lines.append([region])
        else:
            line.append(region)
    right_to_left = language in {"ja", "ko"}
    ordered: list[DetectionRegion] = []
    for line in sorted(lines, key=lambda group: min(item.y1 for item in group)):
        ordered.extend(sorted(line, key=lambda item: (item.x1, item.id), reverse=right_to_left))
    return ordered


class TranslationTaskManager:
    """Persistent current-page translation tasks with optimistic result writes."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        runtime: LLMRuntime,
        secret_store: SecretStore | None,
        broadcaster: TaskBroadcaster,
    ) -> None:
        self.database = database
        self.settings = settings
        self.runtime = runtime
        self.secret_store = secret_store
        self.broadcaster = broadcaster
        self._active: dict[str, asyncio.Task[None]] = {}
        self._semaphore = asyncio.Semaphore(settings.llm_concurrency)
        self._profile_semaphores: dict[tuple[str, int], asyncio.Semaphore] = {}

    async def start(self) -> None:
        async with self.database.session_factory() as session:
            task_ids = list(
                (
                    await session.scalars(
                        select(Task.id).where(
                            Task.task_type == "quick_translation",
                            Task.status.in_(["pending", "running"]),
                        )
                    )
                ).all()
            )
            if task_ids:
                await session.execute(
                    update(Task)
                    .where(Task.id.in_(task_ids), Task.status == "running")
                    .values(status="pending", stage="recovering", updated_at=utc_now())
                )
                await session.execute(
                    update(TaskItem)
                    .where(
                        TaskItem.task_id.in_(task_ids),
                        TaskItem.status.in_(["running", "processing"]),
                    )
                    .values(status="pending")
                )
                await session.commit()
        async with self.database.session_factory() as session:
            for task_id in task_ids:
                if not await task_blocked_by_paused_batch(session, task_id):
                    self.enqueue(task_id)

    async def stop(self) -> None:
        active = list(self._active.values())
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        self._active.clear()

    def enqueue(self, task_id: str) -> None:
        current = self._active.get(task_id)
        if current and not current.done():
            return
        task = asyncio.create_task(self._run_guarded(task_id), name=f"translate-{task_id}")
        self._active[task_id] = task
        task.add_done_callback(lambda _done, key=task_id: self._active.pop(key, None))

    async def cancel(self, task_id: str) -> None:
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                raise LookupError(task_id)
            if task.task_type != "quick_translation":
                raise ValueError(task_id)
            if task.status not in {"completed", "failed", "cancelled"}:
                task.cancel_requested = True
                task.updated_at = utc_now()
                await session.commit()
        await self.broadcaster.notify(task_id)

    async def interrupt_project(self, project_id: str) -> list[str]:
        async with self.database.session_factory() as session:
            tasks = list(
                (
                    await session.scalars(
                        select(Task).where(
                            Task.project_id == project_id,
                            Task.task_type == "quick_translation",
                            Task.status.in_(["pending", "running"]),
                        )
                    )
                ).all()
            )
            ids = [task.id for task in tasks]
            for task in tasks:
                task.cancel_requested = True
            await session.commit()
        running = [self._active[item] for item in ids if item in self._active]
        for active in running:
            active.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)
        return ids

    async def _run_guarded(self, task_id: str) -> None:
        async with self._semaphore:
            try:
                await self._run_task(task_id)
            except asyncio.CancelledError:
                raise
            except LLMSecretStoreError as exc:
                await self._fail_task(task_id, exc.code, _safe_task_error(exc, "凭据存储不可用"))
            except Exception:
                logger.error("Translation task failed", extra={"request_id": "-"})
                await self._fail_task(task_id, "TRANSLATION_FAILED", "翻译任务失败")

    async def _run_task(self, task_id: str) -> None:
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None or task.status not in {"pending", "running"}:
                return
            recovering = task.stage == "recovering"
            task.status = "running"
            task.stage = "preparing_context"
            task.started_at = task.started_at or utc_now()
            task.updated_at = utc_now()
            parameters = json.loads(task.parameters_json or "{}")
            item_ids = list(
                (
                    await session.scalars(
                        select(TaskItem.id)
                        .where(TaskItem.task_id == task_id, TaskItem.status == "pending")
                        .order_by(TaskItem.sequence_index)
                    )
                ).all()
            )
            first_item = await session.get(TaskItem, item_ids[0]) if item_ids else None
            standalone = task.parent_task_id is None
            total = task.total
            page_index = first_item.page_index if first_item else None
            await session.commit()
        if standalone:
            task_progress(
                task_id,
                "resumed" if recovering else "started",
                task_type="quick_translation",
                stage="preparing_context",
                page_index=page_index,
                total=total,
                message="服务重启后恢复执行" if recovering else None,
            )
        await self.broadcaster.notify(task_id)
        if not item_ids:
            await self._finish(task_id)
            return
        await self._process_page(task_id, item_ids, parameters)
        if await self._cancel_requested(task_id):
            await self._finish_cancelled(task_id)
        else:
            await self._finish(task_id)

    async def _process_page(
        self, task_id: str, item_ids: list[str], parameters: dict[str, object]
    ) -> None:
        process_started = time.perf_counter()
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                return
            items = list(
                (
                    await session.scalars(
                        select(TaskItem)
                        .where(TaskItem.id.in_(item_ids))
                        .order_by(TaskItem.sequence_index, TaskItem.id)
                    )
                ).all()
            )
            regions = [await session.get(DetectionRegion, item.region_id) for item in items]
            regions = [region for region in regions if region is not None]
            page = await session.get(Page, items[0].page_id) if items else None
            project = await session.get(Project, task.project_id)
            if page is None or project is None:
                return
            profile_id = str(parameters.get("profile_id") or project.llm_profile_id or "")
            profile = await self._profile_config(session, profile_id)
            context_started = time.perf_counter()
            context = await self._context(session, page, project)
            context_ms = (time.perf_counter() - context_started) * 1000
            render_changed = False
            for item in items:
                item.status = "processing"
            task.stage = "translating"
            task.current_page_id = page.id
            task.updated_at = utc_now()
            await session.commit()
            inputs = tuple(
                TranslationRegion(
                    region_id=region.id,
                    source_text=region.source_text or "",
                    class_name=region.class_name,
                    reading_order=region.reading_order or index + 1,
                    sfx_strategy=region.sfx_strategy,
                )
                for index, region in enumerate(regions)
            )
            standalone = task.parent_task_id is None
            page_index = page.page_index
            total = task.total
        if standalone:
            task_progress(
                task_id,
                "started",
                task_type="quick_translation",
                stage="translation",
                translation_mode="quick",
                page_index=page_index,
                total=total,
                provider=profile.provider,
                model=profile.model,
            )
        await self.broadcaster.notify(task_id)
        profile_key = (profile.id, profile.max_concurrency)
        profile_semaphore = self._profile_semaphores.setdefault(
            profile_key, asyncio.Semaphore(profile.max_concurrency)
        )
        profile_wait_started = time.perf_counter()
        try:
            async with profile_semaphore:
                profile_queue_ms = (time.perf_counter() - profile_wait_started) * 1000
                result = await self.runtime.translate_page(
                    profile,
                    TranslationInput(
                        source_language=project.source_language,
                        target_language=project.target_language,
                        regions=inputs,
                        context=tuple(context),
                    ),
                )
        except Exception as exc:
            await self._mark_all_failed(
                task_id,
                item_ids,
                _safe_task_error(exc, "当前页翻译失败"),
                error_code=exc.code if isinstance(exc, LLMError) else "TRANSLATION_FAILED",
                provider=profile.provider,
                model=profile.model,
            )
            return
        persist_started = time.perf_counter()
        if await self._cancel_requested(task_id):
            await self._mark_cancelled_items(task_id, item_ids)
            return
        expected_ids = {region.id for region in regions}
        if set(result.translations) != expected_ids or any(
            not isinstance(value, str) or not value.strip() or len(value) > 10_000
            for value in result.translations.values()
        ):
            await self._mark_all_failed(
                task_id,
                item_ids,
                "Provider 未准确覆盖所有翻译区域",
                error_code=LLMProtocolError.code,
                provider=result.provider,
                model=result.model,
            )
            return
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                return
            for item_id in item_ids:
                item = await session.get(TaskItem, item_id)
                region = await session.get(DetectionRegion, item.region_id) if item else None
                if item is None or region is None:
                    if item:
                        item.status = "skipped"
                        task.skipped += 1
                    continue
                text = result.translations.get(region.id)
                if text is None or len(text) > 10_000:
                    item.status = "failed"
                    item.error_code = "TRANSLATION_EMPTY_RESULT"
                    item.error_message = "Provider 未返回有效译文"
                    if region.target_text_origin == "manual":
                        # An explicitly requested overwrite must never erase
                        # the last human translation when the provider returns
                        # an empty/invalid item.
                        region.translation_status = "manual"
                        region.translation_error = None
                    else:
                        region.translation_status = "failed"
                        region.translation_error = item.error_message
                    task.failed += 1
                    task.error_code = task.error_code or item.error_code
                    task.error_message = task.error_message or item.error_message
                    continue
                claim = await session.execute(
                    update(DetectionRegion)
                    .where(
                        DetectionRegion.id == region.id,
                        DetectionRegion.geometry_revision == item.expected_revision,
                        DetectionRegion.ocr_revision == item.expected_content_revision,
                        DetectionRegion.translation_revision == item.expected_output_revision,
                        DetectionRegion.translation_status == "processing",
                    )
                    .values(
                        target_text=text,
                        target_text_origin="provider",
                        translation_mode="quick",
                        translation_status="completed",
                        llm_profile_id=profile.id,
                        llm_provider=result.provider,
                        llm_model=result.model,
                        translation_error=None,
                        translated_from_ocr_revision=item.expected_content_revision,
                        translation_revision=item.expected_output_revision + 1,
                        translation_updated_at=utc_now(),
                        updated_at=utc_now(),
                    )
                )
                if claim.rowcount != 1:
                    item.status = "skipped"
                    item.error_code = "TRANSLATION_CONFLICT"
                    item.error_message = "区域已发生更新，旧译文未写入"
                    task.skipped += 1
                else:
                    item.status = "completed"
                    item.error_code = None
                    item.error_message = None
                    task.completed += 1
                    render_changed = True
            task.updated_at = utc_now()
            if render_changed:
                await mark_page_render_outdated(
                    session, page, reason="译文已更新，需要重新生成成品"
                )
            await refresh_page_translation_status(session, page.id)
            task.result_json = json.dumps(
                {
                    "provider": result.provider,
                    "model": result.model,
                    "regions": len(inputs),
                    "timings_ms": {
                        "context": round(context_ms, 2),
                        "profile_queue": round(profile_queue_ms, 2),
                        "provider": result.latency_ms,
                        "persist": round((time.perf_counter() - persist_started) * 1000, 2),
                        "total": round((time.perf_counter() - process_started) * 1000, 2),
                    },
                    "provider_diagnostics": result.diagnostics,
                },
                ensure_ascii=False,
            )
            await session.commit()
            standalone = task.parent_task_id is None
            completed = task.completed
            failed = task.failed
            skipped = task.skipped
            total = task.total
            error_code = task.error_code
            error_message = task.error_message
        await self.broadcaster.notify(task_id)
        if standalone:
            task_progress(
                task_id,
                "warning" if failed or skipped else "completed",
                task_type="quick_translation",
                stage="translation",
                translation_mode="quick",
                page_index=page_index,
                total=total,
                completed=completed,
                failed=failed,
                skipped=skipped,
                provider=result.provider,
                model=result.model,
                error_code=error_code,
                message=error_message,
            )

    async def _profile_config(self, session, profile_id: str) -> LLMProfileConfig:
        from app.db.models import APIProfile

        profile = await session.get(APIProfile, profile_id) if profile_id else None
        if profile is None or profile.profile_type != "llm":
            raise LLMSecretStoreError("尚未配置可用的翻译 Profile")
        if self.secret_store is None:
            raise LLMSecretStoreError("Windows 凭据存储不可用")
        try:
            key = await self.secret_store.get(profile.credential_target)
        except LLMSecretStoreError:
            raise
        except Exception as exc:
            raise LLMSecretStoreError("Windows 凭据存储不可用") from exc
        if not key:
            raise LLMSecretStoreError("翻译 Profile 缺少 API Key")
        return LLMProfileConfig(
            id=profile.id,
            provider=profile.provider,
            base_url=profile.base_url,
            model=profile.model,
            api_key=key,
            temperature=profile.temperature,
            max_tokens=profile.max_tokens,
            timeout_seconds=profile.timeout_seconds,
            max_concurrency=profile.max_concurrency,
        )

    async def _context(self, session, page: Page, project: Project) -> list[dict[str, object]]:
        previous = list(
            (
                await session.scalars(
                    select(Page)
                    .where(Page.project_id == project.id, Page.page_index < page.page_index)
                    .order_by(Page.page_index.desc())
                    .limit(3)
                )
            ).all()
        )
        result: list[dict[str, object]] = []
        used = 0
        for current in previous:
            regions = list(
                (
                    await session.scalars(
                        select(DetectionRegion)
                        .where(DetectionRegion.page_id == current.id)
                        .order_by(DetectionRegion.reading_order)
                    )
                ).all()
            )
            entries = [
                {"source_text": region.source_text or "", "translation": region.target_text or ""}
                for region in regions
            ]
            encoded = json.dumps(entries, ensure_ascii=False)
            if used + len(encoded) > 30_000:
                break
            used += len(encoded)
            result.append({"page_index": current.page_index, "regions": entries})
        return list(reversed(result))

    async def _mark_all_failed(
        self,
        task_id: str,
        item_ids: list[str],
        message: str,
        *,
        error_code: str = "TRANSLATION_FAILED",
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                return
            page_index: int | None = None
            for item_id in item_ids:
                item = await session.get(TaskItem, item_id)
                if item is None or item.status not in {"pending", "processing"}:
                    continue
                page_index = page_index if page_index is not None else item.page_index
                region = (
                    await session.get(DetectionRegion, item.region_id) if item.region_id else None
                )
                if region is None:
                    item.status = "skipped"
                    item.error_code = "TRANSLATION_TARGET_REMOVED"
                    item.error_message = "区域已被删除，未写入翻译失败状态"
                    task.skipped += 1
                    continue
                claim = await session.execute(
                    update(DetectionRegion)
                    .where(
                        DetectionRegion.id == region.id,
                        DetectionRegion.geometry_revision == item.expected_revision,
                        DetectionRegion.ocr_revision == item.expected_content_revision,
                        DetectionRegion.translation_revision == item.expected_output_revision,
                        DetectionRegion.translation_status == "processing",
                    )
                    .values(
                        translation_status=(
                            "manual" if region.target_text_origin == "manual" else "failed"
                        ),
                        translation_error=(
                            None if region.target_text_origin == "manual" else message[:500]
                        ),
                        updated_at=utc_now(),
                    )
                )
                if claim.rowcount != 1:
                    item.status = "skipped"
                    item.error_code = "TRANSLATION_CONFLICT"
                    item.error_message = "区域已发生更新，旧翻译失败状态未写入"
                    task.skipped += 1
                else:
                    item.status = "failed"
                    item.error_code = error_code
                    item.error_message = message[:500]
                    task.failed += 1
                    task.error_code = task.error_code or error_code
                    task.error_message = task.error_message or message[:500]
                    await refresh_page_translation_status(session, region.page_id)
            task.updated_at = utc_now()
            await session.commit()
            standalone = task.parent_task_id is None
            total = task.total
            completed = task.completed
            failed = task.failed
            skipped = task.skipped
        await self.broadcaster.notify(task_id)
        if standalone:
            task_progress(
                task_id,
                "failed",
                task_type="quick_translation",
                stage="translation",
                translation_mode="quick",
                page_index=page_index,
                total=total,
                completed=completed,
                failed=failed,
                skipped=skipped,
                provider=provider,
                model=model,
                error_code=error_code,
                message=message,
            )

    async def _mark_cancelled_items(self, task_id: str, item_ids: list[str]) -> None:
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                return
            for item_id in item_ids:
                item = await session.get(TaskItem, item_id)
                if item and item.status in {"pending", "processing"}:
                    item.status = "skipped"
                    item.error_code = "TASK_CANCELLED"
                    task.skipped += 1
                    if item.region_id:
                        region = await session.get(DetectionRegion, item.region_id)
                        if region and region.translation_status == "processing":
                            region.translation_status = (
                                "manual"
                                if region.target_text_origin == "manual"
                                else "outdated"
                                if region.target_text is not None
                                else "pending"
                            )
                            await refresh_page_translation_status(session, region.page_id)
            await session.commit()

    async def _finish(self, task_id: str) -> None:
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                return
            first_item = await session.scalar(
                select(TaskItem)
                .where(TaskItem.task_id == task_id)
                .order_by(TaskItem.sequence_index, TaskItem.id)
                .limit(1)
            )
            task.status = "completed"
            if task.failed and task.completed == 0 and task.skipped == 0:
                task.status = "failed"
                task.stage = "failed"
                task.error_code = task.error_code or "TRANSLATION_FAILED"
            else:
                task.stage = (
                    "completed_with_warnings" if task.failed or task.skipped else "completed"
                )
            task.current_page_id = None
            task.finished_at = utc_now()
            task.updated_at = utc_now()
            await session.commit()
            standalone = task.parent_task_id is None
            status = task.status
            stage = task.stage
            total = task.total
            completed = task.completed
            failed = task.failed
            skipped = task.skipped
            error_code = task.error_code
            error_message = task.error_message
            page_index = first_item.page_index if first_item else None
        await self.broadcaster.notify(task_id)
        if standalone:
            task_progress(
                task_id,
                "failed"
                if status == "failed"
                else "warning"
                if stage == "completed_with_warnings"
                else "completed",
                task_type="quick_translation",
                page_index=page_index,
                total=total,
                completed=completed,
                failed=failed,
                skipped=skipped,
                error_code=error_code,
                message=error_message,
            )

    async def _finish_cancelled(self, task_id: str) -> None:
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                return
            first_item = await session.scalar(
                select(TaskItem)
                .where(TaskItem.task_id == task_id)
                .order_by(TaskItem.sequence_index, TaskItem.id)
                .limit(1)
            )
            task.status = "cancelled"
            task.stage = "cancelled"
            task.current_page_id = None
            task.finished_at = utc_now()
            task.updated_at = utc_now()
            await session.commit()
            standalone = task.parent_task_id is None
            page_index = first_item.page_index if first_item else None
            total = task.total
            completed = task.completed
            failed = task.failed
            skipped = task.skipped
        await self.broadcaster.notify(task_id)
        if standalone:
            task_progress(
                task_id,
                "cancelled",
                task_type="quick_translation",
                page_index=page_index,
                total=total,
                completed=completed,
                failed=failed,
                skipped=skipped,
            )

    async def _fail_task(self, task_id: str, code: str, message: str) -> None:
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                return
            items = list(
                (
                    await session.scalars(
                        select(TaskItem).where(
                            TaskItem.task_id == task_id,
                            TaskItem.status.in_(["pending", "processing", "running"]),
                        )
                    )
                ).all()
            )
            for item in items:
                item.status = "failed"
                item.error_code = code
                item.error_message = message[:500]
                if item.region_id:
                    region = await session.get(DetectionRegion, item.region_id)
                    if region and region.translation_status == "processing":
                        if region.target_text_origin == "manual":
                            region.translation_status = "manual"
                            region.translation_error = None
                        else:
                            region.translation_status = "failed"
                            region.translation_error = message[:500]
                        await refresh_page_translation_status(session, region.page_id)
                        task.failed += 1
            task.status = "failed"
            task.stage = "failed"
            task.error_code = code
            task.error_message = message[:500]
            task.finished_at = utc_now()
            task.current_page_id = None
            await session.commit()
            standalone = task.parent_task_id is None
            page_index = items[0].page_index if items else None
            total = task.total
            completed = task.completed
            failed = task.failed
            skipped = task.skipped
        await self.broadcaster.notify(task_id)
        if standalone:
            task_progress(
                task_id,
                "failed",
                task_type="quick_translation",
                stage="preparing_context",
                page_index=page_index,
                total=total,
                completed=completed,
                failed=failed,
                skipped=skipped,
                error_code=code,
                message=message,
            )

    async def _cancel_requested(self, task_id: str) -> bool:
        async with self.database.session_factory() as session:
            return bool(
                await session.scalar(select(Task.cancel_requested).where(Task.id == task_id))
            )
