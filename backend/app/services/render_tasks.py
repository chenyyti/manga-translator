from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path

from PIL import Image, ImageChops
from sqlalchemy import select, update

from app.api.schemas import TaskSnapshot
from app.core.config import Settings
from app.db.models import (
    DetectionRegion,
    Font,
    Page,
    PageRenderSettings,
    Project,
    RegionRenderSettings,
    RenderSettings,
    Task,
    TaskItem,
)
from app.db.session import Database
from app.providers.inpainting.base import (
    InpaintingDependencyError,
    InpaintingError,
    InpaintingModelMissingError,
    InpaintingRuntime,
)
from app.services.images import generate_rendered_thumbnail_from_image
from app.services.render_state import refresh_project_render_status
from app.services.rendering import (
    LayoutError,
    LayoutOptions,
    LayoutOverflowError,
    MaskRegion,
    atomic_save_png,
    auto_contrast_layout_options,
    build_page_mask,
    choose_repair_mode,
    exif_oriented,
    find_system_font,
    measure_background_complexity,
    render_region_text_layer,
)
from app.services.storage import relative_to_root, resolve_within
from app.services.tasks import TaskBroadcaster, task_blocked_by_paused_batch, utc_now

logger = logging.getLogger(__name__)


def _safe_error(error: Exception, fallback: str) -> str:
    if isinstance(error, LayoutError):
        return f"{error.code}: {error}"[:500]
    code = getattr(error, "code", None)
    if code:
        return f"{code}: {fallback}"[:500]
    return fallback


def _canonical_hash(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _preserve_sfx(region: DetectionRegion) -> bool:
    return (
        region.target_text_origin == "sfx_preserve"
        or (
            region.sfx_strategy == "preserve"
            and (
                region.target_text is None
                or region.target_text == region.source_text
            )
        )
    )


def _json_dict(value: str | None) -> dict[str, object]:
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _setting_values(settings: RenderSettings) -> dict[str, object]:
    return {
        "repair_mode": settings.repair_mode,
        "device": settings.device,
        "mask_padding_ratio": settings.mask_padding_ratio,
        "mask_dilation_px": settings.mask_dilation_px,
        "opencv_radius": settings.opencv_radius,
        "lama_max_edge": settings.lama_max_edge,
        "ai_fallback": settings.ai_fallback,
        "font_id": settings.font_id,
        "font_size": settings.font_size,
        "auto_font_size": settings.auto_font_size,
        "min_font_size": settings.min_font_size,
        "max_font_size": settings.max_font_size,
        "margin_ratio": settings.margin_ratio,
        "font_color": settings.font_color,
        "stroke_color": settings.stroke_color,
        "stroke_width": settings.stroke_width,
        "orientation": settings.orientation,
        "rotation_degrees": settings.rotation_degrees,
    }


def _effective_values(
    settings: RenderSettings,
    page_overrides: dict[str, object],
    region_overrides: dict[str, object] | None = None,
) -> dict[str, object]:
    values = _setting_values(settings)
    values.update({key: value for key, value in page_overrides.items() if value is not None})
    if region_overrides:
        values.update({key: value for key, value in region_overrides.items() if value is not None})
    return values


def _layout_options(values: dict[str, object]) -> LayoutOptions:
    return LayoutOptions(
        font_size=int(values.get("font_size", 36)),
        auto_font_size=bool(values.get("auto_font_size", True)),
        min_font_size=int(values.get("min_font_size", 12)),
        max_font_size=int(values.get("max_font_size", 96)),
        margin_ratio=float(values.get("margin_ratio", 0.08)),
        font_color=str(values.get("font_color", "#000000")),
        stroke_color=str(values.get("stroke_color", "#FFFFFF")),
        stroke_width=int(values.get("stroke_width", 0)),
        orientation=str(values.get("orientation", "auto")),
        rotation_degrees=float(values.get("rotation_degrees", 0)),
    )


def _versioned_name(prefix: str, page_index: int, revision: int, digest: str, suffix: str) -> str:
    return f"{prefix}/page-{page_index:04d}-r{revision}-{digest[:16]}.{suffix}"


def _remove(path: Path | None) -> None:
    if path is not None:
        path.unlink(missing_ok=True)


def _resolve_font_path(
    font_id: str,
    *,
    data_dir: Path,
    fonts: dict[str, Font],
) -> Path:
    if font_id.startswith("system:"):
        filename = font_id.removeprefix("system:")
        if Path(filename).name != filename or not filename:
            raise LayoutError("系统字体标识无效")
        path = find_system_font(filename)
        if path is None:
            raise LayoutError("默认中文字体尚未找到")
        return path
    font = fonts.get(font_id)
    if font is None or font.source != "custom" or not font.relative_path:
        raise LayoutError("所选字体不存在")
    path = resolve_within(data_dir, font.relative_path)
    if not path.is_file():
        raise LayoutError("所选字体文件缺失")
    return path


def _select_repair_mode(
    requested: str,
    complexity,
    *,
    lama_ready: bool,
    opencv_ready: bool,
) -> tuple[str, str | None]:
    """Translate selection failures into stable task error codes."""

    try:
        return choose_repair_mode(
            requested,
            complexity,
            lama_ready=lama_ready,
            opencv_ready=opencv_ready,
        )
    except ValueError as exc:
        raise InpaintingError("图像修复模式无效") from exc
    except RuntimeError as exc:
        message = str(exc)
        if "OpenCV" in message and "LaMa" not in message:
            raise InpaintingDependencyError("OpenCV 修复依赖尚未安装") from exc
        if "Big-LaMa" in message:
            raise InpaintingModelMissingError("Big-LaMa 模型尚未准备") from exc
        raise InpaintingError("没有可用的图像修复运行时") from exc


class RenderTaskManager:
    """Persistent current-page repair and full-resolution rendering tasks."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        runtime: InpaintingRuntime,
        broadcaster: TaskBroadcaster,
    ) -> None:
        self.database = database
        self.settings = settings
        self.runtime = runtime
        self.broadcaster = broadcaster
        self._active: dict[str, asyncio.Task[None]] = {}
        self._semaphore = asyncio.Semaphore(1)

    async def start(self) -> None:
        await asyncio.to_thread(_cleanup_render_part_files, self.settings.projects_dir)
        async with self.database.session_factory() as session:
            task_ids = list(
                (
                    await session.scalars(
                        select(Task.id).where(
                            Task.task_type.in_(["page_repair", "page_render"]),
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
                pages = list(
                    (
                        await session.scalars(
                            select(Page)
                            .where(
                                Page.id.in_(
                                    select(TaskItem.page_id).where(
                                        TaskItem.task_id.in_(task_ids), TaskItem.page_id.is_not(None)
                                    )
                                )
                            )
                        )
                    ).all()
                )
                for page in pages:
                    if page.repair_status == "processing":
                        page.repair_status = "outdated" if page.inpainted_path else "pending"
                    if page.render_status == "processing":
                        page.render_status = "outdated" if page.rendered_path else "pending"
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
        task = asyncio.create_task(self._run_guarded(task_id), name=f"render-{task_id}")
        self._active[task_id] = task
        task.add_done_callback(lambda _done, key=task_id: self._active.pop(key, None))

    async def cancel(self, task_id: str) -> TaskSnapshot:
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                raise LookupError(task_id)
            if task.task_type not in {"page_repair", "page_render"}:
                raise ValueError(task_id)
            if task.status not in {"completed", "failed", "cancelled"}:
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
                            Task.task_type.in_(["page_repair", "page_render"]),
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
            task_revision=task.task_revision,
        )

    async def _run_guarded(self, task_id: str) -> None:
        async with self._semaphore:
            try:
                await self._run_task(task_id)
            except asyncio.CancelledError:
                raise
            except (InpaintingError, LayoutError) as exc:
                await self._fail_task(task_id, getattr(exc, "code", "RENDER_FAILED"), _safe_error(exc, "生成任务失败"))
            except Exception:
                logger.error("Render task failed", extra={"request_id": "-"})
                await self._fail_task(task_id, "RENDER_FAILED", "生成任务失败")

    async def _run_task(self, task_id: str) -> None:
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None or task.status not in {"pending", "running"}:
                return
            parameters = _json_dict(task.parameters_json)
            item_ids = list(
                (
                    await session.scalars(
                        select(TaskItem.id)
                        .where(TaskItem.task_id == task_id, TaskItem.status == "pending")
                        .order_by(TaskItem.sequence_index, TaskItem.id)
                    )
                ).all()
            )
            if task.cancel_requested:
                items = list(
                    (
                        await session.scalars(
                            select(TaskItem).where(TaskItem.id.in_(item_ids))
                        )
                    ).all()
                )
                for item in items:
                    if item.status in {"pending", "processing", "running"}:
                        item.status = "skipped"
                        item.error_code = "TASK_CANCELLED"
                        item.error_message = "任务已取消"
                        task.skipped += 1
                    if item.page_id:
                        page = await session.get(Page, item.page_id)
                        if page is not None:
                            if task.task_type == "page_repair":
                                page.repair_status = "outdated" if page.inpainted_path else "pending"
                            else:
                                page.render_status = "outdated" if page.rendered_path else "pending"
                            await refresh_project_render_status(session, page.project_id)
                task.status = "cancelled"
                task.stage = "cancelled"
                task.current_page_id = None
                task.finished_at = utc_now()
                task.updated_at = utc_now()
                await session.commit()
                await self.broadcaster.notify(task_id)
                return
            task.status = "running"
            task.stage = "generating_mask" if task.task_type == "page_repair" else "queued"
            task.started_at = task.started_at or utc_now()
            task.updated_at = utc_now()
            await session.commit()
        await self.broadcaster.notify(task_id)
        if not item_ids:
            await self._finish(task_id)
            return
        item_id = item_ids[0]
        if task.task_type == "page_repair":
            await self._process_repair(task_id, item_id, parameters)
        else:
            await self._process_render(task_id, item_id, parameters)
        if await self._cancel_requested(task_id):
            await self._finish_cancelled(task_id, item_id)
        else:
            await self._finish(task_id)

    async def _process_repair(self, task_id: str, item_id: str, parameters: dict[str, object]) -> None:
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            item = await session.get(TaskItem, item_id)
            page = await session.get(Page, item.page_id) if item else None
            if task is None or item is None or page is None:
                return
            project = await session.get(Project, page.project_id)
            if project is None:
                return
            expected = (
                item.expected_repair_input_revision
                if item.expected_repair_input_revision is not None
                else page.repair_input_revision
            )
            if page.repair_input_revision != expected:
                item.status = "skipped"
                item.error_code = "REPAIR_CONFLICT"
                item.error_message = "页面设置已更新，旧修复结果已跳过"
                task.skipped += 1
                await session.commit()
                return
            global_settings = await self._ensure_settings(session)
            page_settings = await session.get(PageRenderSettings, page.id)
            page_overrides = _json_dict(page_settings.overrides_json) if page_settings else {}
            regions = list(
                (
                    await session.scalars(
                        select(DetectionRegion)
                        .where(DetectionRegion.page_id == page.id)
                        .order_by(DetectionRegion.reading_order, DetectionRegion.created_at, DetectionRegion.id)
                    )
                ).all()
            )
            region_overrides = {
                value.region_id: _json_dict(value.overrides_json)
                for value in list(
                    (
                        await session.scalars(
                            select(RegionRenderSettings).where(
                                RegionRenderSettings.region_id.in_([region.id for region in regions])
                            )
                        )
                    ).all()
                )
            }
            original = resolve_within(self.settings.data_dir, page.original_path or "")
            effective_repairs = [
                _effective_values(global_settings, page_overrides, region_overrides.get(region.id))
                for region in regions
                if not _preserve_sfx(region)
            ]
            first_repair = effective_repairs[0] if effective_repairs else _setting_values(global_settings)
            requested = str(
                parameters.get("provider")
                or first_repair.get("repair_mode", global_settings.repair_mode)
            )
            device = str(parameters.get("device") or first_repair.get("device", global_settings.device))
            padding = float(first_repair.get("mask_padding_ratio", global_settings.mask_padding_ratio))
            dilation = int(first_repair.get("mask_dilation_px", global_settings.mask_dilation_px))
            settings_hash = _canonical_hash(
                {
                    "revision": global_settings.revision,
                    "page_revision": page_settings.revision if page_settings else 0,
                    "provider": requested,
                    "device": device,
                    "mask_algorithm": "text-pixels-v1",
                    "padding": padding,
                    "dilation": dilation,
                    "regions": [
                        (region.id, region.x1, region.y1, region.x2, region.y2, _preserve_sfx(region))
                        for region in regions
                    ],
                }
            )
            mask_result = await asyncio.to_thread(
                self._build_mask,
                original,
                regions,
                padding,
                dilation,
                region_overrides,
                global_settings,
                page_overrides,
            )
            root = resolve_within(self.settings.data_dir, project.workspace_path)
            mask_path = root / _versioned_name("masks", page.page_index, expected, settings_hash, "png")
            inpaint_root = root / "cache" / "inpainted"
            inpaint_path = inpaint_root / Path(_versioned_name("", page.page_index, expected, settings_hash, "png")).name
            atomic_save_png(mask_result[0], mask_path)
            item.status = "processing"
            task.stage = "inpainting_fast"
            task.current_page_id = page.id
            task.updated_at = utc_now()
            page.repair_status = "processing"
            page.repair_error = None
            await session.commit()
        await self.broadcaster.notify(task_id)

        warning: str | None = None
        actual_provider = requested
        actual_device = "cpu"
        try:
            image_for_measure = exif_oriented(original)
            if regions:
                complexities = [
                    measure_background_complexity(image_for_measure, mask_result[0], bounds)
                    for bounds in mask_result[1].values()
                ]
                complexity = max(
                    complexities,
                    key=lambda value: (value.standard_deviation, value.edge_density),
                )
            else:
                from app.services.rendering import BackgroundComplexity

                complexity = BackgroundComplexity(0, 0)
            infos = await self.runtime.providers()
            info_by_id = {info.id: info for info in infos}
            actual_provider, warning = _select_repair_mode(
                requested,
                complexity,
                lama_ready=bool(info_by_id.get("lama") and info_by_id["lama"].model_ready),
                opencv_ready=bool(info_by_id.get("opencv") and info_by_id["opencv"].installed),
            )
            stage = {
                "fast": "inpainting_fast",
                "opencv": "inpainting_opencv",
                "lama": "inpainting_lama",
            }[actual_provider]
            if actual_provider == "lama":
                await self._set_stage(task_id, "loading_lama")
                await self._set_stage(task_id, "inpainting_lama")
            else:
                await self._set_stage(task_id, stage)
            try:
                result = await self.runtime.inpaint(
                    actual_provider,
                    original,
                    mask_path,
                    inpaint_path,
                    device=device,
                    max_edge=int(first_repair.get("lama_max_edge", global_settings.lama_max_edge)),
                    opencv_radius=float(first_repair.get("opencv_radius", global_settings.opencv_radius)),
                )
                actual_device = result.actual_device
                warning = warning or result.warning
            except Exception as exc:
                if actual_provider != "lama" or not bool(
                    first_repair.get("ai_fallback", global_settings.ai_fallback)
                ):
                    if isinstance(exc, InpaintingError):
                        raise
                    raise InpaintingError("图像修复运行时失败") from exc
                fallback = info_by_id.get("opencv")
                if fallback is None or not fallback.installed:
                    raise InpaintingError("LaMa 失败且 OpenCV 不可用") from exc
                actual_provider = "opencv"
                warning = "Big-LaMa 推理失败，已回退 OpenCV"
                await self._set_stage(task_id, "inpainting_opencv")
                try:
                    result = await self.runtime.inpaint(
                        "opencv",
                        original,
                        mask_path,
                        inpaint_path,
                        device="cpu",
                        max_edge=int(first_repair.get("lama_max_edge", global_settings.lama_max_edge)),
                        opencv_radius=float(first_repair.get("opencv_radius", global_settings.opencv_radius)),
                    )
                except InpaintingError:
                    raise
                except Exception as fallback_exc:
                    raise InpaintingError("OpenCV 修复运行时失败") from fallback_exc
                actual_device = result.actual_device
        except Exception:
            _remove(mask_path)
            _remove(inpaint_path)
            raise
        if await self._cancel_requested(task_id):
            _remove(mask_path)
            _remove(inpaint_path)
            await self._mark_cancelled_page(task_id, item_id)
            return
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            item = await session.get(TaskItem, item_id)
            page = await session.get(Page, item.page_id) if item else None
            if task is None or item is None or page is None:
                _remove(mask_path)
                _remove(inpaint_path)
                return
            if page.repair_input_revision != expected:
                _remove(mask_path)
                _remove(inpaint_path)
                item.status = "skipped"
                item.error_code = "REPAIR_CONFLICT"
                item.error_message = "页面设置已更新，旧修复结果已跳过"
                task.skipped += 1
                page.repair_status = "outdated" if page.inpainted_path else "pending"
            else:
                page.repair_status = "completed_with_warnings" if warning else "completed"
                page.repair_revision += 1
                page.mask_path = relative_to_root(self.settings.data_dir, mask_path)
                page.inpainted_path = relative_to_root(self.settings.data_dir, inpaint_path)
                page.repair_methods_json = json.dumps(
                    {"requested": requested, "actual": actual_provider, "device": actual_device},
                    ensure_ascii=False,
                )
                page.repair_error = warning
                if page.rendered_path:
                    page.render_status = "outdated"
                item.status = "completed"
                task.completed += 1
                task.result_json = json.dumps(
                    {"provider": actual_provider, "device": actual_device}, ensure_ascii=False
                )
            task.updated_at = utc_now()
            await refresh_project_render_status(session, page.project_id)
            await session.commit()
        await self.broadcaster.notify(task_id)

    @staticmethod
    def _build_mask(
        original: Path | Image.Image,
        regions: list[DetectionRegion],
        padding: float,
        dilation: int,
        region_overrides: dict[str, dict[str, object]] | None = None,
        global_settings: RenderSettings | None = None,
        page_overrides: dict[str, object] | None = None,
    ) -> tuple[Image.Image, dict[str, tuple[int, int, int, int]]]:
        image = exif_oriented(original) if isinstance(original, Path) else original
        if image.mode != "RGB":
            image = image.convert("RGB")
        if not region_overrides or global_settings is None:
            result = build_page_mask(
                image.size,
                [
                    MaskRegion(
                        region.id,
                        (region.x1, region.y1, region.x2, region.y2),
                        _preserve_sfx(region),
                    )
                    for region in regions
                ],
                padding,
                dilation,
                source=image,
            )
            return result.mask, result.regions

        # Region-level repair overrides can use different padding/dilation.
        # Build one mask per region and union them so each box receives its
        # most specific settings while overlapping masks remain deterministic.
        combined = Image.new("L", image.size, 0)
        bounds: dict[str, tuple[int, int, int, int]] = {}
        page_values = page_overrides or {}
        for region in regions:
            values = _effective_values(
                global_settings,
                page_values,
                region_overrides.get(region.id),
            )
            result = build_page_mask(
                image.size,
                [
                    MaskRegion(
                        region.id,
                        (region.x1, region.y1, region.x2, region.y2),
                        _preserve_sfx(region),
                    )
                ],
                float(values.get("mask_padding_ratio", padding)),
                int(values.get("mask_dilation_px", dilation)),
                source=image,
            )
            combined = ImageChops.lighter(combined, result.mask)
            bounds.update(result.regions)
        # Apply preservation after union, including per-region override builds.
        for region in regions:
            if _preserve_sfx(region):
                protected = build_page_mask(image.size, [MaskRegion(region.id, (region.x1, region.y1, region.x2, region.y2))], 0, 0).mask
                combined = ImageChops.subtract(combined, protected)
        return combined, bounds

    async def _process_render(self, task_id: str, item_id: str, parameters: dict[str, object]) -> None:
        render_started = time.perf_counter()
        repair_ms = 0.0
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            item = await session.get(TaskItem, item_id)
            page = await session.get(Page, item.page_id) if item else None
            if task is None or item is None or page is None:
                return
            project = await session.get(Project, page.project_id)
            if project is None:
                return
            expected_render = (
                item.expected_render_input_revision
                if item.expected_render_input_revision is not None
                else page.render_input_revision
            )
            if page.render_input_revision != expected_render:
                item.status = "skipped"
                item.error_code = "RENDER_CONFLICT"
                item.error_message = "页面内容已更新，旧成品已跳过"
                task.skipped += 1
                await session.commit()
                return
            regions = list(
                (
                    await session.scalars(
                        select(DetectionRegion)
                        .where(DetectionRegion.page_id == page.id)
                        .order_by(DetectionRegion.reading_order, DetectionRegion.created_at, DetectionRegion.id)
                    )
                ).all()
            )
            invalid = [
                region.id
                for region in regions
                if not _preserve_sfx(region)
                and (
                    region.translation_status not in {"completed", "manual"}
                    or not (region.target_text or "").strip()
                )
            ]
            if invalid:
                raise LayoutError("仍有区域没有可用译文")
            global_settings = await self._ensure_settings(session)
            page_settings = await session.get(PageRenderSettings, page.id)
            page_overrides = _json_dict(page_settings.overrides_json) if page_settings else {}
            region_settings = {
                value.region_id: _json_dict(value.overrides_json)
                for value in list(
                    (
                        await session.scalars(
                            select(RegionRenderSettings).where(
                                RegionRenderSettings.region_id.in_([region.id for region in regions])
                            )
                        )
                    ).all()
                )
            }
            fonts = {
                value.id: value
                for value in list((await session.scalars(select(Font))).all())
            }
            requires_repair = any(not _preserve_sfx(region) for region in regions)
            repair_needed = requires_repair and (
                page.repair_status not in {"completed", "completed_with_warnings"}
                or not page.inpainted_path
                or bool(parameters.get("force_repair"))
            )
            if requires_repair and page.inpainted_path:
                try:
                    repair_needed = repair_needed or not resolve_within(
                        self.settings.data_dir, page.inpainted_path
                    ).is_file()
                except Exception:
                    repair_needed = True
            if requires_repair and page.repair_status in {"outdated", "failed"}:
                repair_needed = True
            if not requires_repair:
                repair_needed = False
            item.status = "processing"
            task.current_page_id = page.id
            task.stage = "generating_mask" if repair_needed else "typesetting"
            page.render_status = "processing"
            page.render_error = None
            await session.commit()
        await self.broadcaster.notify(task_id)

        if repair_needed:
            repair_started = time.perf_counter()
            inline_result = await self._repair_inline(task_id, page.id, parameters)
            repair_ms = (time.perf_counter() - repair_started) * 1000
            if inline_result == "cancelled":
                return
            if inline_result == "conflict":
                await self._mark_render_conflict(task_id, item_id)
                return

        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            item = await session.get(TaskItem, item_id)
            page = await session.get(Page, item.page_id) if item else None
            if task is None or item is None or page is None:
                return
            project = await session.get(Project, page.project_id)
            if project is None:
                return
            # The repair stage may run inline. Capture the exact repair
            # revision used for typesetting and compare it again before the
            # generated PNG is committed.
            expected_repair = page.repair_revision
            expected_render = item.expected_render_input_revision
            if expected_render is None:
                expected_render = page.render_input_revision
            if page.render_input_revision != expected_render:
                item.status = "skipped"
                item.error_code = "RENDER_CONFLICT"
                item.error_message = "页面内容已更新，旧成品已跳过"
                task.skipped += 1
                await session.commit()
                return
            regions = list(
                (
                    await session.scalars(
                        select(DetectionRegion)
                        .where(DetectionRegion.page_id == page.id)
                        .order_by(DetectionRegion.reading_order, DetectionRegion.created_at, DetectionRegion.id)
                    )
                ).all()
            )
            global_settings = await self._ensure_settings(session)
            page_settings = await session.get(PageRenderSettings, page.id)
            page_overrides = _json_dict(page_settings.overrides_json) if page_settings else {}
            region_settings = {
                value.region_id: _json_dict(value.overrides_json)
                for value in list(
                    (
                        await session.scalars(
                            select(RegionRenderSettings).where(
                                RegionRenderSettings.region_id.in_([region.id for region in regions])
                            )
                        )
                    ).all()
                )
            }
            fonts = {value.id: value for value in list((await session.scalars(select(Font))).all())}
            if requires_repair and page.inpainted_path:
                base = resolve_within(self.settings.data_dir, page.inpainted_path)
            else:
                base = resolve_within(self.settings.data_dir, page.original_path or "")
            decode_started = time.perf_counter()
            image = exif_oriented(base)
            original_path = resolve_within(self.settings.data_dir, page.original_path or "")
            original_image = image if base == original_path else exif_oriented(original_path)
            decode_ms = (time.perf_counter() - decode_started) * 1000
            canvas = image.convert("RGBA")
            successful_mask = Image.new("L", image.size, 0)
            preserved_mask = Image.new("L", image.size, 0)
            used_settings: list[dict[str, object]] = []
            skipped_regions: list[dict[str, object]] = []
            mask_ms = 0.0
            layout_ms = 0.0
            composite_ms = 0.0
            for region in regions:
                if _preserve_sfx(region):
                    mask_started = time.perf_counter()
                    preserved_mask = ImageChops.lighter(preserved_mask, build_page_mask(
                        image.size, [MaskRegion(region.id, (region.x1, region.y1, region.x2, region.y2))], 0, 0,
                    ).mask)
                    mask_ms += (time.perf_counter() - mask_started) * 1000
                    continue
                values = _effective_values(
                    global_settings,
                    page_overrides,
                    region_settings.get(region.id),
                )
                # Include both the typesetting rectangle and any ink cleanup
                # just outside it. Failed regions protect this entire footprint.
                mask_started = time.perf_counter()
                region_mask, _ = self._build_mask(
                    original_image, [region], float(values["mask_padding_ratio"]),
                    int(values["mask_dilation_px"]),
                )
                region_mask = ImageChops.lighter(region_mask, build_page_mask(
                    image.size, [MaskRegion(region.id, (region.x1, region.y1, region.x2, region.y2))], 0, 0,
                ).mask)
                mask_ms += (time.perf_counter() - mask_started) * 1000
                font_path = _resolve_font_path(str(values["font_id"]), data_dir=self.settings.data_dir, fonts=fonts)
                layout_started = time.perf_counter()
                layout_options = auto_contrast_layout_options(
                    image,
                    (region.x1, region.y1, region.x2, region.y2),
                    _layout_options(values),
                )
                try:
                    layer, origin, chosen_size = render_region_text_layer(
                        image.size,
                        str(region.target_text or ""),
                        (region.x1, region.y1, region.x2, region.y2),
                        font_path,
                        layout_options,
                    )
                except LayoutOverflowError as exc:
                    layout_ms += (time.perf_counter() - layout_started) * 1000
                    preserved_mask = ImageChops.lighter(preserved_mask, region_mask)
                    skipped_regions.append({"region_id": region.id, "code": exc.code, "reason": str(exc)})
                    continue
                layout_ms += (time.perf_counter() - layout_started) * 1000
                if layer is not None:
                    composite_started = time.perf_counter()
                    canvas.alpha_composite(layer, origin)
                    composite_ms += (time.perf_counter() - composite_started) * 1000
                successful_mask = ImageChops.lighter(successful_mask, region_mask)
                used_settings.append({
                    "region_id": region.id,
                    "font_size": chosen_size,
                    "font_color": layout_options.font_color,
                    "below_preferred_minimum": chosen_size < int(values["min_font_size"]),
                })
            # Original pixels win for skipped/retained boxes even when another
            # region overlaps them, regardless of region processing order.
            composite_started = time.perf_counter()
            successful_mask = ImageChops.subtract(successful_mask, preserved_mask)
            image = Image.composite(canvas.convert("RGB"), original_image, successful_mask)
            composite_ms += (time.perf_counter() - composite_started) * 1000
            render_hash = _canonical_hash(
                {
                    "layout_algorithm": "auto-contrast-skip-overflow-v2",
                    "input_revision": expected_render,
                    "repair_revision": expected_repair,
                    "regions": [
                        (
                            region.id,
                            region.geometry_revision,
                            region.translation_revision,
                            region.target_text,
                            region.translation_mode,
                            region_settings.get(region.id, {}),
                        )
                        for region in regions
                    ],
                    "page_settings": page_overrides,
                    "global_revision": global_settings.revision,
                }
            )
            output = resolve_within(self.settings.data_dir, project.workspace_path) / _versioned_name(
                "rendered", page.page_index, expected_render, render_hash, "png"
            )
            await self._set_stage(task_id, "saving_render")
            save_started = time.perf_counter()
            atomic_save_png(image, output)
            save_ms = (time.perf_counter() - save_started) * 1000
            thumbnail_output = output.with_suffix(".webp")
            thumbnail_dimensions: tuple[int, int] | None = None
            thumbnail_started = time.perf_counter()
            try:
                thumbnail_dimensions = await asyncio.to_thread(
                    generate_rendered_thumbnail_from_image,
                    image,
                    thumbnail_output,
                    thumbnail_size=self.settings.thumbnail_size,
                )
            except Exception as exc:  # pragma: no cover - depends on optional image codecs
                # A rendered PNG is still useful when a WebP encoder is
                # unavailable.  The bookshelf will lazily retry the
                # thumbnail generation when it needs the cover.
                logger.warning("Rendered thumbnail generation failed: %s", type(exc).__name__)
                thumbnail_output = None
            thumbnail_ms = (time.perf_counter() - thumbnail_started) * 1000
        if await self._cancel_requested(task_id):
            _remove(output)
            if thumbnail_output:
                _remove(thumbnail_output)
            await self._mark_cancelled_page(task_id, item_id)
            return
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            item = await session.get(TaskItem, item_id)
            page = await session.get(Page, item.page_id) if item else None
            if task is None or item is None or page is None:
                _remove(output)
                if thumbnail_output:
                    _remove(thumbnail_output)
                return
            expected_render = item.expected_render_input_revision
            if expected_render is None:
                expected_render = page.render_input_revision
            if page.render_input_revision != expected_render or page.repair_revision != expected_repair:
                _remove(output)
                if thumbnail_output:
                    _remove(thumbnail_output)
                item.status = "skipped"
                item.error_code = "RENDER_CONFLICT"
                item.error_message = "页面内容已更新，旧成品已跳过"
                task.skipped += 1
                page.render_status = "outdated" if page.rendered_path else "pending"
                page.render_error = item.error_message
            else:
                page.render_status = "completed"
                page.rendered_path = relative_to_root(self.settings.data_dir, output)
                page.rendered_thumbnail_path = (
                    relative_to_root(self.settings.data_dir, thumbnail_output)
                    if thumbnail_output and thumbnail_output.is_file()
                    else None
                )
                if thumbnail_dimensions:
                    page.rendered_thumbnail_width, page.rendered_thumbnail_height = (
                        thumbnail_dimensions
                    )
                else:
                    page.rendered_thumbnail_width = None
                    page.rendered_thumbnail_height = None
                page.rendered_sha256 = await asyncio.to_thread(_sha256_file, output)
                page.rendered_width = image.width
                page.rendered_height = image.height
                page.render_error = None
                page.render_revision += 1
                item.status = "completed"
                task.completed += 1
                task.stage = "completed"
                task.result_json = json.dumps(
                    {
                        "regions": used_settings,
                        "skipped_regions": skipped_regions,
                        "skipped_region_count": len(skipped_regions),
                        "timings_ms": {
                            "repair": round(repair_ms, 2),
                            "decode": round(decode_ms, 2),
                            "mask": round(mask_ms, 2),
                            "layout": round(layout_ms, 2),
                            "composite": round(composite_ms, 2),
                            "save": round(save_ms, 2),
                            "thumbnail": round(thumbnail_ms, 2),
                            "total": round((time.perf_counter() - render_started) * 1000, 2),
                        },
                    },
                    ensure_ascii=False,
                )
            task.updated_at = utc_now()
            await refresh_project_render_status(session, page.project_id)
            await session.commit()
        await self.broadcaster.notify(task_id)

    async def _repair_inline(
        self, task_id: str, page_id: str, parameters: dict[str, object]
    ) -> str:
        """Reuse the repair implementation while keeping one render task item."""

        async with self.database.session_factory() as session:
            page = await session.get(Page, page_id)
            if page is None:
                raise LayoutError("页面不存在")
            global_settings = await self._ensure_settings(session)
            page_settings = await session.get(PageRenderSettings, page.id)
            overrides = _json_dict(page_settings.overrides_json) if page_settings else {}
            regions = list(
                (
                    await session.scalars(
                        select(DetectionRegion).where(DetectionRegion.page_id == page.id)
                    )
                ).all()
            )
            region_overrides = {
                value.region_id: _json_dict(value.overrides_json)
                for value in list(
                    (
                        await session.scalars(
                            select(RegionRenderSettings).where(
                                RegionRenderSettings.region_id.in_([region.id for region in regions])
                            )
                        )
                    ).all()
                )
            }
            project = await session.get(Project, page.project_id)
            if project is None:
                raise LayoutError("项目不存在")
            original = resolve_within(self.settings.data_dir, page.original_path or "")
            expected = page.repair_input_revision
            effective_repairs = [
                _effective_values(global_settings, overrides, region_overrides.get(region.id))
                for region in regions
                if not _preserve_sfx(region)
            ]
            first_repair = effective_repairs[0] if effective_repairs else _setting_values(global_settings)
            requested = str(
                parameters.get("provider")
                or first_repair.get("repair_mode", global_settings.repair_mode)
            )
            padding = float(first_repair.get("mask_padding_ratio", global_settings.mask_padding_ratio))
            dilation = int(first_repair.get("mask_dilation_px", global_settings.mask_dilation_px))
            mask, bounds = self._build_mask(
                original,
                regions,
                padding,
                dilation,
                region_overrides,
                global_settings,
                overrides,
            )
            digest = _canonical_hash({"revision": expected, "provider": requested, "bounds": bounds, "mask_algorithm": "text-pixels-v1"})
            root = resolve_within(self.settings.data_dir, project.workspace_path)
            mask_path = root / _versioned_name("masks", page.page_index, expected, digest, "png")
            inpaint_path = root / "cache" / "inpainted" / f"page-{page.page_index:04d}-r{expected}-{digest[:16]}.png"
            atomic_save_png(mask, mask_path)
            page.repair_status = "processing"
            page.repair_error = None
            await session.commit()
        await self._set_stage(task_id, "generating_mask")
        infos = {info.id: info for info in await self.runtime.providers()}
        image = exif_oriented(original)
        if bounds:
            complexities = [measure_background_complexity(image, mask, bound) for bound in bounds.values()]
            complexity = max(complexities, key=lambda value: (value.standard_deviation, value.edge_density))
        else:
            from app.services.rendering import BackgroundComplexity

            complexity = BackgroundComplexity(0, 0)
        actual, warning = _select_repair_mode(
            requested,
            complexity,
            lama_ready=bool(infos.get("lama") and infos["lama"].model_ready),
            opencv_ready=bool(infos.get("opencv") and infos["opencv"].installed),
        )
        if actual == "lama":
            await self._set_stage(task_id, "loading_lama")
            await self._set_stage(task_id, "inpainting_lama")
        else:
            await self._set_stage(
                task_id,
                {"fast": "inpainting_fast", "opencv": "inpainting_opencv"}[actual],
            )
        try:
            result = await self.runtime.inpaint(
                actual,
                original,
                mask_path,
                inpaint_path,
                device=str(parameters.get("device") or first_repair.get("device", global_settings.device)),
                max_edge=int(first_repair.get("lama_max_edge", global_settings.lama_max_edge)),
                opencv_radius=float(first_repair.get("opencv_radius", global_settings.opencv_radius)),
            )
        except Exception as exc:
            if actual != "lama" or not bool(first_repair.get("ai_fallback", global_settings.ai_fallback)):
                _remove(mask_path)
                _remove(inpaint_path)
                if isinstance(exc, InpaintingError):
                    raise
                raise InpaintingError("图像修复运行时失败") from exc
            fallback = infos.get("opencv")
            if fallback is None or not fallback.installed:
                _remove(mask_path)
                _remove(inpaint_path)
                raise InpaintingError("LaMa 失败且 OpenCV 不可用") from exc
            actual = "opencv"
            warning = "Big-LaMa 推理失败，已回退 OpenCV"
            await self._set_stage(task_id, "inpainting_opencv")
            try:
                result = await self.runtime.inpaint(
                    "opencv",
                    original,
                    mask_path,
                    inpaint_path,
                    device="cpu",
                    max_edge=int(first_repair.get("lama_max_edge", global_settings.lama_max_edge)),
                    opencv_radius=float(first_repair.get("opencv_radius", global_settings.opencv_radius)),
                )
            except InpaintingError:
                raise
            except Exception as fallback_exc:
                raise InpaintingError("OpenCV 修复运行时失败") from fallback_exc
        if await self._cancel_requested(task_id):
            _remove(mask_path)
            _remove(inpaint_path)
            async with self.database.session_factory() as session:
                page = await session.get(Page, page_id)
                if page is not None:
                    page.repair_status = "outdated" if page.inpainted_path else "pending"
                    page.repair_error = "任务已取消"
                    await session.commit()
            return "cancelled"
        async with self.database.session_factory() as session:
            page = await session.get(Page, page_id)
            if page is None:
                _remove(mask_path)
                _remove(inpaint_path)
                raise LayoutError("页面不存在")
            if page.repair_input_revision != expected:
                _remove(mask_path)
                _remove(inpaint_path)
                page.repair_status = "outdated" if page.inpainted_path else "pending"
                page.repair_error = "页面设置已更新，旧修复结果已跳过"
                await session.commit()
                return "conflict"
            page.repair_status = "completed_with_warnings" if warning else "completed"
            page.repair_revision += 1
            page.mask_path = relative_to_root(self.settings.data_dir, mask_path)
            page.inpainted_path = relative_to_root(self.settings.data_dir, inpaint_path)
            page.repair_methods_json = json.dumps(
                {"requested": requested, "actual": actual, "device": result.actual_device}, ensure_ascii=False
            )
            page.repair_error = warning
            await session.commit()
        await self.broadcaster.notify(task_id)
        return "completed"

    async def _mark_render_conflict(self, task_id: str, item_id: str) -> None:
        """Discard an inline repair when page inputs changed during generation."""

        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            item = await session.get(TaskItem, item_id)
            page = await session.get(Page, item.page_id) if item else None
            if task is None or item is None:
                return
            if item.status in {"pending", "processing", "running"}:
                item.status = "skipped"
                item.error_code = "RENDER_CONFLICT"
                item.error_message = "页面内容已更新，旧修复结果已跳过"
                task.skipped += 1
            if page is not None:
                page.render_status = "outdated" if page.rendered_path else "pending"
                page.render_error = item.error_message
                await refresh_project_render_status(session, page.project_id)
            task.updated_at = utc_now()
            await session.commit()
        await self.broadcaster.notify(task_id)

    async def _ensure_settings(self, session) -> RenderSettings:
        settings = await session.get(RenderSettings, 1)
        if settings is None:
            settings = RenderSettings(id=1)
            session.add(settings)
            await session.flush()
        return settings

    async def _set_stage(self, task_id: str, stage: str) -> None:
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                return
            task.stage = stage
            task.updated_at = utc_now()
            await session.commit()
        await self.broadcaster.notify(task_id)

    async def _cancel_requested(self, task_id: str) -> bool:
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            return bool(task and task.cancel_requested)

    async def _mark_cancelled_page(self, task_id: str, item_id: str) -> None:
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            item = await session.get(TaskItem, item_id)
            page = await session.get(Page, item.page_id) if item else None
            if task is None:
                return
            if item and item.status in {"pending", "processing"}:
                item.status = "skipped"
                item.error_code = "TASK_CANCELLED"
                item.error_message = "任务已取消"
                task.skipped += 1
            if page:
                if task.task_type == "page_repair":
                    page.repair_status = "outdated" if page.inpainted_path else "pending"
                elif task.task_type == "page_render":
                    page.render_status = "outdated" if page.rendered_path else "pending"
            await session.commit()
        await self.broadcaster.notify(task_id)

    async def _finish(self, task_id: str) -> None:
        async with self.database.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                return
            task.status = "completed"
            if task.failed and task.completed == 0 and task.skipped == 0:
                task.status = "failed"
                task.stage = "failed"
            else:
                task.stage = "completed_with_warnings" if task.failed or task.skipped else "completed"
            task.current_page_id = None
            task.finished_at = utc_now()
            task.updated_at = utc_now()
            await session.commit()
        await self.broadcaster.notify(task_id)

    async def _finish_cancelled(self, task_id: str, item_id: str) -> None:
        await self._mark_cancelled_page(task_id, item_id)
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
                task.failed += 1
                if item.page_id:
                    page = await session.get(Page, item.page_id)
                    if page:
                        if task.task_type == "page_repair":
                            page.repair_status = "outdated" if page.inpainted_path else "failed"
                            page.repair_error = message[:500]
                        else:
                            page.render_status = "outdated" if page.rendered_path else "failed"
                            page.render_error = message[:500]
                            if page.repair_status == "processing":
                                page.repair_status = "outdated" if page.inpainted_path else "failed"
                                page.repair_error = message[:500]
                        await refresh_project_render_status(session, page.project_id)
            task.status = "failed"
            task.stage = "failed"
            task.error_code = code
            task.error_message = message[:500]
            task.finished_at = utc_now()
            task.updated_at = utc_now()
            await session.commit()
        await self.broadcaster.notify(task_id)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _cleanup_render_part_files(root: Path) -> None:
    """Remove orphaned atomic-render temporary files after an unclean exit."""

    if not root.is_dir():
        return
    for part in root.rglob("*.part"):
        try:
            if part.is_file():
                part.unlink(missing_ok=True)
        except OSError:
            # A locked temporary file can be retried by the next startup; it
            # must never prevent the application from opening the bookshelf.
            continue


__all__ = ["RenderTaskManager"]
