from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, update

from app.api.detection_routes import _region_read
from app.api.routes import database, ok
from app.api.schemas import (
    OCRSettingsWrite,
    OCRTaskCreate,
    OCRTextWrite,
    ProjectOCRProviderWrite,
)
from app.core.errors import AppError, ConflictError, NotFoundError
from app.db.models import (
    DetectionRegion,
    OCRSettings,
    Page,
    Project,
    Task,
    TaskItem,
)
from app.providers.ocr.base import OCRProviderInfo
from app.providers.ocr.registry import (
    BUILTIN_DEFAULTS,
    fixed_provider_for_language,
    resolve_provider,
)
from app.services.ocr_tasks import OCRTaskManager, refresh_page_ocr_status
from app.services.render_state import mark_page_render_outdated
from app.services.tasks import utc_now
from app.services.translation_tasks import refresh_page_translation_status

router = APIRouter(prefix="/api")


def ocr_manager(request: Request) -> OCRTaskManager:
    return request.app.state.ocr_task_manager


async def _get_ocr_settings(request: Request) -> OCRSettings:
    db = database(request)
    async with db.session_factory() as session:
        value = await session.get(OCRSettings, 1)
        if value is None:
            value = OCRSettings(id=1)
            session.add(value)
            await session.commit()
            await session.refresh(value)
        return value


def _settings_read(value: OCRSettings) -> dict[str, object]:
    return {
        "japanese_provider": fixed_provider_for_language("ja"),
        "korean_provider": fixed_provider_for_language("ko"),
        "english_provider": fixed_provider_for_language("en"),
        "device": value.device,
    }


def _preserved(region: DetectionRegion, *, source_text: str | None = None) -> bool:
    source = region.source_text if source_text is None else source_text
    return region.target_text_origin == "sfx_preserve" or (
        region.sfx_strategy == "preserve"
        and (region.target_text is None or region.target_text == source)
    )


def _provider_read(value: OCRProviderInfo) -> dict[str, object]:
    return {
        "id": value.id,
        "name": value.name,
        "supported_languages": value.supported_languages,
        "installed": value.installed,
        "version": value.version,
        "model_ready": value.model_ready,
        "model_names": value.model_names,
        "cuda_available": value.cuda_available,
        "architecture": "isolated_process",
    }


def _validate_project_provider(provider: str, language: str) -> None:
    fixed = fixed_provider_for_language(language)
    if provider not in {"auto", fixed}:
        raise AppError(
            "OCR_PROVIDER_FIXED",
            "OCR Provider 已按项目源语言固定",
            status_code=422,
        )


async def _assert_provider_ready(request: Request, provider: str) -> None:
    providers = {item.id: item for item in await ocr_manager(request).runtime.providers()}
    current = providers.get(provider)
    if current is None:
        raise AppError("OCR_PROVIDER_NOT_FOUND", "OCR Provider 不存在", status_code=422)
    if not current.installed:
        raise ConflictError(
            "OCR_DEPENDENCY_MISSING",
            "OCR 运行依赖尚未安装，请先运行对应安装脚本",
        )
    if not current.model_ready:
        raise ConflictError(
            "OCR_MODEL_NOT_READY",
            "OCR 模型尚未准备，请先运行 scripts/prepare-ocr-models.ps1",
        )


@router.get("/providers/ocr")
async def list_ocr_providers(request: Request) -> JSONResponse:
    items = await ocr_manager(request).runtime.providers()
    return ok(
        {
            "items": [_provider_read(item) for item in items],
            "automatic": BUILTIN_DEFAULTS,
        }
    )


@router.get("/settings/ocr")
async def get_ocr_settings(request: Request) -> JSONResponse:
    return ok(_settings_read(await _get_ocr_settings(request)))


@router.put("/settings/ocr")
async def update_ocr_settings(payload: OCRSettingsWrite, request: Request) -> JSONResponse:
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
            raise ConflictError("BATCH_SETTINGS_IN_USE", "批处理任务活动期间不能修改 OCR 设置")
        fixed_settings = {
            "japanese_provider": fixed_provider_for_language("ja"),
            "korean_provider": fixed_provider_for_language("ko"),
            "english_provider": fixed_provider_for_language("en"),
        }
        requested_settings = {
            "japanese_provider": payload.japanese_provider.value,
            "korean_provider": payload.korean_provider.value,
            "english_provider": payload.english_provider.value,
        }
        if any(
            requested not in {"auto", fixed_settings[field]}
            for field, requested in requested_settings.items()
        ):
            raise AppError(
                "OCR_PROVIDER_FIXED",
                "OCR Provider 已按语言固定，不能修改默认路由",
                status_code=422,
            )
        value = await session.get(OCRSettings, 1)
        if value is None:
            value = OCRSettings(id=1)
            session.add(value)
        value.japanese_provider = fixed_settings["japanese_provider"]
        value.korean_provider = fixed_settings["korean_provider"]
        value.english_provider = fixed_settings["english_provider"]
        value.device = payload.device
        value.updated_at = utc_now()
        await session.commit()
        await session.refresh(value)
    return ok(_settings_read(value))


@router.put("/projects/{project_id}/ocr-provider")
async def update_project_ocr_provider(
    project_id: str, payload: ProjectOCRProviderWrite, request: Request
) -> JSONResponse:
    db = database(request)
    async with db.session_factory() as session:
        project = await session.get(Project, project_id)
        if project is None:
            raise NotFoundError("项目不存在")
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
            raise ConflictError("BATCH_SETTINGS_IN_USE", "该项目批处理期间不能切换 OCR Provider")
        _validate_project_provider(payload.provider.value, project.source_language)
        project.ocr_provider = fixed_provider_for_language(project.source_language)
        project.updated_at = utc_now()
        await session.commit()
    return ok(
        {
            "project_id": project_id,
            "ocr_provider": fixed_provider_for_language(project.source_language),
        }
    )


async def _create_ocr_task(
    request: Request,
    page: Page,
    project: Project,
    regions: list[DetectionRegion],
    payload: OCRTaskCreate,
    *,
    single_region: bool,
) -> JSONResponse:
    if page.detection_status not in {"detected", "reviewed"}:
        raise ConflictError("PAGE_NOT_DETECTED", "页面完成检测后才能执行 OCR")
    if not page.original_path:
        raise ConflictError("PAGE_NOT_READY", "页面原图尚不可用")
    if not regions:
        raise ConflictError("NO_OCR_REGIONS", "本页没有可识别的文本区域")
    configured = await _get_ocr_settings(request)
    try:
        provider = resolve_provider(
            project.source_language,
            project.ocr_provider,
            configured,
            payload.provider.value if payload.provider else None,
        )
    except ValueError as exc:
        raise AppError("OCR_PROVIDER_FIXED", str(exc), status_code=422) from exc
    await _assert_provider_ready(request, provider)

    if single_region and regions[0].source_text_origin == "manual" and not payload.overwrite_manual:
        raise ConflictError(
            "OCR_MANUAL_CONFIRM_REQUIRED",
            "重新识别会覆盖人工原文，请确认后重试",
        )
    db = database(request)
    region_ids = [region.id for region in regions]
    async with db.session_factory() as session:
        batch_active = await session.scalar(
            select(TaskItem.id)
            .join(Task, Task.id == TaskItem.task_id)
            .where(
                Task.task_type == "batch_pipeline",
                Task.status.in_(["pending", "running", "pausing", "paused"]),
                TaskItem.page_id == page.id,
            )
            .limit(1)
        )
        if batch_active:
            raise ConflictError("PAGE_TASK_ALREADY_RUNNING", "当前页已有批处理任务正在运行")
        active = await session.scalar(
            select(TaskItem.id)
            .join(Task, Task.id == TaskItem.task_id)
            .where(
                Task.task_type == "ocr",
                Task.status.in_(["pending", "running"]),
                TaskItem.region_id.in_(region_ids),
                TaskItem.status.in_(["pending", "running"]),
            )
            .limit(1)
        )
        if active:
            raise ConflictError("OCR_ALREADY_RUNNING", "所选区域已有 OCR 任务正在运行")
        task = Task(
            project_id=project.id,
            task_type="ocr",
            status="pending",
            stage="queued",
            total=len(regions),
            parameters_json=json.dumps(
                {"provider": provider, "device": configured.device}, ensure_ascii=False
            ),
        )
        session.add(task)
        await session.flush()
        skipped = 0
        sequence = 0
        for source_region in regions:
            region = await session.get(DetectionRegion, source_region.id)
            if region is None:
                skipped += 1
                continue
            skip_manual = region.source_text_origin == "manual" and not payload.overwrite_manual
            skip_completed = region.ocr_status == "completed" and not payload.overwrite_completed
            if skip_manual or skip_completed:
                skipped += 1
                continue
            sequence += 1
            session.add(
                TaskItem(
                    task_id=task.id,
                    page_id=page.id,
                    region_id=region.id,
                    source_path=page.original_path,
                    page_index=page.page_index,
                    sequence_index=sequence,
                    status="pending",
                    expected_revision=region.geometry_revision,
                    expected_content_revision=region.ocr_revision,
                )
            )
        task.skipped = skipped
        await session.commit()
        await session.refresh(task)
    ocr_manager(request).enqueue(task.id)
    return ok(
        {"task_id": task.id, "project_id": project.id, "provider": provider},
        status_code=202,
    )


@router.post("/pages/{page_id}/ocr")
async def ocr_page(page_id: str, payload: OCRTaskCreate, request: Request) -> JSONResponse:
    db = database(request)
    async with db.session_factory() as session:
        page = await session.get(Page, page_id)
        if page is None:
            raise NotFoundError("页面不存在")
        project = await session.get(Project, page.project_id)
        regions = list(
            (
                await session.scalars(
                    select(DetectionRegion)
                    .where(DetectionRegion.page_id == page_id)
                    .order_by(DetectionRegion.created_at, DetectionRegion.id)
                )
            ).all()
        )
    if project is None:
        raise NotFoundError("项目不存在")
    return await _create_ocr_task(request, page, project, regions, payload, single_region=False)


@router.post("/regions/{region_id}/ocr")
async def ocr_region(region_id: str, payload: OCRTaskCreate, request: Request) -> JSONResponse:
    db = database(request)
    async with db.session_factory() as session:
        region = await session.get(DetectionRegion, region_id)
        if region is None:
            raise NotFoundError("文本区域不存在")
        if (
            payload.expected_ocr_revision is not None
            and payload.expected_ocr_revision != region.ocr_revision
        ):
            raise ConflictError("OCR_REVISION_CONFLICT", "OCR 原文已更新，请重新加载")
        page = await session.get(Page, region.page_id)
        project = await session.get(Project, page.project_id) if page else None
    if page is None or project is None:
        raise NotFoundError("页面不存在")
    return await _create_ocr_task(request, page, project, [region], payload, single_region=True)


@router.put("/regions/{region_id}/ocr-text")
async def update_region_ocr_text(
    region_id: str, payload: OCRTextWrite, request: Request
) -> JSONResponse:
    db = database(request)
    async with db.session_factory() as session:
        region = await session.get(DetectionRegion, region_id)
        if region is None:
            raise NotFoundError("文本区域不存在")
        changed = region.source_text != payload.text
        preserved_before = _preserved(region)
        preserved_after = _preserved(region, source_text=payload.text)
        preserve_state_changed = preserved_before != preserved_after
        invalidate_render = changed or region.translation_status == "processing"
        values: dict[str, object] = {
            "source_text": payload.text,
            "source_text_origin": "manual",
            "ocr_status": "manual",
            "ocr_confidence": None,
            "ocr_error": None,
            "ocr_revision": payload.expected_ocr_revision + (1 if changed else 0),
            "ocr_updated_at": utc_now(),
            "updated_at": utc_now(),
        }
        # A manual save during an in-flight translation must invalidate that
        # result even when the normalized text itself is unchanged.
        if changed or region.translation_status == "processing":
            values.update(
                {
                    "translation_status": (
                        "outdated" if region.target_text is not None else "pending"
                    ),
                    "translation_revision": region.translation_revision + 1,
                    "translation_error": None,
                }
            )
        claim = await session.execute(
            update(DetectionRegion)
            .where(
                DetectionRegion.id == region_id,
                DetectionRegion.ocr_revision == payload.expected_ocr_revision,
            )
            .values(**values)
        )
        if claim.rowcount != 1:
            await session.rollback()
            raise ConflictError("OCR_REVISION_CONFLICT", "OCR 原文已更新，请重新加载")
        if changed:
            page = await session.get(Page, region.page_id)
            if page:
                await mark_page_render_outdated(
                    session,
                    page,
                    repair=preserve_state_changed,
                    reason="OCR 原文已更新，需要重新生成成品",
                )
        if invalidate_render and not changed:
            page = await session.get(Page, region.page_id)
            if page is not None:
                await mark_page_render_outdated(
                    session, page, reason="OCR 正在处理的译文已被人工确认，需要重新生成成品"
                )
        await refresh_page_ocr_status(session, region.page_id)
        await refresh_page_translation_status(session, region.page_id)
        await session.commit()
        saved = await session.get(DetectionRegion, region_id)
    return ok(_region_read(saved))
