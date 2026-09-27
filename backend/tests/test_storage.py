from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.errors import AppError
from app.services.storage import natural_sort_key, normalize_relative_path, resolve_within


def test_default_data_directory_has_single_application_segment(monkeypatch) -> None:
    monkeypatch.delenv("MANGA_TRANSLATOR_DATA_DIR", raising=False)
    data_dir = Settings(_env_file=None).data_dir
    assert data_dir.name == "MangaTranslator"
    assert data_dir.parent.name != "MangaTranslator"


def test_natural_sort_preserves_manga_page_order() -> None:
    names = ["第10页.png", "第2页.png", "chapter/001.png", "第1页.png"]
    assert sorted(names, key=natural_sort_key) == [
        "chapter/001.png",
        "第1页.png",
        "第2页.png",
        "第10页.png",
    ]


@pytest.mark.parametrize("value", ["../escape.png", "C:/escape.png", "/escape.png", "a/../b.png"])
def test_unsafe_relative_paths_are_rejected(value: str) -> None:
    with pytest.raises(AppError):
        normalize_relative_path(value)


def test_resolve_within_rejects_parent_escape(tmp_path: Path) -> None:
    with pytest.raises(AppError):
        resolve_within(tmp_path / "root", "../outside")
