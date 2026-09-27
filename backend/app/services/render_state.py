from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import or_, select, update

from app.db.models import Page, Project
from app.services.tasks import utc_now


def aggregate_project_render_status(
    statuses: Iterable[str], *, warning_pages: int = 0
) -> str:
    values = list(statuses)
    if not values:
        return "pending"
    if any(value == "processing" for value in values):
        return "partial"
    warning_states = sum(value == "completed_with_warnings" for value in values)
    completed = sum(value in {"completed", "completed_with_warnings"} for value in values)
    failed = sum(value == "failed" for value in values)
    stale = sum(value == "outdated" for value in values)
    pending = sum(value == "pending" for value in values)
    if completed == len(values):
        return "completed_with_warnings" if warning_pages or warning_states else "completed"
    if stale:
        # Any stale page means the bookshelf must hide the project until a
        # fresh full-resolution asset exists, even when another page is still
        # pending or an older completed asset remains available.
        return "outdated"
    if completed and (failed or stale or pending):
        return "partial"
    if failed and not completed:
        return "partial"
    return "pending"


async def refresh_project_render_status(session, project_id: str) -> str:
    pages = list(
        (
            await session.scalars(
                select(Page).where(Page.project_id == project_id, Page.import_status == "ready")
            )
        ).all()
    )
    warning_pages = int(
        await session.scalar(
            select(Page.id).where(
                Page.project_id == project_id,
                or_(
                    Page.import_status.in_(["corrupt", "failed"]),
                    Page.repair_status == "completed_with_warnings",
                ),
            ).limit(1)
        )
        is not None
    )
    value = aggregate_project_render_status(
        [page.render_status for page in pages], warning_pages=warning_pages
    )
    await session.execute(
        update(Project)
        .where(Project.id == project_id)
        .values(render_status=value, updated_at=utc_now())
    )
    return value


async def mark_page_render_outdated(
    session,
    page: Page,
    *,
    repair: bool = False,
    reason: str | None = None,
) -> None:
    """Invalidate canonical generated assets after an upstream edit."""

    page.render_input_revision += 1
    if page.rendered_path:
        page.render_status = "outdated"
    else:
        page.render_status = "pending"
    page.render_error = reason
    if repair:
        page.repair_input_revision += 1
        page.repair_status = "outdated" if page.inpainted_path or page.mask_path else "pending"
        page.repair_error = reason
    page.updated_at = utc_now()
    await refresh_project_render_status(session, page.project_id)


async def mark_project_render_outdated(session, project_id: str, *, reason: str) -> None:
    pages = list(
        (
            await session.scalars(
                select(Page).where(Page.project_id == project_id, Page.import_status == "ready")
            )
        ).all()
    )
    for page in pages:
        await mark_page_render_outdated(session, page, reason=reason)
