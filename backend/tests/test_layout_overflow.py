from pathlib import Path

import pytest
from PIL import Image, ImageChops

from app.services import rendering
from app.services.render_tasks import _safe_error
from app.services.rendering import (
    LayoutOptions,
    LayoutOverflowError,
    auto_contrast_layout_options,
    find_system_font,
    render_region_text,
    render_region_text_layer,
)


@pytest.fixture
def font() -> Path:
    path = find_system_font("msyh.ttc")
    if path is None:
        pytest.skip("Chinese font unavailable")
    return path


def test_small_annotation_auto_shrinks_without_changing_outside(font):
    original = Image.new("RGB", (100, 100), "white")
    result, size = render_region_text(original, "夏日祭典", (20, 20, 40, 38), font,
                                      LayoutOptions(orientation="horizontal"))
    assert 6 <= size < 12
    assert result.crop((0, 0, 20, 100)).tobytes() == original.crop((0, 0, 20, 100)).tobytes()
    assert result.tobytes() != original.tobytes()


def test_impossible_box_has_actionable_error(font):
    with pytest.raises(LayoutOverflowError, match="扩大文字框") as error:
        render_region_text(Image.new("RGB", (40, 40)), "测试文字", (0, 0, 2, 2), font, LayoutOptions())
    assert "扩大文字框" in _safe_error(error.value, "生成任务失败")


def test_fixed_size_still_reports_overflow(font):
    with pytest.raises(LayoutOverflowError):
        render_region_text(Image.new("RGB", (40, 40)), "测试", (0, 0, 10, 10), font,
                           LayoutOptions(auto_font_size=False, font_size=36))


def test_long_first_token_wraps(font):
    _, size = render_region_text(Image.new("RGB", (80, 200), "white"), "ABCDEFGHIJKLMN",
                                (10, 10, 50, 190), font,
                                LayoutOptions(auto_font_size=False, font_size=14, orientation="horizontal"))
    assert size == 14


@pytest.mark.parametrize("text", ["——", "……", "⋯⋯", "......"])
def test_vertical_punctuation_is_centered_and_tall(font, text):
    original = Image.new("RGB", (100, 200), "white")
    result, _ = render_region_text(original, text, (0, 0, 100, 200), font,
                                  LayoutOptions(auto_font_size=False, font_size=32, orientation="vertical"))
    box = ImageChops.difference(result, original).getbbox()
    assert box is not None
    assert box[3] - box[1] > 10 * (box[2] - box[0])
    if text == "——":
        x = (box[0] + box[2]) // 2
        assert all(result.getpixel((x, y)) != (255, 255, 255) for y in range(box[1], box[3]))


def test_ellipsis_run_moves_together_to_next_column(font):
    image = Image.new("RGB", (100, 130), "white")
    result, _ = render_region_text(image, "甲乙……", (0, 0, 100, 130), font,
                                  LayoutOptions(auto_font_size=False, font_size=32, orientation="vertical"))
    # Capacity is 3 glyphs: the two ellipses must share the left column.
    box = ImageChops.difference(result.crop((0, 0, 55, 130)), image.crop((0, 0, 55, 130))).getbbox()
    assert box is not None
    assert box[2] - box[0] <= 4
    assert box[3] - box[1] > 55


def test_shared_rgba_canvas_matches_sequential_rendering(font):
    original = Image.new("RGB", (360, 260), "white")
    entries = [
        ("第一段译文", (30, 30, 220, 150), LayoutOptions(orientation="horizontal")),
        ("第二段", (150, 100, 330, 240), LayoutOptions(orientation="vertical")),
    ]
    sequential = original
    expected_sizes = []
    for text, bounds, options in entries:
        sequential, size = render_region_text(sequential, text, bounds, font, options)
        expected_sizes.append(size)

    canvas = original.convert("RGBA")
    actual_sizes = []
    for text, bounds, options in entries:
        layer, origin, size = render_region_text_layer(
            original.size, text, bounds, font, options
        )
        assert layer is not None
        canvas.alpha_composite(layer, origin)
        actual_sizes.append(size)

    assert actual_sizes == expected_sizes
    assert canvas.convert("RGB").tobytes() == sequential.tobytes()


def test_auto_size_rasterizes_only_the_chosen_size(font, monkeypatch):
    calls: list[bool] = []
    original = rendering._render_at_size

    def tracked(*args, **kwargs):
        calls.append(bool(kwargs.get("render", True)))
        return original(*args, **kwargs)

    monkeypatch.setattr(rendering, "_render_at_size", tracked)
    layer, _, _ = render_region_text_layer(
        (600, 400),
        "自动字号只应绘制一次",
        (20, 20, 580, 380),
        font,
        LayoutOptions(orientation="horizontal"),
    )
    assert layer is not None
    assert calls.count(True) == 1
    assert calls.count(False) > 1


def test_font_objects_are_cached(font, monkeypatch):
    rendering._cached_font.cache_clear()
    calls = 0
    original = rendering.ImageFont.truetype

    def tracked(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(rendering.ImageFont, "truetype", tracked)
    options = LayoutOptions(auto_font_size=False, font_size=24, orientation="horizontal")
    for _ in range(2):
        render_region_text_layer((300, 200), "缓存字体", (10, 10, 290, 190), font, options)
    assert calls == 1


def test_monochrome_text_uses_contrasting_background_color():
    dark = Image.new("RGB", (100, 100), "#050505")
    light = Image.new("RGB", (100, 100), "#fafafa")
    bounds = (10, 10, 90, 90)

    assert auto_contrast_layout_options(
        dark, bounds, LayoutOptions(font_color="#000000")
    ).font_color == "#FFFFFF"
    assert auto_contrast_layout_options(
        light, bounds, LayoutOptions(font_color="#FFFFFF")
    ).font_color == "#000000"


def test_custom_or_outlined_text_color_is_not_overridden():
    dark = Image.new("RGB", (100, 100), "black")
    bounds = (10, 10, 90, 90)
    custom = LayoutOptions(font_color="#123456")
    outlined = LayoutOptions(font_color="#000000", stroke_width=2)

    assert auto_contrast_layout_options(dark, bounds, custom) is custom
    assert auto_contrast_layout_options(dark, bounds, outlined) is outlined
