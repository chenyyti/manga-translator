from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from app.core.config import get_settings
from app.providers.ocr.local_runtime import LocalOCRRuntime


async def run(provider: str, language: str, image: Path, device: str) -> None:
    settings = get_settings()
    runtime = LocalOCRRuntime(settings.ocr_models_dir)
    try:
        result = await runtime.recognize(
            provider, image.resolve(), language=language, device=device
        )
        print(f"Provider: {provider}")
        print(f"Device: {result.actual_device}")
        print(f"Confidence: {result.confidence if result.confidence is not None else 'N/A'}")
        print(f"Text: {result.text}")
        if not result.text.strip():
            raise RuntimeError("OCR 返回了空文本")
    finally:
        await runtime.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="使用已准备模型执行真实 OCR 样本验收")
    parser.add_argument("provider", choices=("mangaocr", "paddleocr"))
    parser.add_argument("language", choices=("ja", "ko", "en"))
    parser.add_argument("image", type=Path)
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    args = parser.parse_args()
    if args.provider == "mangaocr" and args.language != "ja":
        parser.error("MangaOCR 仅支持 ja")
    if args.provider == "paddleocr" and args.language not in {"ko", "en"}:
        parser.error("PaddleOCR 仅支持 ko/en")
    if not args.image.is_file():
        parser.error("样本图片不存在")
    asyncio.run(run(args.provider, args.language, args.image, args.device))


if __name__ == "__main__":
    main()
