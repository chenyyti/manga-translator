from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from alembic import command
from app.core.config import Settings
from app.db.models import (
    APIProfile,
    Character,
    DetectionRegion,
    ExportArtifact,
    ImportSession,
    Page,
    Project,
    Task,
    VLMAnalysis,
    VLMSettings,
)
from app.db.session import Database, run_migrations
from app.main import create_app
from app.providers.llm.secrets import MemorySecretStore
from app.services.feature_cleanup import finish_feature_cleanup


def test_old_refined_projects_are_cleared_before_task_recovery(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", project_root=tmp_path / "project")
    settings.ensure_directories()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{settings.database_path.as_posix()}")
    command.upgrade(config, "0014_detection_validation_cache")

    old_id = "00000000-0000-0000-0000-000000000001"
    quick_id = "00000000-0000-0000-0000-000000000002"
    page_id = "00000000-0000-0000-0000-000000000003"
    region_id = "00000000-0000-0000-0000-000000000004"
    target = "removed-vlm-profile"
    workspace = f"workspace/projects/{old_id}"
    for part in ("original", "masks", "cache/inpainted", "rendered", "export", "characters"):
        (settings.data_dir / workspace / part).mkdir(parents=True, exist_ok=True)
    (settings.data_dir / workspace / "original/page.png").write_bytes(b"original")
    (settings.data_dir / workspace / "characters/avatar.png").write_bytes(b"avatar")
    quick_workspace = settings.data_dir / "workspace/projects" / quick_id
    (quick_workspace / "rendered").mkdir(parents=True)
    (quick_workspace / "rendered/page.png").write_bytes(b"quick output")
    for part in ("masks/mask.png", "cache/inpainted/repair.png", "rendered/page.png", "export/book.zip"):
        (settings.data_dir / workspace / part).write_bytes(b"derived")

    async def seed() -> None:
        db = Database(settings.database_path)
        async with db.session_factory() as session:
            session.add(APIProfile(
                id="00000000-0000-0000-0000-000000000005", name="old vlm",
                profile_type="vlm", provider="openai", base_url="https://example.test",
                model="vision", credential_target=target,
            ))
            await session.flush()
            session.add(VLMSettings(id=1, default_profile_id="00000000-0000-0000-0000-000000000005"))
            session.add(Project(
                id=old_id, name="old", source_language="ja", translation_mode="refined",
                workspace_path=workspace, status="ready", total_pages=1,
                render_status="completed", cover_page_id=page_id,
                vlm_profile_id="00000000-0000-0000-0000-000000000005",
            ))
            session.add(Project(
                id=quick_id, name="quick", source_language="ja", translation_mode="quick",
                workspace_path=f"workspace/projects/{quick_id}", status="ready", total_pages=0,
            ))
            await session.flush()
            session.add(Page(
                id=page_id, project_id=old_id, page_index=1, source_filename="page.png",
                original_path=f"{workspace}/original/page.png", import_status="ready",
                detection_status="reviewed", ocr_status="completed", translation_status="completed",
                vlm_status="completed", refinement_status="completed", render_status="completed",
                repair_status="completed", mask_path=f"{workspace}/masks/mask.png",
                inpainted_path=f"{workspace}/cache/inpainted/repair.png",
                rendered_path=f"{workspace}/rendered/page.png",
            ))
            await session.flush()
            session.add(DetectionRegion(
                id=region_id, page_id=page_id, class_id=0, class_name="text",
                x1=1, y1=1, x2=20, y2=20, source="manual", source_text="原文",
                ocr_status="completed", target_text="人工译文", target_text_origin="manual",
                translation_status="manual", translation_mode="refined",
            ))
            session.add(VLMAnalysis(page_id=page_id, scene="private scene"))
            session.add(Character(
                project_id=old_id, character_uid="c1", name="character",
                avatar_path=f"{workspace}/characters/avatar.png",
            ))
            session.add(Task(project_id=old_id, task_type="refined_translation", status="running"))
            session.add(ExportArtifact(
                project_id=old_id, format="zip", status="completed", filename="book.zip",
                relative_path=f"{workspace}/export/book.zip",
            ))
            session.add(ImportSession(
                project_name="pending old", source_language="ja", translation_mode="refined",
                source_type="single", status="uploading", expires_at=datetime.now(UTC) + timedelta(hours=1),
            ))
            await session.commit()
        await db.dispose()

    asyncio.run(seed())
    store = MemorySecretStore()
    asyncio.run(store.set(target, "secret"))
    with TestClient(create_app(settings, secret_store=store)) as client:
        assert client.get("/api/vlm-profiles").status_code == 404
        assert client.post(f"/api/pages/{page_id}/refined-translate").status_code in {404, 405}
        assert client.get(f"/api/projects/{old_id}").json()["data"]["translation_mode"] == "quick"

    async def verify() -> None:
        db = Database(settings.database_path)
        async with db.session_factory() as session:
            project = await session.get(Project, old_id)
            quick = await session.get(Project, quick_id)
            page = await session.get(Page, page_id)
            region = await session.get(DetectionRegion, region_id)
            assert project and project.cover_page_id is None and project.render_status == "pending"
            assert quick and quick.translation_mode == "quick"
            assert page and page.original_path and page.detection_status == "reviewed"
            assert page.ocr_status == "completed" and page.rendered_path is None
            assert region and region.source_text == "原文" and region.ocr_status == "completed"
            assert region.target_text is None and region.translation_status == "pending"
            assert await session.scalar(select(VLMAnalysis.id)) is None
            assert await session.scalar(select(Character.id)) is None
            assert await session.scalar(select(APIProfile.id).where(APIProfile.profile_type == "vlm")) is None
            assert await session.scalar(select(Task.id).where(Task.project_id == old_id)) is None
            assert await session.scalar(select(ExportArtifact.id).where(ExportArtifact.project_id == old_id)) is None
            assert await session.scalar(text("SELECT count(*) FROM feature_cleanup")) == 0
            assert await session.scalar(text("SELECT count(*) FROM import_sessions WHERE translation_mode = 'refined'")) == 0
        await db.dispose()

    asyncio.run(verify())
    assert target not in store.values
    assert (settings.data_dir / workspace / "original/page.png").exists()
    for part in ("masks", "cache/inpainted", "rendered", "export"):
        assert not (settings.data_dir / workspace / part).exists()
    assert not (settings.data_dir / workspace / "characters/avatar.png").exists()
    assert (quick_workspace / "rendered/page.png").read_bytes() == b"quick output"
    with TestClient(create_app(settings, secret_store=store)):
        pass


def test_feature_cleanup_keeps_failed_jobs_for_retry(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", project_root=tmp_path / "project")
    settings.ensure_directories()
    run_migrations(settings.database_path)
    target = "old-visual-key"
    db = Database(settings.database_path)

    async def queue() -> None:
        async with db.session_factory() as session:
            await session.execute(
                text("INSERT INTO feature_cleanup(kind, target) VALUES ('credential', :target)"),
                {"target": target},
            )
            await session.commit()

    asyncio.run(queue())

    class FailingStore(MemorySecretStore):
        async def delete(self, target: str) -> None:
            raise RuntimeError("temporary credential-store failure")

    asyncio.run(finish_feature_cleanup(db, settings.data_dir, FailingStore()))

    async def pending_count() -> int:
        async with db.session_factory() as session:
            return int(await session.scalar(text("SELECT count(*) FROM feature_cleanup")) or 0)

    assert asyncio.run(pending_count()) == 1
    store = MemorySecretStore()
    asyncio.run(store.set(target, "secret"))
    asyncio.run(finish_feature_cleanup(db, settings.data_dir, store))
    assert asyncio.run(pending_count()) == 0
    assert target not in store.values
    asyncio.run(db.dispose())


def test_feature_cleanup_retries_failed_file_removal(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(data_dir=tmp_path / "data", project_root=tmp_path / "project")
    settings.ensure_directories()
    run_migrations(settings.database_path)
    target = "workspace/projects/00000000-0000-0000-0000-000000000001/rendered"
    directory = settings.data_dir / target
    directory.mkdir(parents=True)
    (directory / "page.png").write_bytes(b"derived")
    db = Database(settings.database_path)

    async def queue() -> None:
        async with db.session_factory() as session:
            await session.execute(
                text("INSERT INTO feature_cleanup(kind, target) VALUES ('directory', :target)"),
                {"target": target},
            )
            await session.commit()

    asyncio.run(queue())
    import app.services.feature_cleanup as cleanup_module

    real_remove = cleanup_module._remove_directory

    def fail_once(path: Path) -> None:
        raise PermissionError("temporary file lock")

    monkeypatch.setattr(cleanup_module, "_remove_directory", fail_once)
    asyncio.run(finish_feature_cleanup(db, settings.data_dir, MemorySecretStore()))
    assert directory.exists()
    monkeypatch.setattr(cleanup_module, "_remove_directory", real_remove)
    asyncio.run(finish_feature_cleanup(db, settings.data_dir, MemorySecretStore()))
    assert not directory.exists()

    async def pending_count() -> int:
        async with db.session_factory() as session:
            return int(await session.scalar(text("SELECT count(*) FROM feature_cleanup")) or 0)

    assert asyncio.run(pending_count()) == 0
    asyncio.run(db.dispose())
