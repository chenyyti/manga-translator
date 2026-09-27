from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.api.routes import _ensure_rendered_thumbnail, database, ok, settings
from app.api.schemas import CoverPageWrite, ReaderPageSummary, ReadingProgressWrite
from app.core.errors import AppError, NotFoundError
from app.db.models import Page, Project, ReadingProgress
from app.services.images import is_valid_png_file
from app.services.storage import resolve_within

router = APIRouter(prefix="/api")


def _reader_ready(project: Project) -> bool:
    return project.render_status in {"completed", "completed_with_warnings"}


def _require_ready(project: Project) -> None:
    if not _reader_ready(project):
        raise AppError(
            "READER_NOT_READY",
            "项目尚未生成全部可阅读成品",
            status_code=409,
        )


def _rendered_file(page: Page, project: Project, request: Request) -> Path | None:
    if page.render_status != "completed" or not page.rendered_path:
        return None
    try:
        config = settings(request)
        path = resolve_within(config.data_dir, page.rendered_path)
        project_root = resolve_within(config.data_dir, project.workspace_path)
    except (AppError, OSError, RuntimeError):
        return None
    if path.suffix.casefold() != ".png":
        return None
    return (
        path
        if path.is_file()
        and path.is_relative_to(project_root)
        and is_valid_png_file(path)
        else None
    )


def _missing_reason(page: Page, project: Project, request: Request) -> str | None:
    if page.import_status in {"corrupt", "failed"}:
        return "source_invalid"
    if page.render_status == "outdated":
        return "render_outdated"
    if page.render_status == "failed":
        return "render_failed"
    if _rendered_file(page, project, request) is None:
        return "render_missing"
    return None


async def _cover_for_pages(
    session, project: Project, pages: list[Page], request: Request
) -> tuple[Page | None, bool]:
    readable = [page for page in pages if _rendered_file(page, project, request) is not None]
    configured = next(
        (page for page in pages if page.id == project.cover_page_id),
        None,
    )
    cover = configured if configured in readable else (readable[0] if readable else None)
    fallback = bool(project.cover_page_id and (cover is None or cover.id != project.cover_page_id))
    if cover is not None:
        await _ensure_rendered_thumbnail(session, cover, project, settings(request))
    return cover, fallback


async def _cover_for_project(
    session, project: Project, request: Request
) -> tuple[Page | None, bool]:
    """Resolve a cover without loading every page in a reader page request."""

    configured = (
        await session.get(Page, project.cover_page_id) if project.cover_page_id else None
    )
    if configured and configured.project_id == project.id and _rendered_file(configured, project, request):
        await _ensure_rendered_thumbnail(session, configured, project, settings(request))
        return configured, False
    # Search only rendered candidates in bounded chunks.  Most projects find
    # the first valid page immediately; corrupt/missing files do not force a
    # full 200-page metadata load into the response path.
    offset = 0
    fallback: Page | None = None
    while True:
        candidates = list(
            (
                await session.scalars(
                    select(Page)
                    .where(Page.project_id == project.id, Page.render_status == "completed")
                    .order_by(Page.page_index)
                    .offset(offset)
                    .limit(50)
                )
            ).all()
        )
        if not candidates:
            break
        for candidate in candidates:
            if _rendered_file(candidate, project, request):
                fallback = candidate
                break
        if fallback:
            break
        offset += len(candidates)
    if fallback:
        await _ensure_rendered_thumbnail(session, fallback, project, settings(request))
    return fallback, bool(project.cover_page_id and (fallback is None or fallback.id != project.cover_page_id))


@router.get("/projects/{project_id}/reader")
async def get_reader_manifest(project_id: str, request: Request) -> JSONResponse:
    db = database(request)
    async with db.session_factory() as session:
        project = await session.get(Project, project_id)
        if project is None:
            raise NotFoundError("项目不存在")
        _require_ready(project)
        pages = list(
            (
                await session.scalars(
                    select(Page).where(Page.project_id == project_id).order_by(Page.page_index)
                )
            ).all()
        )
        cover, fallback = await _cover_for_pages(session, project, pages, request)
        progress = await session.get(ReadingProgress, project_id)
        readable_count = sum(
            _rendered_file(page, project, request) is not None for page in pages
        )
        first_page = next(
            (page for page in pages if _rendered_file(page, project, request) is not None),
            None,
        )
        await session.commit()
        return ok(
            {
                "project_id": project.id,
                "project_name": project.name,
                "readable": True,
                "total_pages": len(pages),
                "readable_pages": readable_count,
                "render_status": project.render_status,
                "cover_page_id": cover.id if cover else None,
                "cover_is_fallback": fallback,
                "last_read_page_id": progress.last_page_id if progress else None,
                "last_read_page_index": progress.last_page_index if progress else None,
                "first_page_id": first_page.id if first_page else None,
                "first_page_index": first_page.page_index if first_page else None,
            }
        )


@router.get("/projects/{project_id}/reader/pages")
async def list_reader_pages(
    project_id: str,
    request: Request,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=50),
) -> JSONResponse:
    db = database(request)
    async with db.session_factory() as session:
        project = await session.get(Project, project_id)
        if project is None:
            raise NotFoundError("项目不存在")
        _require_ready(project)
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
        cover, _fallback = await _cover_for_project(session, project, request)
        items: list[dict[str, object]] = []
        for page in pages:
            readable = _rendered_file(page, project, request) is not None
            items.append(
                ReaderPageSummary(
                    id=page.id,
                    page_index=page.page_index,
                    source_filename=page.source_filename,
                    import_status=page.import_status,
                    render_status=page.render_status,
                    readable=readable,
                    rendered_url=(
                        f"/api/pages/{page.id}/asset/rendered" if readable else None
                    ),
                    rendered_thumbnail_url=(
                        f"/api/pages/{page.id}/asset/rendered-thumbnail" if readable else None
                    ),
                    missing_reason=None if readable else _missing_reason(page, project, request),
                    is_cover=bool(cover and cover.id == page.id),
                ).model_dump()
            )
        await session.commit()
        return ok({"items": items, "total": total, "offset": offset, "limit": limit})


@router.put("/projects/{project_id}/reading-progress")
async def update_reading_progress(
    project_id: str, payload: ReadingProgressWrite, request: Request
) -> JSONResponse:
    db = database(request)
    async with db.session_factory() as session:
        project = await session.get(Project, project_id)
        if project is None:
            raise NotFoundError("项目不存在")
        _require_ready(project)
        page = await session.get(Page, payload.page_id)
        if page is None or page.project_id != project_id:
            raise AppError("INVALID_PROGRESS_PAGE", "阅读页不属于当前项目", status_code=422)
        now = datetime.now(UTC)
        # Use SQLite's native upsert instead of a read-then-insert sequence so
        # two browser tabs recording progress at the same time cannot race on
        # the singleton project primary key.
        statement = sqlite_insert(ReadingProgress).values(
            project_id=project_id,
            last_page_id=page.id,
            last_page_index=page.page_index,
            created_at=now,
            updated_at=now,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[ReadingProgress.project_id],
            set_={
                "last_page_id": statement.excluded.last_page_id,
                "last_page_index": statement.excluded.last_page_index,
                "updated_at": statement.excluded.updated_at,
            },
        )
        await session.execute(statement)
        await session.commit()
        progress = await session.get(ReadingProgress, project_id)
        if progress is None:  # pragma: no cover - the upsert just inserted it
            raise AppError("PROGRESS_SAVE_FAILED", "阅读进度保存失败", status_code=500)
        return ok(
            {
                "project_id": project_id,
                "page_id": page.id,
                "page_index": page.page_index,
                "updated_at": progress.updated_at,
            }
        )


@router.put("/projects/{project_id}/cover")
async def update_project_cover(
    project_id: str, payload: CoverPageWrite, request: Request
) -> JSONResponse:
    db = database(request)
    async with db.session_factory() as session:
        project = await session.get(Project, project_id)
        if project is None:
            raise NotFoundError("项目不存在")
        _require_ready(project)
        page = await session.get(Page, payload.page_id)
        if page is None or page.project_id != project_id:
            raise AppError("INVALID_COVER_PAGE", "封面页不属于当前项目", status_code=422)
        if _rendered_file(page, project, request) is None:
            raise AppError(
                "COVER_PAGE_NOT_READY",
                "封面必须选择最新成品页",
                status_code=409,
            )
        await _ensure_rendered_thumbnail(session, page, project, settings(request))
        project.cover_page_id = page.id
        project.updated_at = datetime.now(UTC)
        await session.commit()
        return ok(
            {
                "project_id": project_id,
                "cover_page_id": page.id,
                "cover_thumbnail_url": f"/api/pages/{page.id}/asset/rendered-thumbnail",
            }
        )
