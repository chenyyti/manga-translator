from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import tempfile
import time
from pathlib import Path

from app.core.config import get_settings
from app.providers.ocr.local_runtime import LocalOCRRuntime
from app.services.ocr_images import (
    OCRCropRequest,
    OCRCropStreamEnd,
    create_ocr_crop,
    stream_ocr_crop_batches,
)


def _outputs_match(
    legacy: list[tuple[str, float | None]], batched: list[tuple[str, float | None]]
) -> bool:
    if len(legacy) != len(batched):
        return False
    for (legacy_text, legacy_score), (batch_text, batch_score) in zip(
        legacy, batched, strict=True
    ):
        if legacy_text != batch_text:
            return False
        if legacy_score is None or batch_score is None:
            if legacy_score is not batch_score:
                return False
        elif abs(legacy_score - batch_score) > 1e-3:
            return False
    return True


def _load_regions(path: Path) -> list[OCRCropRequest]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("regions JSON 必须是数组")
    result: list[OCRCropRequest] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError("regions JSON 项必须是对象")
        region_id = str(item.get("id") or item.get("region_id") or f"region-{index}")
        result.append(
            OCRCropRequest(
                region_id,
                (
                    float(item["x1"]),
                    float(item["y1"]),
                    float(item["x2"]),
                    float(item["y2"]),
                ),
            )
        )
    if not result:
        raise ValueError("regions JSON 不能为空")
    return result


async def _legacy_once(
    runtime: LocalOCRRuntime,
    provider: str,
    language: str,
    device: str,
    image: Path,
    regions: list[OCRCropRequest],
    temporary: Path,
) -> tuple[list[tuple[str, float | None]], float]:
    started = time.perf_counter()
    values: list[tuple[str, float | None]] = []
    for index, region in enumerate(regions):
        crop = temporary / f"crop-{index}.png"
        await asyncio.to_thread(create_ocr_crop, image, crop, region.bounds)
        recognition = await runtime.recognize(
            provider, crop, language=language, device=device
        )
        values.append((recognition.text, recognition.confidence))
    return values, time.perf_counter() - started


async def _batch_once(
    runtime: LocalOCRRuntime,
    provider: str,
    language: str,
    device: str,
    image: Path,
    regions: list[OCRCropRequest],
) -> tuple[list[tuple[str, float | None]], float]:
    started = time.perf_counter()
    values: list[tuple[str, float | None]] = []
    batch_size = 1 if device == "cpu" else 8
    async for batch in stream_ocr_crop_batches(image, regions, batch_size=batch_size):
        if isinstance(batch, OCRCropStreamEnd):
            break
        if batch.failures:
            raise RuntimeError(batch.failures[0].message)
        result = await runtime.recognize_batch(
            provider,
            batch.images,
            language=language,
            device=device,
            batch_size=batch_size,
        )
        for item in result.items:
            if item.recognition is None:
                raise RuntimeError(item.error or "OCR 失败")
            values.append((item.recognition.text, item.recognition.confidence))
    return values, time.perf_counter() - started


async def run(args: argparse.Namespace) -> None:
    settings = get_settings()
    runtime = LocalOCRRuntime(settings.ocr_models_dir)
    regions = _load_regions(args.regions)
    legacy_times: list[float] = []
    batch_times: list[float] = []
    try:
        with tempfile.TemporaryDirectory(prefix="manga-ocr-benchmark-") as directory:
            temporary = Path(directory)
            # Warm both model and CUDA context before measuring.
            warm_crop = temporary / "warm.png"
            await asyncio.to_thread(
                create_ocr_crop, args.image, warm_crop, regions[0].bounds
            )
            await runtime.recognize(
                args.provider,
                warm_crop,
                language=args.language,
                device=args.device,
            )
            for _ in range(args.iterations):
                legacy, legacy_time = await _legacy_once(
                    runtime,
                    args.provider,
                    args.language,
                    args.device,
                    args.image,
                    regions,
                    temporary,
                )
                batched, batch_time = await _batch_once(
                    runtime,
                    args.provider,
                    args.language,
                    args.device,
                    args.image,
                    regions,
                )
                if not _outputs_match(legacy, batched):
                    raise RuntimeError(
                        "批量 OCR 结果与逐图基线不一致："
                        + json.dumps(
                            {"legacy": legacy, "batched": batched}, ensure_ascii=False
                        )
                    )
                legacy_times.append(legacy_time)
                batch_times.append(batch_time)
    finally:
        await runtime.close()
    legacy_median = statistics.median(legacy_times)
    batch_median = statistics.median(batch_times)
    print(
        json.dumps(
            {
                "regions": len(regions),
                "iterations": args.iterations,
                "legacy_median_seconds": round(legacy_median, 3),
                "batch_median_seconds": round(batch_median, 3),
                "speedup": round(legacy_median / batch_median, 2),
                "outputs_match": True,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="比较逐图 OCR 与页面内存批量 OCR")
    parser.add_argument("provider", choices=("mangaocr", "paddleocr"))
    parser.add_argument("language", choices=("ja", "ko", "en"))
    parser.add_argument("image", type=Path)
    parser.add_argument("regions", type=Path, help="包含 id/x1/y1/x2/y2 的 JSON 数组")
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    parser.add_argument("--iterations", type=int, default=3)
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("--iterations 必须大于 0")
    if not args.image.is_file() or not args.regions.is_file():
        parser.error("图片或区域 JSON 不存在")
    if args.provider == "mangaocr" and args.language != "ja":
        parser.error("MangaOCR 仅支持 ja")
    if args.provider == "paddleocr" and args.language not in {"ko", "en"}:
        parser.error("PaddleOCR 仅支持 ko/en")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
