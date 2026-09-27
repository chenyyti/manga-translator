from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


@pytest.fixture
def test_settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "manga-data",
        project_root=tmp_path / "project",
        thumbnail_size=120,
        preview_size=800,
        max_import_bytes=50 * 1024 * 1024,
    )


@pytest.fixture
def client(test_settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(test_settings)) as test_client:
        yield test_client
