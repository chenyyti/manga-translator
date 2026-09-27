from __future__ import annotations

import asyncio
from pathlib import Path

from app.core.config import Settings
from app.db.models import DetectionModel
from app.db.session import Database, run_migrations
from scripts.migrate_models_to_project import migrate_yolo


def test_settings_create_project_yolo_directory(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", project_root=tmp_path / "project")
    settings.ensure_directories()
    assert settings.models_dir == tmp_path / "project" / "models"
    assert settings.yolo_models_dir.is_dir()
    assert not settings.yolo_models_dir.is_relative_to(settings.data_dir)


def test_legacy_yolo_model_migrates_without_deleting_source(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", project_root=tmp_path / "project")
    settings.ensure_directories()
    database_path = settings.database_path
    run_migrations(database_path)

    model_id = "legacy-model"
    source = settings.data_dir / "models" / model_id / "model.pt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"legacy model")

    async def seed() -> None:
        database = Database(database_path)
        async with database.session_factory() as session:
            session.add(
                DetectionModel(
                    id=model_id,
                    name="旧模型",
                    filename="legacy.pt",
                    relative_path=f"models/{model_id}/model.pt",
                    sha256="0" * 64,
                    size=source.stat().st_size,
                    status="ready",
                    class_names_json='{"0":"text"}',
                    framework_version="test",
                    task_name="detect",
                    storage_scope="data",
                    origin="uploaded",
                )
            )
            await session.commit()
        await database.dispose()

    asyncio.run(seed())
    copied, failures = asyncio.run(migrate_yolo(settings))

    assert failures == []
    assert len(copied) == 1
    destination = next(settings.yolo_models_dir.glob("legacy-model_*.pt"))
    assert destination.read_bytes() == source.read_bytes()
    assert source.is_file()

    async def read_model() -> tuple[str, str, str]:
        database = Database(database_path)
        async with database.session_factory() as session:
            model = await session.get(DetectionModel, model_id)
            assert model is not None
            values = {
                "storage_scope": model.storage_scope,
                "origin": model.origin,
                "relative_path": model.relative_path,
            }
        await database.dispose()
        return values["storage_scope"], values["origin"], values["relative_path"]

    storage_scope, origin, relative_path = asyncio.run(read_model())
    assert storage_scope == "project"
    assert origin == "migrated"
    assert relative_path.startswith("yolo/")
