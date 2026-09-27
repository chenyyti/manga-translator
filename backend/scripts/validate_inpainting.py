from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

from app.core.config import get_settings
from app.providers.inpainting.local_runtime import LocalInpaintingRuntime


def main() -> None:
    parser = argparse.ArgumentParser(description="验证本地 Phase 6 图像修复运行时")
    parser.add_argument("--provider", choices=("fast", "opencv", "lama"), default="fast")
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    args = parser.parse_args()
    settings = get_settings()
    settings.ensure_directories()
    with tempfile.TemporaryDirectory(prefix="manga-render-check-") as temp:
        root = Path(temp)
        image_path = root / "source.png"
        mask_path = root / "mask.png"
        output_path = root / "output.png"
        image = Image.new("RGB", (320, 240), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((80, 70, 240, 170), fill="#d44")
        image.save(image_path)
        mask = Image.new("L", image.size, 0)
        ImageDraw.Draw(mask).rectangle((100, 90, 220, 150), fill=255)
        mask.save(mask_path)
        import asyncio

        async def run() -> None:
            runtime = LocalInpaintingRuntime(settings.inpainting_models_dir)
            try:
                infos = await runtime.providers()
                print([item.__dict__ for item in infos])
                result = await runtime.inpaint(
                    args.provider,
                    image_path,
                    mask_path,
                    output_path,
                    device=args.device,
                    max_edge=2048,
                    opencv_radius=3,
                )
                with Image.open(output_path) as checked:
                    print(result, "size=", checked.size)
            finally:
                await runtime.close()

        asyncio.run(run())


if __name__ == "__main__":
    main()
