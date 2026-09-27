from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.core.config import get_settings
from app.providers.ocr.local_runtime import LocalOCRRuntime

SAMPLES = (
    ("mangaocr", "ja", "こんにちは世界", Path(r"C:\Windows\Fonts\msgothic.ttc")),
    ("paddleocr", "ko", "안녕하세요 세계", Path(r"C:\Windows\Fonts\malgun.ttf")),
    ("paddleocr", "en", "Hello world", Path(r"C:\Windows\Fonts\arial.ttf")),
)


def _sample(path: Path, text: str, font_path: Path) -> None:
    if not font_path.is_file():
        raise RuntimeError(f"验收字体不存在：{font_path}")
    image = Image.new("RGB", (720, 150), "white")
    draw = ImageDraw.Draw(image)
    draw.text((24, 25), text, fill="black", font=ImageFont.truetype(str(font_path), 64))
    image.save(path, format="PNG")


async def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    settings = get_settings()
    runtime = LocalOCRRuntime(settings.ocr_models_dir)
    try:
        with tempfile.TemporaryDirectory(prefix="manga-ocr-acceptance-") as temporary:
            root = Path(temporary)
            for index, (provider, language, expected, font) in enumerate(SAMPLES):
                image_path = root / f"{language}-{index}.png"
                _sample(image_path, expected, font)
                result = await runtime.recognize(
                    provider,
                    image_path,
                    language=language,
                    device="auto",
                )
                if not result.text.strip():
                    raise RuntimeError(f"{provider}/{language} 返回了空文本")
                confidence = result.confidence if result.confidence is not None else "N/A"
                print(
                    f"{provider}/{language}: device={result.actual_device}, "
                    f"confidence={confidence}, text={result.text}"
                )
    finally:
        await runtime.close()


if __name__ == "__main__":
    asyncio.run(main())
