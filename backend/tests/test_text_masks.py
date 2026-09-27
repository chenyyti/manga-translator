from PIL import Image, ImageDraw, ImageOps

from app.services.rendering import MaskRegion, build_page_mask


def sample():
    image = Image.new("RGB", (180, 220), "#777777")
    draw = ImageDraw.Draw(image)
    draw.ellipse((25, 10, 155, 210), fill="white", outline="black", width=3)
    draw.rectangle((65, 65, 75, 90), fill="black")
    draw.rectangle((100, 110, 111, 140), fill="black")
    return image


def test_ink_removed_outline_and_background_preserved():
    image = sample()
    mask = build_page_mask(image.size, [MaskRegion("a", (30, 50, 145, 170))], .04, 2, source=image).mask
    assert mask.getpixel((70, 75)) == 255
    assert mask.getpixel((105, 125)) == 255
    assert mask.getpixel((85, 100)) == 0
    assert mask.getpixel((26, 110)) == 0
    assert mask.getpixel((30, 60)) == 0


def test_dark_background_and_small_punctuation():
    image = ImageOps.invert(sample())
    ImageDraw.Draw(image).rectangle((88, 150, 90, 152), fill="white")
    mask = build_page_mask(image.size, [MaskRegion("a", (30, 50, 145, 170))], .04, 2, source=image).mask
    assert mask.getpixel((70, 75)) == 255
    assert mask.getpixel((89, 151)) == 255
    assert mask.getpixel((26, 110)) == 0


def test_preserved_overlap_and_blank_region():
    image = sample()
    regions = [MaskRegion("a", (30, 50, 145, 170)), MaskRegion("b", (60, 60, 80, 100), True)]
    mask = build_page_mask(image.size, regions, .04, 2, source=image).mask
    assert mask.getpixel((70, 75)) == 0
    assert mask.getpixel((105, 125)) == 255
    blank = Image.new("RGB", (40, 40), "white")
    assert build_page_mask(blank.size, [MaskRegion("c", (5, 5, 35, 35))], .04, 2, source=blank).mask.getbbox() is None


def test_complex_background_fallback_does_not_expand():
    image = Image.new("RGB", (80, 80))
    pixels = image.load()
    for y in range(80):
        for x in range(80):
            pixels[x, y] = ((x * 31) % 256, (y * 43) % 256, ((x+y) * 17) % 256)
    mask = build_page_mask(image.size, [MaskRegion("a", (20, 20, 60, 60))], .25, 64, source=image).mask
    assert mask.getbbox() == (20, 20, 60, 60)
