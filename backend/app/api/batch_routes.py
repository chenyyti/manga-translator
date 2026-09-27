from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.api.routes import database, ok
from app.api.schemas import (
    BatchTaskCreate,
    BatchTaskPreviewRequest,
    PerformanceSettingsWrite,
    RetryFailedTaskWrite,
)
from app.core.errors import AppError, ConflictError, NotFoundError
from app.db.models import (
    APIProfile,
    DetectionRegion,
    OCRSettings,
    Page,
    PerformanceSettings,
    Project,
    Task,
    TaskItem,
    TaskItemStage,
    TranslationSettings,
)
from app.providers.ocr.registry import fixed_provider_for_language
from app.services.batch_tasks import (
    DEFAULT_SFX_NAMES,
    BatchTaskCoordinator,
    get_performance_settings,
)
from app.services.tasks import utc_now
from app.services.translation_tasks import is_untranslated_provider_region

router = APIRouter(prefix="/api")


def coordinator(request: Request) -> BatchTaskCoordinator:
    return request.app.state.batch_task_manager


async def _load_plan(request: Request, project_id: str, payload: BatchTaskPreviewRequest) -> dict[str, Any]:
    if payload.vlm_profile_id is not None:
        raise AppError("VLM_REMOVED", "视觉模型配置已移除", status_code=422)
    if payload.detection is not None:
        raise ConflictError(
            "BATCH_DETECTION_SEPARATE",
            "YOLO 检测已从批量处理拆分，请先单独执行批量检测",
        )
    async with database(request).session_factory() as session:
        project = await session.get(Project, project_id)
        if project is None:
            raise NotFoundError("项目不存在")
        if payload.translation_mode.value != project.translation_mode:
            raise ConflictError("PROJECT_MODE_MISMATCH", "批处理模式必须与项目模式一致")
        if project.status not in {"ready", "ready_with_warnings"}:
            raise ConflictError("PROJECT_NOT_READY", "项目导入完成后才能批处理")
        start = payload.start_page or 1
        end = payload.end_page or project.total_pages
        if end < start or end > project.total_pages:
            raise AppError("INVALID_PAGE_RANGE", "页码范围无效", status_code=422)
        pages = list(
            (
                await session.scalars(
                    select(Page)
                    .where(
                        Page.project_id == project_id,
                        Page.page_index.between(start, end),
                        Page.import_status == "ready",
                    )
                    .order_by(Page.page_index)
                )
            ).all()
        )
        selected = list(range(start, end + 1))
        plan_items: list[dict[str, Any]] = []
        needs_llm = False
        eligible: list[Page] = []
        for page in pages:
            reason: str | None = None
            if not page.original_path:
                reason = "原图尚未准备"
            elif page.detection_status not in {"detected", "reviewed"}:
                reason = "尚未完成 YOLO 检测"
            if reason:
                plan_items.append({"page_id": page.id, "page_index": page.page_index, "eligible": False, "reason": reason})
            else:
                eligible.append(page)
                regions = list((await session.scalars(select(DetectionRegion).where(DetectionRegion.page_id == page.id))).all())
                ocr_candidates = [
                    region
                    for region in regions
                    if (
                        payload.overwrite_manual_ocr
                        and region.source_text_origin == "manual"
                    )
                    or (
                        region.source_text_origin != "manual"
                        and (
                            payload.overwrite_model_results
                            or region.ocr_status in {"pending", "outdated", "failed"}
                        )
                    )
                ]
                source_chars = sum(len(region.source_text or "") for region in regions)
                page_needs_llm = False
                if payload.run_translation and len(regions) > 200:
                    raise AppError("TRANSLATION_TOO_MANY_REGIONS", "单页最多处理 200 个区域", status_code=422)
                if payload.run_translation and source_chars > 100_000:
                    raise AppError("TRANSLATION_INPUT_TOO_LARGE", "当前页 OCR 文本超过 100,000 字符", status_code=422)
                translation_settings = await session.get(TranslationSettings, 1)
                sfx_names = {
                    str(value).casefold()
                    for value in json.loads(
                        translation_settings.sfx_class_names_json
                        if translation_settings
                        else json.dumps(sorted(DEFAULT_SFX_NAMES), ensure_ascii=False)
                    )
                }
                strategy = translation_settings.sfx_strategy if translation_settings else "preserve"
                for region in regions:
                    if region.class_name.casefold() in sfx_names and strategy == "preserve":
                        continue
                    if region.target_text_origin == "manual" and not payload.overwrite_manual_translation:
                        continue
                    if (
                        (region.target_text_origin == "manual" and payload.overwrite_manual_translation)
                        or payload.overwrite_model_results
                        or region.translation_status in {"pending", "outdated", "failed"}
                        or is_untranslated_provider_region(region, project)
                        or not (region.target_text or "").strip()
                    ):
                        needs_llm = True
                        page_needs_llm = True
                plan_items.append(
                    {
                        "page_id": page.id,
                        "page_index": page.page_index,
                        "eligible": True,
                        "regions": len(regions),
                        "source_chars": source_chars,
                        "needs_ocr": bool(payload.run_ocr and ocr_candidates),
                        "needs_llm": page_needs_llm,
                        "needs_render": bool(
                            payload.run_render and page.render_status != "completed"
                        ),
                        "stages": [
                            *(["ocr"] if payload.run_ocr else []),
                            *(["translation"] if payload.run_translation else []),
                            *( ["repair", "render"] if payload.run_render else []),
                        ],
                    }
                )
        missing = [index for index in selected if index not in {page.page_index for page in pages}]
        for index in missing:
            plan_items.append({"page_index": index, "eligible": False, "reason": "页面不存在或导入失败"})
        # The ready-page query is ordered, but missing/corrupt pages are
        # appended separately.  Keep preview and persisted task details in
        # deterministic page order for the task drawer and retry selection.
        plan_items.sort(key=lambda item: int(item.get("page_index") or 0))
        settings = await session.get(TranslationSettings, 1)
        ocr_settings = await session.get(OCRSettings, 1)
        try:
            ocr_provider = fixed_provider_for_language(project.source_language)
        except ValueError as exc:
            raise AppError("OCR_PROVIDER_LANGUAGE_UNSUPPORTED", str(exc), status_code=422) from exc
        llm_profile_id = payload.llm_profile_id or project.llm_profile_id or (settings.default_profile_id if settings else None)
        # Validate the resolved IDs even when the corresponding stage is
        # currently reusable/skipped.  This keeps profile-type isolation
        # deterministic and prevents a malformed project setting from being
        # persisted into a future batch task.
        if llm_profile_id:
            llm_profile = await session.get(APIProfile, llm_profile_id)
            if llm_profile is None or llm_profile.profile_type != "llm":
                raise NotFoundError("翻译 Profile 不存在")
        if payload.run_translation and payload.translation_mode.value == "quick" and needs_llm:
            if llm_profile_id:
                store = request.app.state.secret_store
                if store is None:
                    raise ConflictError("SECRET_STORE_UNAVAILABLE", "Windows 凭据存储不可用")
                try:
                    key = await store.get(llm_profile.credential_target)
                except Exception as exc:
                    raise ConflictError("SECRET_STORE_UNAVAILABLE", "Windows 凭据存储不可用") from exc
                if not key:
                    raise ConflictError("PROFILE_KEY_MISSING", "翻译 Profile 缺少 API Key")
            else:
                raise ConflictError("LLM_PROFILE_REQUIRED", "批量翻译前必须选择翻译 Profile")
        return {
            "project": project,
            "start_page": start,
            "end_page": end,
            "items": plan_items,
            "eligible_pages": eligible,
            "llm_profile_id": llm_profile_id,
            "ocr_provider": ocr_provider,
            "ocr_device": ocr_settings.device if ocr_settings else "auto",
            "sfx_strategy": settings.sfx_strategy if settings else "preserve",
        }


def _plan_response(plan: dict[str, Any], payload: BatchTaskPreviewRequest) -> dict[str, Any]:
    items = plan["items"]
    eligible = [item for item in items if item.get("eligible")]
    skipped = [item for item in items if not item.get("eligible")]
    region_count = sum(int(item.get("regions", 0)) for item in eligible)
    warnings: list[str] = []
    if payload.run_translation:
        warnings.append("翻译只发送文本")
    return {
        "project_id": plan["project"].id,
        "start_page": plan["start_page"],
        "end_page": plan["end_page"],
        "translation_mode": payload.translation_mode.value,
        "eligible_pages": len(eligible),
        "skipped_pages": len(skipped),
        "estimated_ocr_requests": sum(1 for item in eligible if item.get("needs_ocr"))
        if payload.run_ocr
        else 0,
        "estimated_llm_requests": sum(1 for item in eligible if item.get("needs_llm"))
        if payload.run_translation
        else 0,
        "estimated_render_requests": sum(1 for item in eligible if item.get("needs_render"))
        if payload.run_render
        else 0,
        "region_count": region_count,
        "items": items,
        "skipped": skipped,
        "warnings": warnings,
    }


@router.post("/projects/{project_id}/batch-tasks/preview")
async def preview_batch_task(project_id: str, payload: BatchTaskPreviewRequest, request: Request) -> JSONResponse:
    plan = await _load_plan(request, project_id, payload)
    if not plan["eligible_pages"]:
        raise ConflictError(
            "NO_ELIGIBLE_PAGES",
            "所选范围没有已完成 YOLO 检测的可处理页面，请先执行批量检测",
        )
    return ok(_plan_response(plan, payload))


@router.post("/projects/{project_id}/batch-tasks")
async def create_batch_task(project_id: str, payload: BatchTaskCreate, request: Request) -> JSONResponse:
    plan = await _load_plan(request, project_id, payload)
    eligible = plan["eligible_pages"]
    if not eligible:
        raise ConflictError(
            "NO_ELIGIBLE_PAGES",
            "所选范围没有已完成 YOLO 检测的可处理页面，请先执行批量检测",
        )
    async with database(request).session_factory() as session:
        active = await session.scalar(
            select(Task.id).where(
                Task.project_id == project_id,
                Task.parent_task_id.is_(None),
                Task.task_type == "batch_pipeline",
                Task.status.in_(["pending", "running", "pausing", "paused"]),
            ).limit(1)
        )
        if active:
            raise ConflictError("BATCH_ALREADY_RUNNING", "该项目已有批处理任务")
        page_ids = [page.id for page in eligible]
        page_active = await session.scalar(
            select(TaskItem.id)
            .join(Task, Task.id == TaskItem.task_id)
            .where(
                Task.project_id == project_id,
                Task.task_type != "batch_pipeline",
                Task.status.in_(["pending", "running", "pausing", "paused"]),
                TaskItem.page_id.in_(page_ids),
                TaskItem.status.in_(["pending", "processing", "running"]),
            )
            .limit(1)
        )
        if page_active:
            raise ConflictError("PAGE_TASK_ALREADY_RUNNING", "所选页面已有任务正在运行")
        params = payload.model_dump(mode="json")
        params.update(
            {
                "llm_profile_id": plan["llm_profile_id"],
                "ocr_provider": plan["ocr_provider"],
                "ocr_device": plan["ocr_device"],
                "sfx_strategy": plan["sfx_strategy"],
                "start_page": plan["start_page"],
                "end_page": plan["end_page"],
            }
        )
        task = Task(
            project_id=project_id,
            task_type="batch_pipeline",
            status="pending",
            stage="queued",
            # Include pages that are intentionally skipped by review/import
            # policy in the denominator.  They are persisted as skipped page
            # items below so the task snapshot and detail view agree.
            total=len(plan["items"]),
            skipped=len(plan["items"]) - len(eligible),
            llm_profile_id=plan["llm_profile_id"],
            parameters_json=json.dumps(params, ensure_ascii=False),
        )
        session.add(task)
        await session.flush()
        stages: list[str] = []
        if payload.run_ocr:
            stages.append("ocr")
        if payload.run_translation:
            stages.append("translation")
        if payload.run_render:
            stages.extend(["repair", "render"])
        # Persist one page item for every selected page, including pages that
        # the review/import policy intentionally skips.  This keeps the task
        # detail drawer a faithful page-by-page record and lets the progress
        # denominator agree with the preview response.  Skipped items never
        # enter the coordinator's pending work list.
        page_by_id = {
            page.id: page
            for page in list(
                (
                    await session.scalars(
                        select(Page).where(
                            Page.project_id == project_id,
                            Page.page_index.between(plan["start_page"], plan["end_page"]),
                        )
                    )
                ).all()
            )
        }
        for plan_item in plan["items"]:
            page = page_by_id.get(plan_item.get("page_id")) if plan_item.get("page_id") else None
            is_eligible = bool(plan_item.get("eligible")) and page is not None
            page_index = int(plan_item.get("page_index") or (page.page_index if page else 0))
            item = TaskItem(
                task_id=task.id,
                page_id=page.id if page else None,
                source_path=page.original_path if page and page.original_path else "",
                page_index=page_index,
                sequence_index=page_index,
                item_type="page",
                status="pending" if is_eligible else "skipped",
                expected_revision=page.region_revision if page else 0,
                expected_content_revision=0,
                expected_output_revision=0,
                expected_repair_input_revision=page.repair_input_revision if page else None,
                expected_repair_revision=page.repair_revision if page else None,
                expected_render_input_revision=page.render_input_revision if page else None,
                error_code=None if is_eligible else "BATCH_PAGE_SKIPPED",
                error_message=None if is_eligible else str(plan_item.get("reason") or "页面被批处理策略跳过"),
                finished_at=None if is_eligible else utc_now(),
            )
            session.add(item)
            await session.flush()
            page_stages = list(stages)
            for stage_name in page_stages:
                session.add(
                    TaskItemStage(
                        task_item_id=item.id,
                        stage=stage_name,
                        status="pending" if is_eligible else "skipped",
                        error_code=None if is_eligible else "BATCH_PAGE_SKIPPED",
                        error_message=None if is_eligible else str(plan_item.get("reason") or "页面被批处理策略跳过"),
                        finished_at=None if is_eligible else utc_now(),
                    )
                )
        await session.commit()
        task_id = task.id
    coordinator(request).enqueue(task_id)
    return ok({"task_id": task_id, "project_id": project_id, "preview": _plan_response(plan, payload)}, status_code=202)


@router.get("/tasks")
async def list_tasks(
    request: Request,
    project_id: str | None = None,
    status: str | None = None,
    task_type: str | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=30, ge=1, le=100),
) -> JSONResponse:
    snapshots, total = await coordinator(request).list_snapshots(
        project_id=project_id, status=status, task_type=task_type, offset=offset, limit=limit
    )
    return ok({"items": [item.model_dump(mode="json") for item in snapshots], "total": total, "offset": offset, "limit": limit})


@router.get("/tasks/{task_id}/items")
async def list_task_items(
    task_id: str,
    request: Request,
    status: str | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
) -> JSONResponse:
    result = await coordinator(request).items(task_id, status=status, offset=offset, limit=limit)
    if result is None:
        raise NotFoundError("任务不存在")
    items, total = result
    return ok({"items": [item.model_dump(mode="json") for item in items], "total": total, "offset": offset, "limit": limit})


@router.post("/tasks/{task_id}/pause")
async def pause_task(task_id: str, request: Request) -> JSONResponse:
    try:
        return ok((await coordinator(request).pause(task_id)).model_dump(mode="json"))
    except LookupError as exc:
        raise NotFoundError("批处理任务不存在") from exc


@router.post("/tasks/{task_id}/resume")
async def resume_task(task_id: str, request: Request) -> JSONResponse:
    try:
        return ok((await coordinator(request).resume(task_id)).model_dump(mode="json"))
    except LookupError as exc:
        raise NotFoundError("批处理任务不存在") from exc


@router.post("/tasks/{task_id}/retry-failed")
async def retry_failed_task(
    task_id: str, request: Request, payload: RetryFailedTaskWrite | None = None
) -> JSONResponse:
    async with database(request).session_factory() as session:
        source = await session.get(Task, task_id)
        if source is None or source.task_type != "batch_pipeline":
            raise NotFoundError("批处理任务不存在")
        if source.status not in {"completed", "failed", "cancelled"}:
            raise ConflictError("TASK_STILL_RUNNING", "任务完成或取消后才能重试失败页面")
        active_batch = await session.scalar(
            select(Task.id).where(
                Task.project_id == source.project_id,
                Task.parent_task_id.is_(None),
                Task.task_type == "batch_pipeline",
                Task.status.in_(["pending", "running", "pausing", "paused"]),
            ).limit(1)
        )
        if active_batch:
            raise ConflictError("BATCH_ALREADY_RUNNING", "该项目已有批处理任务")
        params = json.loads(source.parameters_json or "{}")
        failed_query = select(TaskItem).where(TaskItem.task_id == task_id, TaskItem.status == "failed")
        if payload and payload.item_ids:
            failed_query = failed_query.where(TaskItem.id.in_(payload.item_ids))
        failed_items = list(
            (await session.scalars(failed_query.order_by(TaskItem.page_index, TaskItem.id))).all()
        )
        if not failed_items:
            raise ConflictError("NO_FAILED_ITEMS", "任务没有可重试的失败页面")
        task = Task(
            project_id=source.project_id,
            task_type="batch_pipeline",
            status="pending",
            stage="queued",
            total=len(failed_items),
            retry_of_task_id=source.id,
            llm_profile_id=source.llm_profile_id,
            parameters_json=source.parameters_json,
        )
        session.add(task)
        await session.flush()
        stage_names = []
        if params.get("run_ocr", True):
            stage_names.append("ocr")
        if params.get("run_translation", True):
            stage_names.append("translation")
        if params.get("run_render", True):
            stage_names.extend(["repair", "render"])
        source_project = await session.get(Project, source.project_id)
        if source_project is None:
            raise NotFoundError("项目不存在")
        cloned = 0
        for old_item in failed_items:
            page = await session.get(Page, old_item.page_id) if old_item.page_id else None
            if page is None:
                continue
            cloned += 1
            item = TaskItem(
                task_id=task.id,
                page_id=page.id,
                source_path=page.original_path or "",
                page_index=page.page_index,
                sequence_index=page.page_index,
                item_type="page",
                status="pending",
                retry_count=old_item.retry_count + 1,
                expected_revision=page.region_revision,
                expected_repair_input_revision=page.repair_input_revision,
                expected_repair_revision=page.repair_revision,
                expected_render_input_revision=page.render_input_revision,
            )
            session.add(item)
            await session.flush()
            retry_stages = list(stage_names)
            previous_stages = {
                stage.stage: stage
                for stage in list(
                    (
                        await session.scalars(
                            select(TaskItemStage).where(TaskItemStage.task_item_id == old_item.id)
                        )
                    ).all()
                )
            }
            for stage in retry_stages:
                previous = previous_stages.get(stage)
                session.add(
                    TaskItemStage(
                        task_item_id=item.id,
                        stage=stage,
                        retry_count=(previous.retry_count + 1) if previous else 1,
                    )
                )
        if cloned == 0:
            await session.rollback()
            raise ConflictError("NO_RETRYABLE_ITEMS", "失败页面已不存在，无法重试")
        task.total = cloned
        await session.commit()
        new_id = task.id
    coordinator(request).enqueue(new_id)
    return ok({"task_id": new_id, "project_id": source.project_id, "retry_of_task_id": task_id}, status_code=202)


def _performance_read(value: PerformanceSettings) -> dict[str, Any]:
    return {
        "revision": value.revision,
        "batch_concurrency": value.batch_concurrency,
        "pipeline_window": value.pipeline_window,
        "ocr_concurrency": value.ocr_concurrency,
        "llm_concurrency": value.llm_concurrency,
        "yolo_concurrency": 1,
        "lama_concurrency": 1,
        "render_concurrency": 1,
    }


@router.get("/settings/performance")
async def get_performance(request: Request) -> JSONResponse:
    return ok(_performance_read(await get_performance_settings(database(request))))


@router.put("/settings/performance")
async def update_performance(payload: PerformanceSettingsWrite, request: Request) -> JSONResponse:
    async with database(request).session_factory() as session:
        active = await session.scalar(select(Task.id).where(Task.status.in_(["pending", "running", "pausing", "paused"])).limit(1))
        if active:
            raise ConflictError("PERFORMANCE_SETTINGS_IN_USE", "存在活动任务时不能修改并发设置")
        value = await session.get(PerformanceSettings, 1)
        if value is None:
            value = PerformanceSettings(id=1)
            session.add(value)
            await session.flush()
        if value.revision != payload.expected_revision:
            raise ConflictError("SETTINGS_REVISION_CONFLICT", "性能设置已更新，请重新加载")
        value.batch_concurrency = payload.batch_concurrency
        value.pipeline_window = payload.pipeline_window
        value.ocr_concurrency = payload.ocr_concurrency
        value.llm_concurrency = payload.llm_concurrency
        value.revision += 1
        value.updated_at = utc_now()
        await session.commit()
        await session.refresh(value)
    # Managers read the defaults at startup; apply a new idle-time setting to
    # their semaphores immediately for the next queued page.
    manager = coordinator(request)
    manager._semaphore = asyncio.Semaphore(value.batch_concurrency)  # noqa: SLF001
    manager.pipeline_window = value.pipeline_window
    request.app.state.ocr_task_manager._semaphore = asyncio.Semaphore(value.ocr_concurrency)  # noqa: SLF001
    request.app.state.translation_task_manager._semaphore = asyncio.Semaphore(value.llm_concurrency)  # noqa: SLF001
    return ok(_performance_read(value))


@router.websocket("/ws/task-events")
async def task_events_socket(websocket: WebSocket) -> None:
    origin = websocket.headers.get("origin")
    if origin and origin not in {"http://127.0.0.1:5173", "http://localhost:5173", "http://127.0.0.1:8000", "http://localhost:8000"}:
        await websocket.close(code=1008)
        return
    manager: BatchTaskCoordinator = websocket.app.state.batch_task_manager
    await websocket.accept()
    # Capture the cursor before querying the initial snapshot.  A task can
    # finish while that query or the subsequent network write is in flight;
    # keeping the cursor first ensures the bounded event journal replays the
    # change instead of losing it between the snapshot and the first delta.
    version = manager.broadcaster.global_version()
    snapshots, _ = await manager.list_snapshots(offset=0, limit=100)
    await websocket.send_json({"type": "tasks.snapshot", "data": [item.model_dump(mode="json") for item in snapshots]})
    try:
        while True:
            previous_version = version
            next_version = await manager.broadcaster.wait_global(previous_version)
            if next_version == previous_version:
                await websocket.send_json({"type": "heartbeat"})
                continue
            version = next_version
            # Child managers share the broadcaster, but the task center only
            # renders top-level tasks.  Convert a child notification into its
            # parent snapshot and send one incremental record instead of
            # repeatedly shipping the whole 200-page task list.  Coalesce
            # repeated notifications for the same parent within this wakeup.
            journal = getattr(manager.broadcaster, "global_task_ids", None)
            changed_ids = journal(previous_version) if callable(journal) else []
            # The fallback keeps compatibility with broadcasters created by
            # older callers that do not expose the bounded event journal.
            if not changed_ids:
                changed_id = manager.broadcaster.last_task_id()
                changed_ids = [changed_id] if changed_id else []
            parents: list[str] = []
            for changed_id in changed_ids:
                snapshot = await manager.snapshot(changed_id)
                if snapshot is None:
                    continue
                parent_id = snapshot.parent_task_id or snapshot.id
                if parent_id not in parents:
                    parents.append(parent_id)
            snapshots = []
            for parent_id in parents:
                snapshot = await manager.snapshot(parent_id)
                if snapshot is not None and snapshot.parent_task_id is None:
                    snapshots.append(snapshot.model_dump(mode="json"))
            if snapshots:
                await websocket.send_json({"type": "tasks.delta", "data": snapshots})
    except WebSocketDisconnect:
        return
