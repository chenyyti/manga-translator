from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import JSONResponse
from PIL import ImageFont
from sqlalchemy import select

from app.api.detection_routes import _region_read
from app.api.routes import database, ok, settings
from app.api.schemas import (
    FontRead,
    RenderOverridesWrite,
    RenderSettingsWrite,
    RenderTaskCreate,
    RepairTaskCreate,
)
from app.core.errors import AppError, ConflictError, NotFoundError
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
from app.services.render_state import mark_page_render_outdated
from app.services.render_tasks import _setting_values
from app.services.rendering import find_system_font
from app.services.storage import relative_to_root, resolve_within, stream_to_atomic_file
from app.services.tasks import utc_now

router = APIRouter(prefix="/api")

_REPAIR_KEYS = {
    "repair_mode",
    "device",
    "mask_padding_ratio",
    "mask_dilation_px",
    "opencv_radius",
    "lama_max_edge",
    "ai_fallback",
}
_FONT_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SYSTEM_FONTS = (
    ("system:msyh.ttc", "微软雅黑", "msyh.ttc"),
    ("system:simhei.ttf", "黑体", "simhei.ttf"),
)


def manager(request: Request):
    return request.app.state.render_task_manager


def _json_dict(value: str | None) -> dict[str, object]:
    try:
        decoded = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


async def _settings(session) -> RenderSettings:
    value = await session.get(RenderSettings, 1)
    if value is None:
        value = RenderSettings(id=1)
        session.add(value)
        await session.flush()
    return value


def _settings_read(value: RenderSettings) -> dict[str, object]:
    result = _setting_values(value)
    result["revision"] = value.revision
    return result


def _font_read(value: Font, *, deletable: bool = True) -> dict[str, object]:
    return FontRead(
        id=value.id,
        display_name=value.display_name,
        family_name=value.family_name,
        source=value.source,
        filename=value.filename,
        size=value.size,
        status=value.status,
        deletable=deletable,
    ).model_dump(mode="json")


def _system_font_read(font_id: str, name: str, filename: str) -> dict[str, object]:
    path = find_system_font(filename)
    available = path is not None
    return {
        "id": font_id,
        "display_name": name,
        "family_name": name,
        "source": "system",
        "filename": filename,
        "size": path.stat().st_size if path else 0,
        "status": "ready" if available else "unavailable",
        "deletable": False,
    }


async def _validate_font_reference(session, request: Request, font_id: str) -> None:
    if font_id.startswith("system:"):
        filename = font_id.removeprefix("system:")
        if Path(filename).name != filename or find_system_font(filename) is None:
            raise AppError("FONT_NOT_READY", "所选系统字体不可用", status_code=422)
        return
    value = await session.get(Font, font_id)
    if value is None or value.status != "ready":
        raise AppError("FONT_NOT_READY", "所选自定义字体不可用", status_code=422)
    if not value.relative_path:
        raise AppError("FONT_NOT_READY", "所选自定义字体文件缺失", status_code=422)
    try:
        path = resolve_within(settings(request).data_dir, value.relative_path)
    except AppError:
        raise AppError("UNSAFE_PATH", "字体文件路径校验失败", status_code=422) from None
    if not path.is_file():
        raise AppError("FONT_NOT_READY", "所选自定义字体文件缺失", status_code=422)


def _effective_settings(global_value: RenderSettings, page_value: PageRenderSettings | None) -> dict[str, object]:
    values = _setting_values(global_value)
    if page_value:
        values.update({key: item for key, item in _json_dict(page_value.overrides_json).items() if item is not None})
    return values


def _validate_typography_bounds(values: dict[str, object]) -> None:
    minimum = int(values.get("min_font_size", 12))
    maximum = int(values.get("max_font_size", 96))
    if maximum < minimum:
        raise AppError("INVALID_RENDER_SETTINGS", "最大字号不能小于最小字号", status_code=422)


async def _page_or_404(request: Request, page_id: str) -> tuple[Page, Project]:
    async with database(request).session_factory() as session:
        page = await session.get(Page, page_id)
        if page is None:
            raise NotFoundError("页面不存在")
        project = await session.get(Project, page.project_id)
        if project is None:
            raise NotFoundError("项目不存在")
        return page, project


async def _active_page_task(request: Request, page_id: str) -> bool:
    async with database(request).session_factory() as session:
        return bool(
            await session.scalar(
                select(TaskItem.id)
                .join(Task, Task.id == TaskItem.task_id)
                .where(
                    TaskItem.page_id == page_id,
                    Task.task_type.in_(["page_repair", "page_render", "batch_pipeline"]),
                    Task.status.in_(["pending", "running", "pausing", "paused"]),
                )
                .limit(1)
            )
        )


def _validate_page_gate(page: Page, project: Project) -> None:
    if project.status not in {"ready", "ready_with_warnings"} or page.import_status != "ready":
        raise ConflictError("PAGE_NOT_READY", "页面导入完成后才能生成")
    if page.detection_status not in {"detected", "reviewed"}:
        raise ConflictError("PAGE_NOT_DETECTED", "页面完成检测后才能生成")


def _translation_ready(regions: list[DetectionRegion]) -> bool:
    return all(
        region.target_text_origin == "sfx_preserve"
        or (
            region.sfx_strategy == "preserve"
            and (
                region.target_text is None
                or region.target_text == region.source_text
            )
        )
        or (
            region.translation_status in {"completed", "manual"}
            and bool((region.target_text or "").strip())
        )
        for region in regions
    )


@router.get("/providers/inpainting")
async def list_inpainting_providers(request: Request) -> JSONResponse:
    items = [item.__dict__ for item in await request.app.state.inpainting_runtime.providers()]
    return ok({"items": items})


@router.get("/settings/render")
async def get_render_settings(request: Request) -> JSONResponse:
    async with database(request).session_factory() as session:
        value = await _settings(session)
        await session.commit()
        return ok(_settings_read(value))


@router.put("/settings/render")
async def update_render_settings(payload: RenderSettingsWrite, request: Request) -> JSONResponse:
    async with database(request).session_factory() as session:
        active_batch = await session.scalar(
            select(Task.id).where(
                Task.task_type == "batch_pipeline",
                Task.status.in_(["pending", "running", "pausing", "paused"]),
            ).limit(1)
        )
        if active_batch:
            raise ConflictError("RENDER_SETTINGS_IN_USE", "批处理任务活动期间不能修改修复与排版设置")
        value = await _settings(session)
        if value.revision != payload.expected_revision:
            raise ConflictError("SETTINGS_REVISION_CONFLICT", "修复与排版设置已更新，请重新加载")
        previous = _setting_values(value)
        # PUT accepts a complete settings object from the UI, but also keeps
        # omitted fields untouched for API clients sending a focused update.
        incoming = payload.model_dump(
            exclude={"expected_revision"}, exclude_unset=True, mode="json"
        )
        if "font_id" in incoming:
            await _validate_font_reference(session, request, str(incoming["font_id"]))
        effective = _setting_values(value)
        effective.update(incoming)
        _validate_typography_bounds(effective)
        for key, item in incoming.items():
            setattr(value, key, item)
        changed = {key for key in incoming if previous.get(key) != incoming.get(key)}
        if changed:
            value.revision += 1
            value.updated_at = utc_now()
            pages = list((await session.scalars(select(Page).where(Page.import_status == "ready"))).all())
            for page in pages:
                await mark_page_render_outdated(
                    session,
                    page,
                    repair=bool(changed & _REPAIR_KEYS),
                    reason="全局修复或排版设置已更新",
                )
        await session.commit()
        await session.refresh(value)
    return ok(_settings_read(value))


@router.get("/fonts")
async def list_fonts(request: Request) -> JSONResponse:
    async with database(request).session_factory() as session:
        values = list((await session.scalars(select(Font).order_by(Font.display_name, Font.id))).all())
    items = [_system_font_read(*item) for item in _SYSTEM_FONTS]
    items.extend(_font_read(value) for value in values)
    return ok({"items": items})


@router.post("/fonts")
async def upload_font(
    request: Request,
    file: Annotated[UploadFile, File()],
) -> JSONResponse:
    filename = Path(file.filename or "font.ttf").name
    suffix = Path(filename).suffix.casefold()
    if suffix not in {".ttf", ".otf", ".ttc"}:
        raise AppError("UNSUPPORTED_FONT", "仅支持 TTF、OTF 或 TTC 字体", status_code=415)
    safe_name = _FONT_RE.sub("_", filename).strip("._") or f"font{suffix}"
    config = settings(request)
    temporary = config.fonts_dir / f".{uuid4().hex}{suffix}.part"
    try:
        size, digest = await __import__("asyncio").to_thread(
            stream_to_atomic_file,
            file.file,
            temporary,
            max_bytes=50 * 1024 * 1024,
        )
    finally:
        await file.close()
    try:
        try:
            loaded = ImageFont.truetype(str(temporary), size=24)
            family = loaded.getname()[0] if hasattr(loaded, "getname") else None
        except Exception as exc:
            raise AppError("INVALID_FONT", "字体文件无法加载", status_code=422) from exc
        font_id = f"custom:{digest}"
        # De-duplicate by content before moving the upload into the managed
        # font store.  Otherwise uploading the same bytes with the same
        # filename would overwrite (and then delete) the already referenced
        # file when the existing row is returned below.
        async with database(request).session_factory() as session:
            existing = await session.get(Font, font_id)
            if existing is not None:
                return ok(_font_read(existing), status_code=200)
        destination = config.fonts_dir / f"{digest[:16]}-{safe_name}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temporary, destination)
        async with database(request).session_factory() as session:
            existing = await session.get(Font, font_id)
            if existing is not None:
                existing_path = (
                    resolve_within(config.data_dir, existing.relative_path)
                    if existing.relative_path
                    else None
                )
                if existing_path is None or existing_path != destination.resolve():
                    destination.unlink(missing_ok=True)
                return ok(_font_read(existing), status_code=200)
            value = Font(
                id=font_id,
                display_name=family or Path(safe_name).stem,
                family_name=family,
                source="custom",
                relative_path=relative_to_root(config.data_dir, destination),
                filename=safe_name,
                sha256=digest,
                size=size,
                status="ready",
            )
            session.add(value)
            await session.commit()
            await session.refresh(value)
        return ok(_font_read(value), status_code=201)
    finally:
        temporary.unlink(missing_ok=True)


async def _font_is_referenced(session, font_id: str) -> bool:
    global_value = await session.get(RenderSettings, 1)
    if global_value and global_value.font_id == font_id:
        return True
    pages = list((await session.scalars(select(PageRenderSettings))).all())
    regions = list((await session.scalars(select(RegionRenderSettings))).all())
    return any(_json_dict(item.overrides_json).get("font_id") == font_id for item in [*pages, *regions])


@router.delete("/fonts/{font_id}")
async def delete_font(font_id: str, request: Request) -> JSONResponse:
    if font_id.startswith("system:"):
        raise ConflictError("FONT_NOT_DELETABLE", "系统字体不能删除")
    config = settings(request)
    async with database(request).session_factory() as session:
        value = await session.get(Font, font_id)
        if value is None:
            raise NotFoundError("字体不存在")
        if await _font_is_referenced(session, font_id):
            raise ConflictError("FONT_IN_USE", "字体正在被设置引用，不能删除")
        active = await session.scalar(
            select(Task.id).where(
                Task.task_type.in_(["page_repair", "page_render", "batch_pipeline"]),
                Task.status.in_(["pending", "running", "pausing", "paused"]),
            ).limit(1)
        )
        if active:
            raise ConflictError("FONT_IN_USE", "生成任务运行期间不能删除字体")
        relative_path = value.relative_path
        target_path = None
        if relative_path:
            target_path = resolve_within(config.data_dir, relative_path)
            if not target_path.is_relative_to(config.fonts_dir.resolve()):
                raise AppError("UNSAFE_PATH", "字体文件路径校验失败", status_code=422)
        await session.delete(value)
        await session.commit()
    if target_path:
        target_path.unlink(missing_ok=True)
    return ok({"deleted": True})


async def _get_page_settings(session, page_id: str) -> PageRenderSettings | None:
    return await session.get(PageRenderSettings, page_id)


@router.get("/pages/{page_id}/render-settings")
async def get_page_render_settings(page_id: str, request: Request) -> JSONResponse:
    async with database(request).session_factory() as session:
        page = await session.get(Page, page_id)
        if page is None:
            raise NotFoundError("页面不存在")
        value = await _get_page_settings(session, page_id)
        global_value = await _settings(session)
        return ok({
            "page_id": page_id,
            "revision": value.revision if value else 0,
            "overrides": _json_dict(value.overrides_json) if value else {},
            "effective": _effective_settings(global_value, value),
        })


@router.put("/pages/{page_id}/render-settings")
async def update_page_render_settings(
    page_id: str, payload: RenderOverridesWrite, request: Request
) -> JSONResponse:
    async with database(request).session_factory() as session:
        page = await session.get(Page, page_id)
        if page is None:
            raise NotFoundError("页面不存在")
        active_batch = await session.scalar(
            select(TaskItem.id)
            .join(Task, Task.id == TaskItem.task_id)
            .where(
                TaskItem.page_id == page_id,
                Task.task_type == "batch_pipeline",
                Task.status.in_(["pending", "running", "pausing", "paused"]),
            ).limit(1)
        )
        if active_batch:
            raise ConflictError("RENDER_SETTINGS_IN_USE", "该页面正在批处理，不能修改排版设置")
        value = await _get_page_settings(session, page_id)
        if value is None:
            value = PageRenderSettings(page_id=page_id)
            session.add(value)
            await session.flush()
        if value.revision != payload.expected_revision:
            raise ConflictError("SETTINGS_REVISION_CONFLICT", "页面设置已更新，请重新加载")
        previous = _json_dict(value.overrides_json)
        incoming = payload.overrides.model_dump(exclude_none=True, mode="json")
        if "font_id" in incoming:
            await _validate_font_reference(session, request, str(incoming["font_id"]))
        value.overrides_json = json.dumps(incoming, ensure_ascii=False, sort_keys=True)
        global_value = await _settings(session)
        effective = _effective_settings(global_value, value)
        effective.update(incoming)
        _validate_typography_bounds(effective)
        if previous != incoming:
            value.revision += 1
            changed_keys = {
                key
                for key in set(previous) | set(incoming)
                if previous.get(key) != incoming.get(key)
            }
            repair = bool(changed_keys & _REPAIR_KEYS)
            await mark_page_render_outdated(session, page, repair=repair, reason="页面排版设置已更新")
        await session.commit()
        await session.refresh(value)
        global_value = await _settings(session)
    return ok({"page_id": page_id, "revision": value.revision, "overrides": incoming, "effective": _effective_settings(global_value, value)})


@router.get("/regions/{region_id}/render-settings")
async def get_region_render_settings(region_id: str, request: Request) -> JSONResponse:
    async with database(request).session_factory() as session:
        region = await session.get(DetectionRegion, region_id)
        if region is None:
            raise NotFoundError("文本区域不存在")
        value = await session.get(RegionRenderSettings, region_id)
        page_value = await session.get(PageRenderSettings, region.page_id)
        global_value = await _settings(session)
        effective = _effective_settings(global_value, page_value)
        effective.update(_json_dict(value.overrides_json) if value else {})
        return ok(
            {
                "region_id": region_id,
                "revision": value.revision if value else 0,
                "overrides": _json_dict(value.overrides_json) if value else {},
                "effective": effective,
            }
        )


@router.put("/regions/{region_id}/render-settings")
async def update_region_render_settings(
    region_id: str, payload: RenderOverridesWrite, request: Request
) -> JSONResponse:
    async with database(request).session_factory() as session:
        region = await session.get(DetectionRegion, region_id)
        if region is None:
            raise NotFoundError("文本区域不存在")
        active_batch = await session.scalar(
            select(TaskItem.id)
            .join(Task, Task.id == TaskItem.task_id)
            .where(
                TaskItem.page_id == region.page_id,
                Task.task_type == "batch_pipeline",
                Task.status.in_(["pending", "running", "pausing", "paused"]),
            ).limit(1)
        )
        if active_batch:
            raise ConflictError("RENDER_SETTINGS_IN_USE", "该页面正在批处理，不能修改区域排版设置")
        value = await session.get(RegionRenderSettings, region_id)
        if value is None:
            value = RegionRenderSettings(region_id=region_id)
            session.add(value)
            await session.flush()
        if value.revision != payload.expected_revision:
            raise ConflictError("SETTINGS_REVISION_CONFLICT", "区域设置已更新，请重新加载")
        incoming = payload.overrides.model_dump(exclude_none=True, mode="json")
        if "font_id" in incoming:
            await _validate_font_reference(session, request, str(incoming["font_id"]))
        previous = _json_dict(value.overrides_json)
        global_value = await _settings(session)
        page_value = await session.get(PageRenderSettings, region.page_id)
        effective = _effective_settings(global_value, page_value)
        effective.update(incoming)
        _validate_typography_bounds(effective)
        value.overrides_json = json.dumps(incoming, ensure_ascii=False, sort_keys=True)
        if previous != incoming:
            value.revision += 1
            page = await session.get(Page, region.page_id)
            if page is not None:
                changed_keys = {
                    key
                    for key in set(previous) | set(incoming)
                    if previous.get(key) != incoming.get(key)
                }
                await mark_page_render_outdated(
                    session,
                    page,
                    repair=bool(changed_keys & _REPAIR_KEYS),
                    reason="区域修复或排版设置已更新",
                )
        await session.commit()
        await session.refresh(value)
    async with database(request).session_factory() as session:
        global_value = await _settings(session)
        page_value = await session.get(PageRenderSettings, region.page_id)
        effective = _effective_settings(global_value, page_value)
        effective.update(incoming)
    return ok(
        {
            "region_id": region_id,
            "revision": value.revision,
            "overrides": incoming,
            "effective": effective,
        }
    )


@router.post("/pages/{page_id}/repair")
async def create_repair_task(
    page_id: str, request: Request, payload: RepairTaskCreate | None = None
) -> JSONResponse:
    payload = payload or RepairTaskCreate()
    async with database(request).session_factory() as session:
        page = await session.get(Page, page_id)
        if page is None:
            raise NotFoundError("页面不存在")
        project = await session.get(Project, page.project_id)
        if project is None:
            raise NotFoundError("项目不存在")
        _validate_page_gate(page, project)
        if payload.expected_repair_input_revision is not None and payload.expected_repair_input_revision != page.repair_input_revision:
            raise ConflictError("REPAIR_REVISION_CONFLICT", "页面修复输入已更新，请重新加载")
        active = await session.scalar(
            select(TaskItem.id)
            .join(Task)
            .where(
                TaskItem.page_id == page_id,
                Task.task_type.in_(["page_repair", "page_render", "batch_pipeline"]),
                Task.status.in_(["pending", "running", "pausing", "paused"]),
            )
            .limit(1)
        )
        if active:
            raise ConflictError("REPAIR_ALREADY_RUNNING", "当前页已有修复任务")
        task = Task(project_id=project.id, task_type="page_repair", status="pending", stage="queued", total=1, parameters_json=json.dumps(payload.model_dump(exclude_none=True, mode="json"), ensure_ascii=False))
        session.add(task)
        await session.flush()
        session.add(
            TaskItem(
                task_id=task.id,
                page_id=page.id,
                source_path=page.original_path or "",
                page_index=page.page_index,
                sequence_index=0,
                item_type="page_repair",
                status="pending",
                expected_repair_input_revision=page.repair_input_revision,
            )
        )
        page.repair_status = "pending"
        await session.commit()
        await session.refresh(task)
    manager(request).enqueue(task.id)
    return ok({"task_id": task.id, "project_id": project.id, "page_id": page_id}, status_code=202)


@router.post("/pages/{page_id}/render")
async def create_render_task(
    page_id: str, request: Request, payload: RenderTaskCreate | None = None
) -> JSONResponse:
    payload = payload or RenderTaskCreate()
    async with database(request).session_factory() as session:
        page = await session.get(Page, page_id)
        if page is None:
            raise NotFoundError("页面不存在")
        project = await session.get(Project, page.project_id)
        if project is None:
            raise NotFoundError("项目不存在")
        _validate_page_gate(page, project)
        regions = list((await session.scalars(select(DetectionRegion).where(DetectionRegion.page_id == page_id))).all())
        if not _translation_ready(regions):
            raise ConflictError("TRANSLATION_NOT_READY", "所有非保留区域必须先有有效译文")
        if payload.expected_render_input_revision is not None and payload.expected_render_input_revision != page.render_input_revision:
            raise ConflictError("RENDER_REVISION_CONFLICT", "页面成品输入已更新，请重新加载")
        active = await session.scalar(
            select(TaskItem.id)
            .join(Task)
            .where(
                TaskItem.page_id == page_id,
                Task.task_type.in_(["page_repair", "page_render", "batch_pipeline"]),
                Task.status.in_(["pending", "running", "pausing", "paused"]),
            )
            .limit(1)
        )
        if active:
            raise ConflictError("RENDER_ALREADY_RUNNING", "当前页已有生成任务")
        task = Task(project_id=project.id, task_type="page_render", status="pending", stage="queued", total=1, parameters_json=json.dumps(payload.model_dump(exclude_none=True, mode="json"), ensure_ascii=False))
        session.add(task)
        await session.flush()
        session.add(
            TaskItem(
                task_id=task.id,
                page_id=page.id,
                source_path=page.original_path or "",
                page_index=page.page_index,
                sequence_index=0,
                item_type="page_render",
                status="pending",
                expected_repair_revision=page.repair_revision,
                expected_render_input_revision=page.render_input_revision,
            )
        )
        page.render_status = "pending"
        await session.commit()
        await session.refresh(task)
    manager(request).enqueue(task.id)
    return ok({"task_id": task.id, "project_id": project.id, "page_id": page_id}, status_code=202)


@router.get("/pages/{page_id}/render-state")
async def get_render_state(page_id: str, request: Request) -> JSONResponse:
    async with database(request).session_factory() as session:
        page = await session.get(Page, page_id)
        if page is None:
            raise NotFoundError("页面不存在")
        value = await _settings(session)
        page_settings = await session.get(PageRenderSettings, page_id)
        return ok({
            "page_id": page.id,
            "repair_status": page.repair_status,
            "repair_input_revision": page.repair_input_revision,
            "repair_revision": page.repair_revision,
            "mask_url": f"/api/pages/{page.id}/asset/mask" if page.mask_path else None,
            "inpainted_url": f"/api/pages/{page.id}/asset/inpainted" if page.inpainted_path else None,
            "render_status": page.render_status,
            "render_input_revision": page.render_input_revision,
            "render_revision": page.render_revision,
            "rendered_url": f"/api/pages/{page.id}/asset/rendered" if page.rendered_path else None,
            "rendered_sha256": page.rendered_sha256,
            "rendered_width": page.rendered_width,
            "rendered_height": page.rendered_height,
            "repair_methods": _json_dict(page.repair_methods_json),
            "repair_error": page.repair_error,
            "render_error": page.render_error,
            "settings": _effective_settings(value, page_settings),
        })


@router.get("/pages/{page_id}/render-regions")
async def get_render_regions(page_id: str, request: Request) -> JSONResponse:
    async with database(request).session_factory() as session:
        page = await session.get(Page, page_id)
        if page is None:
            raise NotFoundError("页面不存在")
        regions = list((await session.scalars(select(DetectionRegion).where(DetectionRegion.page_id == page_id).order_by(DetectionRegion.reading_order, DetectionRegion.id))).all())
    return ok({"page_id": page_id, "regions": [_region_read(region) for region in regions]})


__all__ = ["router"]
