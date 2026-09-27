from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from app.services.images import (
    ImageValidationError,
    generate_derivatives,
    generate_rendered_thumbnail_from_image,
)


def test_generates_bounded_webp_derivatives(tmp_path: Path) -> None:
    source = tmp_path / "page.png"
    Image.new("RGB", (1600, 2400), "white").save(source)
    result = generate_derivatives(
        source,
        tmp_path / "thumb.webp",
        tmp_path / "preview.webp",
        thumbnail_size=220,
        preview_size=1200,
    )
    assert (result.width, result.height) == (1600, 2400)
    assert max(result.preview_width, result.preview_height) == 1200
    assert max(result.thumbnail_width, result.thumbnail_height) == 220
    assert len(result.sha256) == 64


def test_corrupt_image_is_reported(tmp_path: Path) -> None:
    source = tmp_path / "broken.png"
    source.write_bytes(b"not an image")
    with pytest.raises(ImageValidationError, match="无法解码"):
        generate_derivatives(
            source,
            tmp_path / "thumb.webp",
            tmp_path / "preview.webp",
            thumbnail_size=220,
            preview_size=1200,
        )


def test_generates_rendered_thumbnail_from_memory(tmp_path: Path) -> None:
    destination = tmp_path / "rendered.webp"
    dimensions = generate_rendered_thumbnail_from_image(
        Image.new("RGB", (1200, 800), "white"),
        destination,
        thumbnail_size=240,
    )
    assert dimensions == (240, 160)
    with Image.open(destination) as thumbnail:
        assert thumbnail.size == dimensions
