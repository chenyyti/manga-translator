from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from app.providers.detection.ultralytics_provider import UltralyticsDetectionRuntime
from app.services.storage import SUPPORTED_IMAGE_SUFFIXES, natural_sort_key


def sample_images(directory: Path) -> list[Path]:
    candidates = (
        path for path in directory.rglob("*") if path.suffix.casefold() in SUPPORTED_IMAGE_SUFFIXES
    )
    images = sorted(
        candidates,
        key=lambda path: natural_sort_key(path.relative_to(directory).as_posix()),
    )
    if len(images) <= 5:
        return images
    indexes = {0, 1, len(images) // 2, len(images) - 2, len(images) - 1}
    return [images[index] for index in sorted(indexes)]


async def run(model: Path, images: Path, device: str) -> None:
    runtime = UltralyticsDetectionRuntime()
    try:
        started = time.perf_counter()
        info = await runtime.inspect(model)
        results: list[dict[str, object]] = []
        for image in sample_images(images):
            page_started = time.perf_counter()
            prediction = await runtime.predict(
                model,
                image,
                confidence=0.25,
                image_size=1280,
                device=device,
            )
            results.append(
                {
                    "file": image.name,
                    "regions": len(prediction.regions),
                    "device": prediction.actual_device,
                    "seconds": round(time.perf_counter() - page_started, 3),
                }
            )
        report = {
            "model": model.name,
            "classes": info.class_names,
            "framework": info.framework_version,
            "task": info.task_name,
            "total_seconds": round(time.perf_counter() - started, 3),
            "samples": results,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        await runtime.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("model", type=Path)
    parser.add_argument("images", type=Path)
    parser.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto")
    args = parser.parse_args()
    asyncio.run(run(args.model.resolve(), args.images.resolve(), args.device))


if __name__ == "__main__":
    main()
