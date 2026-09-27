from __future__ import annotations

import argparse
import json
import math
import statistics
import time
from pathlib import Path

from PIL import Image

from app.services.rendering import (
    LayoutOptions,
    exif_oriented,
    find_system_font,
    render_region_text,
    render_region_text_layer,
)


def _load_regions(path: Path) -> list[tuple[str, tuple[float, float, float, float]]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise ValueError("regions JSON 必须是非空数组")
    result = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("regions JSON 项必须是对象")
        text = str(item.get("text") or item.get("target_text") or "")
        result.append(
            (
                text,
                (
                    float(item["x1"]),
                    float(item["y1"]),
                    float(item["x2"]),
                    float(item["y2"]),
                ),
            )
        )
    return result


def _grid_regions(
    size: tuple[int, int], count: int
) -> list[tuple[str, tuple[float, float, float, float]]]:
    columns = max(1, round(math.sqrt(count * size[0] / size[1])))
    rows = math.ceil(count / columns)
    cell_width = size[0] / columns
    cell_height = size[1] / rows
    result = []
    for index in range(count):
        column, row = index % columns, index // columns
        inset_x, inset_y = cell_width * 0.12, cell_height * 0.12
        result.append(
            (
                f"基准译文{index + 1}",
                (
                    column * cell_width + inset_x,
                    row * cell_height + inset_y,
                    (column + 1) * cell_width - inset_x,
                    (row + 1) * cell_height - inset_y,
                ),
            )
        )
    return result


def _legacy(
    source: Image.Image,
    regions: list[tuple[str, tuple[float, float, float, float]]],
    font: Path,
    options: LayoutOptions,
) -> tuple[Image.Image, float]:
    started = time.perf_counter()
    image = source
    for text, bounds in regions:
        image, _ = render_region_text(image, text, bounds, font, options)
    return image, time.perf_counter() - started


def _shared_canvas(
    source: Image.Image,
    regions: list[tuple[str, tuple[float, float, float, float]]],
    font: Path,
    options: LayoutOptions,
) -> tuple[Image.Image, float]:
    started = time.perf_counter()
    canvas = source.convert("RGBA")
    for text, bounds in regions:
        layer, origin, _ = render_region_text_layer(
            source.size, text, bounds, font, options
        )
        if layer is not None:
            canvas.alpha_composite(layer, origin)
    return canvas.convert("RGB"), time.perf_counter() - started


def main() -> None:
    parser = argparse.ArgumentParser(description="比较逐区域整页合成与共享 RGBA 画布")
    parser.add_argument("image", type=Path)
    parser.add_argument("regions", type=Path, nargs="?", help="包含 text/x1/y1/x2/y2 的 JSON 数组")
    parser.add_argument("--font", type=Path)
    parser.add_argument("--grid", type=int, help="不用区域文件，生成指定数量的均匀测试框")
    parser.add_argument("--iterations", type=int, default=5)
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("--iterations 必须大于 0")
    if not args.image.is_file():
        parser.error("图片不存在")
    if args.grid is None and (args.regions is None or not args.regions.is_file()):
        parser.error("区域 JSON 不存在；也可以使用 --grid 20")
    if args.grid is not None and args.grid < 1:
        parser.error("--grid 必须大于 0")
    font = args.font or find_system_font("msyh.ttc")
    if font is None or not font.is_file():
        parser.error("找不到可用字体，请通过 --font 指定")

    source = exif_oriented(args.image)
    regions = _grid_regions(source.size, args.grid) if args.grid else _load_regions(args.regions)
    options = LayoutOptions()
    _shared_canvas(source, regions, font, options)  # warm font cache
    legacy_times: list[float] = []
    shared_times: list[float] = []
    for _ in range(args.iterations):
        legacy, legacy_time = _legacy(source, regions, font, options)
        shared, shared_time = _shared_canvas(source, regions, font, options)
        if legacy.tobytes() != shared.tobytes():
            raise RuntimeError("共享画布输出与逐区域基线不一致")
        legacy_times.append(legacy_time)
        shared_times.append(shared_time)

    legacy_median = statistics.median(legacy_times)
    shared_median = statistics.median(shared_times)
    print(
        json.dumps(
            {
                "regions": len(regions),
                "iterations": args.iterations,
                "legacy_median_seconds": round(legacy_median, 4),
                "shared_canvas_median_seconds": round(shared_median, 4),
                "speedup": round(legacy_median / shared_median, 2),
                "pixels_match": True,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
