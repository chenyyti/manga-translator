from __future__ import annotations

import math
import os
import re
import statistics
import unicodedata
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps, ImageStat


class LayoutError(RuntimeError):
    code = "LAYOUT_FAILED"


class LayoutOverflowError(LayoutError):
    code = "LAYOUT_TEXT_OVERFLOW"


@dataclass(frozen=True)
class MaskRegion:
    region_id: str
    bounds: tuple[float, float, float, float]
    preserve: bool = False


@dataclass(frozen=True)
class BackgroundComplexity:
    standard_deviation: float
    edge_density: float


@dataclass(frozen=True)
class LayoutOptions:
    font_size: int = 36
    auto_font_size: bool = True
    min_font_size: int = 12
    max_font_size: int = 96
    margin_ratio: float = 0.08
    font_color: str = "#000000"
    stroke_color: str = "#FFFFFF"
    stroke_width: int = 0
    orientation: str = "auto"
    rotation_degrees: float = 0.0


@dataclass(frozen=True)
class MaskBuildResult:
    mask: Image.Image
    regions: dict[str, tuple[int, int, int, int]]


def _relative_luminance(color: tuple[int, int, int]) -> float:
    channels = []
    for value in color:
        channel = value / 255
        channels.append(
            channel / 12.92
            if channel <= 0.04045
            else ((channel + 0.055) / 1.055) ** 2.4
        )
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def auto_contrast_layout_options(
    image: Image.Image,
    bounds: tuple[float, float, float, float],
    options: LayoutOptions,
) -> LayoutOptions:
    """Flip monochrome text to black/white according to the repaired background.

    Custom colors and outlined text remain explicit user choices. Sampling the
    median makes the decision robust to residual glyph pixels and comic texture.
    """

    if options.stroke_width > 0 or options.font_color.upper() not in {"#000000", "#FFFFFF"}:
        return options
    clamped = _clamp_bounds(bounds, image.size, 0)
    sample = image.crop(clamped).convert("RGB")
    sample.thumbnail((64, 64), Image.Resampling.BILINEAR)
    median = tuple(int(value) for value in ImageStat.Stat(sample).median[:3])
    background = _relative_luminance(median)
    black_contrast = (background + 0.05) / 0.05
    white_contrast = 1.05 / (background + 0.05)
    selected = "#000000" if black_contrast >= white_contrast else "#FFFFFF"
    if selected == options.font_color.upper():
        return options
    return replace(options, font_color=selected)


def find_system_font(filename: str, *, windir: str | Path | None = None) -> Path | None:
    """Resolve a usable Windows font without baking an absolute path in code.

    Microsoft YaHei is the preferred default, followed by SimHei and then the
    first loadable font in the Windows Fonts directory.  The fallback keeps
    rendering usable on stripped-down Windows images where one of the
    well-known files was removed.
    """

    windir_value = str(windir) if windir is not None else os.environ.get("WINDIR")
    if not windir_value:
        return None
    root = Path(windir_value)
    fonts_dir = root / "Fonts"
    requested = Path(filename).name
    candidates: list[Path] = [fonts_dir / requested]
    if requested.casefold() == "msyh.ttc":
        candidates.append(fonts_dir / "simhei.ttf")
    elif requested.casefold() == "simhei.ttf":
        candidates.append(fonts_dir / "msyh.ttc")
    try:
        candidates.extend(
            sorted(
                item
                for item in fonts_dir.iterdir()
                if item.suffix.casefold() in {".ttf", ".otf", ".ttc"}
            )
        )
    except OSError:
        return None
    seen: set[Path] = set()
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate in seen or not candidate.is_file():
            continue
        seen.add(candidate)
        try:
            ImageFont.truetype(str(candidate), size=12)
        except (OSError, ValueError):
            continue
        return candidate
    return None


def _clamp_bounds(
    bounds: tuple[float, float, float, float], size: tuple[int, int], padding_ratio: float
) -> tuple[int, int, int, int]:
    width, height = size
    x1, y1, x2, y2 = bounds
    short_side = max(1.0, min(abs(x2 - x1), abs(y2 - y1)))
    padding = max(0, round(short_side * padding_ratio))
    left = max(0, min(width, math.floor(min(x1, x2) - padding)))
    top = max(0, min(height, math.floor(min(y1, y2) - padding)))
    right = max(0, min(width, math.ceil(max(x1, x2) + padding)))
    bottom = max(0, min(height, math.ceil(max(y1, y2) + padding)))
    if right <= left or bottom <= top:
        raise ValueError("区域框尺寸无效")
    return left, top, right, bottom


def build_page_mask(
    size: tuple[int, int], regions: list[MaskRegion], padding_ratio: float, dilation_px: int,
    *, source: Image.Image | None = None,
) -> MaskBuildResult:
    """Build one deterministic union mask without changing the source image."""

    if source is not None:
        if source.size != size:
            raise ValueError("原图与掩膜尺寸不一致")
        mask = Image.new("L", size, 0)
        actual = {}
        for region in regions:
            if region.preserve:
                continue
            bounds = _clamp_bounds(region.bounds, size, 0)
            patch, origin = _text_pixel_mask(source, bounds, padding_ratio, dilation_px)
            layer = Image.new("L", size, 0)
            layer.paste(patch, origin)
            mask = ImageChops.lighter(mask, layer)
            actual[region.region_id] = bounds
        # A preserved region must also survive overlaps with another text box.
        draw = ImageDraw.Draw(mask)
        for region in regions:
            if region.preserve:
                x1, y1, x2, y2 = _clamp_bounds(region.bounds, size, 0)
                draw.rectangle((x1, y1, x2 - 1, y2 - 1), fill=0)
        return MaskBuildResult(mask=mask, regions=actual)

    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    actual: dict[str, tuple[int, int, int, int]] = {}
    for region in regions:
        if region.preserve:
            continue
        bounds = _clamp_bounds(region.bounds, size, padding_ratio)
        actual[region.region_id] = bounds
        # Detection boxes use the usual half-open ``(x1, y1, x2, y2)``
        # convention. Pillow's rectangle endpoint is inclusive, therefore
        # subtract one from the lower/right edge so the mask does not grow by
        # an unintended pixel before the configured dilation is applied.
        left, top, right, bottom = bounds
        draw.rectangle((left, top, max(left, right - 1), max(top, bottom - 1)), fill=255)
    if dilation_px > 0 and actual:
        # MaxFilter's kernel must be odd.  The kernel is intentionally capped
        # so a malformed setting cannot expand a page-wide mask unexpectedly.
        kernel = max(3, min(129, dilation_px * 2 + 1))
        if kernel % 2 == 0:
            kernel += 1
        mask = mask.filter(ImageFilter.MaxFilter(kernel))
    return MaskBuildResult(mask=mask, regions=actual)


def _text_pixel_mask(
    source: Image.Image, bounds: tuple[int, int, int, int],
    padding_ratio: float, dilation_px: int,
) -> tuple[Image.Image, tuple[int, int]]:
    """Extract ink on flat backgrounds; use context to exclude crossing outlines.

    Padding expands the search area, not the erased rectangle. Unsupported or
    textured backgrounds fall back to the original box without outward growth.
    """
    x1, y1, x2, y2 = bounds
    fallback = (Image.new("L", (x2 - x1, y2 - y1), 255), (x1, y1))
    try:
        import cv2
        import numpy as np
    except ImportError:
        return fallback
    short = min(x2 - x1, y2 - y1)
    pad = max(0, round(short * padding_ratio))
    radius = max(0, min(2, dilation_px))
    context = max(8, pad + radius + 4, round(short * 0.18))
    left, top = max(0, x1 - context), max(0, y1 - context)
    right, bottom = min(source.width, x2 + context), min(source.height, y2 + context)
    # Crop before converting. Converting the full page once per region made
    # content-aware masks scale with page pixels * region count.
    rgb = np.asarray(source.crop((left, top, right, bottom)).convert("RGB"))
    ax, ay, bx, by = x1 - left, y1 - top, x2 - left, y2 - top
    core = rgb[ay:by, ax:bx]
    # Find the dominant RGB cluster, supporting both dark and light balloons.
    bins = core.astype(np.int32) // 24
    codes = bins[..., 0] * 121 + bins[..., 1] * 11 + bins[..., 2]
    dominant = np.bincount(codes.ravel()).argmax()
    background = np.median(core[codes == dominant], axis=0)
    distance = np.max(np.abs(rgb.astype(np.float32) - background), axis=2)
    if np.mean(distance[ay:by, ax:bx] <= 24) < 0.55:
        return fallback
    ink = (distance > 32).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(ink, 8)
    selected = np.zeros_like(ink)
    guard = np.zeros_like(ink)
    for label in range(1, count):
        xx, yy, ww, hh, area = stats[label]
        component = labels == label
        inside = int(np.count_nonzero(component[ay:by, ax:bx]))
        crosses_context = xx == 0 or yy == 0 or xx + ww == ink.shape[1] or yy + hh == ink.shape[0]
        long_line = (ww > (x2 - x1) * 0.85 and ww > hh * 5) or (hh > (y2 - y1) * 0.85 and hh > ww * 5)
        if crosses_context or long_line or inside < area * 0.5:
            guard[component] = 1
        elif inside and area >= 2:
            selected[component] = 1
    if radius:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (radius * 2 + 1,) * 2)
        selected = cv2.dilate(selected, kernel)
    # Protect rejected outlines after dilation too, including antialiased rims.
    guard = cv2.dilate(guard, np.ones((5, 5), np.uint8))
    selected[guard != 0] = 0
    allowed = np.zeros_like(ink)
    allowed[max(0, ay-pad):min(ink.shape[0], by+pad), max(0, ax-pad):min(ink.shape[1], bx+pad)] = 1
    selected &= allowed
    return Image.fromarray(selected * 255), (left, top)


def _gray(pixel: tuple[int, ...]) -> float:
    return 0.299 * pixel[0] + 0.587 * pixel[1] + 0.114 * pixel[2]


def measure_background_complexity(
    image: Image.Image, mask: Image.Image, bounds: tuple[int, int, int, int]
) -> BackgroundComplexity:
    """Measure unmasked pixels around a region using only a small border ring."""

    rgb = image.convert("RGB")
    mask_l = mask.convert("L")
    x1, y1, x2, y2 = bounds
    ring = max(4, min(32, round(min(x2 - x1, y2 - y1) * 0.08)))
    left, top = max(0, x1 - ring), max(0, y1 - ring)
    right, bottom = min(rgb.width, x2 + ring), min(rgb.height, y2 + ring)
    source = rgb.load()
    mask_pixels = mask_l.load()
    values: list[float] = []
    transitions = 0
    comparisons = 0
    for y in range(top, bottom):
        for x in range(left, right):
            if mask_pixels[x, y] != 0:
                continue
            value = _gray(source[x, y])
            values.append(value)
            # Compare both axes while treating the mask as a hard boundary.
            # This keeps horizontal comic panel edges from being missed by
            # the AUTO heuristic, without requiring OpenCV to be installed.
            if x + 1 < right and mask_pixels[x + 1, y] == 0:
                comparisons += 1
                if abs(value - _gray(source[x + 1, y])) >= 30:
                    transitions += 1
            if y + 1 < bottom and mask_pixels[x, y + 1] == 0:
                comparisons += 1
                if abs(value - _gray(source[x, y + 1])) >= 30:
                    transitions += 1
    if len(values) < 2:
        return BackgroundComplexity(255.0, 1.0)
    deviation = statistics.pstdev(values)
    density = transitions / comparisons if comparisons else 1.0
    return BackgroundComplexity(deviation, density)


def choose_repair_mode(
    requested: str,
    complexity: BackgroundComplexity,
    *,
    lama_ready: bool,
    opencv_ready: bool,
) -> tuple[str, str | None]:
    if requested not in {"auto", "fast", "opencv", "lama"}:
        raise ValueError("图像修复模式无效")
    if requested == "fast":
        return "fast", None
    if requested == "opencv":
        if not opencv_ready:
            raise RuntimeError("OpenCV 修复依赖尚未安装")
        return "opencv", None
    if requested == "lama":
        if lama_ready:
            return "lama", None
        if opencv_ready:
            return "opencv", "Big-LaMa 未准备，已回退 OpenCV"
        raise RuntimeError("Big-LaMa 模型尚未准备且 OpenCV 不可用")
    if complexity.standard_deviation <= 10 and complexity.edge_density <= 0.03:
        return "fast", None
    if complexity.standard_deviation <= 35 and complexity.edge_density <= 0.15:
        if opencv_ready:
            return "opencv", None
        if lama_ready:
            return "lama", "OpenCV 未安装，已回退 Big-LaMa"
        return "fast", "OpenCV/Big-LaMa 不可用，已回退 FAST"
    if lama_ready:
        return "lama", None
    if opencv_ready:
        return "opencv", "Big-LaMa 未准备，已回退 OpenCV"
    raise RuntimeError("复杂背景需要 Big-LaMa 或 OpenCV")


def _font_bbox(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, stroke: int) -> tuple[int, int]:
    box = draw.multiline_textbbox((0, 0), text, font=font, stroke_width=stroke, spacing=max(1, round(font.size * 0.18)))
    return max(1, box[2] - box[0]), max(1, box[3] - box[1])


_ASCII_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’._/-]*")


def _units(line: str) -> list[str]:
    result: list[str] = []
    cursor = 0
    for match in _ASCII_WORD.finditer(line):
        if match.start() > cursor:
            result.extend(line[cursor : match.start()])
        result.append(match.group(0))
        cursor = match.end()
    if cursor < len(line):
        result.extend(line[cursor:])
    return result


def _wrap_horizontal(
    text: str, draw: ImageDraw.ImageDraw, font: ImageFont.FreeTypeFont, max_width: int, stroke: int
) -> list[str]:
    lines: list[str] = []
    for explicit in text.split("\n"):
        current = ""
        for unit in _units(explicit):
            if _font_bbox(draw, unit, font, stroke)[0] > max_width:
                # Also split a long token at the beginning of a line.
                if current:
                    lines.append(current.rstrip())
                    current = ""
                for char in unit:
                    if current and _font_bbox(draw, current + char, font, stroke)[0] > max_width:
                        lines.append(current)
                        current = ""
                    current += char
                continue
            candidate = current + unit
            width, _ = _font_bbox(draw, candidate, font, stroke)
            if current and width > max_width:
                lines.append(current.rstrip())
                current = unit.lstrip()
                # Break a single very long Latin token by character rather
                # than allowing it to overflow the region.
                if _font_bbox(draw, current, font, stroke)[0] > max_width:
                    pieces: list[str] = []
                    for char in current:
                        piece = (pieces[-1] if pieces else "") + char
                        if pieces and _font_bbox(draw, piece, font, stroke)[0] > max_width:
                            pieces.append(char)
                        elif pieces:
                            pieces[-1] = piece
                        else:
                            pieces.append(piece)
                    lines.extend(pieces[:-1])
                    current = pieces[-1]
            else:
                current = candidate
        lines.append(current.rstrip())
    return lines or [""]


def _is_vertical_glyph(value: str) -> bool:
    if not value:
        return False
    category = unicodedata.east_asian_width(value[0])
    return category in {"W", "F", "A"} or not value[0].isascii()


def _vertical_layout(
    text: str,
    font: ImageFont.FreeTypeFont,
    options: LayoutOptions,
    bounds: tuple[int, int, int, int],
) -> tuple[list[list[str]], int, int, tuple[int, int]]:
    x1, y1, x2, y2 = bounds
    margin_x = round((x2 - x1) * options.margin_ratio)
    margin_y = round((y2 - y1) * options.margin_ratio)
    width = max(1, x2 - x1 - 2 * margin_x)
    height = max(1, y2 - y1 - 2 * margin_y)
    glyph_height = max(1, round(font.size * 1.15))
    columns: list[list[str]] = []
    for explicit in text.split("\n"):
        # Normalize ASCII ellipsis only in the vertical layout copy; OCR and
        # translation text remain unchanged. Keep punctuation runs in one column.
        explicit = re.sub(r"\.{3,}", lambda m: "…" * (len(m[0]) // 3) + "." * (len(m[0]) % 3), explicit)
        units = re.findall(r"[—–―─－-]+|[…⋯]+|.", explicit) or [""]
        capacity = max(1, height // glyph_height)
        column: list[str] = []
        for unit in units:
            if len(unit) > capacity:
                raise LayoutOverflowError("连续竖排标点超出区域高度")
            if column and len(column) + len(unit) > capacity:
                columns.append(column)
                column = []
            column.extend(unit)
        columns.append(column)
    column_width = max(1, round(font.size * 1.15))
    measured = (
        len(columns) * column_width,
        max(1, max(map(len, columns)) * glyph_height),
    )
    if measured[0] > width or glyph_height > height:
        raise LayoutOverflowError("竖排文字超出区域")
    return columns, glyph_height, column_width, measured


def _draw_vertical(
    layer: Image.Image,
    text: str,
    font: ImageFont.FreeTypeFont,
    options: LayoutOptions,
    bounds: tuple[int, int, int, int],
) -> tuple[int, int]:
    draw = ImageDraw.Draw(layer)
    x1, y1, x2, y2 = bounds
    margin_x = round((x2 - x1) * options.margin_ratio)
    margin_y = round((y2 - y1) * options.margin_ratio)
    height = max(1, y2 - y1 - 2 * margin_y)
    columns, glyph_height, column_width, measured = _vertical_layout(
        text, font, options, bounds
    )
    center_y = y1 + margin_y + height / 2
    for index, column in enumerate(columns):
        center_x = x2 - margin_x - column_width / 2 - index * column_width
        start_y = center_y - (len(column) * glyph_height) / 2 + glyph_height / 2
        for glyph_index, glyph in enumerate(column):
            center = (center_x, start_y + glyph_index * glyph_height)
            if glyph in {"—", "–", "―", "─", "－", "-"}:
                # A full advance makes consecutive dashes a continuous rule.
                thickness = max(1, round(font.size * 0.055))
                xx = round(center[0] - thickness / 2)
                yy = round(center[1] - glyph_height / 2)
                stroke = options.stroke_width
                if stroke:
                    draw.rectangle((xx-stroke, yy, xx+thickness-1+stroke, yy+glyph_height-1), fill=options.stroke_color)
                draw.rectangle((xx, yy, xx+thickness-1, yy+glyph_height-1), fill=options.font_color)
            elif glyph in {"…", "⋯"}:
                # Draw centered vertical dots independently of font fallback
                # and horizontal punctuation metrics.
                diameter = max(1, round(font.size * 0.09))
                for fraction in (-1 / 3, 0, 1 / 3):
                    xx = round(center[0] - diameter / 2)
                    yy = round(center[1] + fraction * glyph_height - diameter / 2)
                    stroke = options.stroke_width
                    if stroke:
                        draw.ellipse((xx-stroke, yy-stroke, xx+diameter-1+stroke, yy+diameter-1+stroke), fill=options.stroke_color)
                    draw.ellipse((xx, yy, xx+diameter-1, yy+diameter-1), fill=options.font_color)
            elif glyph and not _is_vertical_glyph(glyph):
                tile = Image.new("RGBA", (font.size * 3, font.size * 3), (0, 0, 0, 0))
                tile_draw = ImageDraw.Draw(tile)
                tile_draw.text(
                    (tile.width / 2, tile.height / 2),
                    glyph,
                    font=font,
                    fill=options.font_color,
                    stroke_width=options.stroke_width,
                    stroke_fill=options.stroke_color,
                    anchor="mm",
                )
                tile = tile.rotate(90, expand=True, resample=Image.Resampling.BICUBIC)
                layer.alpha_composite(tile, (round(center[0] - tile.width / 2), round(center[1] - tile.height / 2)))
            else:
                draw.text(
                    center,
                    glyph,
                    font=font,
                    fill=options.font_color,
                    stroke_width=options.stroke_width,
                    stroke_fill=options.stroke_color,
                    anchor="mm",
                )
    return measured


@lru_cache(maxsize=256)
def _cached_font(path: str, size: int, modified_ns: int) -> ImageFont.FreeTypeFont:
    # modified_ns invalidates the cache if a custom font file is replaced.
    del modified_ns
    return ImageFont.truetype(path, size=size)


def _load_font(font_path: Path, size: int) -> ImageFont.FreeTypeFont:
    try:
        modified_ns = font_path.stat().st_mtime_ns
        return _cached_font(str(font_path), size, modified_ns)
    except (OSError, ValueError) as exc:
        raise LayoutError("字体无法加载") from exc


def _render_at_size(
    text: str,
    font_path: Path,
    size: int,
    options: LayoutOptions,
    bounds: tuple[int, int, int, int],
    *,
    render: bool = True,
) -> tuple[Image.Image | None, tuple[int, int]]:
    font = _load_font(font_path, size)
    x1, y1, x2, y2 = bounds
    margin_x = round((x2 - x1) * options.margin_ratio)
    margin_y = round((y2 - y1) * options.margin_ratio)
    available = (max(1, x2 - x1 - margin_x * 2), max(1, y2 - y1 - margin_y * 2))
    layer_size = (max(1, x2 - x1), max(1, y2 - y1))
    layer = Image.new("RGBA", layer_size, (0, 0, 0, 0)) if render else None
    draw = ImageDraw.Draw(layer if layer is not None else Image.new("L", (1, 1)))
    orientation = options.orientation
    if orientation == "auto":
        orientation = "vertical" if (y2 - y1) >= (x2 - x1) * 1.35 else "horizontal"
    if orientation == "vertical":
        local_bounds = (0, 0, layer_size[0], layer_size[1])
        if layer is None:
            measured = _vertical_layout(text, font, options, local_bounds)[3]
        else:
            measured = _draw_vertical(layer, text, font, options, local_bounds)
    else:
        lines = _wrap_horizontal(text, draw, font, available[0], options.stroke_width)
        spacing = max(1, round(size * 0.18))
        measured = _font_bbox(draw, "\n".join(lines), font, options.stroke_width)
        if measured[0] > available[0] or measured[1] > available[1]:
            raise LayoutOverflowError("横排文字超出区域")
        if layer is not None:
            draw.multiline_text(
                (layer.width / 2, layer.height / 2),
                "\n".join(lines),
                font=font,
                fill=options.font_color,
                stroke_width=options.stroke_width,
                stroke_fill=options.stroke_color,
                spacing=spacing,
                align="center",
                anchor="mm",
            )
    if abs(options.rotation_degrees) > 0.001:
        if layer is None:
            # Exact rotated bounds depend on the rasterized glyph bbox.
            return _render_at_size(text, font_path, size, options, bounds, render=True)
        # Rotate only the rendered glyph bounds, not the transparent region
        # canvas.  Rotating the full region would always enlarge it and reject
        # every non-zero angle before we had a chance to fit the text.
        content_box = layer.getbbox()
        if content_box is not None:
            content = layer.crop(content_box)
            rotated = content.rotate(
                options.rotation_degrees,
                expand=True,
                resample=Image.Resampling.BICUBIC,
            )
            if rotated.width > available[0] or rotated.height > available[1]:
                raise LayoutOverflowError("旋转后的文字超出区域")
            centered = Image.new("RGBA", layer.size, (0, 0, 0, 0))
            centered.alpha_composite(
                rotated,
                (
                    max(0, (layer.width - rotated.width) // 2),
                    max(0, (layer.height - rotated.height) // 2),
                ),
            )
            layer = centered
            measured = (rotated.width, rotated.height)
    return layer, measured


def render_region_text_layer(
    image_size: tuple[int, int],
    text: str,
    bounds: tuple[float, float, float, float],
    font_path: Path,
    options: LayoutOptions,
) -> tuple[Image.Image | None, tuple[int, int], int]:
    """Lay out one region and return a local RGBA layer and its page origin."""

    if not text.strip():
        clamped = _clamp_bounds(bounds, image_size, 0)
        return None, (clamped[0], clamped[1]), options.font_size
    clamped = _clamp_bounds(bounds, image_size, 0)
    lower = max(6, options.min_font_size)
    upper = max(lower, options.max_font_size)
    chosen = max(lower, min(upper, options.font_size))
    chosen_layer: Image.Image | None = None
    raster_measure = abs(options.rotation_degrees) > 0.001
    if options.auto_font_size:
        best: int | None = None
        low, high = lower, upper
        while low <= high:
            candidate = (low + high) // 2
            try:
                candidate_layer, _ = _render_at_size(
                    text, font_path, candidate, options, clamped, render=raster_measure
                )
            except LayoutOverflowError:
                high = candidate - 1
            else:
                best = candidate
                chosen_layer = candidate_layer
                low = candidate + 1
        if best is None:
            # The preferred minimum is a readability target. Small annotations
            # can use a smaller size in auto mode, but never clip or drop text.
            for candidate in range(lower - 1, 5, -1):
                try:
                    candidate_layer, _ = _render_at_size(
                        text, font_path, candidate, options, clamped, render=raster_measure
                    )
                except LayoutOverflowError:
                    continue
                best = candidate
                chosen_layer = candidate_layer
                break
        if best is None:
            raise LayoutOverflowError("文字缩小到 6 像素仍无法放入，请扩大文字框或精简译文")
        chosen = best
    if chosen_layer is None:
        chosen_layer, _ = _render_at_size(
            text, font_path, chosen, options, clamped, render=True
        )
    assert chosen_layer is not None
    return chosen_layer, (clamped[0], clamped[1]), chosen


def render_region_text(
    image: Image.Image,
    text: str,
    bounds: tuple[float, float, float, float],
    font_path: Path,
    options: LayoutOptions,
) -> tuple[Image.Image, int]:
    if not text.strip():
        return image, options.font_size
    layer, origin, chosen = render_region_text_layer(
        image.size, text, bounds, font_path, options
    )
    assert layer is not None
    canvas = image.convert("RGBA")
    # The layer is exactly the region's dimensions.  Alpha compositing keeps
    # pixels outside the box byte-for-byte identical after conversion.
    canvas.alpha_composite(layer, origin)
    return canvas.convert("RGB"), chosen


def exif_oriented(path: Path) -> Image.Image:
    try:
        with Image.open(path) as opened:
            return ImageOps.exif_transpose(opened).convert("RGB")
    except Exception as exc:
        raise LayoutError("原图无法解码") from exc


def atomic_save_png(
    image: Image.Image, destination: Path, *, compress_level: int = 2
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_name(f"{destination.name}.part")
    try:
        image.save(part, "PNG", compress_level=max(0, min(9, compress_level)))
        os.replace(part, destination)
    finally:
        part.unlink(missing_ok=True)
