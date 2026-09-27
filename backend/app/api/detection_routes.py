from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import shutil
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy import delete, func, select, update

from app.api.routes import database, ok, settings
from app.api.schemas import (
    DetectionRegionsWrite,
    DetectionSettingsWrite,
    DetectionTaskCreate,
)
from app.core.errors import AppError, ConflictError, NotFoundError
from app.db.models import (
    DetectionModel,
    DetectionRegion,
    DetectionSettings,
    Page,
    Project,
    Task,
    TaskItem,
)
from app.providers.detection.base import DetectionDependencyError
from app.services.detection_models import (
    DATA_STORAGE,
    PROJECT_STORAGE,
    DetectionModelCatalog,
    legacy_model_path,
    model_is_readonly,
    project_model_relative_path,
    resolve_detection_model_path,
)
from app.services.detection_tasks import DetectionTaskManager
from app.services.ocr_tasks import aggregate_ocr_status
from app.services.render_state import mark_page_render_outdated
from app.services.storage import stream_to_atomic_file
from app.services.tasks import utc_now
from app.services.translation_tasks import aggregate_translation_status

router = APIRouter(prefix="/api")
logger = logging.getLogger(__name__)


def detection_manager(request: Request) -> DetectionTaskManager:
    return request.app.state.detection_task_manager


def detection_catalog(request: Request) -> DetectionModelCatalog:
    return request.app.state.detection_model_catalog


def _model_read(model: DetectionModel) -> dict[str, object]:
    return {
        "id": model.id,
        "name": model.name,
        "filename": model.filename,
        "sha256": model.sha256,
        "size": model.size,
        "status": model.status,
        "class_names": json.loads(model.class_names_json),
        "framework_version": model.framework_version,
        "task_name": model.task_name,
        "source": model.storage_scope,
        "storage_scope": model.storage_scope,
        "relative_path": model.relative_path,
        "readonly": model_is_readonly(model),
        "error_message": model.error_message,
        "created_at": model.created_at,
        "updated_at": model.updated_at,
    }


def _region_read(region: DetectionRegion) -> dict[str, object]:
    return {
        "id": region.id,
        "model_id": region.model_id,
        "class_id": region.class_id,
        "class_name": region.class_name,
        "confidence": region.confidence,
        "x1": region.x1,
        "y1": region.y1,
        "x2": region.x2,
        "y2": region.y2,
        "source": region.source,
        "is_manual_edited": region.is_manual_edited,
        "source_text": region.source_text,
        "source_text_origin": region.source_text_origin,
        "ocr_status": region.ocr_status,
        "ocr_provider": region.ocr_provider,
        "ocr_confidence": region.ocr_confidence,
        "ocr_error": region.ocr_error,
        "geometry_revision": region.geometry_revision,
        "ocr_revision": region.ocr_revision,
        "ocr_updated_at": region.ocr_updated_at,
        "target_text": region.target_text,
        "target_text_origin": region.target_text_origin,
        "translation_status": region.translation_status,
        "llm_profile_id": region.llm_profile_id,
        "llm_provider": region.llm_provider,
        "llm_model": region.llm_model,
        "translation_error": region.translation_error,
        "reading_order": region.reading_order,
        "sfx_strategy": region.sfx_strategy,
        "translation_revision": region.translation_revision,
        "translated_from_ocr_revision": region.translated_from_ocr_revision,
        "translation_updated_at": region.translation_updated_at,
        "reading_order_origin": region.reading_order_origin,
        "translation_mode": region.translation_mode,
    }


async def _get_detection_settings(request: Request) -> DetectionSettings:
    db = database(request)
    async with db.session_factory() as session:
        value = await session.get(DetectionSettings, 1)
        if value is None:
            value = DetectionSettings(id=1)
            session.add(value)
            await session.commit()
            await session.refresh(value)
        return value


@router.get("/detection/runtime")
async def get_detection_runtime(request: Request) -> JSONResponse:
    ultralytics_installed = importlib.util.find_spec("ultralytics") is not None
    torch_installed = importlib.util.find_spec("torch") is not None
    runtime_info = None
    if ultralytics_installed and torch_installed:
        runtime_info = await detection_manager(request).runtime.status()
    return ok(
        {
            "ultralytics_installed": ultralytics_installed,
            "torch_installed": torch_installed,
            "ultralytics_version": runtime_info.ultralytics_version if runtime_info else None,
            "torch_version": runtime_info.torch_version if runtime_info else None,
            "cuda_available": runtime_info.cuda_available if runtime_info else False,
            "gpu_name": runtime_info.gpu_name if runtime_info else None,
            "total_vram_mb": runtime_info.total_vram_mb if runtime_info else None,
            "architecture": "isolated_process",
            "gpu_fallback": True,
        }
    )


@router.get("/detection-models")
async def list_detection_models(request: Request) -> JSONResponse:
    await detection_catalog(request).sync(wait=False)
    db = database(request)
    async with db.session_factory() as session:
        statement = select(DetectionModel).order_by(DetectionModel.created_at)
        models = list((await session.scalars(statement)).all())
    return ok(
        {
            "items": [_model_read(model) for model in models],
            "preparation": detection_catalog(request).snapshot(),
        }
    )


@router.post("/detection-models")
async def upload_detection_model(
    request: Request,
    file: Annotated[UploadFile, File()],
    name: Annotated[str, Form(min_length=1, max_length=120)],
) -> JSONResponse:
    if Path(file.filename or "").suffix.casefold() != ".pt":
        raise AppError("UNSUPPORTED_MODEL", "仅支持 Ultralytics .pt 模型", status_code=415)
    config = settings(request)
    model_id = str(uuid4())
    destination = config.yolo_models_dir / f"{model_id}.pt"
    staged = config.yolo_models_dir / ".uploads" / f"{model_id}.pt"
    try:
        size, sha256 = await asyncio.to_thread(
            stream_to_atomic_file,
            file.file,
            staged,
            max_bytes=config.max_model_bytes,
        )
    finally:
        await file.close()
    try:
        stamp = staged.stat()
        info = await detection_manager(request).runtime.inspect(staged)
        if staged.stat().st_mtime_ns != stamp.st_mtime_ns or staged.stat().st_size != size:
            raise AppError(
                "MODEL_CHANGED", "上传模型校验期间文件发生变化，请重新上传", status_code=409
            )
        if info.task_name != "detect":
            raise AppError("INVALID_MODEL_TASK", "模型不是目标检测模型", status_code=422)
        if not info.class_names:
            raise AppError("INVALID_MODEL", "模型没有可用类别", status_code=422)
        model = DetectionModel(
            id=model_id,
            name=" ".join(name.split()),
            filename=Path(file.filename or "model.pt").name,
            relative_path=project_model_relative_path(config, destination),
            sha256=sha256,
            size=size,
            status="ready",
            class_names_json=json.dumps(info.class_names, ensure_ascii=False),
            framework_version=info.framework_version,
            task_name=info.task_name,
            storage_scope=PROJECT_STORAGE,
            origin="uploaded",
            error_message=None,
            file_mtime_ns=str(stamp.st_mtime_ns),
            validation_environment=detection_catalog(request).environment,
            validation_version=1,
        )
        await detection_catalog(request).publish_upload(model, staged, destination)
    except DetectionDependencyError as exc:
        await asyncio.to_thread(staged.unlink, True)
        raise AppError(
            "DETECTION_DEPENDENCY_MISSING",
            str(exc),
            status_code=503,
        ) from exc
    except Exception:
        await asyncio.to_thread(staged.unlink, True)
        raise
    return ok(_model_read(model), status_code=201)


@router.delete("/detection-models/{model_id}")
async def delete_detection_model(model_id: str, request: Request) -> JSONResponse:
    await detection_catalog(request).sync()
    db = database(request)
    config = settings(request)
    staged_cleanup: Path | None = None
    cleanup_path: Path | None = None
    async with db.session_factory() as session:
        model = await session.get(DetectionModel, model_id)
        if model is None:
            raise NotFoundError("检测模型不存在")
        model_path = resolve_detection_model_path(config, model)
        if model.storage_scope == PROJECT_STORAGE:
            if not model_path.is_relative_to(config.yolo_models_dir.resolve()):
                raise AppError("UNSAFE_PATH", "项目 YOLO 模型路径校验失败", status_code=422)
            cleanup_path = model_path
        elif model.storage_scope == DATA_STORAGE:
            cleanup_path = legacy_model_path(config, model).parent
        else:
            raise AppError("UNSAFE_PATH", "检测模型存储范围无效", status_code=422)
        active = list(
            (
                await session.scalars(
                    select(Task).where(
                        Task.task_type.in_(["page_detection", "batch_pipeline"]),
                        Task.status.in_(["pending", "running", "pausing", "paused"]),
                    )
                )
            ).all()
        )
        if any(task.task_type == "batch_pipeline" for task in active):
            raise ConflictError("MODEL_IN_USE", "批处理任务活动期间不能删除检测模型")
        is_in_use = any(
            json.loads(task.parameters_json or "{}").get("model_id") == model_id for task in active
        )
        if is_in_use:
            raise ConflictError("MODEL_IN_USE", "模型正在执行检测任务，暂时不能删除")
        configured = await session.get(DetectionSettings, 1)
        if configured and configured.default_model_id == model_id:
            replacement = await session.scalar(
                select(DetectionModel.id)
                .where(
                    DetectionModel.id != model_id,
                    DetectionModel.storage_scope == PROJECT_STORAGE,
                    DetectionModel.status == "ready",
                )
                .order_by(DetectionModel.created_at, DetectionModel.id)
                .limit(1)
            )
            configured.default_model_id = replacement

        if cleanup_path.exists():
            trash_root = cleanup_path.parent / ".deleted"
            await asyncio.to_thread(trash_root.mkdir, parents=True, exist_ok=True)
            staged_cleanup = trash_root / f"{uuid4().hex}-{cleanup_path.name}"
            await asyncio.to_thread(cleanup_path.replace, staged_cleanup)

        try:
            await session.delete(model)
            await session.commit()
        except Exception:
            await session.rollback()
            if staged_cleanup is not None and staged_cleanup.exists():
                try:
                    await asyncio.to_thread(staged_cleanup.replace, cleanup_path)
                except OSError:
                    logger.exception("Failed to restore YOLO model after database rollback")
            raise

    if staged_cleanup is not None:
        try:
            if staged_cleanup.is_dir():
                await asyncio.to_thread(shutil.rmtree, staged_cleanup, True)
            else:
                await asyncio.to_thread(staged_cleanup.unlink, True)
        except OSError:
            # The file is already outside every scanned model directory.  A
            # later manual cleanup is sufficient and must not turn a committed
            # deletion into an API failure.
            logger.warning("Failed to remove staged YOLO model %s", staged_cleanup, exc_info=True)
    detection_catalog(request).invalidate()
    return ok({"deleted": True})


@router.get("/settings/detection")
async def get_detection_settings(request: Request) -> JSONResponse:
    await detection_catalog(request).sync(wait=False)
    value = await _get_detection_settings(request)
    return ok(
        {
            "default_model_id": value.default_model_id,
            "device": value.device,
            "confidence": value.confidence,
            "image_size": value.image_size,
        }
    )


@router.put("/settings/detection")
async def update_detection_settings(
    payload: DetectionSettingsWrite,
    request: Request,
) -> JSONResponse:
    await detection_catalog(request).sync()
    db = database(request)
    async with db.session_factory() as session:
        active_batch = await session.scalar(
            select(Task.id)
            .where(
                Task.task_type == "batch_pipeline",
                Task.status.in_(["pending", "running", "pausing", "paused"]),
            )
            .limit(1)
        )
        if active_batch:
            raise ConflictError("BATCH_SETTINGS_IN_USE", "批处理任务活动期间不能修改检测设置")
        selected_model = (
            await session.get(DetectionModel, payload.default_model_id)
            if payload.default_model_id
            else None
        )
        if payload.default_model_id and selected_model is None:
            raise NotFoundError("检测模型不存在")
        if selected_model is not None and selected_model.storage_scope != PROJECT_STORAGE:
            raise ConflictError(
                "YOLO_MODEL_MIGRATION_REQUIRED",
                "旧版检测模型尚未迁移，请先运行 scripts/migrate-models-to-project.ps1",
            )
        if selected_model is not None:
            selected_path = resolve_detection_model_path(settings(request), selected_model)
            if selected_model.status != "ready" or not selected_path.is_file():
                raise ConflictError(
                    "YOLO_MODEL_UNAVAILABLE",
                    selected_model.error_message or "所选检测模型尚未就绪或文件不存在",
                )
        value = await session.get(DetectionSettings, 1)
        if value is None:
            value = DetectionSettings(id=1)
            session.add(value)
        value.default_model_id = payload.default_model_id
        value.device = payload.device
        value.confidence = payload.confidence
        value.image_size = payload.image_size
        value.updated_at = utc_now()
        await session.commit()
        await session.refresh(value)
    return ok(
        {
            "default_model_id": value.default_model_id,
            "device": value.device,
            "confidence": value.confidence,
            "image_size": value.image_size,
        }
    )


@router.post("/projects/{project_id}/detection-tasks")
async def create_detection_task(
    project_id: str,
    payload: DetectionTaskCreate,
    request: Request,
) -> JSONResponse:
    if payload.end_page < payload.start_page:
        raise AppError("INVALID_PAGE_RANGE", "结束页不能小于起始页", status_code=422)
    await detection_catalog(request).sync(wait=False)
    db = database(request)
    async with db.session_factory() as session:
        project = await session.get(Project, project_id)
        if project is None:
            raise NotFoundError("项目不存在")
        if project.status not in {"ready", "ready_with_warnings"}:
            raise ConflictError("PROJECT_NOT_READY", "项目导入完成后才能检测")
        active_batch = await session.scalar(
            select(Task.id)
            .where(
                Task.project_id == project_id,
                Task.task_type == "batch_pipeline",
                Task.status.in_(["pending", "running", "pausing", "paused"]),
            )
            .limit(1)
        )
        if active_batch:
            raise ConflictError("BATCH_SETTINGS_IN_USE", "该项目批处理期间不能启动独立检测")
        model = await session.get(DetectionModel, payload.model_id)
        if model is None:
            project_model = await session.scalar(
                select(DetectionModel.id)
                .where(DetectionModel.storage_scope == PROJECT_STORAGE)
                .limit(1)
            )
            if project_model is None:
                raise ConflictError(
                    "YOLO_MODEL_REQUIRED",
                    "请将 YOLO .pt 模型放入项目 models/yolo/目录",
                )
            raise NotFoundError("检测模型不存在")
        if model.storage_scope != PROJECT_STORAGE:
            raise ConflictError(
                "YOLO_MODEL_MIGRATION_REQUIRED",
                "旧版检测模型尚未迁移，请先运行 scripts/migrate-models-to-project.ps1",
            )
        model_path = resolve_detection_model_path(settings(request), model)
        if model.status == "preparing" or (
            model_path.is_file()
            and model.status == "ready"
            and (
                model.size != model_path.stat().st_size
                or model.file_mtime_ns != str(model_path.stat().st_mtime_ns)
            )
        ):
            raise ConflictError("YOLO_MODEL_PREPARING", "检测模型准备中，请稍后重试")
        if model.status != "ready" or not model_path.is_file():
            raise ConflictError(
                "YOLO_MODEL_UNAVAILABLE",
                model.error_message or "请将有效的 YOLO .pt 模型放入项目 models/yolo 目录",
            )
        if payload.end_page > project.total_pages:
            raise AppError("INVALID_PAGE_RANGE", "页码范围超出项目", status_code=422)
        active_count = int(
            await session.scalar(
                select(func.count(Task.id)).where(
                    Task.project_id == project_id,
                    Task.task_type == "page_detection",
                    Task.status.in_(["pending", "running"]),
                )
            )
            or 0
        )
        if active_count:
            raise ConflictError("DETECTION_ALREADY_RUNNING", "该项目已有检测任务")
        pages = list(
            (
                await session.scalars(
                    select(Page)
                    .where(
                        Page.project_id == project_id,
                        Page.page_index.between(payload.start_page, payload.end_page),
                        Page.import_status == "ready",
                    )
                    .order_by(Page.page_index)
                )
            ).all()
        )
        selected_count = payload.end_page - payload.start_page + 1
        pages = [page for page in pages if page.original_path]
        eligible = (
            pages
            if payload.overwrite
            else [page for page in pages if page.detection_status == "undetected"]
        )
        if not eligible:
            raise ConflictError("NO_ELIGIBLE_PAGES", "所选范围没有需要检测的页面")
        task = Task(
            project_id=project_id,
            task_type="page_detection",
            status="pending",
            stage="queued",
            total=len(eligible),
            skipped=selected_count - len(eligible),
            parameters_json=json.dumps(payload.model_dump(), ensure_ascii=False),
        )
        session.add(task)
        await session.flush()
        for page in eligible:
            if not page.original_path:
                continue
            session.add(
                TaskItem(
                    task_id=task.id,
                    page_id=page.id,
                    source_path=page.original_path,
                    page_index=page.page_index,
                    sequence_index=page.page_index,
                    status="pending",
                    expected_revision=page.region_revision,
                )
            )
        await session.commit()
        await session.refresh(task)
    detection_manager(request).enqueue(task.id)
    return ok({"task_id": task.id, "project_id": project_id}, status_code=202)


@router.get("/pages/{page_id}/regions")
async def get_page_regions(page_id: str, request: Request) -> JSONResponse:
    db = database(request)
    async with db.session_factory() as session:
        page = await session.get(Page, page_id)
        if page is None:
            raise NotFoundError("页面不存在")
        regions = list(
            (
                await session.scalars(
                    select(DetectionRegion)
                    .where(DetectionRegion.page_id == page_id)
                    .order_by(DetectionRegion.created_at, DetectionRegion.id)
                )
            ).all()
        )
    return ok(
        {
            "page_id": page_id,
            "revision": page.region_revision,
            "detection_status": page.detection_status,
            "ocr_status": page.ocr_status,
            "translation_status": page.translation_status,
            "reviewed_at": page.reviewed_at,
            "regions": [_region_read(region) for region in regions],
        }
    )


@router.put("/pages/{page_id}/regions")
async def update_page_regions(
    page_id: str,
    payload: DetectionRegionsWrite,
    request: Request,
) -> JSONResponse:
    db = database(request)
    async with db.session_factory() as session:
        page = await session.get(Page, page_id)
        if page is None:
            raise NotFoundError("页面不存在")
        if page.import_status != "ready" or not page.width or not page.height:
            raise ConflictError("PAGE_NOT_READY", "页面尚不可编辑")
        created: list[DetectionRegion] = []
        existing = {
            region.id: region
            for region in list(
                (
                    await session.scalars(
                        select(DetectionRegion).where(DetectionRegion.page_id == page_id)
                    )
                ).all()
            )
        }
        seen_ids: set[str] = set()
        geometry_or_set_changed = False
        for item in payload.regions:
            if item.id in seen_ids:
                raise AppError("DUPLICATE_REGION", "标注 ID 重复", status_code=422)
            seen_ids.add(item.id)
            if item.x2 <= item.x1 or item.y2 <= item.y1:
                raise AppError("INVALID_REGION", "标注框尺寸无效", status_code=422)
            if item.x2 > page.width or item.y2 > page.height:
                raise AppError("INVALID_REGION", "标注框超出图片范围", status_code=422)
            previous = existing.get(item.id)
            class_changed = (
                previous is not None
                and previous.class_name.casefold() != item.class_name.casefold()
            )
            geometry_changed = previous is not None and (
                previous.x1,
                previous.y1,
                previous.x2,
                previous.y2,
            ) != (item.x1, item.y1, item.x2, item.y2)
            geometry_or_set_changed = (
                geometry_or_set_changed or geometry_changed or previous is None
            )
            region = DetectionRegion(
                id=item.id,
                page_id=page_id,
                model_id=(
                    previous.model_id
                    if previous is not None
                    else page.last_detection_model_id
                    if item.source == "model"
                    else None
                ),
                class_id=item.class_id,
                class_name=item.class_name,
                confidence=item.confidence,
                x1=item.x1,
                y1=item.y1,
                x2=item.x2,
                y2=item.y2,
                source=item.source,
                is_manual_edited=item.is_manual_edited or item.source == "manual",
                source_text=previous.source_text if previous else None,
                source_text_origin=previous.source_text_origin if previous else None,
                ocr_status=(
                    "outdated"
                    if geometry_changed and previous and previous.source_text is not None
                    else "pending"
                    if geometry_changed
                    else previous.ocr_status
                    if previous
                    else "pending"
                ),
                ocr_provider=previous.ocr_provider if previous else None,
                ocr_confidence=previous.ocr_confidence if previous else None,
                ocr_error=None if geometry_changed else previous.ocr_error if previous else None,
                geometry_revision=(
                    previous.geometry_revision + 1
                    if geometry_changed and previous
                    else previous.geometry_revision
                    if previous
                    else 0
                ),
                ocr_revision=previous.ocr_revision if previous else 0,
                ocr_updated_at=previous.ocr_updated_at if previous else None,
                target_text=previous.target_text if previous else None,
                target_text_origin=(
                    previous.target_text_origin
                    if previous and not class_changed
                    else "manual"
                    if previous and previous.target_text_origin == "manual"
                    else "provider"
                    if previous and previous.target_text is not None and class_changed
                    else None
                ),
                translation_status=(
                    "outdated"
                    if geometry_changed and previous and previous.target_text is not None
                    else "pending"
                    if geometry_changed
                    else "outdated"
                    if (
                        class_changed
                        and previous
                        and previous.target_text is not None
                        and previous.target_text_origin != "manual"
                    )
                    else previous.translation_status
                    if previous
                    else "pending"
                ),
                llm_profile_id=previous.llm_profile_id if previous else None,
                llm_provider=previous.llm_provider if previous else None,
                llm_model=previous.llm_model if previous else None,
                translation_error=None
                if geometry_changed
                else previous.translation_error
                if previous
                else None,
                reading_order=previous.reading_order if previous else None,
                sfx_strategy=previous.sfx_strategy if previous and not class_changed else None,
                translation_revision=(
                    previous.translation_revision + 1
                    if geometry_changed and previous
                    else previous.translation_revision
                    if previous
                    else 0
                ),
                translated_from_ocr_revision=previous.translated_from_ocr_revision
                if previous
                else None,
                translation_updated_at=previous.translation_updated_at if previous else None,
                reading_order_origin=previous.reading_order_origin if previous else None,
                translation_mode=previous.translation_mode if previous else None,
            )
            created.append(region)
        region_set_changed = set(existing) != seen_ids
        geometry_or_set_changed = geometry_or_set_changed or region_set_changed
        region_metadata_changed = False
        # A repeated save of the exact same boxes is a no-op.  Keeping the
        # revision stable is important for render/OCR optimistic locks and
        # avoids making an unchanged page appear stale.
        if (
            not geometry_or_set_changed
            and payload.expected_revision == page.region_revision
            and page.detection_status in {"detected", "reviewed"}
        ):
            unchanged = all(
                (
                    existing[item.id].class_id,
                    existing[item.id].class_name,
                    existing[item.id].confidence,
                    existing[item.id].x1,
                    existing[item.id].y1,
                    existing[item.id].x2,
                    existing[item.id].y2,
                    existing[item.id].source,
                    existing[item.id].is_manual_edited,
                )
                == (
                    item.class_id,
                    item.class_name,
                    item.confidence,
                    item.x1,
                    item.y1,
                    item.x2,
                    item.y2,
                    item.source,
                    item.is_manual_edited or item.source == "manual",
                )
                for item in payload.regions
            )
            if (
                unchanged
                and payload.expected_revision == page.region_revision
                and page.detection_status in {"detected", "reviewed"}
            ):
                return ok(
                    {
                        "page_id": page_id,
                        "revision": page.region_revision,
                        "detection_status": page.detection_status,
                        "ocr_status": page.ocr_status,
                        "translation_status": page.translation_status,
                        "reviewed_at": page.reviewed_at,
                        "regions": [_region_read(item) for item in existing.values()],
                    }
                )
            # A class/source/manual flag edit keeps the same geometry, but it
            # can change SFX preservation and therefore the generated asset.
            region_metadata_changed = True
        page_ocr_status = aggregate_ocr_status([region.ocr_status for region in created])
        page_translation_status = aggregate_translation_status(
            [region.translation_status for region in created]
        )
        page_claim = await session.execute(
            update(Page)
            .where(
                Page.id == page_id,
                Page.region_revision == payload.expected_revision,
            )
            .values(
                region_revision=payload.expected_revision + 1,
                detection_status="detected",
                reviewed_at=None,
                last_detection_error=None,
                ocr_status=page_ocr_status,
                translation_status=page_translation_status,
                updated_at=utc_now(),
            )
            .execution_options(synchronize_session=False)
        )
        if page_claim.rowcount != 1:
            await session.rollback()
            raise ConflictError(
                "REGION_REVISION_CONFLICT",
                "标注已在其他操作中更新，请重新加载",
            )
        await session.execute(delete(DetectionRegion).where(DetectionRegion.page_id == page_id))
        session.add_all(created)
        if geometry_or_set_changed:
            await mark_page_render_outdated(
                session,
                page,
                repair=True,
                reason="文本区域已变化，需要重新修复和生成",
            )
        elif region_metadata_changed:
            await mark_page_render_outdated(
                session,
                page,
                repair=True,
                reason="文本区域属性已变化，需要重新生成成品",
            )
        await session.commit()
    return ok(
        {
            "page_id": page_id,
            "revision": payload.expected_revision + 1,
            "detection_status": "detected",
            "ocr_status": page_ocr_status,
            "translation_status": page_translation_status,
            "reviewed_at": None,
            "regions": [_region_read(region) for region in created],
        }
    )


@router.post("/pages/{page_id}/review")
async def review_page_regions(page_id: str, request: Request) -> JSONResponse:
    db = database(request)
    async with db.session_factory() as session:
        page = await session.get(Page, page_id)
        if page is None:
            raise NotFoundError("页面不存在")
        if page.detection_status not in {"detected", "reviewed"}:
            raise ConflictError("PAGE_NOT_DETECTED", "页面完成检测后才能保存")
        # Compatibility endpoint: review is no longer a workflow state.
        page.detection_status = "detected"
        page.reviewed_at = None
        page.updated_at = utc_now()
        await session.commit()
        regions = list(
            (
                await session.scalars(
                    select(DetectionRegion)
                    .where(DetectionRegion.page_id == page_id)
                    .order_by(DetectionRegion.created_at, DetectionRegion.id)
                )
            ).all()
        )
    return ok(
        {
            "page_id": page_id,
            "revision": page.region_revision,
            "detection_status": page.detection_status,
            "ocr_status": page.ocr_status,
            "translation_status": page.translation_status,
            "reviewed_at": page.reviewed_at,
            "regions": [_region_read(region) for region in regions],
        }
    )


@router.get("/projects/{project_id}/next-unreviewed")
async def get_next_unreviewed_page(
    project_id: str,
    request: Request,
    after: int = Query(default=0, ge=0),
) -> JSONResponse:
    db = database(request)
    async with db.session_factory() as session:
        page = await session.scalar(
            select(Page)
            .where(
                Page.project_id == project_id,
                Page.page_index > after,
                Page.import_status == "ready",
                Page.detection_status == "undetected",
            )
            .order_by(Page.page_index)
            .limit(1)
        )
        if page is None and after:
            page = await session.scalar(
                select(Page)
                .where(
                    Page.project_id == project_id,
                    Page.import_status == "ready",
                    Page.detection_status == "undetected",
                )
                .order_by(Page.page_index)
                .limit(1)
            )
    return ok(
        {
            "page_id": page.id if page else None,
            "page_index": page.page_index if page else None,
        }
    )
