from __future__ import annotations

import asyncio
import logging
import shutil
from collections import defaultdict, deque
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select, update
from sqlalchemy.orm import selectinload

from app.api.schemas import TaskSnapshot
from app.core.config import Settings
from app.core.errors import AppError
from app.db.models import ImportSession, Page, Project, Task, TaskItem
from app.db.session import Database
from app.services.archive import extract_archive_images
from app.services.images import ImageValidationError, generate_derivatives
from app.services.storage import (
    atomic_copy,
    natural_sort_key,
    relative_to_root,
    resolve_within,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


logger = logging.getLogger(__name__)


class TaskBroadcaster:
    def __init__(self) -> None:
        self._conditions: dict[str, asyncio.Condition] = defaultdict(asyncio.Condition)
        self._versions: dict[str, int] = defaultdict(int)
        self._global_condition = asyncio.Condition()
        self._global_version = 0
        self._last_task_id: str | None = None
        # Keep a bounded journal so a busy batch cannot collapse several
        # child notifications into only the last ID observed by the global
        # task-center WebSocket.
        self._global_events: deque[tuple[int, str]] = deque(maxlen=2048)

    def version(self, task_id: str) -> int:
        return self._versions[task_id]

    async def notify(self, task_id: str) -> None:
        condition = self._conditions[task_id]
        async with condition:
            self._versions[task_id] += 1
            condition.notify_all()
        async with self._global_condition:
            self._global_version += 1
            self._last_task_id = task_id
            self._global_events.append((self._global_version, task_id))
            self._global_condition.notify_all()

    def global_version(self) -> int:
        return self._global_version

    def last_task_id(self) -> str | None:
        return self._last_task_id

    def global_task_ids(self, after_version: int) -> list[str]:
        """Return changed task IDs after a global notification version.

        Notifications are intentionally coalesced by caller, but preserving
        all IDs in the bounded journal prevents a fast child-task sequence
        from dropping an unrelated top-level task update.
        """
        return [task_id for version, task_id in self._global_events if version > after_version]

    async def wait_global(self, after_version: int, timeout: float = 20.0) -> int:
        async with self._global_condition:
            if self._global_version > after_version:
                return self._global_version
            try:
                await asyncio.wait_for(
                    self._global_condition.wait_for(lambda: self._global_version > after_version),
                    timeout,
                )
            except TimeoutError:
                pass
            return self._global_version

    async def wait(self, task_id: str, after_version: int, timeout: float = 20.0) -> int:
        condition = self._conditions[task_id]
        async with condition:
            if self._versions[task_id] > after_version:
                return self._versions[task_id]
            try:
                await asyncio.wait_for(
                    condition.wait_for(lambda: self._versions[task_id] > after_version), timeout
                )
            except TimeoutError:
                pass
            return self._versions[task_id]


async def task_blocked_by_paused_batch(session, task_id: str) -> bool:
    """Whether a child task belongs to a paused batch parent.

    Child managers recover their own persisted queues independently.  A
    paused parent must keep pending child work dormant until the parent is
    resumed; otherwise a restart could advance a page while the user still
    expects the pipeline to be paused.
    """
    task = await session.get(Task, task_id)
    if task is None or task.parent_task_id is None:
        return False
    parent = await session.get(Task, task.parent_task_id)
    return bool(
        parent and parent.task_type == "batch_pipeline" and parent.status in {"paused", "pausing"}
    )


class ImportTaskManager:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        broadcaster: TaskBroadcaster | None = None,
    ) -> None:
        self.database = database
        self.settings = settings
        self.broadcaster = broadcaster or TaskBroadcaster()
        self._active: dict[str, asyncio.Task[None]] = {}
        self._semaphore = asyncio.Semaphore(settings.import_concurrency)

    async def start(self) -> None:
        await self._cleanup_stale_uploads()
        async with self.database.session_factory() as session:
            await session.execute(
                update(Task)
                .where(Task.task_type == "project_import", Task.status == "running")
                .values(status="pending", stage="recovering", updated_at=utc_now())
            )
            await session.execute(
                update(TaskItem)
                .where(
                    TaskItem.task_id.in_(select(Task.id).where(Task.task_type == "project_import")),
                    TaskItem.status == "running",
                )
                .values(status="pending")
            )
            await session.commit()
            task_ids = list(
                (
                    await session.scalars(
                        select(Task.id).where(
                            Task.task_type == "project_import", Task.status == "pending"
                        )
                    )
                ).all()
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
        existing = self._active.get(task_id)
        if existing and not existing.done():
            return
        task = asyncio.create_task(self._run_guarded(task_id), name=f"import-{task_id}")
        self._active[task_id] = task
        task.add_done_callback(lambda _done, key=task_id: self._active.pop(key, None))

    async def cancel(self, task_id: str) -> TaskSnapshot:
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                raise LookupError(task_id)
            if task.status in {"completed", "failed", "cancelled"}:
                return self._snapshot(task)
            task.cancel_requested = True
            task.updated_at = utc_now()
            await session.commit()
            snapshot = self._snapshot(task)
        await self.broadcaster.notify(task_id)
        return snapshot

    async def interrupt_project(self, project_id: str) -> list[str]:
        async with self.database.session_factory() as session:
            tasks = list(
                (
                    await session.scalars(
                        select(Task).where(
                            Task.project_id == project_id,
                            Task.status.in_(["pending", "running"]),
                        )
                    )
                ).all()
            )
            task_ids = [task.id for task in tasks]
            for task in tasks:
                task.cancel_requested = True
            await session.commit()
        running = [self._active[task_id] for task_id in task_ids if task_id in self._active]
        for active in running:
            active.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)
        return task_ids

    async def snapshot(self, task_id: str) -> TaskSnapshot | None:
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            return self._snapshot(task) if task else None

    @staticmethod
    def _snapshot(task: Task) -> TaskSnapshot:
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
            parent_task_id=task.parent_task_id,
            retry_of_task_id=task.retry_of_task_id,
            task_revision=task.task_revision,
        )

    async def _run_guarded(self, task_id: str) -> None:
        async with self._semaphore:
            try:
                await self._run_import(task_id)
            except asyncio.CancelledError:
                raise
            except AppError as exc:
                await self._fail_task(task_id, exc.code, exc.message)
            except Exception:
                # Keep source paths and parser/provider exception bodies out of
                # the persistent application log.
                logger.error("Import task failed", extra={"request_id": "-"})
                await self._fail_task(task_id, "IMPORT_FAILED", "导入任务失败")

    async def _run_import(self, task_id: str) -> None:
        await self._set_task_running(task_id)
        await self._ensure_manifest(task_id)

        async with self.database.session_factory() as session:
            item_ids = list(
                (
                    await session.scalars(
                        select(TaskItem.id)
                        .where(TaskItem.task_id == task_id, TaskItem.status == "pending")
                        .order_by(TaskItem.page_index)
                    )
                ).all()
            )

        pending = iter(item_ids)
        stop_dispatch = asyncio.Event()
        write_lock = asyncio.Lock()

        async def process_pages() -> None:
            while not stop_dispatch.is_set():
                if await self._is_cancel_requested(task_id):
                    stop_dispatch.set()
                    return
                item_id = next(pending, None)
                if item_id is None:
                    return
                try:
                    await self._process_item(task_id, item_id, write_lock)
                except Exception:
                    stop_dispatch.set()
                    raise

        workers = [
            asyncio.create_task(process_pages(), name=f"import-page-{task_id}-{index}")
            for index in range(min(self.settings.import_page_concurrency, len(item_ids)))
        ]
        joined = asyncio.gather(*workers, return_exceptions=True)
        try:
            results = await asyncio.shield(joined)
        except asyncio.CancelledError:
            stop_dispatch.set()
            # to_thread cannot stop a file copy or image encoder. Let those
            # operations finish before a caller removes the project tree.
            await joined
            raise
        for result in results:
            if isinstance(result, BaseException):
                raise result

        if await self._is_cancel_requested(task_id):
            await self._finish_cancelled(task_id)
        else:
            await self._finish_import(task_id)

    async def _set_task_running(self, task_id: str) -> None:
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None or task.status not in {"pending", "running"}:
                return
            task.status = "running"
            task.stage = "validating"
            task.current_page_id = None
            task.started_at = task.started_at or utc_now()
            task.updated_at = utc_now()
            await session.commit()
        await self.broadcaster.notify(task_id)

    async def _ensure_manifest(self, task_id: str) -> None:
        async with self.database.session_factory() as session:
            task = await session.scalar(
                select(Task).where(Task.id == task_id).options(selectinload(Task.items))
            )
            if task is None or task.items:
                return
            import_session = await session.scalar(
                select(ImportSession)
                .where(ImportSession.id == task.import_session_id)
                .options(selectinload(ImportSession.files))
            )
            project = await session.get(Project, task.project_id)
            if import_session is None or project is None:
                raise RuntimeError("导入会话或项目不存在")
            uploaded = list(import_session.files)
            source_type = import_session.source_type
            task.stage = "extracting" if source_type == "zip" else "indexing"
            await session.commit()

        await self.broadcaster.notify(task_id)
        if source_type == "zip":
            if len(uploaded) != 1:
                raise RuntimeError("ZIP 导入必须且只能包含一个文件")
            archive_path = resolve_within(self.settings.data_dir, uploaded[0].stored_path)
            extraction_dir = self.settings.staging_dir / import_session.id / "extracted"
            archive_images = await asyncio.to_thread(
                extract_archive_images, archive_path, extraction_dir, self.settings
            )
            manifest = [
                (item.relative_path, relative_to_root(self.settings.data_dir, item.extracted_path))
                for item in archive_images
            ]
        else:
            manifest = sorted(
                [(item.relative_path, item.stored_path) for item in uploaded],
                key=lambda item: natural_sort_key(item[0]),
            )

        if not manifest:
            raise RuntimeError("没有可导入的漫画图片")

        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            project = await session.get(Project, task.project_id) if task else None
            if task is None or project is None:
                raise RuntimeError("导入任务不存在")
            existing_count = await session.scalar(
                select(func.count(Page.id)).where(Page.project_id == project.id)
            )
            if existing_count:
                return
            pages = []
            items = []
            for page_index, (relative_path, source_path) in enumerate(manifest, start=1):
                page = Page(
                    id=str(uuid4()),
                    project_id=project.id,
                    page_index=page_index,
                    source_filename=relative_path,
                    import_status="pending",
                )
                pages.append(page)
                items.append(
                    TaskItem(
                        task_id=task.id,
                        page_id=page.id,
                        source_path=source_path,
                        page_index=page_index,
                        sequence_index=page_index,
                        status="pending",
                    )
                )
            session.add_all(pages)
            session.add_all(items)
            project.total_pages = len(manifest)
            task.total = len(manifest)
            task.stage = "generating_derivatives"
            await session.commit()
        await self.broadcaster.notify(task_id)

    async def _process_item(self, task_id: str, item_id: str, write_lock: asyncio.Lock) -> None:
        async with write_lock:
            async with self.database.session_factory() as session:
                item = await session.scalar(
                    select(TaskItem)
                    .where(TaskItem.id == item_id)
                    .options(selectinload(TaskItem.page))
                )
                task = await session.get(Task, task_id)
                project = await session.get(Project, task.project_id) if task else None
                if item is None or item.page is None or task is None or project is None:
                    raise RuntimeError("导入任务项数据不完整")
                item.status = "running"
                item.page.import_status = "processing"
                task.stage = "generating_derivatives"
                await session.commit()
                page_id = item.page.id
                page_index = item.page_index
                source_relative = item.source_path
                source_filename = item.page.source_filename
                workspace_path = project.workspace_path
        await self.broadcaster.notify(task_id)

        source = resolve_within(self.settings.data_dir, source_relative)
        project_root = resolve_within(self.settings.data_dir, workspace_path)
        suffix = Path(source_filename).suffix.casefold()
        original = project_root / "original" / f"{page_index:06d}{suffix}"
        thumbnail = project_root / "thumbnails" / f"{page_index:06d}.webp"
        preview = project_root / "preview" / f"{page_index:06d}.webp"

        try:
            await asyncio.to_thread(atomic_copy, source, original)
            derivatives = await asyncio.to_thread(
                generate_derivatives,
                original,
                thumbnail,
                preview,
                thumbnail_size=self.settings.thumbnail_size,
                preview_size=self.settings.preview_size,
            )
        except ImageValidationError as exc:
            await self._mark_item_failed(
                task_id,
                item_id,
                page_id,
                "CORRUPT_IMAGE",
                str(exc),
                original_path=relative_to_root(self.settings.data_dir, original),
                page_status="corrupt",
                write_lock=write_lock,
            )
            return
        except Exception:
            await self._mark_item_failed(
                task_id,
                item_id,
                page_id,
                "IMAGE_PROCESS_FAILED",
                "图片处理失败",
                original_path=(
                    relative_to_root(self.settings.data_dir, original)
                    if original.exists()
                    else None
                ),
                page_status="failed",
                write_lock=write_lock,
            )
            return

        async with write_lock:
            async with self.database.session_factory() as session:
                item = await session.get(TaskItem, item_id)
                page = await session.get(Page, page_id)
                if item is None or page is None or item.status != "running":
                    return
                page.original_path = relative_to_root(self.settings.data_dir, original)
                page.preview_path = relative_to_root(self.settings.data_dir, preview)
                page.thumbnail_path = relative_to_root(self.settings.data_dir, thumbnail)
                page.width = derivatives.width
                page.height = derivatives.height
                page.preview_width = derivatives.preview_width
                page.preview_height = derivatives.preview_height
                page.thumbnail_width = derivatives.thumbnail_width
                page.thumbnail_height = derivatives.thumbnail_height
                page.sha256 = derivatives.sha256
                page.import_status = "ready"
                item.status = "completed"
                item.error_code = None
                item.error_message = None
                await session.execute(
                    update(Task)
                    .where(Task.id == task_id)
                    .values(completed=Task.completed + 1, updated_at=utc_now())
                )
                await session.commit()
        await self.broadcaster.notify(task_id)

    async def _mark_item_failed(
        self,
        task_id: str,
        item_id: str,
        page_id: str,
        code: str,
        message: str,
        *,
        original_path: str | None,
        page_status: str,
        write_lock: asyncio.Lock,
    ) -> None:
        async with write_lock:
            async with self.database.session_factory() as session:
                item = await session.get(TaskItem, item_id)
                page = await session.get(Page, page_id)
                if item is None or page is None or item.status != "running":
                    return
                item.status = "failed"
                item.error_code = code
                item.error_message = message
                page.import_status = page_status
                page.original_path = original_path
                await session.execute(
                    update(Task)
                    .where(Task.id == task_id)
                    .values(failed=Task.failed + 1, updated_at=utc_now())
                )
                await session.commit()
        await self.broadcaster.notify(task_id)

    async def _finish_import(self, task_id: str) -> None:
        cleanup_session_id: str | None = None
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            project = await session.get(Project, task.project_id) if task else None
            import_session = (
                await session.get(ImportSession, task.import_session_id)
                if task and task.import_session_id
                else None
            )
            if task is None or project is None:
                return
            if task.completed == 0:
                task.status = "failed"
                task.stage = "failed"
                task.error_code = "NO_VALID_IMAGES"
                task.error_message = "没有可用的漫画图片"
                project.status = "import_failed"
                if import_session:
                    import_session.status = "failed"
            else:
                task.status = "completed"
                task.stage = "completed"
                project.status = "ready_with_warnings" if task.failed else "ready"
                first_page = await session.scalar(
                    select(Page.id)
                    .where(Page.project_id == project.id, Page.import_status == "ready")
                    .order_by(Page.page_index)
                    .limit(1)
                )
                project.cover_page_id = first_page
                if import_session:
                    import_session.status = "completed"
                    cleanup_session_id = import_session.id
            task.current_page_id = None
            task.finished_at = utc_now()
            task.updated_at = utc_now()
            project.updated_at = utc_now()
            await session.commit()
        if cleanup_session_id:
            await asyncio.to_thread(
                shutil.rmtree,
                self.settings.staging_dir / cleanup_session_id,
                True,
            )
        await self.broadcaster.notify(task_id)

    async def _finish_cancelled(self, task_id: str) -> None:
        cleanup_session_id: str | None = None
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            project = await session.get(Project, task.project_id) if task else None
            import_session = (
                await session.get(ImportSession, task.import_session_id)
                if task and task.import_session_id
                else None
            )
            if task is None:
                return
            task.status = "cancelled"
            task.stage = "cancelled"
            task.finished_at = utc_now()
            task.current_page_id = None
            if project:
                project.status = "import_failed"
            if import_session:
                import_session.status = "cancelled"
                cleanup_session_id = import_session.id
            await session.commit()
        if cleanup_session_id:
            await asyncio.to_thread(
                shutil.rmtree, self.settings.staging_dir / cleanup_session_id, True
            )
        await self.broadcaster.notify(task_id)

    async def _fail_task(self, task_id: str, code: str, message: str) -> None:
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            project = await session.get(Project, task.project_id) if task else None
            import_session = (
                await session.get(ImportSession, task.import_session_id)
                if task and task.import_session_id
                else None
            )
            if task is None:
                return
            task.status = "failed"
            task.stage = "failed"
            task.error_code = code
            task.error_message = message[:500]
            task.finished_at = utc_now()
            if project:
                project.status = "import_failed"
            if import_session:
                import_session.status = "failed"
            await session.commit()
        await self.broadcaster.notify(task_id)

    async def _is_cancel_requested(self, task_id: str) -> bool:
        async with self.database.session_factory() as session:
            statement = select(Task.cancel_requested).where(Task.id == task_id)
            return bool(await session.scalar(statement))

    async def _cleanup_stale_uploads(self) -> None:
        now = utc_now()
        async with self.database.session_factory() as session:
            stale_ids = list(
                (
                    await session.scalars(
                        select(ImportSession.id).where(
                            ImportSession.status == "uploading", ImportSession.expires_at < now
                        )
                    )
                ).all()
            )
            if stale_ids:
                await session.execute(
                    sa_delete(ImportSession).where(ImportSession.id.in_(stale_ids))
                )
                await session.commit()
        for session_id in stale_ids:
            await asyncio.to_thread(shutil.rmtree, self.settings.staging_dir / session_id, True)
