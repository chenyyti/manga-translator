"""Offline mask comparison; never modifies the input image."""
import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw

from app.services.rendering import MaskRegion, build_page_mask


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("image", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--boxes", required=True, help="JSON list of [x1,y1,x2,y2]")
    args = parser.parse_args()
    image = Image.open(args.image).convert("RGB")
    regions = [MaskRegion(str(i), tuple(box)) for i, box in enumerate(json.loads(args.boxes))]
    old = build_page_mask(image.size, regions, .04, 2).mask
    new = build_page_mask(image.size, regions, .04, 2, source=image).mask
    red = Image.new("RGB", image.size, "#ff2040")
    overlay = Image.blend(image, red, .65)
    panels = [image, Image.composite(overlay, image, old), Image.composite(overlay, image, new)]
    canvas = Image.new("RGB", (image.width * 3, image.height + 32), "#222222")
    draw = ImageDraw.Draw(canvas)
    for index, (panel, title) in enumerate(zip(panels, ["SOURCE", "OLD RECTANGLE MASK", "NEW TEXT MASK"], strict=True)):
        canvas.paste(panel, (index * image.width, 32))
        draw.text((index * image.width + 8, 8), title, fill="white")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(args.output)
    print(json.dumps({"old_mask_pixels": sum(old.histogram()[1:]), "new_mask_pixels": sum(new.histogram()[1:]), "preview": str(args.output)}))


if __name__ == "__main__":
    main()
