from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.errors import UnsafeArchiveError
from app.services.archive import extract_archive_images
from tests.helpers import image_bytes


def test_zip_slip_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("../outside.png", image_bytes())
    with pytest.raises(UnsafeArchiveError):
        extract_archive_images(archive, tmp_path / "extract", Settings(data_dir=tmp_path))


def test_archive_images_are_naturally_sorted(tmp_path: Path) -> None:
    archive = tmp_path / "pages.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("10.png", image_bytes(color="black"))
        output.writestr("2.png", image_bytes())
        output.writestr("notes.txt", "ignored")
    pages = extract_archive_images(archive, tmp_path / "extract", Settings(data_dir=tmp_path))
    assert [page.relative_path for page in pages] == ["2.png", "10.png"]
