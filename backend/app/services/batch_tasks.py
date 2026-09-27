"""Persistent Phase 7 batch pipeline coordinator.

The single-page managers remain the source of truth for each model/runtime.  A
batch task owns one page item per selected page and creates small child tasks
for each stage.  This keeps the existing optimistic write locks and recovery
behaviour while adding a durable parent queue with pause/resume semantics.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from sqlalchemy import func, select, update

from app.api.schemas import TaskItemRead, TaskItemStageRead, TaskSnapshot
from app.core.config import Settings
from app.core.logging import task_progress
from app.db.models import (
    APIProfile,
    DetectionRegion,
    Page,
    PerformanceSettings,
    Project,
    Task,
    TaskItem,
    TaskItemStage,
    TranslationSettings,
)
from app.db.session import Database
from app.providers.ocr.registry import fixed_provider_for_language
from app.services.render_state import mark_page_render_outdated
from app.services.storage import resolve_within
from app.services.tasks import TaskBroadcaster, utc_now
from app.services.translation_tasks import (
    deterministic_reading_order,
    is_untranslated_provider_region,
    refresh_page_translation_status,
)

logger = logging.getLogger(__name__)

TERMINAL = {"completed", "failed", "cancelled"}
ACTIVE = {"pending", "running", "pausing"}
STAGES = ("ocr", "translation", "repair", "render", "export")
DEFAULT_SFX_NAMES = {
    "sfx",
    "sound_effect",
    "sound-effect",
    "onomatopoeia",
    "拟声词",
    "効果音",
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _is_sfx(region: DetectionRegion, names: set[str]) -> bool:
    return region.class_name.casefold() in names


def _preserved(region: DetectionRegion) -> bool:
    return region.target_text_origin == "sfx_preserve" or (
        region.sfx_strategy == "preserve"
        and (region.target_text is None or region.target_text == region.source_text)
    )


class BatchTaskCoordinator:
    """Owns top-level ``batch_pipeline`` tasks and their child work."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        broadcaster: TaskBroadcaster,
        *,
        ocr_manager: Any,
        translation_manager: Any,
        render_manager: Any,
    ) -> None:
        self.database = database
        self.settings = settings
        self.broadcaster = broadcaster
        self.ocr_manager = ocr_manager
        self.translation_manager = translation_manager
        self.render_manager = render_manager
        self._active: dict[str, asyncio.Task[None]] = {}
        self._semaphore = asyncio.Semaphore(2)
        self.pipeline_window = 6

    async def start(self) -> None:
        async with self.database.session_factory() as session:
            task_ids = list(
                (
                    await session.scalars(
                    select(Task.id).where(
                        Task.task_type == "batch_pipeline", Task.status.in_(ACTIVE | {"paused"})
                    ).order_by(Task.created_at, Task.id)
                    )
                ).all()
            )
            # A process exit can only leave an item in ``running``.  It is
            # safe to replay the item because child managers use revisions.
            await session.execute(
                update(Task)
                .where(Task.task_type == "batch_pipeline", Task.status == "running")
                .values(status="pending", stage="recovering", updated_at=utc_now())
            )
            # A process can exit after a pause request is persisted but before
            # the coordinator reaches its page boundary.  Treat that state as
            # a real pause on restart; it must not remain stranded in
            # ``pausing`` with no worker attached.
            await session.execute(
                update(Task)
                .where(Task.task_type == "batch_pipeline", Task.status == "pausing")
                .values(status="paused", pause_requested=True, stage="paused", updated_at=utc_now())
            )
            await session.execute(
                update(TaskItem)
                .where(TaskItem.task_id.in_(task_ids), TaskItem.status.in_(["running", "processing"]))
                .values(status="pending", current_stage=None)
            )
            await session.execute(
                update(TaskItemStage)
                .where(TaskItemStage.task_item_id.in_(select(TaskItem.id).where(TaskItem.task_id.in_(task_ids))), TaskItemStage.status == "processing")
                .values(status="pending", updated_at=utc_now())
            )
            await session.commit()
        for task_id in task_ids:
            async with self.database.session_factory() as session:
                task = await session.get(Task, task_id)
                if task and task.status == "pending":
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
        current = asyncio.create_task(self._run_guarded(task_id), name=f"batch-{task_id}")
        self._active[task_id] = current
        current.add_done_callback(lambda _done, key=task_id: self._active.pop(key, None))

    async def snapshot(self, task_id: str) -> TaskSnapshot | None:
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                return None
            return await self._snapshot_session(session, task)

    async def list_snapshots(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        task_type: str | None = None,
        offset: int = 0,
        limit: int = 30,
    ) -> tuple[list[TaskSnapshot], int]:
        async with self.database.session_factory() as session:
            where = [Task.parent_task_id.is_(None)]
            if project_id:
                where.append(Task.project_id == project_id)
            if status:
                where.append(Task.status == status)
            if task_type:
                where.append(Task.task_type == task_type)
            total = int(await session.scalar(select(func.count(Task.id)).where(*where)) or 0)
            tasks = list(
                (
                    await session.scalars(
                        select(Task)
                        .where(*where)
                        .order_by(Task.created_at.desc(), Task.id.desc())
                        .offset(offset)
                        .limit(limit)
                    )
                ).all()
            )
            result = [await self._snapshot_session(session, task) for task in tasks]
            return result, total

    async def items(
        self, task_id: str, *, status: str | None = None, offset: int = 0, limit: int = 100
    ) -> tuple[list[TaskItemRead], int] | None:
        async with self.database.session_factory() as session:
            if await session.get(Task, task_id) is None:
                return None
            where = [TaskItem.task_id == task_id]
            if status:
                where.append(TaskItem.status == status)
            total = int(await session.scalar(select(func.count(TaskItem.id)).where(*where)) or 0)
            rows = list(
                (
                    await session.scalars(
                        select(TaskItem)
                        .where(*where)
                        .order_by(TaskItem.page_index, TaskItem.sequence_index)
                        .offset(offset)
                        .limit(limit)
                    )
                ).all()
            )
            result: list[TaskItemRead] = []
            for item in rows:
                stages = list(
                    (
                        await session.scalars(
                            select(TaskItemStage)
                            .where(TaskItemStage.task_item_id == item.id)
                            .order_by(TaskItemStage.id)
                        )
                ).all()
                )
                stage_order = {name: index for index, name in enumerate(STAGES)}
                stages.sort(key=lambda value: stage_order.get(value.stage, len(STAGES)))
                result.append(
                    TaskItemRead(
                        id=item.id,
                        page_id=item.page_id,
                        region_id=item.region_id,
                        page_index=item.page_index,
                        sequence_index=item.sequence_index,
                        item_type=item.item_type,
                        status=item.status,
                        current_stage=item.current_stage,
                        error_code=item.error_code,
                        error_message=item.error_message,
                        started_at=item.started_at,
                        finished_at=item.finished_at,
                        retry_count=item.retry_count,
                        stages=[
                            TaskItemStageRead(
                                stage=stage.stage,
                                status=stage.status,
                                child_task_id=stage.child_task_id,
                                retry_count=stage.retry_count,
                                error_code=stage.error_code,
                                error_message=stage.error_message,
                                started_at=stage.started_at,
                                finished_at=stage.finished_at,
                            )
                            for stage in stages
                        ],
                    )
                )
            return result, total

    async def pause(self, task_id: str) -> TaskSnapshot:
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None or task.task_type != "batch_pipeline":
                raise LookupError(task_id)
            if task.status in TERMINAL or task.status == "paused":
                return await self._snapshot_session(session, task)
            task.pause_requested = True
            task.status = "pausing"
            task.task_revision += 1
            task.updated_at = utc_now()
            await session.commit()
            snapshot = await self._snapshot_session(session, task)
        await self.broadcaster.notify(task_id)
        task_progress(task_id, "pause_requested", task_type="batch_pipeline")
        return snapshot

    async def resume(self, task_id: str) -> TaskSnapshot:
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None or task.task_type != "batch_pipeline":
                raise LookupError(task_id)
            if task.status != "paused":
                return await self._snapshot_session(session, task)
            task.pause_requested = False
            task.cancel_requested = False
            task.status = "pending"
            task.stage = "queued"
            task.task_revision += 1
            task.updated_at = utc_now()
            await session.commit()
            snapshot = await self._snapshot_session(session, task)
        self.enqueue(task_id)
        await self._enqueue_children(task_id)
        await self.broadcaster.notify(task_id)
        task_progress(task_id, "resumed", task_type="batch_pipeline", total=snapshot.total)
        return snapshot

    async def cancel(self, task_id: str) -> TaskSnapshot:
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None or task.task_type != "batch_pipeline":
                raise LookupError(task_id)
            if task.status not in TERMINAL:
                was_cancel_requested = task.cancel_requested
                task.cancel_requested = True
                task.pause_requested = False
                # Propagate cancellation to persisted child work as well.
                # OCR can contain several region requests and a child that
                # only observes its own flag would otherwise process every
                # remaining region before the parent notices cancellation.
                children = list(
                    (
                        await session.scalars(
                            select(Task).where(
                                Task.parent_task_id == task.id,
                                Task.status.in_(["pending", "running"]),
                            )
                        )
                    ).all()
                )
                for child in children:
                    child.cancel_requested = True
                if not was_cancel_requested:
                    task.task_revision += 1
                if task.status == "paused":
                    # A paused parent has no worker that could observe the
                    # cancellation flag.  Finalize it immediately while
                    # preserving the already completed pages.
                    items = list(
                        (
                            await session.scalars(
                                select(TaskItem).where(
                                    TaskItem.task_id == task.id,
                                    TaskItem.status.in_(["pending", "processing"]),
                                )
                            )
                        ).all()
                    )
                    for item in items:
                        item.status = "skipped"
                        item.error_code = "TASK_CANCELLED"
                        item.error_message = "任务已取消"
                        task.skipped += 1
                        stages = list(
                            (
                                await session.scalars(
                                    select(TaskItemStage).where(
                                        TaskItemStage.task_item_id == item.id,
                                        TaskItemStage.status.in_(["pending", "processing"]),
                                    )
                                )
                            ).all()
                        )
                        for stage in stages:
                            stage.status = "skipped"
                            stage.error_code = "TASK_CANCELLED"
                            stage.error_message = "任务已取消"
                            stage.finished_at = utc_now()
                    task.status = "cancelled"
                    task.stage = "cancelled"
                    task.current_page_id = None
                    task.finished_at = utc_now()
                task.updated_at = utc_now()
                await session.commit()
            snapshot = await self._snapshot_session(session, task)
        await self.broadcaster.notify(task_id)
        if snapshot.status == "cancelled":
            task_progress(
                task_id,
                "cancelled",
                task_type="batch_pipeline",
                total=snapshot.total,
                completed=snapshot.completed,
                failed=snapshot.failed,
                skipped=snapshot.skipped,
            )
        elif snapshot.cancel_requested:
            task_progress(task_id, "cancel_requested", task_type="batch_pipeline")
        return snapshot

    # Neutral queue API aliases used by callers that do not need to know that
    # the current top-level implementation is the batch coordinator.
    async def pause_task(self, task_id: str) -> TaskSnapshot:
        return await self.pause(task_id)

    async def resume_task(self, task_id: str) -> TaskSnapshot:
        return await self.resume(task_id)

    async def cancel_task(self, task_id: str) -> TaskSnapshot:
        return await self.cancel(task_id)

    async def _enqueue_children(self, parent_id: str) -> None:
        """Wake persisted child work after a paused parent is resumed."""
        async with self.database.session_factory() as session:
            children = list(
                (
                    await session.scalars(
                        select(Task).where(
                            Task.parent_task_id == parent_id,
                            Task.status.in_(["pending", "running"]),
                        )
                    )
                ).all()
            )
        managers = {
            "ocr": self.ocr_manager,
            "quick_translation": self.translation_manager,
            "page_repair": self.render_manager,
            "page_render": self.render_manager,
        }
        for child in children:
            child_manager = managers.get(child.task_type)
            if child_manager is not None:
                child_manager.enqueue(child.id)

    async def interrupt_project(self, project_id: str) -> list[str]:
        async with self.database.session_factory() as session:
            tasks = list(
                (
                    await session.scalars(
                        select(Task).where(Task.project_id == project_id, Task.status.in_(ACTIVE | {"paused"}))
                    )
                ).all()
            )
            ids = [task.id for task in tasks]
            for task in tasks:
                task.cancel_requested = True
                task.pause_requested = False
            if ids:
                await session.execute(
                    update(Task)
                    .where(
                        Task.parent_task_id.in_(ids),
                        Task.status.in_(["pending", "running"]),
                    )
                    .values(cancel_requested=True, updated_at=utc_now())
                )
            await session.commit()
        active = [self._active[item] for item in ids if item in self._active]
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        return ids

    async def _run_guarded(self, task_id: str) -> None:
        async with self._semaphore:
            try:
                await self._run(task_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("Batch pipeline failed", extra={"request_id": "-"})
                await self._fail_parent(task_id, "BATCH_PIPELINE_FAILED", "批处理任务失败")

    async def _run(self, task_id: str) -> None:
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None or task.status not in {"pending", "running", "pausing"}:
                return
            if task.status == "pausing" and task.pause_requested:
                task.status = "paused"
                task.stage = "paused"
                await session.commit()
                await self.broadcaster.notify(task_id)
                return
            recovering = task.stage == "recovering"
            first_start = task.started_at is None
            task.status = "running"
            task.stage = "preparing"
            task.started_at = task.started_at or utc_now()
            task.updated_at = utc_now()
            item_ids = list(
                (
                    await session.scalars(
                        select(TaskItem.id)
                        .where(TaskItem.task_id == task_id, TaskItem.status == "pending")
                        .order_by(TaskItem.page_index)
                    )
                ).all()
            )
            total = task.total
            skipped_items = (
                list(
                    (
                        await session.scalars(
                            select(TaskItem).where(
                                TaskItem.task_id == task_id,
                                TaskItem.status == "skipped",
                            )
                        )
                    ).all()
                )
                if first_start
                else []
            )
            performance = await session.get(PerformanceSettings, 1)
            window = max(1, int(performance.pipeline_window if performance else self.pipeline_window))
            await session.commit()
        task_progress(
            task_id,
            "resumed" if recovering else "started",
            task_type="batch_pipeline",
            total=total,
            message="服务重启后恢复执行" if recovering else None,
        )
        for skipped_item in skipped_items:
            task_progress(
                task_id,
                "skipped",
                task_type="batch_pipeline",
                page_index=skipped_item.page_index,
                error_code=skipped_item.error_code,
                message=skipped_item.error_message,
            )
        await self.broadcaster.notify(task_id)
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            if task:
                task.stage = "pipeline"
                await session.commit()
        async def run_one(item_id: str) -> None:
            try:
                await self._run_item(task_id, item_id)
            finally:
                await self._refresh_parent_progress(task_id, emit_log=True)

        pending: set[asyncio.Task[None]] = set()
        for item_id in item_ids:
            if await self._cancel_requested(task_id) or await self._pause_requested(task_id):
                break
            pending.add(asyncio.create_task(run_one(item_id)))
            if len(pending) >= window:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                await asyncio.gather(*done)
        if pending:
            await asyncio.gather(*pending)
        await self._refresh_parent_progress(task_id)
        if await self._cancel_requested(task_id):
            await self._finish_cancelled(task_id)
        elif await self._pause_requested(task_id):
            await self._set_paused(task_id)
        else:
            await self._finish(task_id)

    async def _run_item(
        self,
        task_id: str,
        item_id: str,
    ) -> None:
        async with self.database.session_factory() as session:
            item = await session.get(TaskItem, item_id)
            task = await session.get(Task, task_id)
            if item is None or task is None:
                return
            if not item.page_id:
                item.status = "skipped"
                item.error_code = "PAGE_MISSING"
                item.error_message = "页面不存在"
                item.finished_at = utc_now()
                await session.commit()
                return
            page = await session.get(Page, item.page_id)
            if page is None:
                item.status = "skipped"
                item.error_code = "PAGE_MISSING"
                item.error_message = "页面不存在"
                item.finished_at = utc_now()
                await session.commit()
                return
            item.status = "processing"
            item.started_at = item.started_at or utc_now()
            task.current_page_id = page.id
            task.updated_at = utc_now()
            parameters = json.loads(task.parameters_json or "{}")
            stages = list(
                (
                    await session.scalars(
                        select(TaskItemStage)
                        .where(TaskItemStage.task_item_id == item_id)
                        .order_by(TaskItemStage.id)
                    )
                ).all()
            )
            stage_order = {name: index for index, name in enumerate(STAGES)}
            stages.sort(key=lambda value: stage_order.get(value.stage, len(STAGES)))
            await session.commit()
        await self.broadcaster.notify(task_id)
        failed = False
        warned = False
        for stage in stages:
            if await self._cancel_requested(task_id):
                break
            if await self._pause_requested(task_id):
                break
            outcome = await self._run_stage(task_id, item_id, stage.stage, parameters)
            if outcome == "failed":
                failed = True
                await self._skip_remaining_stages(task_id, item_id, stage.stage)
                break
            if outcome == "completed_with_warnings":
                # A child task can complete while reporting failed/skipped
                # regions (for example, OCR on one damaged crop).  Keep the
                # page eligible for subsequent stages, but retain the warning
                # at stage level so the parent task finishes with an honest
                # ``completed_with_warnings`` outcome.
                warned = True
        async with self.database.session_factory() as session:
            item = await session.get(TaskItem, item_id)
            task = await session.get(Task, task_id)
            if item is None or task is None:
                return
            if await self._cancel_requested(task_id):
                item.status = "skipped"
                item.error_code = "TASK_CANCELLED"
                item.error_message = "任务已取消"
            elif await self._pause_requested(task_id):
                item.status = "pending"
                item.finished_at = None
            elif failed:
                item.status = "failed"
                failed_stage = await session.scalar(
                    select(TaskItemStage)
                    .where(
                        TaskItemStage.task_item_id == item.id,
                        TaskItemStage.status == "failed",
                    )
                    .order_by(TaskItemStage.finished_at.desc(), TaskItemStage.id.desc())
                )
                if failed_stage is not None:
                    item.error_code = failed_stage.error_code or "STAGE_FAILED"
                    item.error_message = failed_stage.error_message or "阶段执行失败"
                    task.error_code = task.error_code or item.error_code
                    task.error_message = task.error_message or item.error_message
            else:
                item.status = "completed"
                if warned:
                    item.error_code = item.error_code or "STAGE_COMPLETED_WITH_WARNINGS"
                    item.error_message = item.error_message or "页面部分阶段存在失败或跳过项"
            item.finished_at = utc_now()
            item.current_stage = None
            task.updated_at = utc_now()
            await session.commit()
        await self.broadcaster.notify(task_id)

    async def _refresh_parent_progress(self, task_id: str, *, emit_log: bool = False) -> None:
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                return
            rows = list((await session.scalars(select(TaskItem).where(TaskItem.task_id == task_id))).all())
            task.completed = sum(value.status == "completed" for value in rows)
            task.failed = sum(value.status == "failed" for value in rows)
            task.skipped = sum(value.status == "skipped" for value in rows)
            task.updated_at = utc_now()
            await session.commit()
            total = task.total
            completed = task.completed
            failed = task.failed
            skipped = task.skipped
        if emit_log:
            task_progress(
                task_id,
                "progress",
                task_type="batch_pipeline",
                total=total,
                completed=completed,
                failed=failed,
                skipped=skipped,
            )

    async def _run_stage(
        self, task_id: str, item_id: str, stage_name: str, parameters: dict[str, Any]
    ) -> str:
        existing_child_id: str | None = None
        existing_child_status: str | None = None
        page_index: int | None = None
        translation_mode = str(parameters.get("translation_mode") or "quick")
        provider: str | None = None
        model: str | None = None
        async with self.database.session_factory() as session:
            stage = await session.scalar(
                select(TaskItemStage).where(
                    TaskItemStage.task_item_id == item_id, TaskItemStage.stage == stage_name
                )
            )
            item = await session.get(TaskItem, item_id)
            task = await session.get(Task, task_id)
            if stage is None or item is None or task is None:
                return "skipped"
            page_index = item.page_index
            if stage_name == "translation":
                project = await session.get(Project, task.project_id)
                profile_id = parameters.get("llm_profile_id") or (
                    project.llm_profile_id if project else None
                )
                profile = await session.get(APIProfile, profile_id) if profile_id else None
                if profile is not None and profile.profile_type == "llm":
                    provider = profile.provider
                    model = profile.model
            if stage.status in {"completed", "completed_with_warnings", "skipped"}:
                return stage.status
            # Recovery may leave the child task persisted while the parent
            # stage is reset to pending.  Reuse an active/completed child
            # instead of creating a duplicate external request.  A failed or
            # cancelled child is deliberately retried below.
            if stage.child_task_id:
                child = await session.get(Task, stage.child_task_id)
                if child is not None and child.status in {"pending", "running"}:
                    existing_child_id = child.id
                    existing_child_status = child.status
                elif child is not None and child.status == "completed":
                    existing_child_id = child.id
                    existing_child_status = child.status
                else:
                    stage.child_task_id = None
                    stage.retry_count += 1
            page = await session.get(Page, item.page_id) if item.page_id else None
            if page is None:
                stage.status = "skipped"
                stage.error_code = "PAGE_MISSING"
                stage.error_message = "页面不存在"
                stage.finished_at = utc_now()
                await session.commit()
                task_progress(
                    task_id,
                    "skipped",
                    task_type="batch_pipeline",
                    stage=stage_name,
                    translation_mode=translation_mode,
                    page_index=page_index,
                    provider=provider,
                    model=model,
                    error_code=stage.error_code,
                    message=stage.error_message,
                )
                return "skipped"
            stage.status = "processing"
            stage.started_at = stage.started_at or utc_now()
            item.current_stage = stage_name
            task.stage = "pipeline"
            task.current_page_id = page.id
            task.updated_at = utc_now()
            await session.commit()
        task_progress(
            task_id,
            "started",
            task_type="batch_pipeline",
            stage=stage_name,
            translation_mode=translation_mode,
            page_index=page_index,
            provider=provider,
            model=model,
        )
        await self.broadcaster.notify(task_id)

        try:
            child_id: str | None = existing_child_id
            if child_id is None:
                child_type = {
                    "ocr": "ocr",
                    "translation": "quick_translation",
                    "repair": "page_repair",
                    "render": "page_render",
                }.get(stage_name)
                # A process can exit after a child is committed but before its
                # ID is linked into the stage row.  Recover that orphaned child
                # by its durable parent/page/type identity instead of issuing
                # a duplicate external request.
                if child_type:
                    async with self.database.session_factory() as session:
                        orphan = await session.scalar(
                            select(Task.id)
                            .join(TaskItem, TaskItem.task_id == Task.id)
                            .where(
                                Task.parent_task_id == task_id,
                                Task.task_type == child_type,
                                Task.status.in_(["pending", "running", "completed"]),
                                TaskItem.page_id == page.id,
                            )
                            .order_by(Task.created_at.desc())
                            .limit(1)
                        )
                        if orphan:
                            child_id = orphan
                if stage_name == "ocr":
                    if child_id is None:
                        child_id = await self._create_ocr_child(task_id, page.id, parameters)
                elif stage_name == "translation":
                    if child_id is None:
                        child_id = await self._create_translation_child(task_id, page.id, parameters)
                elif stage_name == "repair":
                    if child_id is None:
                        child_id = await self._create_render_child(task_id, page.id, parameters, "page_repair")
                elif stage_name == "render":
                    if child_id is None:
                        child_id = await self._create_render_child(task_id, page.id, parameters, "page_render")
            if child_id is None:
                outcome = "skipped"
            else:
                async with self.database.session_factory() as session:
                    stage = await session.scalar(
                        select(TaskItemStage).where(
                            TaskItemStage.task_item_id == item_id, TaskItemStage.stage == stage_name
                        )
                    )
                    if stage:
                        stage.child_task_id = child_id
                        await session.commit()
                if existing_child_status == "completed":
                    # A recovered child may have completed with per-item
                    # failures/skips.  Preserve that warning on the parent
                    # stage instead of treating every completed child as a
                    # clean success.
                    async with self.database.session_factory() as session:
                        child = await session.get(Task, child_id)
                        outcome = (
                            "completed_with_warnings"
                            if child
                            and (
                                child.failed
                                or child.skipped
                                or child.stage == "completed_with_warnings"
                            )
                            else "completed"
                        )
                else:
                    await self._wait_child(child_id)
                    async with self.database.session_factory() as session:
                        child = await session.get(Task, child_id)
                        if child and child.status == "failed":
                            outcome = "failed"
                        elif child and (child.failed or child.skipped or child.stage == "completed_with_warnings"):
                            outcome = "completed_with_warnings"
                        else:
                            outcome = "completed"
                        if child and child.status == "cancelled":
                            outcome = "skipped" if await self._cancel_requested(task_id) else "failed"
                if stage_name == "translation" and child_id:
                    async with self.database.session_factory() as session:
                        child = await session.get(Task, child_id)
                        try:
                            result = json.loads(child.result_json or "{}") if child else {}
                        except (TypeError, ValueError):
                            result = {}
                        if isinstance(result, dict):
                            actual_provider = result.get("provider")
                            actual_model = result.get("model")
                            if isinstance(actual_provider, str) and actual_provider.strip():
                                provider = actual_provider
                            if isinstance(actual_model, str) and actual_model.strip():
                                model = actual_model
            error_code: str | None = None
            error_message: str | None = None
            async with self.database.session_factory() as session:
                stage = await session.scalar(
                    select(TaskItemStage).where(
                        TaskItemStage.task_item_id == item_id, TaskItemStage.stage == stage_name
                    )
                )
                if stage:
                    stage.status = outcome
                    stage.finished_at = utc_now()
                    if outcome in {"failed", "completed_with_warnings"}:
                        child = await session.get(Task, stage.child_task_id) if stage.child_task_id else None
                        if outcome == "failed":
                            stage.error_code = child.error_code if child else "STAGE_FAILED"
                            stage.error_message = child.error_message if child else "阶段执行失败"
                        else:
                            stage.error_code = (
                                child.error_code if child and child.error_code else "STAGE_COMPLETED_WITH_WARNINGS"
                            )
                            stage.error_message = (
                                child.error_message
                                if child and child.error_message
                                else "阶段部分项目失败或跳过"
                            )
                    error_code = stage.error_code
                    error_message = stage.error_message
                    await session.commit()
            await self.broadcaster.notify(task_id)
            event = {
                "completed": "completed",
                "completed_with_warnings": "warning",
                "skipped": "skipped",
                "failed": "failed",
            }.get(outcome, "warning")
            task_progress(
                task_id,
                event,
                task_type="batch_pipeline",
                stage=stage_name,
                translation_mode=translation_mode,
                page_index=page_index,
                provider=provider,
                model=model,
                error_code=error_code,
                message=error_message,
            )
            return outcome
        except Exception:
            logger.warning("Batch stage failed", extra={"request_id": "-"})
            async with self.database.session_factory() as session:
                stage = await session.scalar(
                    select(TaskItemStage).where(
                        TaskItemStage.task_item_id == item_id, TaskItemStage.stage == stage_name
                    )
                )
                if stage:
                    stage.status = "failed"
                    stage.error_code = "STAGE_FAILED"
                    stage.error_message = "阶段执行失败"
                    stage.finished_at = utc_now()
                    await session.commit()
            task_progress(
                task_id,
                "failed",
                task_type="batch_pipeline",
                stage=stage_name,
                translation_mode=translation_mode,
                page_index=page_index,
                provider=provider,
                model=model,
                error_code="STAGE_FAILED",
                message="阶段执行失败",
            )
            return "failed"

    async def _skip_remaining_stages(self, task_id: str, item_id: str, after: str) -> None:
        async with self.database.session_factory() as session:
            stages = list(
                (
                    await session.scalars(
                        select(TaskItemStage).where(
                            TaskItemStage.task_item_id == item_id,
                            TaskItemStage.status == "pending",
                        )
                    )
                ).all()
            )
            for stage in stages:
                stage.status = "skipped"
                stage.error_code = "DEPENDENCY_FAILED"
                stage.error_message = "前置阶段失败，已跳过"
                stage.finished_at = utc_now()
            await session.commit()

    async def _wait_child(self, task_id: str) -> None:
        while True:
            async with self.database.session_factory() as session:
                task = await session.get(Task, task_id)
                if task is None or task.status in TERMINAL:
                    return
            await asyncio.sleep(0.05)

    async def _create_ocr_child(self, parent_id: str, page_id: str, params: dict[str, Any]) -> str | None:
        async with self.database.session_factory() as session:
            parent = await session.get(Task, parent_id)
            page = await session.get(Page, page_id)
            project = await session.get(Project, parent.project_id) if parent else None
            if parent is None or page is None or project is None:
                return None
            regions = list((await session.scalars(select(DetectionRegion).where(DetectionRegion.page_id == page_id))).all())
            overwrite = bool(params.get("overwrite_model_results"))
            overwrite_manual = bool(params.get("overwrite_manual_ocr"))
            candidates = [
                region
                for region in regions
                if (
                    overwrite_manual and region.source_text_origin == "manual"
                )
                or (
                    region.source_text_origin != "manual"
                    and (overwrite or region.ocr_status in {"pending", "outdated", "failed"})
                )
            ]
            if not candidates:
                return None
            child = Task(
                project_id=project.id,
                parent_task_id=parent_id,
                task_type="ocr",
                status="pending",
                stage="queued",
                total=len(candidates),
                parameters_json=_json(
                    {
                        "provider": fixed_provider_for_language(project.source_language),
                        "device": params.get("ocr_device", "auto"),
                    }
                ),
            )
            session.add(child)
            await session.flush()
            for index, region in enumerate(candidates, 1):
                session.add(
                    TaskItem(
                        task_id=child.id,
                        page_id=page.id,
                        region_id=region.id,
                        source_path=page.original_path or "",
                        page_index=page.page_index,
                        sequence_index=index,
                        status="pending",
                        expected_revision=region.geometry_revision,
                        expected_content_revision=region.ocr_revision,
                    )
                )
            await session.commit()
            child_id = child.id
        self.ocr_manager.enqueue(child_id)
        return child_id

    async def _create_translation_child(
        self, parent_id: str, page_id: str, params: dict[str, Any]
    ) -> str | None:
        async with self.database.session_factory() as session:
            parent = await session.get(Task, parent_id)
            page = await session.get(Page, page_id)
            project = await session.get(Project, parent.project_id) if parent else None
            if parent is None or page is None or project is None:
                return None
            settings = await session.get(TranslationSettings, 1)
            names = {
                str(value).casefold()
                for value in json.loads(
                    settings.sfx_class_names_json
                    if settings
                    else json.dumps(sorted(DEFAULT_SFX_NAMES), ensure_ascii=False)
                )
            }
            strategy = str(params.get("sfx_strategy") or (settings.sfx_strategy if settings else "preserve"))
            regions = list((await session.scalars(select(DetectionRegion).where(DetectionRegion.page_id == page_id))).all())
            ordered = deterministic_reading_order(regions, project.source_language)
            for index, region in enumerate(ordered, 1):
                region.reading_order = index
                region.reading_order_origin = "deterministic"
                region.sfx_strategy = strategy if _is_sfx(region, names) else None
            candidates: list[DetectionRegion] = []
            for region in ordered:
                if not region.source_text or region.ocr_status not in {"completed", "manual"}:
                    continue
                if _is_sfx(region, names) and strategy == "preserve":
                    if region.target_text_origin == "manual" and not params.get(
                        "overwrite_manual_translation"
                    ):
                        continue
                    if region.target_text != region.source_text or region.target_text_origin != "sfx_preserve":
                        region.target_text = region.source_text
                        region.target_text_origin = "sfx_preserve"
                        region.translation_mode = "quick"
                        region.translation_status = "completed"
                        region.translation_revision += 1
                        region.translated_from_ocr_revision = region.ocr_revision
                        region.translation_updated_at = utc_now()
                        await mark_page_render_outdated(session, page, reason="SFX 保留策略已更新")
                    continue
                if region.target_text_origin == "manual" and not params.get("overwrite_manual_translation"):
                    continue
                if (
                    region.target_text_origin == "manual"
                    and params.get("overwrite_manual_translation")
                ) or (
                    region.translation_status in {"pending", "outdated", "failed"}
                    or is_untranslated_provider_region(region, project)
                    or (
                        params.get("overwrite_model_results")
                        and region.translation_status == "completed"
                    )
                ):
                    candidates.append(region)
            await refresh_page_translation_status(session, page.id)
            if not candidates:
                await session.commit()
                return None
            child = Task(
                project_id=project.id,
                parent_task_id=parent_id,
                task_type="quick_translation",
                status="pending",
                stage="queued",
                total=len(candidates),
                llm_profile_id=params.get("llm_profile_id"),
                parameters_json=_json(
                    {"profile_id": params.get("llm_profile_id"), "sfx_strategy": strategy}
                ),
            )
            session.add(child)
            await session.flush()
            for index, region in enumerate(candidates, 1):
                region.translation_status = "processing"
                region.translation_error = None
                session.add(
                    TaskItem(
                        task_id=child.id,
                        page_id=page.id,
                        region_id=region.id,
                        source_path=page.original_path or "",
                        page_index=page.page_index,
                        sequence_index=region.reading_order or index,
                        status="pending",
                        expected_revision=region.geometry_revision,
                        expected_content_revision=region.ocr_revision,
                        expected_output_revision=region.translation_revision,
                    )
                )
            await refresh_page_translation_status(session, page.id)
            await session.commit()
            child_id = child.id
        self.translation_manager.enqueue(child_id)
        return child_id

    async def _create_render_child(
        self, parent_id: str, page_id: str, params: dict[str, Any], task_type: str
    ) -> str | None:
        async with self.database.session_factory() as session:
            parent = await session.get(Task, parent_id)
            page = await session.get(Page, page_id)
            project = await session.get(Project, parent.project_id) if parent else None
            if parent is None or page is None or project is None:
                return None
            if task_type == "page_repair":
                regions = list(
                    (
                        await session.scalars(
                            select(DetectionRegion).where(DetectionRegion.page_id == page.id)
                        )
                    ).all()
                )
                settings = await session.get(TranslationSettings, 1)
                names = {
                    str(value).casefold()
                    for value in json.loads(
                        settings.sfx_class_names_json
                        if settings
                        else json.dumps(sorted(DEFAULT_SFX_NAMES), ensure_ascii=False)
                    )
                }
                strategy = str(
                    params.get("sfx_strategy")
                    or (settings.sfx_strategy if settings else "preserve")
                )
                # When a batch intentionally skips translation, still apply
                # the configured SFX policy before deciding whether a repair
                # mask is needed.  This keeps preserve-only effects untouched.
                strategy_changed = False
                for region in regions:
                    if _is_sfx(region, names):
                        if region.sfx_strategy != strategy:
                            region.sfx_strategy = strategy
                            strategy_changed = True
                        if (
                            strategy == "preserve"
                            and region.target_text_origin != "manual"
                            and (region.target_text is None or region.target_text != region.source_text)
                        ):
                            # A render-only batch still needs to materialize
                            # the local SFX-preserve decision.  Otherwise an
                            # old model translation could make the renderer
                            # erase and typeset a sound effect even though the
                            # current policy says to leave it untouched.
                            region.target_text = region.source_text
                            region.target_text_origin = "sfx_preserve"
                            region.translation_mode = region.translation_mode or "quick"
                            region.translation_status = "completed"
                            region.translated_from_ocr_revision = region.ocr_revision
                            region.translation_revision += 1
                            region.translation_updated_at = utc_now()
                            strategy_changed = True
                # Empty pages and preserve-only SFX pages need no inpainting;
                # the render stage can still write an EXIF-oriented PNG.
                if not any(not _preserved(region) for region in regions):
                    if strategy_changed:
                        await mark_page_render_outdated(
                            session, page, reason="SFX 保留策略已更新，需要重新生成成品"
                        )
                        await session.commit()
                    return None
                if strategy_changed:
                    await mark_page_render_outdated(
                        session, page, reason="SFX 保留策略已更新，需要重新生成成品"
                    )
            # A valid artifact is a reusable stage result.  Do not enqueue a
            # second repair/render child (and therefore do not repeat the
            # expensive model call) when the page input and output versions
            # are still current.  Missing files invalidate the database flag
            # and are rebuilt normally.
            if task_type == "page_repair":
                if page.repair_status in {"completed", "completed_with_warnings"} and page.inpainted_path:
                    try:
                        if resolve_within(self.settings.data_dir, page.inpainted_path).is_file():
                            return None
                    except Exception:
                        pass
            elif task_type == "page_render":
                if page.render_status == "completed" and page.rendered_path:
                    try:
                        if resolve_within(self.settings.data_dir, page.rendered_path).is_file():
                            return None
                    except Exception:
                        pass
            child = Task(
                project_id=project.id,
                parent_task_id=parent_id,
                task_type=task_type,
                status="pending",
                stage="queued",
                total=1,
                parameters_json=_json({"provider": params.get("render_provider"), "device": params.get("render_device")}),
            )
            session.add(child)
            await session.flush()
            session.add(
                TaskItem(
                    task_id=child.id,
                    page_id=page.id,
                    source_path=page.original_path or "",
                    page_index=page.page_index,
                    sequence_index=0,
                    item_type=task_type,
                    status="pending",
                    expected_repair_input_revision=page.repair_input_revision,
                    expected_repair_revision=page.repair_revision,
                    expected_render_input_revision=page.render_input_revision,
                )
            )
            if task_type == "page_repair":
                page.repair_status = "pending"
            else:
                page.render_status = "pending"
            await session.commit()
            child_id = child.id
        self.render_manager.enqueue(child_id)
        return child_id

    async def _set_paused(self, task_id: str) -> None:
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                return
            task.status = "paused"
            task.stage = "paused"
            task.pause_requested = True
            task.current_page_id = None
            task.updated_at = utc_now()
            await session.commit()
        await self.broadcaster.notify(task_id)
        task_progress(task_id, "paused", task_type="batch_pipeline")

    async def _finish(self, task_id: str) -> None:
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                return
            warning_stage = await session.scalar(
                select(TaskItemStage.id)
                .join(TaskItem, TaskItem.id == TaskItemStage.task_item_id)
                .where(
                    TaskItem.task_id == task_id,
                    TaskItemStage.status == "completed_with_warnings",
                )
                .limit(1)
            )
            if task.failed and task.completed == 0:
                task.status = "failed"
                task.stage = "failed"
                task.error_code = task.error_code or "BATCH_PIPELINE_FAILED"
                task.error_message = task.error_message or "所有可处理页面均执行失败"
            else:
                task.status = "completed"
                task.stage = (
                    "completed_with_warnings"
                    if task.failed or task.skipped or warning_stage
                    else "completed"
                )
            task.current_page_id = None
            task.finished_at = utc_now()
            task.updated_at = utc_now()
            await session.commit()
            status = task.status
            stage = task.stage
            total = task.total
            completed = task.completed
            failed = task.failed
            skipped = task.skipped
            error_code = task.error_code
            error_message = task.error_message
        await self.broadcaster.notify(task_id)
        task_progress(
            task_id,
            "failed"
            if status == "failed"
            else "warning"
            if stage == "completed_with_warnings"
            else "completed",
            task_type="batch_pipeline",
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
            children = list(
                (
                    await session.scalars(
                        select(Task).where(
                            Task.parent_task_id == task_id,
                            Task.status.in_(["pending", "running"]),
                        )
                    )
                ).all()
            )
            for child in children:
                child.cancel_requested = True
            items = list((await session.scalars(select(TaskItem).where(TaskItem.task_id == task_id, TaskItem.status.in_(["pending", "processing"])))).all())
            for item in items:
                item.status = "skipped"
                item.error_code = "TASK_CANCELLED"
                item.error_message = "任务已取消"
                task.skipped += 1
                stages = list(
                    (
                        await session.scalars(
                            select(TaskItemStage).where(
                                TaskItemStage.task_item_id == item.id,
                                TaskItemStage.status.in_(["pending", "processing"]),
                            )
                        )
                    ).all()
                )
                for stage in stages:
                    stage.status = "skipped"
                    stage.error_code = "TASK_CANCELLED"
                    stage.error_message = "任务已取消"
                    stage.finished_at = utc_now()
            task.status = "cancelled"
            task.stage = "cancelled"
            task.current_page_id = None
            task.finished_at = utc_now()
            task.updated_at = utc_now()
            await session.commit()
            total = task.total
            completed = task.completed
            failed = task.failed
            skipped = task.skipped
        await self.broadcaster.notify(task_id)
        task_progress(
            task_id,
            "cancelled",
            task_type="batch_pipeline",
            total=total,
            completed=completed,
            failed=failed,
            skipped=skipped,
        )

    async def _fail_parent(self, task_id: str, code: str, message: str) -> None:
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                return
            children = list(
                (
                    await session.scalars(
                        select(Task).where(
                            Task.parent_task_id == task_id,
                            Task.status.in_(["pending", "running"]),
                        )
                    )
                ).all()
            )
            for child in children:
                child.cancel_requested = True
            task.status = "failed"
            task.stage = "failed"
            task.error_code = code
            task.error_message = message
            task.current_page_id = None
            task.finished_at = utc_now()
            task.updated_at = utc_now()
            await session.commit()
            total = task.total
            completed = task.completed
            failed = task.failed
            skipped = task.skipped
        await self.broadcaster.notify(task_id)
        task_progress(
            task_id,
            "failed",
            task_type="batch_pipeline",
            total=total,
            completed=completed,
            failed=failed,
            skipped=skipped,
            error_code=code,
            message=message,
        )

    async def _cancel_requested(self, task_id: str) -> bool:
        async with self.database.session_factory() as session:
            return bool(await session.scalar(select(Task.cancel_requested).where(Task.id == task_id)))

    async def _pause_requested(self, task_id: str) -> bool:
        async with self.database.session_factory() as session:
            return bool(await session.scalar(select(Task.pause_requested).where(Task.id == task_id)))

    async def _snapshot_session(self, session, task: Task) -> TaskSnapshot:
        counts: dict[str, dict[str, int]] = {}
        stage_rows = list(
            (
                await session.execute(
                    select(TaskItemStage.stage, TaskItemStage.status, func.count(TaskItemStage.id))
                    .join(TaskItem, TaskItem.id == TaskItemStage.task_item_id)
                    .where(TaskItem.task_id == task.id)
                    .group_by(TaskItemStage.stage, TaskItemStage.status)
                )
            ).all()
        )
        for stage, status, count in stage_rows:
            counts.setdefault(stage, {})[status] = int(count)
        can_pause = (
            task.task_type == "batch_pipeline"
            and task.status in {"pending", "running"}
            and not task.cancel_requested
        )
        can_resume = (
            task.task_type == "batch_pipeline"
            and task.status == "paused"
            and not task.cancel_requested
        )
        queue_position = None
        if task.parent_task_id is None and task.status in {
            "pending",
            "running",
            "pausing",
            "paused",
        }:
            queue_position = int(
                await session.scalar(
                    select(func.count(Task.id)).where(
                        Task.parent_task_id.is_(None),
                        Task.status.in_(["pending", "running", "pausing", "paused"]),
                        (
                            (Task.created_at < task.created_at)
                            | ((Task.created_at == task.created_at) & (Task.id < task.id))
                        ),
                    )
                )
                or 0
            ) + 1
        return TaskSnapshot(
            id=task.id,
            project_id=task.project_id,
            task_type=task.task_type,
            status=task.status,
            stage=task.stage,
            total=task.total,
            completed=task.completed,
            failed=task.failed,
            skipped=task.skipped,
            current_page_id=task.current_page_id,
            cancel_requested=task.cancel_requested,
            error_code=task.error_code,
            error_message=task.error_message,
            created_at=task.created_at,
            updated_at=task.updated_at,
            pause_requested=task.pause_requested,
            can_pause=can_pause,
            can_resume=can_resume,
            active_page_id=task.current_page_id,
            parent_task_id=task.parent_task_id,
            retry_of_task_id=task.retry_of_task_id,
            task_revision=task.task_revision,
            queue_position=queue_position,
            stage_counts=counts,
        )


async def get_performance_settings(database: Database) -> PerformanceSettings:
    async with database.session_factory() as session:
        value = await session.get(PerformanceSettings, 1)
        if value is None:
            value = PerformanceSettings(id=1)
            session.add(value)
            await session.commit()
            await session.refresh(value)
        return value


# Public name used by the Phase 7 task-center contract.  Keeping the
# implementation in this module avoids a second coordinator implementation
# while allowing callers to depend on the neutral queue abstraction.
TaskQueueCoordinator = BatchTaskCoordinator
