"""Persistent ZIP/EPUB export worker for Phase 9."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, update

from app.api.schemas import TaskSnapshot
from app.core.errors import AppError
from app.db.models import ExportArtifact, Page, Project, Task, TaskItem, TaskItemStage
from app.db.session import Database
from app.services.exporting import (
    ExportPage,
    build_epub_archive,
    build_zip_archive,
    cleanup_export_parts,
    cleanup_export_staging,
    copy_file_atomic,
    file_sha256,
    page_filename,
    resolve_current_rendered,
)
from app.services.storage import relative_to_root, resolve_within
from app.services.tasks import TaskBroadcaster, utc_now

logger = logging.getLogger(__name__)

EXPORT_TASK_TYPES = {"export_zip", "export_epub"}
ACTIVE = {"pending", "running"}
TERMINAL = {"completed", "failed", "cancelled"}


def _safe_message(exc: Exception) -> str:
    if isinstance(exc, AppError):
        return exc.message
    return "导出任务失败"


class ExportTaskManager:
    """Executes one durable page-copy/package task at a time per request."""

    def __init__(
        self,
        database: Database,
        settings: Any,
        broadcaster: TaskBroadcaster,
    ) -> None:
        self.database = database
        self.settings = settings
        self.broadcaster = broadcaster
        self._active: dict[str, asyncio.Task[None]] = {}

    async def start(self) -> None:
        await asyncio.to_thread(cleanup_export_parts, self.settings.projects_dir)
        async with self.database.session_factory() as session:
            task_ids = list(
                (
                    await session.scalars(
                        select(Task.id)
                        .where(Task.task_type.in_(EXPORT_TASK_TYPES), Task.status.in_(ACTIVE))
                        .order_by(Task.created_at, Task.id)
                    )
                ).all()
            )
            await session.execute(
                update(Task)
                .where(Task.task_type.in_(EXPORT_TASK_TYPES), Task.status == "running")
                .values(status="pending", stage="recovering", updated_at=utc_now())
            )
            await session.execute(
                update(TaskItem)
                .where(
                    TaskItem.task_id.in_(
                        select(Task.id).where(Task.task_type.in_(EXPORT_TASK_TYPES))
                    ),
                    TaskItem.status.in_(["processing", "running"]),
                )
                .values(status="pending", current_stage="copying_pages")
            )
            await session.commit()
        await asyncio.to_thread(
            cleanup_export_staging,
            self.settings.projects_dir,
            keep_task_ids=task_ids,
        )
        for task_id in task_ids:
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
        task = asyncio.create_task(self._run_guarded(task_id), name=f"export-{task_id}")
        self._active[task_id] = task
        task.add_done_callback(lambda _done, key=task_id: self._active.pop(key, None))

    async def interrupt_project(self, project_id: str) -> list[str]:
        async with self.database.session_factory() as session:
            tasks = list(
                (
                    await session.scalars(
                        select(Task).where(
                            Task.project_id == project_id,
                            Task.task_type.in_(EXPORT_TASK_TYPES),
                            Task.status.in_(ACTIVE),
                        )
                    )
                ).all()
            )
            ids = [task.id for task in tasks]
            for task in tasks:
                task.cancel_requested = True
                task.updated_at = utc_now()
            await session.commit()
        running = [self._active[task_id] for task_id in ids if task_id in self._active]
        for task in running:
            task.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)
        return ids

    async def cancel(self, task_id: str) -> TaskSnapshot:
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None or task.task_type not in EXPORT_TASK_TYPES:
                raise LookupError(task_id)
            if task.status not in TERMINAL:
                task.cancel_requested = True
                task.updated_at = utc_now()
                await session.commit()
            snapshot = await self._snapshot_session(session, task)
        await self.broadcaster.notify(task_id)
        return snapshot

    async def snapshot(self, task_id: str) -> TaskSnapshot | None:
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                return None
            return await self._snapshot_session(session, task)

    async def _run_guarded(self, task_id: str) -> None:
        try:
            await self._run(task_id)
        except asyncio.CancelledError:
            # Shutdown and project deletion deliberately leave a running task
            # recoverable.  The next application start resets it to pending.
            raise
        except Exception as exc:  # pragma: no cover - defensive worker guard
            logger.error("Export task failed", extra={"task_id": task_id})
            staging: Path | None = None
            try:
                async with self.database.session_factory() as session:
                    task = await session.get(Task, task_id)
                    if task is not None:
                        params = self._params(task)
                        staging = resolve_within(
                            self.settings.data_dir, str(params["staging_path"])
                        )
            except Exception:
                staging = None
            code = exc.code if isinstance(exc, AppError) else "EXPORT_FAILED"
            await self._fail(task_id, code, _safe_message(exc), staging)

    async def _run(self, task_id: str) -> None:
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None or task.status not in ACTIVE:
                return
            artifact = await session.get(ExportArtifact, self._artifact_id(task))
            project = await session.get(Project, task.project_id)
            if artifact is None or project is None:
                raise AppError("EXPORT_NOT_FOUND", "导出记录不存在", status_code=404)
            params = self._params(task)
            staging = resolve_within(self.settings.data_dir, str(params["staging_path"]))
            if not staging.is_relative_to(resolve_within(self.settings.data_dir, project.workspace_path)):
                raise AppError("UNSAFE_PATH", "导出暂存路径校验失败", status_code=422)
            artifact.status = "running"
            task.status = "running"
            task.stage = "preparing"
            task.started_at = task.started_at or utc_now()
            task.updated_at = utc_now()
            await session.commit()

        items = await self._items(task_id)
        for item_id in items:
            if await self._cancel_requested(task_id):
                await self._cancelled(task_id, staging)
                return
            await self._copy_item(task_id, item_id, staging)

        if await self._cancel_requested(task_id):
            await self._cancelled(task_id, staging)
            return
        await self._package(task_id, staging)

    @staticmethod
    def _artifact_id(task: Task) -> str:
        try:
            value = json.loads(task.parameters_json or "{}").get("artifact_id")
        except (TypeError, ValueError):
            value = None
        if not isinstance(value, str) or not value:
            raise AppError("EXPORT_NOT_FOUND", "导出记录不存在", status_code=404)
        return value

    @staticmethod
    def _params(task: Task) -> dict[str, Any]:
        try:
            value = json.loads(task.parameters_json or "{}")
        except (TypeError, ValueError) as exc:
            raise AppError("EXPORT_INVALID_TASK", "导出任务参数无效", status_code=500) from exc
        if not isinstance(value, dict) or not isinstance(value.get("staging_path"), str):
            raise AppError("EXPORT_INVALID_TASK", "导出任务参数无效", status_code=500)
        return value

    async def _items(self, task_id: str) -> list[str]:
        async with self.database.session_factory() as session:
            return list(
                (
                    await session.scalars(
                        select(TaskItem.id)
                        .where(TaskItem.task_id == task_id, TaskItem.status != "completed")
                        .order_by(TaskItem.page_index, TaskItem.sequence_index, TaskItem.id)
                    )
                ).all()
            )

    async def _copy_item(self, task_id: str, item_id: str, staging: Path) -> None:
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            item = await session.get(TaskItem, item_id)
            page = await session.get(Page, item.page_id) if item and item.page_id else None
            project = await session.get(Project, task.project_id) if task else None
            if task is None or item is None or page is None or project is None:
                raise AppError("EXPORT_PAGE_NOT_FOUND", "导出页不存在", status_code=422)
            if item.status == "completed":
                return
            manifest = self._params(task).get("pages", {}).get(page.id)
            if not isinstance(manifest, dict):
                raise AppError("EXPORT_INVALID_TASK", "导出页清单无效", status_code=500)
            source = resolve_current_rendered(page, project, self.settings.data_dir)
            if source is None or page.render_revision != int(manifest.get("render_revision", -1)):
                raise AppError("EXPORT_INPUT_CHANGED", "页面成品已变化，请重新导出", status_code=409)
            expected_hash = manifest.get("rendered_sha256")
            if expected_hash and page.rendered_sha256 and page.rendered_sha256 != expected_hash:
                raise AppError("EXPORT_INPUT_CHANGED", "页面成品已变化，请重新导出", status_code=409)
            if expected_hash and await asyncio.to_thread(file_sha256, source) != expected_hash:
                raise AppError("EXPORT_INPUT_CHANGED", "页面成品已变化，请重新导出", status_code=409)
            item.status = "processing"
            item.current_stage = "copying_pages"
            item.started_at = item.started_at or utc_now()
            task.current_page_id = page.id
            task.stage = "copying_pages"
            task.updated_at = utc_now()
            stage = await session.scalar(
                select(TaskItemStage).where(
                    TaskItemStage.task_item_id == item.id,
                    TaskItemStage.stage == "export",
                )
            )
            if stage:
                stage.status = "processing"
                stage.started_at = stage.started_at or utc_now()
            await session.commit()
        destination = staging / page_filename(page.page_index)
        await asyncio.to_thread(copy_file_atomic, source, destination)
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            item = await session.get(TaskItem, item_id)
            if task is None or item is None:
                return
            if task.cancel_requested:
                return
            item.status = "completed"
            item.current_stage = None
            item.finished_at = utc_now()
            task.completed += 1
            task.current_page_id = None
            task.updated_at = utc_now()
            stage = await session.scalar(
                select(TaskItemStage).where(
                    TaskItemStage.task_item_id == item.id,
                    TaskItemStage.stage == "export",
                )
            )
            if stage:
                stage.status = "completed"
                stage.finished_at = utc_now()
            await session.commit()
        await self.broadcaster.notify(task_id)

    async def _package(self, task_id: str, staging: Path) -> None:
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                return
            params = self._params(task)
            artifact = await session.get(ExportArtifact, self._artifact_id(task))
            project = await session.get(Project, task.project_id)
            if artifact is None or project is None:
                raise AppError("EXPORT_NOT_FOUND", "导出记录不存在", status_code=404)
            pages = self._manifest_pages(params)
            output = resolve_within(self.settings.data_dir, artifact.relative_path or "") if artifact.relative_path else resolve_within(self.settings.data_dir, f"{project.workspace_path}/export/{artifact.filename}")
            project_root = resolve_within(self.settings.data_dir, project.workspace_path)
            if not output.is_relative_to(project_root / "export"):
                raise AppError("UNSAFE_PATH", "导出输出路径校验失败", status_code=422)
            task.stage = "packaging"
            task.current_page_id = None
            task.updated_at = utc_now()
            await session.commit()
        await self.broadcaster.notify(task_id)
        if artifact.format == "zip":
            await asyncio.to_thread(
                build_zip_archive,
                staging,
                output,
                project_name=project.name,
                pages=pages,
            )
        else:
            await asyncio.to_thread(
                build_epub_archive,
                staging,
                output,
                project_name=project.name,
                pages=pages,
                cover_page_index=int(params["cover_page_index"]),
                identifier=project.id,
            )
        if await self._cancel_requested(task_id):
            output.unlink(missing_ok=True)
            await self._cancelled(task_id, staging)
            return
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                output.unlink(missing_ok=True)
                return
            task.stage = "saving_export"
            task.updated_at = utc_now()
            await session.commit()
        await self.broadcaster.notify(task_id)
        digest = await asyncio.to_thread(file_sha256, output)
        size = output.stat().st_size
        if not await self._manifest_still_current(task_id):
            output.unlink(missing_ok=True)
            await self._fail(task_id, "EXPORT_INPUT_CHANGED", "页面成品已变化，请重新导出", staging)
            return
        # A cancellation can arrive while the archive is being hashed or the
        # final manifest check is reading page metadata.  Honour it before
        # committing the artifact so a request received during packaging never
        # turns into a completed download unexpectedly.
        if await self._cancel_requested(task_id):
            output.unlink(missing_ok=True)
            await self._cancelled(task_id, staging)
            return
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            artifact = await session.get(ExportArtifact, self._artifact_id(task)) if task else None
            if task is None or artifact is None:
                output.unlink(missing_ok=True)
                return
            artifact.status = "completed"
            artifact.relative_path = relative_to_root(self.settings.data_dir, output)
            artifact.sha256 = digest
            artifact.size = size
            artifact.page_count = task.total
            artifact.error_code = None
            artifact.error_message = None
            task.status = "completed"
            task.stage = "completed"
            task.result_json = json.dumps(
                {"artifact_id": artifact.id, "format": artifact.format}, ensure_ascii=False
            )
            task.current_page_id = None
            task.finished_at = utc_now()
            task.updated_at = utc_now()
            await session.commit()
        await asyncio.to_thread(shutil.rmtree, staging, True)
        await self.broadcaster.notify(task_id)

    async def _manifest_still_current(self, task_id: str) -> bool:
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                return False
            params = self._params(task)
            project = await session.get(Project, task.project_id)
            if project is None:
                return False
            for page_id, expected in params.get("pages", {}).items():
                page = await session.get(Page, page_id)
                if page is None or page.project_id != project.id:
                    return False
                if page.render_revision != int(expected.get("render_revision", -1)):
                    return False
                if (
                    expected.get("rendered_sha256")
                    and page.rendered_sha256
                    and page.rendered_sha256 != expected["rendered_sha256"]
                ):
                    return False
                source = resolve_current_rendered(page, project, self.settings.data_dir)
                if source is None:
                    return False
                if expected.get("rendered_sha256") and await asyncio.to_thread(file_sha256, source) != expected["rendered_sha256"]:
                    return False
            return True

    @staticmethod
    def _manifest_pages(params: dict[str, Any]) -> list[ExportPage]:
        values: list[ExportPage] = []
        for value in params.get("pages", {}).values():
            values.append(
                ExportPage(
                    page_id=str(value["page_id"]),
                    page_index=int(value["page_index"]),
                    path=Path(str(value["staging_path"])),
                    rendered_revision=int(value["render_revision"]),
                    rendered_sha256=value.get("rendered_sha256"),
                    width=int(value["width"]),
                    height=int(value["height"]),
                )
            )
        return sorted(values, key=lambda item: item.page_index)

    async def _cancel_requested(self, task_id: str) -> bool:
        async with self.database.session_factory() as session:
            return bool(await session.scalar(select(Task.cancel_requested).where(Task.id == task_id)))

    async def _cancelled(self, task_id: str, staging: Path) -> None:
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                return
            artifact = await session.get(ExportArtifact, self._artifact_id(task))
            pending = list(
                (
                    await session.scalars(
                        select(TaskItem).where(
                            TaskItem.task_id == task_id,
                            TaskItem.status.in_(["pending", "processing"]),
                        )
                    )
                ).all()
            )
            for item in pending:
                item.status = "skipped"
                item.error_code = "TASK_CANCELLED"
                item.error_message = "导出任务已取消"
                item.finished_at = utc_now()
                stages = list(
                    (
                        await session.scalars(
                            select(TaskItemStage).where(TaskItemStage.task_item_id == item.id)
                        )
                    ).all()
                )
                for stage in stages:
                    if stage.status not in {"completed", "skipped"}:
                        stage.status = "skipped"
                        stage.error_code = "TASK_CANCELLED"
                        stage.error_message = "导出任务已取消"
                        stage.finished_at = utc_now()
            task.skipped += len(pending)
            task.status = "cancelled"
            task.stage = "cancelled"
            task.current_page_id = None
            task.finished_at = utc_now()
            task.updated_at = utc_now()
            if artifact:
                artifact.status = "cancelled"
                artifact.error_code = "TASK_CANCELLED"
                artifact.error_message = "导出任务已取消"
            await session.commit()
        await asyncio.to_thread(shutil.rmtree, staging, True)
        await self.broadcaster.notify(task_id)

    async def _fail(
        self,
        task_id: str,
        code: str,
        message: str,
        staging: Path | None = None,
    ) -> None:
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                return
            artifact = await session.get(ExportArtifact, self._artifact_id(task))
            for item in list(
                (
                    await session.scalars(
                        select(TaskItem).where(
                            TaskItem.task_id == task_id,
                            TaskItem.status.in_(["pending", "processing"]),
                        )
                    )
                ).all()
            ):
                item.status = "failed"
                item.error_code = code
                item.error_message = message[:500]
                item.finished_at = utc_now()
                stages = list(
                    (
                        await session.scalars(
                            select(TaskItemStage).where(TaskItemStage.task_item_id == item.id)
                        )
                    ).all()
                )
                for stage in stages:
                    if stage.status not in {"completed", "skipped"}:
                        stage.status = "failed"
                        stage.error_code = code
                        stage.error_message = message[:500]
                        stage.finished_at = utc_now()
                task.failed += 1
            task.status = "failed"
            task.stage = "failed"
            task.error_code = code
            task.error_message = message[:500]
            task.current_page_id = None
            task.finished_at = utc_now()
            task.updated_at = utc_now()
            if artifact:
                artifact.status = "failed"
                artifact.error_code = code
                artifact.error_message = message[:500]
            await session.commit()
        if staging is not None:
            await asyncio.to_thread(shutil.rmtree, staging, True)
        await self.broadcaster.notify(task_id)

    async def _snapshot_session(self, session, task: Task) -> TaskSnapshot:
        rows = list(
            (
                await session.execute(
                    select(TaskItemStage.stage, TaskItemStage.status, func.count(TaskItemStage.id))
                    .join(TaskItem, TaskItem.id == TaskItemStage.task_item_id)
                    .where(TaskItem.task_id == task.id)
                    .group_by(TaskItemStage.stage, TaskItemStage.status)
                )
            ).all()
        )
        counts: dict[str, dict[str, int]] = {}
        for stage, status, count in rows:
            counts.setdefault(stage, {})[status] = int(count)
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
            can_pause=False,
            can_resume=False,
            active_page_id=task.current_page_id,
            parent_task_id=task.parent_task_id,
            retry_of_task_id=task.retry_of_task_id,
            task_revision=task.task_revision,
            stage_counts=counts,
        )


__all__ = ["EXPORT_TASK_TYPES", "ExportTaskManager"]
