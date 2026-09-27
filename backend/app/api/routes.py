from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import (
    APIRouter,
    File,
    Form,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.api.schemas import ImportSessionCreate, LauncherSessionHeartbeatRequest
from app.core.config import Settings
from app.core.errors import AppError, ConflictError, NotFoundError
from app.db.models import (
    APIProfile,
    ImportSession,
    Page,
    Project,
    ReadingProgress,
    Task,
    UploadedFile,
)
from app.db.session import Database
from app.providers.ocr.registry import fixed_provider_for_language
from app.services.images import generate_rendered_thumbnail, is_valid_png_file
from app.services.launcher_session import LauncherSession
from app.services.storage import (
    create_project_tree,
    is_supported_image,
    normalize_relative_path,
    relative_to_root,
    remove_project_tree,
    resolve_within,
    stream_to_atomic_file,
)
from app.services.tasks import ImportTaskManager

router = APIRouter(prefix="/api")


def utc_now_for(value: datetime) -> datetime:
    now = datetime.now(UTC)
    return now if value.tzinfo is not None else now.replace(tzinfo=None)


def ok(data: object, *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder({"success": True, "data": data}),
    )


def database(request: Request) -> Database:
    return request.app.state.database


def settings(request: Request) -> Settings:
    return request.app.state.settings


def task_manager(request: Request) -> ImportTaskManager:
    return request.app.state.task_manager


def detection_task_manager(request: Request):
    return request.app.state.detection_task_manager


def ocr_task_manager(request: Request):
    return request.app.state.ocr_task_manager


def translation_task_manager(request: Request):
    return request.app.state.translation_task_manager


def launcher_session(request: Request) -> LauncherSession:
    return request.app.state.launcher_session


def _file_urls(page: Page) -> tuple[str | None, str | None]:
    thumbnail = f"/api/pages/{page.id}/asset/thumbnail" if page.thumbnail_path else None
    preview = f"/api/pages/{page.id}/asset/preview" if page.preview_path else None
    return thumbnail, preview


def _page_summary(page: Page) -> dict[str, object]:
    thumbnail, preview = _file_urls(page)
    return {
        "id": page.id,
        "page_index": page.page_index,
        "source_filename": page.source_filename,
        "import_status": page.import_status,
        "detection_status": page.detection_status,
        "ocr_status": page.ocr_status,
        "translation_status": page.translation_status,
        "repair_status": page.repair_status,
        "render_status": page.render_status,
        "region_revision": page.region_revision,
        "reviewed_at": page.reviewed_at,
        "last_detection_error": page.last_detection_error,
        "thumbnail_url": thumbnail,
        "preview_url": preview,
        "rendered_thumbnail_url": (
            f"/api/pages/{page.id}/asset/rendered-thumbnail"
            if page.rendered_path and page.render_status == "completed"
            else None
        ),
        "rendered_thumbnail_width": page.rendered_thumbnail_width,
        "rendered_thumbnail_height": page.rendered_thumbnail_height,
    }


async def _ensure_rendered_thumbnail(
    session, page: Page, project: Project, config: Settings
) -> bool:
    """Ensure a cover-sized WebP exists for a current rendered page.

    Older Phase 6/7 databases have rendered PNGs but no thumbnail columns.
    Generate that derivative lazily and only commit it when the rendered path
    has not changed while the image was being read.
    """

    if page.render_status not in {"completed", "outdated"} or not page.rendered_path:
        return False
    project_root = resolve_within(config.data_dir, project.workspace_path)
    try:
        source = resolve_within(config.data_dir, page.rendered_path)
    except AppError:
        return False
    if (
        not source.is_file()
        or not source.is_relative_to(project_root)
        or source.suffix.casefold() != ".png"
        or not is_valid_png_file(source)
    ):
        return False
    if page.rendered_thumbnail_path:
        try:
            thumbnail = resolve_within(config.data_dir, page.rendered_thumbnail_path)
        except AppError:
            thumbnail = None
        if thumbnail and thumbnail.is_file() and thumbnail.is_relative_to(project_root):
            return True
    thumbnail = source.with_suffix(".webp")
    if not thumbnail.is_relative_to(project_root):
        return False
    try:
        width, height = await asyncio.to_thread(
            generate_rendered_thumbnail,
            source,
            thumbnail,
            thumbnail_size=config.thumbnail_size,
        )
    except Exception as exc:  # pragma: no cover - codec errors are environment-specific
        logger = logging.getLogger(__name__)
        logger.warning("Rendered cover thumbnail unavailable: %s", type(exc).__name__)
        return False
    if page.rendered_path != relative_to_root(config.data_dir, source):
        thumbnail.unlink(missing_ok=True)
        return False
    page.rendered_thumbnail_path = relative_to_root(config.data_dir, thumbnail)
    page.rendered_thumbnail_width = width
    page.rendered_thumbnail_height = height
    return True


async def _project_summaries(
    db: Database, projects: list[Project], config: Settings
) -> list[dict[str, object]]:
    if not projects:
        return []
    project_ids = [project.id for project in projects]
    async with db.session_factory() as session:
        all_pages = list(
            (
                await session.scalars(
                    select(Page)
                    .where(Page.project_id.in_(project_ids))
                    .order_by(Page.project_id, Page.page_index)
                )
            ).all()
        )
        pages_by_id = {page.id: page for page in all_pages}
        project_roots: dict[str, Path] = {}
        for project in projects:
            try:
                project_roots[project.id] = resolve_within(
                    config.data_dir, project.workspace_path
                )
            except (AppError, OSError, RuntimeError):
                # A malformed legacy workspace must not make the whole
                # bookshelf endpoint fail; it simply has no readable cover.
                continue
        first_valid: dict[str, Page] = {}
        for page in all_pages:
            if page.render_status != "completed" or not page.rendered_path:
                continue
            try:
                rendered = resolve_within(config.data_dir, page.rendered_path)
            except (AppError, OSError, RuntimeError):
                continue
            project_root = project_roots.get(page.project_id)
            if project_root is None:
                continue
            if (
                rendered.suffix.casefold() == ".png"
                and rendered.is_relative_to(project_root)
                and rendered.is_file()
                and is_valid_png_file(rendered)
            ):
                first_valid.setdefault(page.project_id, page)
        progresses = {
            progress.project_id: progress
            for progress in list(
                (
                    await session.scalars(
                        select(ReadingProgress).where(
                            ReadingProgress.project_id.in_(project_ids)
                        )
                    )
                ).all()
            )
        }
        active_tasks = list(
            (
                await session.scalars(
                    select(Task)
                    .where(
                        Task.project_id.in_(project_ids),
                        Task.parent_task_id.is_(None),
                        Task.status.in_(["pending", "running", "pausing", "paused"]),
                    )
                    .order_by(Task.created_at.desc())
                )
            ).all()
        )
        task_by_project: dict[str, str] = {}
        for task in active_tasks:
            task_by_project.setdefault(task.project_id, task.id)

        result: list[dict[str, object]] = []
        for project in projects:
            configured_cover = (
                pages_by_id.get(project.cover_page_id) if project.cover_page_id else None
            )
            cover = configured_cover
            if cover is None or cover.project_id != project.id:
                cover = None
            if cover is not None:
                try:
                    project_root = project_roots.get(project.id)
                    cover_path = (
                        resolve_within(config.data_dir, cover.rendered_path)
                        if cover.rendered_path
                        else None
                    )
                    cover_path_valid = bool(
                        cover_path
                        and cover.render_status == "completed"
                        and project_root is not None
                        and cover_path.suffix.casefold() == ".png"
                        and cover_path.is_relative_to(project_root)
                        and cover_path.is_file()
                        and is_valid_png_file(cover_path)
                    )
                except (AppError, OSError, RuntimeError):
                    cover_path_valid = False
                if not cover_path_valid:
                    cover = None
            if cover is None:
                cover = first_valid.get(project.id)
            cover_is_fallback = bool(
                project.cover_page_id and (cover is None or cover.id != project.cover_page_id)
            )
            if cover:
                await _ensure_rendered_thumbnail(session, cover, project, config)
            progress = progresses.get(project.id)
            continue_index = (
                progress.last_page_index
                if progress and progress.last_page_index is not None
                else (first_valid[project.id].page_index if project.id in first_valid else None)
            )
            result.append(
                {
                    "id": project.id,
                    "name": project.name,
                    "source_language": project.source_language,
                    "target_language": project.target_language,
                    "translation_mode": project.translation_mode,
                    "ocr_provider": project.ocr_provider,
                    "llm_profile_id": project.llm_profile_id,
                    "render_status": project.render_status,
                    "status": project.status,
                    "total_pages": project.total_pages,
                    "cover_page_id": project.cover_page_id,
                    "cover_thumbnail_url": (
                        f"/api/pages/{cover.id}/asset/rendered-thumbnail"
                        if cover and cover.rendered_path
                        else None
                    ),
                    "last_read_page_id": progress.last_page_id if progress else None,
                    "last_read_page_index": progress.last_page_index if progress else None,
                    "cover_is_fallback": cover_is_fallback,
                    "continue_url": (
                        f"/reader/{project.id}?page={continue_index}" if continue_index else None
                    ),
                    "active_task_id": task_by_project.get(project.id),
                    "created_at": project.created_at,
                    "updated_at": project.updated_at,
                }
            )
        await session.commit()
        return result


@router.get("/health")
async def health() -> JSONResponse:
    return ok({"status": "ok"})


@router.get("/runtime/startup")
async def startup_status(request: Request) -> JSONResponse:
    return ok({
        "core_status": "ready",
        "yolo": request.app.state.detection_model_catalog.snapshot(),
        "timings": request.app.state.startup_timings,
    })


@router.get("/runtime/session")
async def get_launcher_session(request: Request) -> JSONResponse:
    return ok(launcher_session(request).info())


@router.post("/runtime/session/heartbeat")
async def launcher_session_heartbeat(
    payload: LauncherSessionHeartbeatRequest, request: Request
) -> JSONResponse:
    launcher_session(request).heartbeat(payload.session_id)
    return ok({"active": True})


@router.get("/runtime/session/status")
async def get_launcher_session_status(
    request: Request, session_id: str = Query(..., min_length=1, max_length=128)
) -> JSONResponse:
    return ok(launcher_session(request).status(session_id))


@router.get("/settings/runtime")
async def runtime_settings(request: Request) -> JSONResponse:
    config = settings(request)
    return ok(
        {
            "app_name": config.app_name,
            "version": config.version,
            "data_dir": str(config.data_dir),
            "database_path": str(config.database_path),
            "models_dir": str(config.models_dir),
            "ocr_models_dir": str(config.ocr_models_dir),
            "inpainting_models_dir": str(config.inpainting_models_dir),
            "yolo_models_dir": str(config.yolo_models_dir),
            "model_source": "project",
            "thumbnail_size": config.thumbnail_size,
            "preview_size": config.preview_size,
            "local_only": True,
        }
    )


@router.post("/import-sessions")
async def create_import_session(payload: ImportSessionCreate, request: Request) -> JSONResponse:
    db = database(request)
    config = settings(request)
    try:
        fixed_ocr_provider = fixed_provider_for_language(payload.source_language.value)
    except ValueError as exc:
        raise AppError("OCR_PROVIDER_LANGUAGE_UNSUPPORTED", str(exc), status_code=422) from exc
    if payload.ocr_provider.value not in {"auto", fixed_ocr_provider}:
        raise AppError(
            "OCR_PROVIDER_FIXED",
            "OCR Provider 已按项目源语言固定",
            status_code=422,
        )
    expires_at = datetime.now(UTC) + timedelta(hours=config.upload_session_ttl_hours)
    import_session = ImportSession(
        project_name=payload.project_name,
        source_language=payload.source_language.value,
        target_language=payload.target_language,
        translation_mode=payload.translation_mode.value,
        source_type=payload.source_type.value,
        ocr_provider=fixed_ocr_provider,
        llm_profile_id=payload.llm_profile_id,
        vlm_profile_id=None,
        status="uploading",
        expires_at=expires_at,
    )
    async with db.session_factory() as session:
        if payload.llm_profile_id:
            profile = await session.get(APIProfile, payload.llm_profile_id)
            if profile is None or profile.profile_type != "llm":
                raise NotFoundError("翻译 Profile 不存在")
        session.add(import_session)
        await session.commit()
        await session.refresh(import_session)
    (config.staging_dir / import_session.id / "uploads").mkdir(parents=True, exist_ok=True)
    return ok(
        {
            "id": import_session.id,
            "project_name": import_session.project_name,
            "source_language": import_session.source_language,
            "target_language": import_session.target_language,
            "translation_mode": import_session.translation_mode,
            "source_type": import_session.source_type,
            "ocr_provider": import_session.ocr_provider,
            "llm_profile_id": import_session.llm_profile_id,
            "status": import_session.status,
            "total_bytes": 0,
            "expires_at": import_session.expires_at,
            "files": [],
        },
        status_code=201,
    )


@router.get("/import-sessions/{session_id}")
async def get_import_session(session_id: str, request: Request) -> JSONResponse:
    db = database(request)
    async with db.session_factory() as session:
        import_session = await session.scalar(
            select(ImportSession)
            .where(ImportSession.id == session_id)
            .options(selectinload(ImportSession.files))
        )
        if import_session is None:
            raise NotFoundError("导入会话不存在")
        return ok(
            {
                "id": import_session.id,
                "project_name": import_session.project_name,
                "source_language": import_session.source_language,
                "target_language": import_session.target_language,
                "translation_mode": import_session.translation_mode,
                "source_type": import_session.source_type,
                "ocr_provider": import_session.ocr_provider,
                "llm_profile_id": import_session.llm_profile_id,
                "status": import_session.status,
                "total_bytes": import_session.total_bytes,
                "expires_at": import_session.expires_at,
                "files": [
                    {
                        "id": item.id,
                        "relative_path": item.relative_path,
                        "size": item.size,
                        "sha256": item.sha256,
                    }
                    for item in sorted(import_session.files, key=lambda value: value.relative_path)
                ],
            }
        )


@router.post("/import-sessions/{session_id}/files")
async def upload_import_file(
    session_id: str,
    request: Request,
    file: Annotated[UploadFile, File()],
    relative_path: Annotated[str, Form()],
) -> JSONResponse:
    db = database(request)
    config = settings(request)
    normalized_path = normalize_relative_path(relative_path)
    async with db.session_factory() as session:
        import_session = await session.get(ImportSession, session_id)
        if import_session is None:
            raise NotFoundError("导入会话不存在")
        if import_session.status != "uploading":
            raise ConflictError("IMPORT_SESSION_CLOSED", "导入会话已经关闭")
        if import_session.expires_at < utc_now_for(import_session.expires_at):
            raise ConflictError("IMPORT_SESSION_EXPIRED", "导入会话已经过期")
        remaining = config.max_import_bytes - import_session.total_bytes
        source_type = import_session.source_type
    suffix = Path(normalized_path).suffix.casefold()
    if source_type == "zip":
        if suffix != ".zip":
            raise AppError("UNSUPPORTED_FILE", "ZIP 导入仅接受 .zip 文件", status_code=415)
    elif not is_supported_image(normalized_path):
        raise AppError("UNSUPPORTED_FILE", "仅支持 PNG、JPG、JPEG 和 WEBP", status_code=415)
    if remaining <= 0:
        raise AppError("IMPORT_TOO_LARGE", "导入内容超过允许的容量", status_code=413)

    upload_id = str(uuid4())
    destination = config.staging_dir / session_id / "uploads" / f"{upload_id}{suffix}"
    try:
        size, sha256 = await asyncio.to_thread(
            stream_to_atomic_file,
            file.file,
            destination,
            max_bytes=remaining,
        )
    finally:
        await file.close()
    stored_path = relative_to_root(config.data_dir, destination)
    uploaded = UploadedFile(
        id=upload_id,
        session_id=session_id,
        relative_path=normalized_path,
        stored_path=stored_path,
        size=size,
        sha256=sha256,
    )
    try:
        async with db.session_factory() as session:
            import_session = await session.get(ImportSession, session_id)
            if import_session is None or import_session.status != "uploading":
                raise ConflictError("IMPORT_SESSION_CLOSED", "导入会话已经关闭")
            if import_session.total_bytes + size > config.max_import_bytes:
                raise AppError("IMPORT_TOO_LARGE", "导入内容超过允许的容量", status_code=413)
            session.add(uploaded)
            import_session.total_bytes += size
            await session.commit()
    except IntegrityError as exc:
        destination.unlink(missing_ok=True)
        raise ConflictError("DUPLICATE_UPLOAD", "同一路径的文件已经上传") from exc
    except AppError:
        destination.unlink(missing_ok=True)
        raise
    return ok(
        {
            "id": uploaded.id,
            "relative_path": uploaded.relative_path,
            "size": uploaded.size,
            "sha256": uploaded.sha256,
        },
        status_code=201,
    )


@router.delete("/import-sessions/{session_id}")
async def delete_import_session(session_id: str, request: Request) -> JSONResponse:
    db = database(request)
    config = settings(request)
    async with db.session_factory() as session:
        import_session = await session.get(ImportSession, session_id)
        if import_session is None:
            raise NotFoundError("导入会话不存在")
        if import_session.status != "uploading":
            raise ConflictError("IMPORT_SESSION_CLOSED", "已提交的导入会话不能直接删除")
        await session.delete(import_session)
        await session.commit()
    await asyncio.to_thread(shutil.rmtree, config.staging_dir / session_id, True)
    return ok({"deleted": True})


@router.post("/import-sessions/{session_id}/commit")
async def commit_import_session(session_id: str, request: Request) -> JSONResponse:
    db = database(request)
    config = settings(request)
    manager = task_manager(request)
    project_id = str(uuid4())
    workspace_path = f"workspace/projects/{project_id}"
    project_root = resolve_within(config.data_dir, workspace_path)

    async with db.session_factory() as session:
        import_session = await session.scalar(
            select(ImportSession)
            .where(ImportSession.id == session_id)
            .options(selectinload(ImportSession.files))
        )
        if import_session is None:
            raise NotFoundError("导入会话不存在")
        if import_session.status != "uploading":
            raise ConflictError("IMPORT_SESSION_CLOSED", "导入会话已经提交或关闭")
        file_count = len(import_session.files)
        if file_count == 0:
            raise AppError("EMPTY_IMPORT", "请至少选择一个漫画文件", status_code=422)
        if import_session.source_type in {"single", "zip"} and file_count != 1:
            raise AppError("INVALID_FILE_COUNT", "该导入方式只允许一个文件", status_code=422)
        project = Project(
            id=project_id,
            name=import_session.project_name,
            source_language=import_session.source_language,
            target_language=import_session.target_language,
            translation_mode=import_session.translation_mode,
            ocr_provider=import_session.ocr_provider,
            llm_profile_id=import_session.llm_profile_id,
            vlm_profile_id=import_session.vlm_profile_id,
            status="importing",
            workspace_path=workspace_path,
        )
        task = Task(
            project_id=project_id,
            import_session_id=import_session.id,
            task_type="project_import",
            status="pending",
            stage="queued",
        )
        try:
            await asyncio.to_thread(create_project_tree, project_root)
            session.add(project)
            session.add(task)
            import_session.status = "committed"
            await session.commit()
            await session.refresh(task)
        except Exception:
            await session.rollback()
            await asyncio.to_thread(shutil.rmtree, project_root, True)
            raise
    manager.enqueue(task.id)
    return ok({"project_id": project_id, "task_id": task.id}, status_code=202)


@router.get("/projects")
async def list_projects(
    request: Request,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=30, ge=1, le=100),
) -> JSONResponse:
    db = database(request)
    async with db.session_factory() as session:
        total = int(await session.scalar(select(func.count(Project.id))) or 0)
        projects = list(
            (
                await session.scalars(
                    select(Project).order_by(Project.updated_at.desc()).offset(offset).limit(limit)
                )
            ).all()
        )
    items = await _project_summaries(db, projects, settings(request))
    return ok({"items": items, "total": total, "offset": offset, "limit": limit})


@router.get("/bookshelf")
async def list_bookshelf(
    request: Request,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=30, ge=1, le=100),
) -> JSONResponse:
    db = database(request)
    readable = Project.render_status.in_(["completed", "completed_with_warnings"])
    async with db.session_factory() as session:
        projects = list(
            (
                await session.scalars(
                    select(Project)
                    .where(readable)
                    .order_by(Project.updated_at.desc())
                )
            ).all()
        )
    # The aggregate render status is persisted, but a user can remove a
    # rendered PNG outside the app.  Resolve the candidate summaries first so
    # the bookshelf never advertises a project with no current readable file.
    all_items = await _project_summaries(db, projects, settings(request))
    visible_items = [item for item in all_items if item.get("cover_thumbnail_url")]
    total = len(visible_items)
    items = visible_items[offset : offset + limit]
    return ok({"items": items, "total": total, "offset": offset, "limit": limit})


@router.get("/projects/{project_id}")
async def get_project(project_id: str, request: Request) -> JSONResponse:
    db = database(request)
    async with db.session_factory() as session:
        project = await session.get(Project, project_id)
        if project is None:
            raise NotFoundError("项目不存在")
        warning_pages = int(
            await session.scalar(
                select(func.count(Page.id)).where(
                    Page.project_id == project_id, Page.import_status.in_(["corrupt", "failed"])
                )
            )
            or 0
        )
    summary = (await _project_summaries(db, [project], settings(request)))[0]
    summary["warning_pages"] = warning_pages
    return ok(summary)


@router.delete("/projects/{project_id}")
async def delete_project(project_id: str, request: Request) -> JSONResponse:
    db = database(request)
    config = settings(request)
    manager = task_manager(request)
    detection_manager = detection_task_manager(request)
    ocr_manager = ocr_task_manager(request)
    translation_manager = translation_task_manager(request)
    render_manager = request.app.state.render_task_manager
    batch_manager = request.app.state.batch_task_manager
    export_manager = request.app.state.export_task_manager
    async with db.session_factory() as session:
        project = await session.get(Project, project_id)
        if project is None:
            raise NotFoundError("项目不存在")
        workspace_path = project.workspace_path
        session_ids = list(
            (
                await session.scalars(
                    select(Task.import_session_id).where(
                        Task.project_id == project_id, Task.import_session_id.is_not(None)
                    )
                )
            ).all()
        )
        project.status = "deleting"
        await session.commit()
    # Stop a batch parent before cancelling its child managers.  The parent
    # waits for an in-flight child request to finish; cancelling the child
    # first would leave a persisted ``running`` child with no worker and make
    # the parent wait forever during deletion.
    await batch_manager.interrupt_project(project_id)
    await manager.interrupt_project(project_id)
    await detection_manager.interrupt_project(project_id)
    await ocr_manager.interrupt_project(project_id)
    await translation_manager.interrupt_project(project_id)
    await render_manager.interrupt_project(project_id)
    await export_manager.interrupt_project(project_id)
    await asyncio.to_thread(remove_project_tree, config.projects_dir, workspace_path)
    async with db.session_factory() as session:
        await session.execute(delete(Project).where(Project.id == project_id))
        await session.commit()
    for import_session_id in session_ids:
        if import_session_id:
            await asyncio.to_thread(shutil.rmtree, config.staging_dir / import_session_id, True)
    return ok({"deleted": True})


@router.get("/projects/{project_id}/pages")
async def list_pages(
    project_id: str,
    request: Request,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
) -> JSONResponse:
    db = database(request)
    async with db.session_factory() as session:
        if await session.get(Project, project_id) is None:
            raise NotFoundError("项目不存在")
        total = int(
            await session.scalar(select(func.count(Page.id)).where(Page.project_id == project_id))
            or 0
        )
        pages = list(
            (
                await session.scalars(
                    select(Page)
                    .where(Page.project_id == project_id)
                    .order_by(Page.page_index)
                    .offset(offset)
                    .limit(limit)
                )
            ).all()
        )
    return ok(
        {
            "items": [_page_summary(page) for page in pages],
            "total": total,
            "offset": offset,
            "limit": limit,
        }
    )


@router.get("/pages/{page_id}")
async def get_page(page_id: str, request: Request) -> JSONResponse:
    db = database(request)
    async with db.session_factory() as session:
        page = await session.get(Page, page_id)
        if page is None:
            raise NotFoundError("页面不存在")
        result = _page_summary(page)
        result.update(
            {
                "width": page.width,
                "height": page.height,
                "preview_width": page.preview_width,
                "preview_height": page.preview_height,
                "sha256": page.sha256,
                "last_detection_model_id": page.last_detection_model_id,
                "repair_input_revision": page.repair_input_revision,
                "repair_revision": page.repair_revision,
                "render_input_revision": page.render_input_revision,
                "render_revision": page.render_revision,
                "rendered_width": page.rendered_width,
                "rendered_height": page.rendered_height,
                "rendered_thumbnail_width": page.rendered_thumbnail_width,
                "rendered_thumbnail_height": page.rendered_thumbnail_height,
                "rendered_sha256": page.rendered_sha256,
            }
        )
        return ok(result)


@router.get("/pages/{page_id}/asset/{asset_type}")
async def get_page_asset(page_id: str, asset_type: str, request: Request) -> FileResponse:
    if asset_type not in {
        "thumbnail",
        "preview",
        "mask",
        "inpainted",
        "rendered",
        "rendered-thumbnail",
    }:
        raise NotFoundError("图片资源不存在")
    db = database(request)
    config = settings(request)
    async with db.session_factory() as session:
        page = await session.get(Page, page_id)
        if page is None:
            raise NotFoundError("页面不存在")
        project = await session.get(Project, page.project_id)
        if project is None:
            raise NotFoundError("项目不存在")
        relative_path = {
            "thumbnail": page.thumbnail_path,
            "preview": page.preview_path,
            "mask": page.mask_path,
            "inpainted": page.inpainted_path,
            "rendered": page.rendered_path,
            "rendered-thumbnail": page.rendered_thumbnail_path,
        }[asset_type]
        if asset_type == "rendered-thumbnail":
            # Regenerate both legacy NULL paths and thumbnails whose file was
            # removed while retaining the current rendered PNG.
            generated = await _ensure_rendered_thumbnail(session, page, project, config)
            if generated:
                relative_path = page.rendered_thumbnail_path
                await session.commit()
    if not relative_path:
        raise NotFoundError("图片资源尚未生成")
    path = resolve_within(config.data_dir, relative_path)
    project_root = resolve_within(config.data_dir, project.workspace_path)
    if not path.is_relative_to(project_root):
        raise AppError("UNSAFE_PATH", "图片资源路径校验失败", status_code=422)
    if not path.is_file():
        raise NotFoundError("图片资源缺失")
    media_type = (
        "image/webp"
        if asset_type in {"thumbnail", "preview", "rendered-thumbnail"}
        else "image/png"
    )
    import hashlib

    etag = f'"{page.rendered_sha256}"' if asset_type == "rendered" and page.rendered_sha256 else None
    if etag is None:
        etag = f'"{hashlib.sha256(path.read_bytes()).hexdigest()}"'
    headers = {"Cache-Control": "private, max-age=86400"}
    if etag:
        headers["ETag"] = etag
        if request.headers.get("if-none-match") == etag:
            from fastapi.responses import Response

            return Response(status_code=304, headers=headers)
    return FileResponse(
        path,
        media_type=media_type,
        headers=headers,
    )


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, request: Request) -> JSONResponse:
    # The Phase 7 coordinator owns the common snapshot shape for every task,
    # including legacy single-page children.  This keeps parent linkage,
    # active-page, pause capability, queue position and stage counters
    # consistent across the task center while the specialised managers still
    # execute their own work.
    snapshot = await request.app.state.batch_task_manager.snapshot(task_id)
    if snapshot is None:
        raise NotFoundError("任务不存在")
    return ok(snapshot)


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, request: Request) -> JSONResponse:
    snapshot = await task_manager(request).snapshot(task_id)
    if snapshot is None:
        raise NotFoundError("任务不存在")
    try:
        if snapshot.task_type == "page_detection":
            await detection_task_manager(request).cancel(task_id)
            snapshot = await task_manager(request).snapshot(task_id)
        elif snapshot.task_type == "ocr":
            await ocr_task_manager(request).cancel(task_id)
            snapshot = await task_manager(request).snapshot(task_id)
        elif snapshot.task_type == "quick_translation":
            await translation_task_manager(request).cancel(task_id)
            snapshot = await task_manager(request).snapshot(task_id)
        elif snapshot.task_type in {"page_repair", "page_render"}:
            await request.app.state.render_task_manager.cancel(task_id)
            snapshot = await request.app.state.render_task_manager.snapshot(task_id)
        elif snapshot.task_type in {"export_zip", "export_epub"}:
            await request.app.state.export_task_manager.cancel(task_id)
            # Read the common coordinator snapshot after cancellation so the
            # response retains queue position and stage counters just like
            # every other task-center task.
            snapshot = await request.app.state.batch_task_manager.snapshot(task_id)
        elif snapshot.task_type == "batch_pipeline":
            snapshot = await request.app.state.batch_task_manager.cancel(task_id)
        else:
            snapshot = await task_manager(request).cancel(task_id)
    except LookupError as exc:
        raise NotFoundError("任务不存在") from exc
    if snapshot is None:
        raise NotFoundError("任务不存在")
    return ok(snapshot)


@router.websocket("/ws/tasks/{task_id}")
async def task_socket(websocket: WebSocket, task_id: str) -> None:
    origin = websocket.headers.get("origin")
    allowed_origins = {
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:8000",
        "http://localhost:8000",
    }
    if origin and origin not in allowed_origins:
        await websocket.close(code=1008)
        return
    manager: ImportTaskManager = websocket.app.state.task_manager
    snapshot = await websocket.app.state.batch_task_manager.snapshot(task_id)
    if snapshot is None:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    version = manager.broadcaster.version(task_id)
    await websocket.send_json({"type": "task.snapshot", "data": snapshot.model_dump(mode="json")})
    try:
        while True:
            next_version = await manager.broadcaster.wait(task_id, version)
            if next_version == version:
                await websocket.send_json({"type": "heartbeat"})
                continue
            version = next_version
            snapshot = await websocket.app.state.batch_task_manager.snapshot(task_id)
            if snapshot is None:
                await websocket.close(code=1000)
                return
            await websocket.send_json(
                {"type": "task.snapshot", "data": snapshot.model_dump(mode="json")}
            )
            if snapshot.status in {"completed", "failed", "cancelled"}:
                await websocket.close(code=1000)
                return
    except WebSocketDisconnect:
        return
