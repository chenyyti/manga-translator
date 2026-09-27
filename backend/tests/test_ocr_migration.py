from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic.config import Config

from alembic import command
from app.db.session import run_migrations


def _alembic_config(database_path: Path) -> Config:
    backend_root = Path(__file__).resolve().parents[1]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")
    return config


def test_fixed_ocr_routing_migrates_existing_projects_and_settings(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy.db"
    config = _alembic_config(database_path)
    command.upgrade(config, "0010_project_translation_mode")

    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            INSERT INTO projects (
                id, name, source_language, target_language, status, total_pages,
                workspace_path, ocr_provider, translation_mode, created_at, updated_at
            ) VALUES
                ('project-ja', '日文项目', 'ja', 'zh-CN', 'ready', 0,
                 'workspace/project-ja', 'paddleocr', 'quick', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
                ('project-ko', '韩文项目', 'ko', 'zh-CN', 'ready', 0,
                 'workspace/project-ko', 'auto', 'quick', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
                ('project-en', '英文项目', 'en', 'zh-CN', 'ready', 0,
                 'workspace/project-en', 'mangaocr', 'quick', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
                ('project-other', '其他项目', 'fr', 'zh-CN', 'ready', 0,
                 'workspace/project-other', 'paddleocr', 'quick', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);

            INSERT INTO import_sessions (
                id, project_name, source_language, target_language, source_type, status,
                total_bytes, expires_at, ocr_provider, translation_mode, created_at, updated_at
            ) VALUES
                ('session-ja', '日文项目', 'ja', 'zh-CN', 'single', 'committed',
                 0, CURRENT_TIMESTAMP, 'paddleocr', 'quick', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
                ('session-ko', '韩文项目', 'ko', 'zh-CN', 'single', 'committed',
                 0, CURRENT_TIMESTAMP, 'auto', 'quick', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
                ('session-en', '英文项目', 'en', 'zh-CN', 'single', 'committed',
                 0, CURRENT_TIMESTAMP, 'mangaocr', 'quick', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
                ('session-other', '其他项目', 'fr', 'zh-CN', 'single', 'committed',
                 0, CURRENT_TIMESTAMP, 'paddleocr', 'quick', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);

            INSERT INTO ocr_settings (
                id, japanese_provider, korean_provider, english_provider, device,
                created_at, updated_at
            ) VALUES (1, 'auto', 'mangaocr', 'auto', 'cuda', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
            """
        )

    run_migrations(database_path)

    with sqlite3.connect(database_path) as connection:
        projects = dict(
            connection.execute(
                "SELECT id, ocr_provider FROM projects ORDER BY id"
            ).fetchall()
        )
        sessions = dict(
            connection.execute(
                "SELECT id, ocr_provider FROM import_sessions ORDER BY id"
            ).fetchall()
        )
        settings = connection.execute(
            "SELECT japanese_provider, korean_provider, english_provider, device "
            "FROM ocr_settings WHERE id = 1"
        ).fetchone()

    assert projects == {
        "project-en": "paddleocr",
        "project-ja": "mangaocr",
        "project-ko": "paddleocr",
        "project-other": "auto",
    }
    assert sessions == {
        "session-en": "paddleocr",
        "session-ja": "mangaocr",
        "session-ko": "paddleocr",
        "session-other": "auto",
    }
    assert settings == ("mangaocr", "paddleocr", "paddleocr", "cuda")
