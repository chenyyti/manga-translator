from __future__ import annotations

import asyncio
import math
import os
import queue
import threading
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from app.providers.ocr.base import OCRImageInput


class OCRCropError(RuntimeError):
    pass


@dataclass(frozen=True)
class OCRCropRequest:
    region_id: str
    bounds: tuple[float, float, float, float]


@dataclass(frozen=True)
class OCRCropFailure:
    region_id: str
    message: str


@dataclass(frozen=True)
class OCRCropBatch:
    images: tuple[OCRImageInput, ...]
    failures: tuple[OCRCropFailure, ...]


@dataclass(frozen=True)
class OCRCropStreamEnd:
    preprocessing_ms: int


def _crop_bounds(
    image: Image.Image, bounds: tuple[float, float, float, float]
) -> tuple[int, int, int, int]:
    x1 = max(0, min(image.width, math.floor(bounds[0])))
    y1 = max(0, min(image.height, math.floor(bounds[1])))
    x2 = max(0, min(image.width, math.ceil(bounds[2])))
    y2 = max(0, min(image.height, math.ceil(bounds[3])))
    if x2 <= x1 or y2 <= y1:
        raise OCRCropError("OCR 区域尺寸无效")
    return x1, y1, x2, y2


async def stream_ocr_crop_batches(
    source: Path,
    requests: Sequence[OCRCropRequest],
    *,
    batch_size: int,
) -> AsyncIterator[OCRCropBatch | OCRCropStreamEnd]:
    """Decode a page once and prefetch at most one in-memory crop batch."""

    size = max(1, batch_size)
    pending: queue.Queue[OCRCropBatch | OCRCropStreamEnd | BaseException] = queue.Queue(
        maxsize=1
    )
    stopped = threading.Event()

    def publish(value: OCRCropBatch | OCRCropStreamEnd | BaseException) -> bool:
        while not stopped.is_set():
            try:
                pending.put(value, timeout=0.1)
                return True
            except queue.Full:
                continue
        return False

    def produce() -> None:
        processing_seconds = 0.0
        try:
            started = time.perf_counter()
            with Image.open(source) as opened:
                page = ImageOps.exif_transpose(opened).convert("RGB")
            processing_seconds += time.perf_counter() - started
            for offset in range(0, len(requests), size):
                images: list[OCRImageInput] = []
                failures: list[OCRCropFailure] = []
                started = time.perf_counter()
                for request in requests[offset : offset + size]:
                    try:
                        crop = page.crop(_crop_bounds(page, request.bounds))
                        images.append(
                            OCRImageInput(
                                region_id=request.region_id,
                                width=crop.width,
                                height=crop.height,
                                rgb=crop.tobytes(),
                            )
                        )
                    except (OCRCropError, OSError, ValueError):
                        failures.append(
                            OCRCropFailure(request.region_id, "无法生成 OCR 区域图")
                        )
                processing_seconds += time.perf_counter() - started
                if not publish(OCRCropBatch(tuple(images), tuple(failures))):
                    return
            publish(OCRCropStreamEnd(round(processing_seconds * 1000)))
        except (UnidentifiedImageError, OSError, ValueError):
            publish(OCRCropError("无法生成 OCR 区域图"))

    producer = asyncio.create_task(asyncio.to_thread(produce))
    try:
        while True:
            value = await asyncio.to_thread(pending.get)
            if isinstance(value, BaseException):
                raise value
            yield value
            if isinstance(value, OCRCropStreamEnd):
                break
    finally:
        stopped.set()
        await producer


def create_ocr_crop(
    source: Path,
    destination: Path,
    bounds: tuple[float, float, float, float],
) -> tuple[int, int]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    part_path = destination.with_name(f"{destination.name}.part")
    try:
        with Image.open(source) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB")
            crop = image.crop(_crop_bounds(image, bounds))
            crop.save(part_path, "PNG", optimize=True)
        os.replace(part_path, destination)
        return crop.size
    except OCRCropError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise OCRCropError("无法生成 OCR 裁剪图") from exc
    finally:
        part_path.unlink(missing_ok=True)
