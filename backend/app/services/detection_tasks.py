from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime

from sqlalchemy import delete, select, update

from app.core.config import Settings
from app.db.models import DetectionModel, DetectionRegion, Page, Task, TaskItem
from app.db.session import Database
from app.providers.detection.base import DetectionDependencyError, DetectionRuntime
from app.services.detection_models import (
    PROJECT_STORAGE,
    DetectionModelCatalog,
    resolve_detection_model_path,
)
from app.services.storage import resolve_within
from app.services.tasks import TaskBroadcaster

logger = logging.getLogger(__name__)

_BALLOON_CLASS_NAMES = {
    "balloon",
    "bubble",
    "speechballoon",
    "speechbubble",
    "气泡",
    "气泡框",
    "对话框",
}
_TEXT_CLASS_NAMES = {
    "text",
    "textregion",
    "textbox",
    "文字",
    "文字框",
    "文本",
    "文本框",
}


def utc_now() -> datetime:
    return datetime.now(UTC)


def _normalized_class_name(value: object) -> str:
    return "".join(character for character in str(value).casefold() if character.isalnum())


def filter_detection_output(
    regions: list[dict[str, object]],
    class_names_json: str,
) -> list[dict[str, object]]:
    """Return text boxes that substantially overlap a detected balloon.

    Models without both recognized text and balloon labels retain their original
    output for backwards compatibility.
    """

    try:
        class_names = json.loads(class_names_json)
    except (TypeError, ValueError):
        return regions
    if not isinstance(class_names, dict):
        return regions
    balloon_class_ids = {
        int(class_id)
        for class_id, class_name in class_names.items()
        if str(class_id).isdigit()
        and _normalized_class_name(class_name) in _BALLOON_CLASS_NAMES
    }
    text_class_ids = {
        int(class_id)
        for class_id, class_name in class_names.items()
        if str(class_id).isdigit() and _normalized_class_name(class_name) in _TEXT_CLASS_NAMES
    }
    if not balloon_class_ids or not text_class_ids:
        return regions

    balloons = [region for region in regions if int(region["class_id"]) in balloon_class_ids]
    texts = [region for region in regions if int(region["class_id"]) in text_class_ids]
    return [text for text in texts if any(_text_overlaps_balloon(text, balloon) for balloon in balloons)]


def _text_overlaps_balloon(
    text_region: dict[str, object],
    balloon_region: dict[str, object],
) -> bool:
    text_x1 = float(text_region["x1"])
    text_y1 = float(text_region["y1"])
    text_x2 = float(text_region["x2"])
    text_y2 = float(text_region["y2"])
    balloon_x1 = float(balloon_region["x1"])
    balloon_y1 = float(balloon_region["y1"])
    balloon_x2 = float(balloon_region["x2"])
    balloon_y2 = float(balloon_region["y2"])

    center_x = (text_x1 + text_x2) / 2
    center_y = (text_y1 + text_y2) / 2
    center_inside = (
        balloon_x1 <= center_x <= balloon_x2 and balloon_y1 <= center_y <= balloon_y2
    )
    intersection_width = max(0.0, min(text_x2, balloon_x2) - max(text_x1, balloon_x1))
    intersection_height = max(0.0, min(text_y2, balloon_y2) - max(text_y1, balloon_y1))
    text_area = max(0.0, text_x2 - text_x1) * max(0.0, text_y2 - text_y1)
    overlap_ratio = (
        intersection_width * intersection_height / text_area if text_area > 0 else 0.0
    )
    return center_inside or overlap_ratio >= 0.5


class DetectionTaskManager:
    """Runs persisted page-detection tasks through one shared inference runtime."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        runtime: DetectionRuntime,
        broadcaster: TaskBroadcaster,
    ) -> None:
        self.database = database
        self.settings = settings
        self.runtime = runtime
        self.catalog: DetectionModelCatalog | None = None
        self.broadcaster = broadcaster
        self._active: dict[str, asyncio.Task[None]] = {}
        self._semaphore = asyncio.Semaphore(1)

    async def start(self) -> None:
        async with self.database.session_factory() as session:
            task_ids = list(
                (
                    await session.scalars(
                        select(Task.id).where(
                            Task.task_type == "page_detection",
                            Task.status.in_(["pending", "running"]),
                        )
                    )
                ).all()
            )
            await session.execute(
                update(Task)
                .where(
                    Task.task_type == "page_detection",
                    Task.status == "running",
                )
                .values(status="pending", stage="recovering", updated_at=utc_now())
            )
            if task_ids:
                await session.execute(
                    update(TaskItem)
                    .where(
                        TaskItem.task_id.in_(task_ids),
                        TaskItem.status == "running",
                    )
                    .values(status="pending")
                )
            await session.commit()
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
        task = asyncio.create_task(self._run_guarded(task_id), name=f"detect-{task_id}")
        self._active[task_id] = task
        task.add_done_callback(lambda _done, key=task_id: self._active.pop(key, None))

    async def cancel(self, task_id: str) -> None:
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                raise LookupError(task_id)
            if task.task_type != "page_detection":
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
                            Task.task_type == "page_detection",
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
            *(self._active[task_id] for task_id in task_ids if task_id in self._active),
            return_exceptions=True,
        )
        return task_ids

    async def _run_guarded(self, task_id: str) -> None:
        async with self._semaphore:
            try:
                if self.catalog is not None:
                    async with self.database.session_factory() as session:
                        task = await session.get(Task, task_id)
                        parameters = json.loads(task.parameters_json or "{}") if task else {}
                    await self.catalog.wait_for_model(str(parameters.get("model_id", "")))
                await self._run_task(task_id)
            except asyncio.CancelledError:
                raise
            except DetectionDependencyError as exc:
                await self._fail_task(task_id, "DETECTION_DEPENDENCY_MISSING", str(exc))
            except Exception:
                logger.error("Detection task failed", extra={"request_id": "-"})
                await self._fail_task(task_id, "DETECTION_FAILED", "检测任务失败")

    async def _run_task(self, task_id: str) -> None:
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None or task.status not in {"pending", "running"}:
                return
            parameters = json.loads(task.parameters_json or "{}")
            task.status = "running"
            task.stage = "loading_model"
            task.started_at = task.started_at or utc_now()
            task.updated_at = utc_now()
            await session.commit()
            item_ids = list(
                (
                    await session.scalars(
                        select(TaskItem.id)
                        .where(TaskItem.task_id == task_id, TaskItem.status == "pending")
                        .order_by(TaskItem.page_index)
                    )
                ).all()
            )
        await self.broadcaster.notify(task_id)

        for item_id in item_ids:
            if await self._cancel_requested(task_id):
                await self._finish_cancelled(task_id)
                return
            await self._process_item(task_id, item_id, parameters)

        if await self._cancel_requested(task_id):
            await self._finish_cancelled(task_id)
        else:
            await self._finish_completed(task_id)

    async def _process_item(
        self,
        task_id: str,
        item_id: str,
        parameters: dict[str, object],
    ) -> None:
        async with self.database.session_factory() as session:
            item = await session.get(TaskItem, item_id)
            task = await session.get(Task, task_id)
            page = await session.get(Page, item.page_id) if item and item.page_id else None
            model = await session.get(DetectionModel, str(parameters.get("model_id", "")))
            if item is None or task is None or page is None or model is None:
                raise RuntimeError("检测任务数据不完整")
            item.status = "running"
            task.stage = "detecting"
            task.current_page_id = page.id
            task.updated_at = utc_now()
            await session.commit()
            page_id = page.id
            expected_revision = item.expected_revision
            image_path = resolve_within(self.settings.data_dir, item.source_path)
            if model.storage_scope != PROJECT_STORAGE:
                raise RuntimeError(
                    "旧版检测模型尚未迁移，请先运行 scripts/migrate-models-to-project.ps1"
                )
            model_path = resolve_detection_model_path(self.settings, model)
            if model.status != "ready" or not model_path.is_file():
                raise RuntimeError("项目 models/yolo 中的检测模型不可用")
            model_id = model.id
            width = page.width
            height = page.height
        await self.broadcaster.notify(task_id)

        try:
            prediction = await self.runtime.predict(
                model_path,
                image_path,
                confidence=float(parameters.get("confidence", 0.25)),
                image_size=int(parameters.get("image_size", 1280)),
                device=str(parameters.get("device", "auto")),
            )
            output_regions = filter_detection_output(
                prediction.regions,
                model.class_names_json,
            )
            regions = self._validated_regions(output_regions, width, height)
        except DetectionDependencyError:
            raise
        except Exception as exc:
            logger.warning("Page detection failed: %s", type(exc).__name__)
            await self._mark_item_failed(
                task_id,
                item_id,
                page_id,
                "PAGE_DETECTION_FAILED",
                "本页检测失败",
            )
            return

        async with self.database.session_factory() as session:
            item = await session.get(TaskItem, item_id)
            task = await session.get(Task, task_id)
            page = await session.get(Page, page_id)
            if item is None or task is None or page is None:
                return
            page_claim = await session.execute(
                update(Page)
                .where(
                    Page.id == page_id,
                    Page.region_revision == expected_revision,
                )
                .values(
                    detection_status="detected",
                    region_revision=expected_revision + 1,
                    reviewed_at=None,
                    last_detection_error=None,
                    last_detection_model_id=model_id,
                    ocr_status="pending",
                    updated_at=utc_now(),
                )
            )
            if page_claim.rowcount != 1:
                item.status = "failed"
                item.error_code = "DETECTION_CONFLICT"
                item.error_message = "检测期间标注已被修改，未覆盖人工结果"
                task.failed += 1
                task.updated_at = utc_now()
                await session.commit()
                await self.broadcaster.notify(task_id)
                return
            await session.execute(delete(DetectionRegion).where(DetectionRegion.page_id == page_id))
            for region in regions:
                session.add(
                    DetectionRegion(
                        page_id=page_id,
                        model_id=model_id,
                        class_id=region["class_id"],
                        class_name=region["class_name"],
                        confidence=region["confidence"],
                        x1=region["x1"],
                        y1=region["y1"],
                        x2=region["x2"],
                        y2=region["y2"],
                        source="model",
                        is_manual_edited=False,
                    )
                )
            item.status = "completed"
            item.error_code = None
            item.error_message = None
            task.completed += 1
            task.result_json = json.dumps(
                {"actual_device": prediction.actual_device}, ensure_ascii=False
            )
            task.updated_at = utc_now()
            await session.commit()
        await self.broadcaster.notify(task_id)

    @staticmethod
    def _validated_regions(
        regions: list[dict[str, object]],
        width: int | None,
        height: int | None,
    ) -> list[dict[str, int | float | str | None]]:
        if not width or not height:
            raise ValueError("页面尺寸不可用")
        validated: list[dict[str, int | float | str | None]] = []
        for region in regions:
            x1 = max(0.0, min(float(region["x1"]), float(width)))
            y1 = max(0.0, min(float(region["y1"]), float(height)))
            x2 = max(0.0, min(float(region["x2"]), float(width)))
            y2 = max(0.0, min(float(region["y2"]), float(height)))
            if x2 <= x1 or y2 <= y1:
                continue
            validated.append(
                {
                    "class_id": int(region["class_id"]),
                    "class_name": str(region["class_name"]),
                    "confidence": float(region["confidence"]),
                    "x1": x1,
                    "y1": y1,
                    "x2": x2,
                    "y2": y2,
                }
            )
        return validated

    async def _mark_item_failed(
        self,
        task_id: str,
        item_id: str,
        page_id: str,
        code: str,
        message: str,
    ) -> None:
        async with self.database.session_factory() as session:
            item = await session.get(TaskItem, item_id)
            task = await session.get(Task, task_id)
            page = await session.get(Page, page_id)
            if item is None or task is None or page is None:
                return
            item.status = "failed"
            item.error_code = code
            item.error_message = message
            page.last_detection_error = message
            task.failed += 1
            task.updated_at = utc_now()
            await session.commit()
        await self.broadcaster.notify(task_id)

    async def _finish_completed(self, task_id: str) -> None:
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                return
            task.status = "completed"
            task.stage = "completed_with_warnings" if task.failed else "completed"
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
