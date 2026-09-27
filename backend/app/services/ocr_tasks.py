from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select, update

from app.core.config import Settings
from app.db.models import (
    DetectionRegion,
    Page,
    Project,
    Task,
    TaskItem,
)
from app.db.session import Database
from app.providers.ocr.base import (
    OCRDependencyError,
    OCRModelMissingError,
    OCRRecognition,
    OCRRuntime,
)
from app.providers.ocr.registry import fixed_provider_for_language
from app.services.ocr_images import (
    OCRCropError,
    OCRCropRequest,
    OCRCropStreamEnd,
    create_ocr_crop,
    stream_ocr_crop_batches,
)
from app.services.render_state import mark_page_render_outdated
from app.services.storage import resolve_within
from app.services.tasks import TaskBroadcaster, task_blocked_by_paused_batch, utc_now
from app.services.translation_tasks import refresh_page_translation_status

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _OCRWorkItem:
    item_id: str
    page_id: str
    region_id: str
    expected_geometry: int
    expected_content: int
    bounds: tuple[float, float, float, float]


@dataclass(frozen=True)
class _OCRPageWork:
    image_path: Path
    language: str
    provider: str
    items: tuple[_OCRWorkItem, ...]


def aggregate_ocr_status(statuses: list[str]) -> str:
    if not statuses:
        return "pending"
    if "processing" in statuses:
        return "processing"
    if "outdated" in statuses:
        return "outdated"
    if "pending" in statuses:
        return "pending"
    successful = sum(value in {"completed", "manual"} for value in statuses)
    failed = statuses.count("failed")
    if failed and successful:
        return "completed_with_warnings"
    if failed:
        return "failed"
    return "completed"


def _preserved(region: DetectionRegion, *, source_text: str | None = None) -> bool:
    source = region.source_text if source_text is None else source_text
    return region.target_text_origin == "sfx_preserve" or (
        region.sfx_strategy == "preserve"
        and (region.target_text is None or region.target_text == source)
    )


async def refresh_page_ocr_status(session, page_id: str) -> str:
    statuses = list(
        (
            await session.scalars(
                select(DetectionRegion.ocr_status).where(DetectionRegion.page_id == page_id)
            )
        ).all()
    )
    value = aggregate_ocr_status(statuses)
    await session.execute(update(Page).where(Page.id == page_id).values(ocr_status=value))
    return value


class OCRTaskManager:
    """Runs persisted region OCR tasks without allowing stale inference writes."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        runtime: OCRRuntime,
        broadcaster: TaskBroadcaster,
    ) -> None:
        self.database = database
        self.settings = settings
        self.runtime = runtime
        self.broadcaster = broadcaster
        self._active: dict[str, asyncio.Task[None]] = {}
        self._semaphore = asyncio.Semaphore(settings.ocr_concurrency)

    async def start(self) -> None:
        async with self.database.session_factory() as session:
            task_ids = list(
                (
                    await session.scalars(
                        select(Task.id).where(
                            Task.task_type == "ocr", Task.status.in_(["pending", "running"])
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
                running_regions = list(
                    (
                        await session.scalars(
                            select(DetectionRegion)
                            .join(TaskItem, TaskItem.region_id == DetectionRegion.id)
                            .where(TaskItem.task_id.in_(task_ids), TaskItem.status == "running")
                        )
                    ).all()
                )
                for region in running_regions:
                    region.ocr_status = "outdated" if region.source_text else "pending"
                await session.execute(
                    update(TaskItem)
                    .where(TaskItem.task_id.in_(task_ids), TaskItem.status == "running")
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
        task = asyncio.create_task(self._run_guarded(task_id), name=f"ocr-{task_id}")
        self._active[task_id] = task
        task.add_done_callback(lambda _done, key=task_id: self._active.pop(key, None))

    async def cancel(self, task_id: str) -> None:
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                raise LookupError(task_id)
            if task.task_type != "ocr":
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
                            Task.task_type == "ocr",
                            Task.status.in_(["pending", "running"]),
                        )
                    )
                ).all()
            )
            task_ids = [task.id for task in tasks]
            for task in tasks:
                task.cancel_requested = True
            await session.commit()
        await asyncio.gather(
            *(self._active[item] for item in task_ids if item in self._active),
            return_exceptions=True,
        )
        return task_ids

    async def _run_guarded(self, task_id: str) -> None:
        async with self._semaphore:
            try:
                await self._run_task(task_id)
            except asyncio.CancelledError:
                raise
            except (OCRDependencyError, OCRModelMissingError) as exc:
                await self._fail_task(task_id, "OCR_DEPENDENCY_MISSING", str(exc))
            except Exception:
                logger.error("OCR task failed", extra={"request_id": "-"})
                await self._fail_task(task_id, "OCR_FAILED", "OCR 任务失败")

    async def _run_task(self, task_id: str) -> None:
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None or task.status not in {"pending", "running"}:
                return
            parameters = json.loads(task.parameters_json or "{}")
            task.status = "running"
            task.stage = "loading_ocr_model"
            task.started_at = task.started_at or utc_now()
            task.updated_at = utc_now()
            await session.commit()
            item_ids = list(
                (
                    await session.scalars(
                        select(TaskItem.id)
                        .where(TaskItem.task_id == task_id, TaskItem.status == "pending")
                        .order_by(TaskItem.sequence_index)
                    )
                ).all()
            )
        await self.broadcaster.notify(task_id)
        batch_method = getattr(self.runtime, "recognize_batch", None)
        if callable(batch_method):
            await self._process_items_batch(task_id, item_ids, parameters)
            if await self._cancel_requested(task_id):
                await self._finish_cancelled(task_id)
            else:
                await self._finish_completed(task_id)
            return
        for item_id in item_ids:
            if await self._cancel_requested(task_id):
                await self._finish_cancelled(task_id)
                return
            await self._process_item(task_id, item_id, parameters)
        if await self._cancel_requested(task_id):
            await self._finish_cancelled(task_id)
        else:
            await self._finish_completed(task_id)

    async def _prepare_page_work(
        self, task_id: str, item_ids: list[str]
    ) -> _OCRPageWork | None:
        prepared: list[_OCRWorkItem] = []
        image_path = None
        language = ""
        provider = ""
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            project = await session.get(Project, task.project_id) if task else None
            if task is None or project is None:
                return None
            language = project.source_language
            provider = fixed_provider_for_language(language)
            for item_id in item_ids:
                item = await session.get(TaskItem, item_id)
                region = await session.get(DetectionRegion, item.region_id) if item else None
                page = await session.get(Page, item.page_id) if item and item.page_id else None
                if item is None:
                    continue
                if region is None or page is None:
                    await self._mark_skipped(session, task, item, "OCR_TARGET_REMOVED")
                    continue
                if (
                    region.geometry_revision != item.expected_revision
                    or region.ocr_revision != item.expected_content_revision
                ):
                    await self._mark_skipped(session, task, item, "OCR_CONFLICT")
                    continue
                candidate = resolve_within(self.settings.data_dir, item.source_path)
                if image_path is None:
                    image_path = candidate
                elif candidate != image_path:
                    raise RuntimeError("单个 OCR 任务包含了多个页面源文件")
                prepared.append(
                    _OCRWorkItem(
                        item_id=item.id,
                        page_id=page.id,
                        region_id=region.id,
                        expected_geometry=item.expected_revision,
                        expected_content=item.expected_content_revision,
                        bounds=(region.x1, region.y1, region.x2, region.y2),
                    )
                )
        if image_path is None:
            return None
        return _OCRPageWork(image_path, language, provider, tuple(prepared))

    async def _claim_batch(
        self, task_id: str, work_items: list[_OCRWorkItem]
    ) -> set[str]:
        claimed: set[str] = set()
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                return claimed
            for work in work_items:
                item = await session.get(TaskItem, work.item_id)
                region = await session.get(DetectionRegion, work.region_id)
                if item is None or region is None:
                    await self._mark_skipped(session, task, item, "OCR_TARGET_REMOVED")
                    continue
                claim = await session.execute(
                    update(DetectionRegion)
                    .where(
                        DetectionRegion.id == work.region_id,
                        DetectionRegion.geometry_revision == work.expected_geometry,
                        DetectionRegion.ocr_revision == work.expected_content,
                    )
                    .values(ocr_status="processing", ocr_error=None, updated_at=utc_now())
                )
                if claim.rowcount != 1:
                    await self._mark_skipped(session, task, item, "OCR_CONFLICT")
                    continue
                item.status = "running"
                claimed.add(work.region_id)
            if work_items:
                task.stage = "recognizing"
                task.current_page_id = work_items[0].page_id
                task.updated_at = utc_now()
                await refresh_page_ocr_status(session, work_items[0].page_id)
            await session.commit()
        await self.broadcaster.notify(task_id)
        return claimed

    async def _process_items_batch(
        self, task_id: str, item_ids: list[str], parameters: dict[str, object]
    ) -> None:
        work = await self._prepare_page_work(task_id, item_ids)
        if work is None or not work.items:
            return
        by_region = {item.region_id: item for item in work.items}
        device = str(parameters.get("device", "auto"))
        requested_batch_size = 1 if device == "cpu" else 8
        preprocessing_ms = 0
        inference_ms = 0
        effective_batch_size = requested_batch_size
        actual_devices: set[str] = set()
        total_started = time.perf_counter()
        requests = [OCRCropRequest(item.region_id, item.bounds) for item in work.items]

        async for crop_batch in stream_ocr_crop_batches(
            work.image_path, requests, batch_size=requested_batch_size
        ):
            if isinstance(crop_batch, OCRCropStreamEnd):
                preprocessing_ms = crop_batch.preprocessing_ms
                break
            if await self._cancel_requested(task_id):
                return
            batch_ids = [item.region_id for item in crop_batch.images]
            batch_ids.extend(item.region_id for item in crop_batch.failures)
            batch_work = [by_region[region_id] for region_id in batch_ids]
            claimed = await self._claim_batch(task_id, batch_work)
            for failure in crop_batch.failures:
                if failure.region_id not in claimed:
                    continue
                item = by_region[failure.region_id]
                await self._mark_item_failed(
                    task_id,
                    item.item_id,
                    item.page_id,
                    item.region_id,
                    item.expected_geometry,
                    item.expected_content,
                    "OCR_CROP_FAILED",
                    failure.message,
                )
            images = tuple(item for item in crop_batch.images if item.region_id in claimed)
            if not images:
                continue
            result = await self.runtime.recognize_batch(
                work.provider,
                images,
                language=work.language,
                device=device,
                batch_size=requested_batch_size,
            )
            inference_ms += result.inference_ms
            effective_batch_size = min(effective_batch_size, result.effective_batch_size)
            for batch_item in result.items:
                item = by_region[batch_item.region_id]
                if batch_item.recognition is None:
                    await self._mark_item_failed(
                        task_id,
                        item.item_id,
                        item.page_id,
                        item.region_id,
                        item.expected_geometry,
                        item.expected_content,
                        "OCR_FAILED",
                        batch_item.error or "本区域 OCR 失败",
                    )
                    continue
                recognition = batch_item.recognition
                actual_devices.add(recognition.actual_device)
                text = recognition.text.replace("\r\n", "\n").replace("\r", "\n").strip()
                if not text:
                    await self._mark_item_failed(
                        task_id,
                        item.item_id,
                        item.page_id,
                        item.region_id,
                        item.expected_geometry,
                        item.expected_content,
                        "OCR_EMPTY_RESULT",
                        "OCR 未识别到文本",
                    )
                    continue
                await self._save_recognition(task_id, item, work.provider, recognition, text)

        total_ms = round((time.perf_counter() - total_started) * 1000)
        await self._record_batch_metrics(
            task_id,
            actual_devices,
            requested_batch_size,
            effective_batch_size,
            preprocessing_ms,
            inference_ms,
            total_ms,
        )

    async def _save_recognition(
        self,
        task_id: str,
        work: _OCRWorkItem,
        provider: str,
        recognition: OCRRecognition,
        text: str,
    ) -> None:
        async with self.database.session_factory() as session:
            existing = await session.get(DetectionRegion, work.region_id)
            translation_changed = bool(existing and existing.source_text != text)
            preserve_state_changed = bool(
                existing and _preserved(existing) != _preserved(existing, source_text=text)
            )
            next_ocr_revision = work.expected_content + (1 if translation_changed else 0)
            ocr_values = {
                "source_text": text,
                "source_text_origin": "provider",
                "ocr_status": "completed",
                "ocr_provider": provider,
                "ocr_confidence": recognition.confidence,
                "ocr_error": None,
                "ocr_revision": next_ocr_revision,
                "ocr_updated_at": utc_now(),
                "updated_at": utc_now(),
            }
            if translation_changed and existing:
                ocr_values.update(
                    {
                        "translation_status": (
                            "outdated" if existing.target_text is not None else "pending"
                        ),
                        "translation_revision": existing.translation_revision + 1,
                        "translation_error": None,
                    }
                )
            claim = await session.execute(
                update(DetectionRegion)
                .where(
                    DetectionRegion.id == work.region_id,
                    DetectionRegion.geometry_revision == work.expected_geometry,
                    DetectionRegion.ocr_revision == work.expected_content,
                    DetectionRegion.ocr_status == "processing",
                )
                .values(**ocr_values)
            )
            item = await session.get(TaskItem, work.item_id)
            task = await session.get(Task, task_id)
            if item is None or task is None:
                return
            if claim.rowcount != 1:
                await self._mark_skipped(session, task, item, "OCR_CONFLICT")
                return
            if translation_changed:
                page = await session.get(Page, work.page_id)
                if page:
                    await mark_page_render_outdated(
                        session,
                        page,
                        repair=preserve_state_changed,
                        reason="OCR 原文已更新，需要重新生成成品",
                    )
            item.status = "completed"
            item.error_code = None
            item.error_message = None
            task.completed += 1
            task.updated_at = utc_now()
            await refresh_page_ocr_status(session, work.page_id)
            await refresh_page_translation_status(session, work.page_id)
            await session.commit()
        await self.broadcaster.notify(task_id)

    async def _record_batch_metrics(
        self,
        task_id: str,
        actual_devices: set[str],
        requested_batch_size: int,
        effective_batch_size: int,
        preprocessing_ms: int,
        inference_ms: int,
        total_ms: int,
    ) -> None:
        devices = sorted(actual_devices)
        result: dict[str, object] = {
            "actual_devices": devices,
            "requested_batch_size": requested_batch_size,
            "effective_batch_size": effective_batch_size,
            "preprocessing_ms": preprocessing_ms,
            "inference_ms": inference_ms,
            "total_ms": total_ms,
        }
        if len(devices) == 1:
            result["actual_device"] = devices[0]
        elif len(devices) > 1:
            result["actual_device"] = "mixed"
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            if task:
                task.result_json = json.dumps(result, ensure_ascii=False)
                task.updated_at = utc_now()
                await session.commit()

    async def _process_item(
        self, task_id: str, item_id: str, parameters: dict[str, object]
    ) -> None:
        async with self.database.session_factory() as session:
            item = await session.get(TaskItem, item_id)
            task = await session.get(Task, task_id)
            region = await session.get(DetectionRegion, item.region_id) if item else None
            page = await session.get(Page, item.page_id) if item and item.page_id else None
            project = await session.get(Project, task.project_id) if task else None
            if item is None or task is None or region is None or page is None or project is None:
                await self._mark_skipped(session, task, item, "OCR_TARGET_REMOVED")
                return
            if (
                region.geometry_revision != item.expected_revision
                or region.ocr_revision != item.expected_content_revision
            ):
                await self._mark_skipped(session, task, item, "OCR_CONFLICT")
                return
            processing_claim = await session.execute(
                update(DetectionRegion)
                .where(
                    DetectionRegion.id == region.id,
                    DetectionRegion.geometry_revision == item.expected_revision,
                    DetectionRegion.ocr_revision == item.expected_content_revision,
                )
                .values(ocr_status="processing", ocr_error=None, updated_at=utc_now())
            )
            if processing_claim.rowcount != 1:
                await self._mark_skipped(session, task, item, "OCR_CONFLICT")
                return
            item.status = "running"
            task.stage = "recognizing"
            task.current_page_id = page.id
            task.updated_at = utc_now()
            await refresh_page_ocr_status(session, page.id)
            await session.commit()
            page_id = page.id
            region_id = region.id
            expected_geometry = item.expected_revision
            expected_content = item.expected_content_revision
            image_path = resolve_within(self.settings.data_dir, item.source_path)
            crop_relative = (
                f"{project.workspace_path}/crops/ocr/{page.id}/"
                f"{region.id}-g{region.geometry_revision}.png"
            )
            crop_path = resolve_within(self.settings.data_dir, crop_relative)
            bounds = (region.x1, region.y1, region.x2, region.y2)
            language = project.source_language
            provider = fixed_provider_for_language(language)
            device = str(parameters.get("device", "auto"))
        await self.broadcaster.notify(task_id)

        try:
            await asyncio.to_thread(create_ocr_crop, image_path, crop_path, bounds)
            recognition = await self.runtime.recognize(
                provider, crop_path, language=language, device=device
            )
            text = recognition.text.replace("\r\n", "\n").replace("\r", "\n").strip()
            if not text:
                await self._mark_item_failed(
                    task_id,
                    item_id,
                    page_id,
                    region_id,
                    expected_geometry,
                    expected_content,
                    "OCR_EMPTY_RESULT",
                    "OCR 未识别到文本",
                )
                return
        except (OCRDependencyError, OCRModelMissingError):
            raise
        except OCRCropError:
            await self._mark_item_failed(
                task_id,
                item_id,
                page_id,
                region_id,
                expected_geometry,
                expected_content,
                "OCR_CROP_FAILED",
                "无法生成 OCR 区域图",
            )
            return
        except Exception:
            logger.warning("Region OCR failed", extra={"request_id": "-"})
            await self._mark_item_failed(
                task_id,
                item_id,
                page_id,
                region_id,
                expected_geometry,
                expected_content,
                "OCR_FAILED",
                "本区域 OCR 失败",
            )
            return

        async with self.database.session_factory() as session:
            existing = await session.get(DetectionRegion, region_id)
            translation_changed = bool(existing and existing.source_text != text)
            preserve_state_changed = bool(
                existing and _preserved(existing) != _preserved(existing, source_text=text)
            )
            next_ocr_revision = expected_content + (1 if translation_changed else 0)
            ocr_values = {
                "source_text": text,
                "source_text_origin": "provider",
                "ocr_status": "completed",
                "ocr_provider": provider,
                "ocr_confidence": recognition.confidence,
                "ocr_error": None,
                "ocr_revision": next_ocr_revision,
                "ocr_updated_at": utc_now(),
                "updated_at": utc_now(),
            }
            if translation_changed and existing:
                ocr_values.update(
                    {
                        "translation_status": (
                            "outdated" if existing.target_text is not None else "pending"
                        ),
                        "translation_revision": existing.translation_revision + 1,
                        "translation_error": None,
                    }
                )
            claim = await session.execute(
                update(DetectionRegion)
                .where(
                    DetectionRegion.id == region_id,
                    DetectionRegion.geometry_revision == expected_geometry,
                    DetectionRegion.ocr_revision == expected_content,
                    DetectionRegion.ocr_status == "processing",
                )
                .values(**ocr_values)
            )
            item = await session.get(TaskItem, item_id)
            task = await session.get(Task, task_id)
            if item is None or task is None:
                return
            if claim.rowcount != 1:
                await self._mark_skipped(session, task, item, "OCR_CONFLICT")
                return
            if translation_changed:
                page = await session.get(Page, page_id)
                if page:
                    await mark_page_render_outdated(
                        session,
                        page,
                        repair=preserve_state_changed,
                        reason="OCR 原文已更新，需要重新生成成品",
                    )
            item.status = "completed"
            item.error_code = None
            item.error_message = None
            task.completed += 1
            task.result_json = json.dumps(
                {"actual_device": recognition.actual_device}, ensure_ascii=False
            )
            task.updated_at = utc_now()
            await refresh_page_ocr_status(session, page_id)
            await refresh_page_translation_status(session, page_id)
            await session.commit()
        await self.broadcaster.notify(task_id)

    async def _mark_item_failed(
        self,
        task_id: str,
        item_id: str,
        page_id: str,
        region_id: str,
        expected_geometry: int,
        expected_content: int,
        code: str,
        message: str,
    ) -> None:
        async with self.database.session_factory() as session:
            item = await session.get(TaskItem, item_id)
            task = await session.get(Task, task_id)
            if item is None or task is None:
                return
            claim = await session.execute(
                update(DetectionRegion)
                .where(
                    DetectionRegion.id == region_id,
                    DetectionRegion.geometry_revision == expected_geometry,
                    DetectionRegion.ocr_revision == expected_content,
                    DetectionRegion.ocr_status == "processing",
                )
                .values(
                    ocr_status="failed",
                    ocr_error=message,
                    ocr_revision=expected_content + 1,
                    ocr_updated_at=utc_now(),
                    updated_at=utc_now(),
                )
            )
            if claim.rowcount != 1:
                await self._mark_skipped(session, task, item, "OCR_CONFLICT")
                return
            item.status = "failed"
            item.error_code = code
            item.error_message = message
            task.failed += 1
            task.error_code = task.error_code or code
            task.error_message = task.error_message or message
            task.updated_at = utc_now()
            await refresh_page_ocr_status(session, page_id)
            await session.commit()
        await self.broadcaster.notify(task_id)

    async def _mark_skipped(self, session, task, item, code: str) -> None:
        if task is None or item is None:
            return
        item.status = "skipped"
        item.error_code = code
        item.error_message = "区域已被修改或删除，未写入旧 OCR 结果"
        task.skipped += 1
        task.updated_at = utc_now()
        if item.page_id:
            await refresh_page_ocr_status(session, item.page_id)
        await session.commit()
        await self.broadcaster.notify(task.id)

    async def _finish_completed(self, task_id: str) -> None:
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                return
            task.status = "completed"
            task.stage = "completed_with_warnings" if task.failed or task.skipped else "completed"
            task.current_page_id = None
            task.finished_at = utc_now()
            task.updated_at = utc_now()
            await session.commit()
        await self.broadcaster.notify(task_id)

    async def _finish_cancelled(self, task_id: str) -> None:
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                return
            task.status = "cancelled"
            task.stage = "cancelled"
            task.current_page_id = None
            task.finished_at = utc_now()
            task.updated_at = utc_now()
            await session.commit()
        await self.broadcaster.notify(task_id)

    async def _fail_task(self, task_id: str, code: str, message: str) -> None:
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                return
            running_items = list(
                (
                    await session.scalars(
                        select(TaskItem).where(
                            TaskItem.task_id == task_id, TaskItem.status == "running"
                        )
                    )
                ).all()
            )
            for item in running_items:
                item.status = "failed"
                item.error_code = code
                item.error_message = message
                if item.region_id:
                    region = await session.get(DetectionRegion, item.region_id)
                    if region and region.ocr_status == "processing":
                        region.ocr_status = "failed"
                        region.ocr_error = message
                        region.ocr_revision += 1
                if item.page_id:
                    await refresh_page_ocr_status(session, item.page_id)
            task.status = "failed"
            task.stage = "failed"
            task.error_code = code
            task.error_message = message[:500]
            task.current_page_id = None
            task.finished_at = utc_now()
            task.updated_at = utc_now()
            await session.commit()
        await self.broadcaster.notify(task_id)

    async def _cancel_requested(self, task_id: str) -> bool:
        async with self.database.session_factory() as session:
            return bool(
                await session.scalar(select(Task.cancel_requested).where(Task.id == task_id))
            )
