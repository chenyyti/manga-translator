"""Project exports, page downloads, and on-demand health reports."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from PIL import Image, UnidentifiedImageError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from starlette.background import BackgroundTask

from app.api.routes import database, ok, settings
from app.api.schemas import (
    ExportArtifactRead,
    HealthIssueRead,
    HealthReportRead,
    PageExportFormat,
    ProjectExportCreate,
)
from app.core.errors import AppError, ConflictError, NotFoundError
from app.db.models import (
    APIProfile,
    DetectionModel,
    DetectionSettings,
    ExportArtifact,
    Page,
    Project,
    ReadingProgress,
    Task,
    TaskItem,
    TaskItemStage,
    TranslationSettings,
)
from app.providers.llm.base import LLMSecretStoreError
from app.services.detection_models import PROJECT_STORAGE, resolve_detection_model_path
from app.services.exporting import (
    file_sha256,
    page_filename,
    resolve_current_rendered,
    safe_filename_component,
    write_jpeg_atomic,
)
from app.services.images import is_valid_png_file
from app.services.storage import resolve_within
from app.services.tasks import utc_now

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

_ACTIVE_EXPORT_STATUSES = {"pending", "running", "pausing", "paused"}
_TERMINAL_EXPORT_STATUSES = {"completed", "failed", "cancelled", "deleted"}


def _artifact_read(artifact: ExportArtifact) -> dict[str, object]:
    result = ExportArtifactRead(
        id=artifact.id,
        project_id=artifact.project_id,
        task_id=artifact.task_id,
        format=artifact.format,
        status=artifact.status,
        filename=artifact.filename,
        size=artifact.size,
        sha256=artifact.sha256,
        page_count=artifact.page_count,
        error_code=artifact.error_code,
        error_message=artifact.error_message,
        created_at=artifact.created_at,
        updated_at=artifact.updated_at,
        download_url=(
            f"/api/exports/{artifact.id}/download" if artifact.status == "completed" else None
        ),
    )
    return result.model_dump(mode="json")


async def _project_or_404(request: Request, project_id: str) -> Project:
    async with database(request).session_factory() as session:
        project = await session.get(Project, project_id)
        if project is None:
            raise NotFoundError("项目不存在")
        return project


def _safe_project_root(request: Request, project: Project) -> Path:
    config = settings(request)
    try:
        return resolve_within(config.data_dir, project.workspace_path)
    except AppError:
        raise AppError("UNSAFE_PATH", "项目工作区路径校验失败", status_code=422) from None


def _safe_page_asset(page: Page, project: Project, request: Request) -> Path | None:
    return resolve_current_rendered(page, project, settings(request).data_dir)


def _content_disposition(filename: str) -> str:
    safe = safe_filename_component(filename, fallback="download")
    # Keep the ASCII fallback stable while allowing a UTF-8 filename in clients
    # that support RFC 5987.  No user path is ever placed in the header.
    ascii_name = "".join(ch if ord(ch) < 128 else "_" for ch in safe) or "download"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(safe)}"


def _etag(value: str) -> str:
    return f'"{value}"'


def _not_modified(request: Request, etag: str) -> Response | None:
    headers = {"ETag": etag, "Cache-Control": "private, max-age=86400"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return None


def _file_response(path: Path, *, media_type: str, filename: str, etag: str) -> FileResponse:
    return FileResponse(
        path,
        media_type=media_type,
        headers={
            "ETag": etag,
            "Cache-Control": "private, max-age=86400",
            "Content-Disposition": _content_disposition(filename),
        },
    )


@router.get("/pages/{page_id}/export/{format}")
async def export_page(page_id: str, format: str, request: Request) -> Response:
    try:
        export_format = PageExportFormat(format.casefold())
    except ValueError as exc:
        raise AppError("UNSUPPORTED_EXPORT_FORMAT", "当前页仅支持 PNG 或 JPG", status_code=422) from exc
    db = database(request)
    async with db.session_factory() as session:
        page = await session.get(Page, page_id)
        if page is None:
            raise NotFoundError("页面不存在")
        project = await session.get(Project, page.project_id)
        if project is None:
            raise NotFoundError("项目不存在")
        source = _safe_page_asset(page, project, request)
        if source is None:
            raise AppError("PAGE_EXPORT_NOT_READY", "当前页面没有最新成品", status_code=409)
        # This endpoint is an explicit download, so hash the bytes currently
        # on disk instead of trusting the database digest.  That keeps ETag
        # validation correct even if a user replaces a rendered file outside
        # the application between requests.
        digest = await asyncio.to_thread(file_sha256, source)
        if export_format is PageExportFormat.PNG:
            etag = _etag(f"{digest}:png")
            not_modified = _not_modified(request, etag)
            if not_modified is not None:
                return not_modified
            return _file_response(
                source,
                media_type="image/png",
                filename=f"page-{page.page_index:04d}.png",
                etag=etag,
            )
        project_root = _safe_project_root(request, project)
        destination = project_root / "export" / f".page-{uuid4().hex}.jpg"
        etag = _etag(f"{digest}:jpg")
        not_modified = _not_modified(request, etag)
        if not_modified is not None:
            return not_modified
    try:
        width, height = await asyncio.to_thread(write_jpeg_atomic, source, destination)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    response = _file_response(
        destination,
        media_type="image/jpeg",
        filename=f"page-{page.page_index:04d}.jpg",
        etag=etag,
    )
    response.headers["X-Image-Width"] = str(width)
    response.headers["X-Image-Height"] = str(height)
    response.background = BackgroundTask(destination.unlink, missing_ok=True)
    return response


def _png_dimensions(path: Path) -> tuple[int, int]:
    with Image.open(path) as image:
        return image.size


def _export_pages_not_ready(pages: list[Page], project: Project, request: Request) -> list[dict[str, object]]:
    problems: list[dict[str, object]] = []
    for page in pages:
        reason: str | None = None
        if page.import_status in {"corrupt", "failed"}:
            reason = "source_invalid"
        elif page.import_status != "ready":
            reason = "source_not_ready"
        elif page.render_status != "completed":
            reason = f"render_{page.render_status}"
        elif _safe_page_asset(page, project, request) is None:
            reason = "render_missing_or_invalid"
        if reason:
            problems.append(
                {"page_id": page.id, "page_index": page.page_index, "reason": reason}
            )
    return problems


@router.post("/projects/{project_id}/exports")
async def create_project_export(
    project_id: str, payload: ProjectExportCreate, request: Request
) -> JSONResponse:
    db = database(request)
    config = settings(request)
    async with db.session_factory() as session:
        project = await session.get(Project, project_id)
        if project is None:
            raise NotFoundError("项目不存在")
        if project.status not in {"ready", "ready_with_warnings"}:
            raise ConflictError("PROJECT_NOT_READY", "项目导入完成后才能导出")
        pages = list(
            (
                await session.scalars(
                    select(Page).where(Page.project_id == project_id).order_by(Page.page_index)
                )
            ).all()
        )
        if not pages:
            raise AppError("EXPORT_PAGES_NOT_READY", "项目没有可导出的页面", status_code=409)
        problems = _export_pages_not_ready(pages, project, request)
        if problems:
            raise AppError(
                "EXPORT_PAGES_NOT_READY",
                "存在缺失、损坏或过期成品页，暂不能导出",
                status_code=409,
                details={"pages": problems[:200]},
            )
        active = await session.scalar(
            select(ExportArtifact.id).where(
                ExportArtifact.project_id == project_id,
                ExportArtifact.format == payload.format.value,
                ExportArtifact.status.in_(_ACTIVE_EXPORT_STATUSES),
            )
        )
        if active:
            raise ConflictError("EXPORT_ALREADY_RUNNING", "同一格式的导出任务正在运行")
        artifact_id = str(uuid4())
        task = Task(
            id=str(uuid4()),
            project_id=project_id,
            task_type=f"export_{payload.format.value}",
            status="pending",
            stage="preparing",
            total=len(pages),
        )
        artifact = ExportArtifact(
            id=artifact_id,
            project_id=project_id,
            task_id=task.id,
            format=payload.format.value,
            status="pending",
            filename=(
                f"{safe_filename_component(project.name)}-{artifact_id[:8]}.{payload.format.value}"
            ),
            page_count=len(pages),
        )
        session.add(task)
        await session.flush()
        session.add(artifact)
        await session.flush()
        staging_path = f"{project.workspace_path}/export/.staging/{task.id}"
        page_manifest: dict[str, dict[str, object]] = {}
        cover: Page | None = None
        for page in pages:
            source = _safe_page_asset(page, project, request)
            if source is None:  # the file may have changed since validation
                raise AppError("EXPORT_PAGES_NOT_READY", "页面成品在导出前发生变化", status_code=409)
            digest = page.rendered_sha256 or await asyncio.to_thread(file_sha256, source)
            width = page.rendered_width
            height = page.rendered_height
            if not width or not height:
                width, height = await asyncio.to_thread(_png_dimensions, source)
            page_manifest[page.id] = {
                "page_id": page.id,
                "page_index": page.page_index,
                "staging_path": f"{staging_path}/{page_filename(page.page_index)}",
                "render_revision": page.render_revision,
                "rendered_sha256": digest,
                "width": width,
                "height": height,
            }
            if cover is None and page.id == project.cover_page_id:
                cover = page
            if cover is None:
                cover = page
            session.add(
                TaskItem(
                    id=str(uuid4()),
                    task_id=task.id,
                    page_id=page.id,
                    source_path=page.rendered_path or "",
                    page_index=page.page_index,
                    sequence_index=page.page_index,
                    item_type="export_page",
                    status="pending",
                    expected_render_input_revision=page.render_revision,
                )
            )
        task.parameters_json = json.dumps(
            {
                "artifact_id": artifact.id,
                "format": artifact.format,
                "staging_path": staging_path,
                "cover_page_index": cover.page_index if cover else pages[0].page_index,
                "pages": page_manifest,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        await session.flush()
        # Add one durable stage row per page so task-center snapshots can show
        # copying/packaging progress without loading image data.
        items = list(
            (
                await session.scalars(select(TaskItem).where(TaskItem.task_id == task.id))
            ).all()
        )
        for item in items:
            session.add(TaskItemStage(task_item_id=item.id, stage="export", status="pending"))
        staging = resolve_within(config.data_dir, staging_path)
        project_root = resolve_within(config.data_dir, project.workspace_path)
        if not staging.is_relative_to(project_root / "export"):
            raise AppError("UNSAFE_PATH", "导出暂存路径校验失败", status_code=422)
        staging.mkdir(parents=True, exist_ok=True)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            await asyncio.to_thread(shutil.rmtree, staging, True)
            active = await session.scalar(
                select(ExportArtifact.id).where(
                    ExportArtifact.project_id == project_id,
                    ExportArtifact.format == payload.format.value,
                    ExportArtifact.status.in_(_ACTIVE_EXPORT_STATUSES),
                )
            )
            if active:
                raise ConflictError("EXPORT_ALREADY_RUNNING", "同一格式的导出任务正在运行") from None
            raise
    request.app.state.export_task_manager.enqueue(task.id)
    return ok(
        {
            "export_id": artifact.id,
            "task_id": task.id,
            "format": artifact.format,
            "page_count": artifact.page_count,
        },
        status_code=202,
    )


@router.get("/projects/{project_id}/exports")
async def list_project_exports(
    project_id: str,
    request: Request,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=30, ge=1, le=100),
) -> JSONResponse:
    db = database(request)
    async with db.session_factory() as session:
        if await session.get(Project, project_id) is None:
            raise NotFoundError("项目不存在")
        total = int(
            await session.scalar(
                select(func.count(ExportArtifact.id)).where(ExportArtifact.project_id == project_id)
            )
            or 0
        )
        artifacts = list(
            (
                await session.scalars(
                    select(ExportArtifact)
                    .where(ExportArtifact.project_id == project_id)
                    .order_by(ExportArtifact.created_at.desc())
                    .offset(offset)
                    .limit(limit)
                )
            ).all()
        )
    return ok(
        {
            "items": [_artifact_read(item) for item in artifacts],
            "total": total,
            "offset": offset,
            "limit": limit,
        }
    )


@router.get("/exports/{export_id}")
async def get_export(export_id: str, request: Request) -> JSONResponse:
    async with database(request).session_factory() as session:
        artifact = await session.get(ExportArtifact, export_id)
        if artifact is None:
            raise NotFoundError("导出记录不存在")
        return ok(_artifact_read(artifact))


@router.get("/exports/{export_id}/download")
async def download_export(export_id: str, request: Request) -> Response:
    db = database(request)
    config = settings(request)
    async with db.session_factory() as session:
        artifact = await session.get(ExportArtifact, export_id)
        if artifact is None:
            raise NotFoundError("导出记录不存在")
        if artifact.status != "completed" or not artifact.relative_path:
            raise AppError("EXPORT_NOT_READY", "导出文件尚未完成", status_code=409)
        project = await session.get(Project, artifact.project_id)
        if project is None:
            raise NotFoundError("项目不存在")
        try:
            path = resolve_within(config.data_dir, artifact.relative_path)
            project_root = resolve_within(config.data_dir, project.workspace_path)
        except AppError:
            raise AppError("UNSAFE_PATH", "导出文件路径校验失败", status_code=422) from None
        if not path.is_relative_to(project_root / "export") or not path.is_file():
            raise AppError("EXPORT_FILE_MISSING", "导出文件不存在", status_code=404)
        digest = artifact.sha256 or await asyncio.to_thread(file_sha256, path)
    etag = _etag(digest)
    not_modified = _not_modified(request, etag)
    if not_modified is not None:
        return not_modified
    media_type = "application/epub+zip" if artifact.format == "epub" else "application/zip"
    return _file_response(path, media_type=media_type, filename=artifact.filename, etag=etag)


@router.delete("/exports/{export_id}")
async def delete_export(export_id: str, request: Request) -> JSONResponse:
    db = database(request)
    config = settings(request)
    path: Path | None = None
    staging: Path | None = None
    async with db.session_factory() as session:
        artifact = await session.get(ExportArtifact, export_id)
        if artifact is None:
            raise NotFoundError("导出记录不存在")
        if artifact.status in _ACTIVE_EXPORT_STATUSES:
            raise ConflictError("EXPORT_ACTIVE", "导出任务正在运行，不能删除")
        project = await session.get(Project, artifact.project_id)
        if project is None:
            raise NotFoundError("项目不存在")
        project_root = resolve_within(config.data_dir, project.workspace_path)
        if artifact.relative_path:
            candidate = resolve_within(config.data_dir, artifact.relative_path)
            if candidate.is_relative_to(project_root / "export"):
                path = candidate
        staging = project_root / "export" / ".staging"
        artifact.status = "deleted"
        artifact.relative_path = None
        # Keep the immutable history metadata (size, digest and page count)
        # after deleting the file; only the filesystem reference is removed.
        artifact.updated_at = utc_now()
        await session.commit()
    if path is not None:
        path.unlink(missing_ok=True)
    if staging is not None:
        await asyncio.to_thread(_remove_empty_staging, staging)
    return ok({"deleted": True, "export_id": export_id})


def _remove_empty_staging(path: Path) -> None:
    if not path.is_dir():
        return
    for child in list(path.iterdir()):
        # Never recursively remove a sibling task's staging directory.  ZIP
        # and EPUB exports may run concurrently for the same project; only
        # an already-empty directory is safe to reap here.
        if child.is_dir() and not child.is_symlink():
            try:
                child.rmdir()
            except OSError:
                pass
    try:
        path.rmdir()
    except OSError:
        pass


def _issue(
    checks: list[dict[str, object]],
    code: str,
    severity: str,
    message: str,
    *,
    page: Page | None = None,
    action: str | None = None,
) -> None:
    checks.append(
        HealthIssueRead(
            code=code,
            severity=severity,
            message=message,
            page_id=page.id if page else None,
            page_index=page.page_index if page else None,
            action=action,
        ).model_dump()
    )


def _verify_image(path: Path) -> bool:
    try:
        with Image.open(path) as image:
            image.verify()
        return True
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        return False


async def _health_profile(
    checks: list[dict[str, object]], request: Request, profile_id: str | None, expected_type: str
) -> None:
    if not profile_id:
        return
    async with database(request).session_factory() as session:
        profile = await session.get(APIProfile, profile_id)
    if profile is None or profile.profile_type != expected_type:
        _issue(checks, "PROFILE_NOT_FOUND", "error", "配置的模型 Profile 不存在或类型不匹配")
        return
    store = getattr(request.app.state, "secret_store", None)
    if store is None:
        _issue(checks, "SECRET_STORE_UNAVAILABLE", "warning", "凭据存储不可用，无法验证模型凭据")
        return
    try:
        secret = await store.get(profile.credential_target)
    except (LLMSecretStoreError, OSError, RuntimeError):
        secret = None
    if not secret:
        _issue(checks, "PROFILE_CREDENTIAL_UNAVAILABLE", "warning", "模型凭据不可用")


async def _health_page(
    page: Page,
    project: Project,
    request: Request,
    deep: bool,
    semaphore: asyncio.Semaphore,
) -> tuple[list[dict[str, object]], int]:
    config = settings(request)
    checks: list[dict[str, object]] = []
    ok_count = 0

    def path_for(value: str | None) -> Path | None:
        if not value:
            return None
        try:
            path = resolve_within(config.data_dir, value)
            root = resolve_within(config.data_dir, project.workspace_path)
        except AppError:
            return None
        return path if path.is_relative_to(root) else None

    original = path_for(page.original_path)
    if page.import_status in {"corrupt", "failed"}:
        _issue(checks, "SOURCE_INVALID", "error", "原图导入失败或已损坏", page=page, action="open_page")
    elif original is None or not original.is_file():
        _issue(checks, "SOURCE_MISSING", "error", "原图文件缺失", page=page, action="open_page")
    elif deep:
        async with semaphore:
            if await asyncio.to_thread(_verify_image, original):
                ok_count += 1
            else:
                _issue(checks, "SOURCE_CORRUPT", "error", "原图结构无法读取", page=page, action="open_page")
    else:
        ok_count += 1
    for label, value in (("thumbnail", page.thumbnail_path), ("preview", page.preview_path)):
        path = path_for(value)
        if path is None or not path.is_file():
            _issue(checks, f"{label.upper()}_MISSING", "warning", f"{label} 文件缺失", page=page, action="open_page")
        elif deep:
            async with semaphore:
                if not await asyncio.to_thread(_verify_image, path):
                    _issue(checks, f"{label.upper()}_CORRUPT", "warning", f"{label} 文件无法读取", page=page, action="open_page")
                else:
                    ok_count += 1
        else:
            ok_count += 1
    if page.detection_status == "undetected":
        _issue(checks, "PAGE_NOT_DETECTED", "warning", "页面尚未完成文本区域检测", page=page, action="open_page")
    for field, prefix, message in (
        (page.ocr_status, "OCR", "OCR 结果失败或已过期"),
        (page.translation_status, "TRANSLATION", "译文失败或已过期"),
        (page.repair_status, "REPAIR", "修复结果失败或已过期"),
        (page.render_status, "RENDER", "成品失败或已过期"),
    ):
        if field in {"failed", "outdated"}:
            _issue(
                checks,
                f"{prefix}_{field.upper()}",
                "error" if field == "failed" else "warning",
                message,
                page=page,
                action="open_page",
            )
    # An outdated page can still have a viewable old PNG.  Report the stale
    # revision without incorrectly classifying that old file as missing.
    rendered = path_for(page.rendered_path)
    if (
        rendered is not None
        and (
            rendered.suffix.casefold() != ".png"
            or not rendered.is_file()
            or not is_valid_png_file(rendered)
        )
    ):
        rendered = None
    if rendered is None:
        _issue(checks, "RENDERED_MISSING", "error", "当前成品 PNG 缺失或结构损坏", page=page, action="open_page")
    elif deep:
        async with semaphore:
            if await asyncio.to_thread(_verify_image, rendered):
                ok_count += 1
            else:
                _issue(checks, "RENDERED_CORRUPT", "error", "当前成品 PNG 无法读取", page=page, action="open_page")
    else:
        ok_count += 1
    return checks, ok_count


@router.get("/projects/{project_id}/health-check")
async def project_health_check(
    project_id: str, request: Request, deep: bool = Query(default=False)
) -> JSONResponse:
    db = database(request)
    config = settings(request)
    async with db.session_factory() as session:
        project = await session.get(Project, project_id)
        if project is None:
            raise NotFoundError("项目不存在")
        pages = list(
            (
                await session.scalars(
                    select(Page).where(Page.project_id == project_id).order_by(Page.page_index)
                )
            ).all()
        )
        progress = await session.get(ReadingProgress, project_id)
        completed_exports = list(
            (
                await session.scalars(
                    select(ExportArtifact).where(
                        ExportArtifact.project_id == project_id,
                        ExportArtifact.status == "completed",
                    )
                )
            ).all()
        )
        detection_model_id = list(
            (
                await session.scalars(
                    select(Page.last_detection_model_id)
                    .where(Page.project_id == project_id, Page.last_detection_model_id.is_not(None))
                    .limit(1)
                )
            ).all()
        )
        detection_settings = await session.get(DetectionSettings, 1)
        model_id = (
            detection_model_id[0]
            if detection_model_id
            else (detection_settings.default_model_id if detection_settings else None)
        )
        model = await session.get(DetectionModel, model_id) if model_id else None
        translation_settings = await session.get(TranslationSettings, 1)
    checks: list[dict[str, object]] = []
    semaphore = asyncio.Semaphore(4)
    page_results = await asyncio.gather(
        *(_health_page(page, project, request, deep, semaphore) for page in pages)
    )
    ok_count = sum(result[1] for result in page_results)
    for result, _ in page_results:
        checks.extend(result)
    try:
        root = _safe_project_root(request, project)
    except AppError:
        root = None
        _issue(checks, "WORKSPACE_UNSAFE", "error", "项目工作区路径校验失败", action="open_project")
    if project.status not in {"ready", "ready_with_warnings"}:
        _issue(checks, "PROJECT_NOT_READY", "error", "项目尚未完成导入", action="open_project")
    if root is not None and not root.is_dir():
        _issue(checks, "WORKSPACE_MISSING", "error", "项目工作区缺失", action="open_project")
    if root is not None and list(root.rglob("*.part")):
        _issue(checks, "TEMP_PART_REMAINS", "warning", "工作区存在未完成的临时文件", action="open_project")
    if model_id:
        model_available = bool(
            model
            and model.status == "ready"
            and model.storage_scope == PROJECT_STORAGE
        )
        if model_available and model and model.relative_path:
            try:
                model_path = resolve_detection_model_path(config, model)
                model_available = model_path.is_file()
            except AppError:
                model_available = False
        if not model_available:
            code = (
                "YOLO_MODEL_MIGRATION_REQUIRED"
                if model is not None and model.storage_scope != PROJECT_STORAGE
                else "YOLO_MODEL_UNAVAILABLE"
            )
            message = (
                "检测模型尚未迁移到项目 models/yolo 目录"
                if code == "YOLO_MODEL_MIGRATION_REQUIRED"
                else "检测模型不可用"
            )
            _issue(checks, code, "warning", message, action="open_settings")
    await _health_profile(
        checks,
        request,
        project.llm_profile_id or (translation_settings.default_profile_id if translation_settings else None),
        "llm",
    )
    for artifact in completed_exports:
        if not artifact.relative_path:
            continue
        try:
            path = resolve_within(config.data_dir, artifact.relative_path)
            if root is None or not path.is_relative_to(root / "export") or not path.is_file():
                _issue(checks, "EXPORT_FILE_MISSING", "warning", "历史导出文件缺失", action="open_project")
        except AppError:
            _issue(checks, "EXPORT_FILE_UNSAFE", "error", "历史导出文件路径校验失败", action="open_project")
    if progress and progress.last_page_id:
        page_ids = {page.id for page in pages}
        if progress.last_page_id not in page_ids:
            _issue(checks, "READING_PROGRESS_INVALID", "warning", "阅读进度引用的页面不存在", action="open_project")
    elif progress and progress.last_page_index is not None:
        _issue(checks, "READING_PROGRESS_INVALID", "warning", "阅读进度引用已失效", action="open_project")
    all_rendered = (
        project.status in {"ready", "ready_with_warnings"}
        and bool(pages)
        and not _export_pages_not_ready(pages, project, request)
    )
    errors = sum(item["severity"] == "error" for item in checks)
    warnings = sum(item["severity"] == "warning" for item in checks)
    status = "error" if errors else ("warning" if warnings else "healthy")
    report = HealthReportRead(
        project_id=project_id,
        status=status,
        checked_at=datetime.now(UTC),
        counts={"errors": int(errors), "warnings": int(warnings), "ok": int(ok_count)},
        checks=checks,
        exportable={"png": all_rendered, "jpg": all_rendered, "zip": all_rendered, "epub": all_rendered},
    )
    return ok(report.model_dump(mode="json"))


__all__ = ["router"]
