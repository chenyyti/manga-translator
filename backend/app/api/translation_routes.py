from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, update

from app.api.detection_routes import _region_read
from app.api.routes import database, ok
from app.api.schemas import TranslationTaskCreate, TranslationTextWrite
from app.core.errors import AppError, ConflictError, NotFoundError
from app.db.models import (
    APIProfile,
    DetectionRegion,
    Page,
    Project,
    Task,
    TaskItem,
    TranslationSettings,
)
from app.services.render_state import mark_page_render_outdated
from app.services.tasks import utc_now
from app.services.translation_tasks import (
    TranslationTaskManager,
    deterministic_reading_order,
    is_untranslated_provider_region,
    refresh_page_translation_status,
)

router = APIRouter(prefix="/api")


def manager(request: Request) -> TranslationTaskManager:
    return request.app.state.translation_task_manager


async def _translation_settings(request: Request) -> TranslationSettings:
    async with database(request).session_factory() as session:
        settings = await session.get(TranslationSettings, 1)
        if settings is None:
            settings = TranslationSettings(id=1)
            session.add(settings)
            await session.commit()
            await session.refresh(settings)
        return settings


def _is_sfx(region: DetectionRegion, names: list[str]) -> bool:
    return region.class_name.casefold() in {name.casefold() for name in names}


def _preserved(region: DetectionRegion) -> bool:
    return region.target_text_origin == "sfx_preserve" or (
        region.sfx_strategy == "preserve"
        and (region.target_text is None or region.target_text == region.source_text)
    )


async def _resolve_profile_id(
    request: Request, project: Project, requested: str | None
) -> str | None:
    if requested:
        return requested
    if project.llm_profile_id:
        return project.llm_profile_id
    return (await _translation_settings(request)).default_profile_id


async def _create_translation_task(
    request: Request,
    page: Page,
    project: Project,
    regions: list[DetectionRegion],
    payload: TranslationTaskCreate,
) -> JSONResponse:
    if project.translation_mode != "quick":
        raise ConflictError("PROJECT_MODE_MISMATCH", "项目需要先完成翻译模式迁移")
    if page.detection_status not in {"detected", "reviewed"}:
        raise ConflictError("PAGE_NOT_DETECTED", "页面完成检测后才能翻译")
    if len(regions) > 200:
        raise AppError("TRANSLATION_TOO_MANY_REGIONS", "单页最多翻译 200 个区域", status_code=422)
    if sum(len(region.source_text or "") for region in regions) > 100_000:
        raise AppError(
            "TRANSLATION_INPUT_TOO_LARGE", "当前页 OCR 文本超过 100,000 字符", status_code=422
        )
    if any(
        region.ocr_status not in {"completed", "manual"} or not (region.source_text or "").strip()
        for region in regions
    ):
        raise ConflictError("OCR_NOT_READY", "当前页所有区域必须先完成 OCR 或人工校对")
    configured = await _translation_settings(request)
    strategy = payload.sfx_strategy.value if payload.sfx_strategy else configured.sfx_strategy
    names = json.loads(configured.sfx_class_names_json)
    ordered = deterministic_reading_order(regions, project.source_language)
    async with database(request).session_factory() as session:
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
        fresh_regions = list(
            (
                await session.scalars(
                    select(DetectionRegion).where(
                        DetectionRegion.id.in_([region.id for region in ordered])
                    )
                )
            ).all()
        )
        by_id = {region.id: region for region in fresh_regions}
        ordered = [by_id[region.id] for region in ordered if region.id in by_id]
        preserved_before = {region.id: _preserved(region) for region in ordered}
        # Keep reading order durable for later phases.
        for index, region in enumerate(ordered, start=1):
            region.reading_order = index
            region.reading_order_origin = "deterministic"
            region.sfx_strategy = strategy if _is_sfx(region, names) else None
        preserved_after = {region.id: _preserved(region) for region in ordered}
        preserve_state_changed = any(
            preserved_before.get(region.id, False) != preserved_after.get(region.id, False)
            for region in ordered
        )
        candidates = [
            region
            for region in ordered
            if (
                (
                    region.translation_status in {"pending", "outdated", "failed"}
                    and region.target_text_origin != "manual"
                )
                or is_untranslated_provider_region(region, project)
                or (
                    payload.overwrite_completed
                    and region.translation_status == "completed"
                    and region.target_text_origin != "manual"
                )
                or (payload.overwrite_manual and region.target_text_origin == "manual")
            )
        ]
        if not candidates:
            raise ConflictError("NO_ELIGIBLE_REGIONS", "当前页没有需要翻译的区域")
        non_sfx = [
            region
            for region in candidates
            if not (_is_sfx(region, names) and strategy == "preserve")
        ]
        profile_id = await _resolve_profile_id(request, project, payload.profile_id)
        if non_sfx and not profile_id:
            raise ConflictError("LLM_PROFILE_REQUIRED", "普通文本翻译前必须选择翻译 Profile")
        if profile_id:
            profile_row = await session.get(APIProfile, profile_id)
            if profile_row is None or profile_row.profile_type != "llm":
                raise NotFoundError("翻译 Profile 不存在")
        if non_sfx:
            store = request.app.state.secret_store
            if store is None:
                raise ConflictError("SECRET_STORE_UNAVAILABLE", "Windows 凭据存储不可用")
            profile_row = await session.get(APIProfile, profile_id)
            try:
                key = await store.get(profile_row.credential_target) if profile_row else None
            except Exception as exc:
                raise ConflictError("SECRET_STORE_UNAVAILABLE", "Windows 凭据存储不可用") from exc
            if not key:
                raise ConflictError("PROFILE_KEY_MISSING", "翻译 Profile 缺少 API Key")
        active = await session.scalar(
            select(TaskItem.id)
            .join(Task, Task.id == TaskItem.task_id)
            .where(
                Task.task_type == "quick_translation",
                Task.status.in_(["pending", "running"]),
                TaskItem.region_id.in_([item.id for item in candidates]),
                TaskItem.status.in_(["pending", "processing"]),
            )
            .limit(1)
        )
        if active:
            raise ConflictError("TRANSLATION_ALREADY_RUNNING", "所选区域已有翻译任务正在运行")
        task = Task(
            project_id=project.id,
            task_type="quick_translation",
            status="pending",
            stage="queued",
            total=len(candidates),
            llm_profile_id=profile_id,
            parameters_json=json.dumps(
                {"profile_id": profile_id, "sfx_strategy": strategy}, ensure_ascii=False
            ),
        )
        session.add(task)
        await session.flush()
        pending_count = 0
        preserve_changed = False
        for region in candidates:
            if _is_sfx(region, names) and strategy == "preserve":
                preserve_changed = preserve_changed or region.target_text != region.source_text
                region.target_text = region.source_text
                region.target_text_origin = "sfx_preserve"
                region.translation_mode = "quick"
                region.translation_status = "completed"
                region.translation_error = None
                region.translation_revision += 1
                region.translated_from_ocr_revision = region.ocr_revision
                region.translation_updated_at = utc_now()
                continue
            region.translation_status = "processing"
            region.translation_error = None
            session.add(
                TaskItem(
                    task_id=task.id,
                    page_id=page.id,
                    region_id=region.id,
                    source_path=page.original_path or "",
                    page_index=page.page_index,
                    sequence_index=region.reading_order or 0,
                    status="pending",
                    expected_revision=region.geometry_revision,
                    expected_content_revision=region.ocr_revision,
                    expected_output_revision=region.translation_revision,
                )
            )
            pending_count += 1
        task.completed = len(candidates) - pending_count
        task.skipped = 0
        if pending_count or preserve_changed or preserve_state_changed:
            fresh_page = await session.get(Page, page.id)
            if fresh_page is not None:
                await mark_page_render_outdated(
                    session,
                    fresh_page,
                    repair=preserve_state_changed or preserve_changed,
                    reason=(
                        "拟声词策略已更新，需要重新生成成品"
                        if preserve_state_changed or preserve_changed
                        else "译文状态已更新，需要重新生成成品"
                    ),
                )
        await refresh_page_translation_status(session, page.id)
        await session.commit()
        await session.refresh(task)
    manager(request).enqueue(task.id)
    return ok(
        {"task_id": task.id, "project_id": project.id, "profile_id": profile_id}, status_code=202
    )


@router.post("/pages/{page_id}/translate")
async def translate_page(
    page_id: str, request: Request, payload: TranslationTaskCreate | None = None
) -> JSONResponse:
    async with database(request).session_factory() as session:
        page = await session.get(Page, page_id)
        if page is None:
            raise NotFoundError("页面不存在")
        project = await session.get(Project, page.project_id)
        regions = list(
            (
                await session.scalars(
                    select(DetectionRegion).where(DetectionRegion.page_id == page_id)
                )
            ).all()
        )
    if project is None:
        raise NotFoundError("项目不存在")
    return await _create_translation_task(
        request, page, project, regions, payload or TranslationTaskCreate()
    )


@router.post("/regions/{region_id}/translate")
async def translate_region(
    region_id: str, request: Request, payload: TranslationTaskCreate | None = None
) -> JSONResponse:
    payload = payload or TranslationTaskCreate()
    async with database(request).session_factory() as session:
        region = await session.get(DetectionRegion, region_id)
        if region is None:
            raise NotFoundError("文本区域不存在")
        if (
            payload.expected_translation_revision is not None
            and payload.expected_translation_revision != region.translation_revision
        ):
            raise ConflictError("TRANSLATION_REVISION_CONFLICT", "译文已更新，请重新加载")
        if region.target_text_origin == "manual" and not payload.overwrite_manual:
            raise ConflictError(
                "TRANSLATION_MANUAL_CONFIRM_REQUIRED", "重新翻译会覆盖人工译文，请确认后重试"
            )
        page = await session.get(Page, region.page_id)
        project = await session.get(Project, page.project_id) if page else None
    if page is None or project is None:
        raise NotFoundError("页面不存在")
    return await _create_translation_task(request, page, project, [region], payload)


@router.put("/regions/{region_id}/translation-text")
async def update_translation_text(
    region_id: str, payload: TranslationTextWrite, request: Request
) -> JSONResponse:
    async with database(request).session_factory() as session:
        region = await session.get(DetectionRegion, region_id)
        if region is None:
            raise NotFoundError("文本区域不存在")
        preserved_before = _preserved(region)
        changed = (
            region.target_text != payload.text
            or region.target_text_origin != "manual"
            or region.translation_status != "manual"
            or region.translation_mode != "manual"
        )
        claim = await session.execute(
            update(DetectionRegion)
            .where(
                DetectionRegion.id == region_id,
                DetectionRegion.translation_revision == payload.expected_translation_revision,
            )
            .values(
                target_text=payload.text,
                target_text_origin="manual",
                translation_mode="manual",
                translation_status="manual",
                translation_error=None,
                translation_revision=payload.expected_translation_revision + (1 if changed else 0),
                translation_updated_at=utc_now(),
                updated_at=utc_now(),
            )
        )
        if claim.rowcount != 1:
            await session.rollback()
            raise ConflictError("TRANSLATION_REVISION_CONFLICT", "译文已更新，请重新加载")
        if changed:
            page = await session.get(Page, region.page_id)
            if page is not None:
                await mark_page_render_outdated(
                    session,
                    page,
                    repair=preserved_before,
                    reason="译文已修改，需要重新生成成品",
                )
        await refresh_page_translation_status(session, region.page_id)
        await session.commit()
        saved = await session.get(DetectionRegion, region_id)
    return ok(_region_read(saved))
