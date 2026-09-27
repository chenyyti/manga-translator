from pathlib import Path

import pytest
from PIL import Image

from app.services.ocr_images import (
    OCRCropRequest,
    OCRCropStreamEnd,
    create_ocr_crop,
    stream_ocr_crop_batches,
)


def test_ocr_crop_uses_oriented_full_resolution_and_is_atomic(tmp_path: Path) -> None:
    source = tmp_path / "source.jpg"
    destination = tmp_path / "crops" / "region.png"
    image = Image.new("RGB", (20, 40), "white")
    exif = image.getexif()
    exif[274] = 6
    image.save(source, exif=exif)

    size = create_ocr_crop(source, destination, (0, 0, 12, 18))

    assert size == (12, 18)
    assert destination.is_file()
    assert Image.open(destination).size == size
    assert not destination.with_name("region.png.part").exists()


@pytest.mark.asyncio
async def test_page_crop_stream_decodes_once_and_keeps_crops_in_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.png"
    Image.new("RGB", (80, 40), (10, 20, 30)).save(source)
    opened = 0
    original_open = Image.open

    def counted_open(*args, **kwargs):
        nonlocal opened
        opened += 1
        return original_open(*args, **kwargs)

    monkeypatch.setattr("app.services.ocr_images.Image.open", counted_open)
    requests = [
        OCRCropRequest(f"r{index}", (index * 10, 0, index * 10 + 8, 12))
        for index in range(5)
    ]
    batches = []
    end = None
    async for value in stream_ocr_crop_batches(source, requests, batch_size=2):
        if isinstance(value, OCRCropStreamEnd):
            end = value
        else:
            batches.append(value)

    assert opened == 1
    assert [len(value.images) for value in batches] == [2, 2, 1]
    assert [item.region_id for value in batches for item in value.images] == [
        "r0",
        "r1",
        "r2",
        "r3",
        "r4",
    ]
    assert all(item.rgb[:3] == bytes((10, 20, 30)) for value in batches for item in value.images)
    assert end is not None
    assert list(tmp_path.rglob("*.part")) == []
