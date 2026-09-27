from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.core.config import Settings
from app.db.models import ImportSession, Page, Project, Task, TaskItem
from app.db.session import Database, run_migrations
from app.services.storage import create_project_tree, relative_to_root
from app.services.tasks import ImportTaskManager
from tests.helpers import image_bytes


def test_migrations_persist_version_and_are_repeatable(test_settings: Settings) -> None:
    test_settings.ensure_directories()
    run_migrations(test_settings.database_path)
    run_migrations(test_settings.database_path)

    with sqlite3.connect(test_settings.database_path) as connection:
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        assert version == ("0015_remove_refined_translation",)


@pytest.mark.asyncio
async def test_restart_resumes_only_unfinished_page(
    test_settings: Settings, tmp_path: Path
) -> None:
    test_settings.ensure_directories()
    await asyncio.to_thread(run_migrations, test_settings.database_path)
    database = Database(test_settings.database_path)

    project_id = str(uuid4())
    session_id = str(uuid4())
    task_id = str(uuid4())
    first_page_id = str(uuid4())
    second_page_id = str(uuid4())
    project_root = test_settings.projects_dir / project_id
    create_project_tree(project_root)
    staging = test_settings.staging_dir / session_id / "uploads"
    staging.mkdir(parents=True)
    first_source = staging / "first.png"
    second_source = staging / "second.png"
    first_source.write_bytes(image_bytes((64, 96), "white"))
    second_source.write_bytes(image_bytes((64, 96), "black"))
    first_original = project_root / "original" / "000001.png"
    first_original.write_bytes(first_source.read_bytes())

    async with database.session_factory() as session:
        session.add_all(
            [
                ImportSession(
                    id=session_id,
                    project_name="恢复测试",
                    source_language="ja",
                    target_language="zh-CN",
                    source_type="multiple",
                    status="committed",
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                ),
                Project(
                    id=project_id,
                    name="恢复测试",
                    source_language="ja",
                    target_language="zh-CN",
                    status="importing",
                    total_pages=2,
                    workspace_path=relative_to_root(test_settings.data_dir, project_root),
                ),
                Page(
                    id=first_page_id,
                    project_id=project_id,
                    page_index=1,
                    source_filename="第1页.png",
                    original_path=relative_to_root(test_settings.data_dir, first_original),
                    import_status="ready",
                ),
                Page(
                    id=second_page_id,
                    project_id=project_id,
                    page_index=2,
                    source_filename="第2页.png",
                    import_status="processing",
                ),
                Task(
                    id=task_id,
                    project_id=project_id,
                    import_session_id=session_id,
                    task_type="project_import",
                    status="running",
                    stage="generating_derivatives",
                    total=2,
                    completed=1,
                    current_page_id=second_page_id,
                ),
                TaskItem(
                    task_id=task_id,
                    page_id=first_page_id,
                    source_path=relative_to_root(test_settings.data_dir, first_source),
                    page_index=1,
                    status="completed",
                ),
                TaskItem(
                    task_id=task_id,
                    page_id=second_page_id,
                    source_path=relative_to_root(test_settings.data_dir, second_source),
                    page_index=2,
                    status="running",
                ),
            ]
        )
        await session.commit()

    manager = ImportTaskManager(database, test_settings)
    try:
        await manager.start()
        for _ in range(200):
            snapshot = await manager.snapshot(task_id)
            if snapshot and snapshot.status in {"completed", "failed", "cancelled"}:
                break
            await asyncio.sleep(0.025)
        else:
            raise AssertionError("recovered task did not finish")

        assert snapshot is not None
        assert snapshot.status == "completed"
        assert (snapshot.completed, snapshot.failed) == (2, 0)
        async with database.session_factory() as session:
            page_count = await session.scalar(
                select(func.count(Page.id)).where(Page.project_id == project_id)
            )
            second_page = await session.get(Page, second_page_id)
        assert page_count == 2
        assert second_page is not None and second_page.import_status == "ready"
        assert not list(tmp_path.rglob("*.part"))
    finally:
        await manager.stop()
        await database.dispose()
