from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class ImageValidationError(Exception):
    pass


@dataclass(frozen=True)
class ImageDerivatives:
    width: int
    height: int
    preview_width: int
    preview_height: int
    thumbnail_width: int
    thumbnail_height: int
    sha256: str


def is_valid_png_file(path: Path) -> bool:
    """Perform a cheap structural check for reader-facing rendered PNGs.

    The reader must turn an externally truncated/replaced render into a
    placeholder without decoding every full-resolution page.  Checking the
    PNG signature, IHDR marker, and non-zero dimensions catches malformed
    files while keeping the paginated metadata endpoint lightweight.
    """

    try:
        with path.open("rb") as stream:
            header = stream.read(24)
            stream.seek(0, 2)
            size = stream.tell()
            if size < 45:  # signature + IHDR chunk + IEND chunk
                return False
            stream.seek(-12, 2)
            trailer = stream.read(12)
    except OSError:
        return False
    if len(header) != 24 or header[:8] != _PNG_SIGNATURE or header[12:16] != b"IHDR":
        return False
    width = int.from_bytes(header[16:20], "big")
    height = int.from_bytes(header[20:24], "big")
    return (
        width > 0
        and height > 0
        and trailer[:8] == b"\x00\x00\x00\x00IEND"
        and trailer[8:] == b"\xaeB`\x82"
    )


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _save_webp_atomic(image: Image.Image, destination: Path, quality: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    part_path = destination.with_name(f"{destination.name}.part")
    try:
        image.save(part_path, "WEBP", quality=quality, method=4)
        os.replace(part_path, destination)
    finally:
        part_path.unlink(missing_ok=True)


def _prepare_mode(image: Image.Image) -> Image.Image:
    if image.mode in {"RGB", "RGBA"}:
        return image
    if "transparency" in image.info:
        return image.convert("RGBA")
    return image.convert("RGB")


def generate_derivatives(
    source: Path,
    thumbnail_path: Path,
    preview_path: Path,
    *,
    thumbnail_size: int,
    preview_size: int,
) -> ImageDerivatives:
    try:
        with Image.open(source) as probe:
            probe.verify()
        with Image.open(source) as opened:
            opened.seek(0)
            oriented = _prepare_mode(ImageOps.exif_transpose(opened))
            width, height = oriented.size
            if width <= 0 or height <= 0:
                raise ImageValidationError("图片尺寸无效")

            preview = oriented.copy()
            preview.thumbnail((preview_size, preview_size), Image.Resampling.LANCZOS)
            thumbnail = oriented.copy()
            thumbnail.thumbnail((thumbnail_size, thumbnail_size), Image.Resampling.LANCZOS)

            _save_webp_atomic(preview, preview_path, quality=88)
            _save_webp_atomic(thumbnail, thumbnail_path, quality=82)
            return ImageDerivatives(
                width=width,
                height=height,
                preview_width=preview.width,
                preview_height=preview.height,
                thumbnail_width=thumbnail.width,
                thumbnail_height=thumbnail.height,
                sha256=sha256_file(source),
            )
    except ImageValidationError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        raise ImageValidationError("图片无法解码") from exc


def generate_rendered_thumbnail(
    source: Path,
    destination: Path,
    *,
    thumbnail_size: int,
) -> tuple[int, int]:
    """Create a WebP thumbnail from a full-resolution rendered PNG.

    Rendered pages are already EXIF-oriented, but applying ``exif_transpose``
    here keeps the helper safe for legacy files and makes the operation
    idempotent.  The destination is written atomically so an interrupted
    thumbnail never becomes visible to the bookshelf.
    """

    try:
        with Image.open(source) as opened:
            oriented = _prepare_mode(ImageOps.exif_transpose(opened))
            if oriented.width <= 0 or oriented.height <= 0:
                raise ImageValidationError("成品尺寸无效")
            thumbnail = oriented.copy()
            thumbnail.thumbnail((thumbnail_size, thumbnail_size), Image.Resampling.LANCZOS)
            _save_webp_atomic(thumbnail, destination, quality=84)
            return thumbnail.width, thumbnail.height
    except ImageValidationError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        raise ImageValidationError("成品无法解码") from exc


def generate_rendered_thumbnail_from_image(
    source: Image.Image,
    destination: Path,
    *,
    thumbnail_size: int,
) -> tuple[int, int]:
    """Create a rendered-page thumbnail without reopening the saved PNG."""

    try:
        prepared = _prepare_mode(source)
        if prepared.width <= 0 or prepared.height <= 0:
            raise ImageValidationError("成品尺寸无效")
        thumbnail = prepared.copy()
        thumbnail.thumbnail((thumbnail_size, thumbnail_size), Image.Resampling.LANCZOS)
        _save_webp_atomic(thumbnail, destination, quality=84)
        return thumbnail.width, thumbnail.height
    except ImageValidationError:
        raise
    except (OSError, ValueError, SyntaxError) as exc:
        raise ImageValidationError("成品无法解码") from exc
