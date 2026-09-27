"""Safe, streaming export primitives used by the Phase 9 task worker."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError

from app.core.errors import AppError
from app.services.images import is_valid_png_file
from app.services.storage import resolve_within


@dataclass(frozen=True)
class ExportPage:
    """The small immutable page manifest captured when an archive is created."""

    page_id: str
    page_index: int
    path: Path
    rendered_revision: int
    rendered_sha256: str | None
    width: int
    height: int


_SAFE_FILENAME = re.compile(r"[^0-9A-Za-z\u00a0-\u024f\u3040-\u30ff\u3400-\u9fff._-]+")


def safe_filename_component(value: str, *, fallback: str = "manga") -> str:
    """Return a filename component safe for Windows and archive headers."""

    cleaned = _SAFE_FILENAME.sub("_", " ".join(value.split())).strip(" ._")
    cleaned = cleaned[:80].rstrip(" ._")
    if not cleaned:
        cleaned = fallback
    # Windows device names are not safe even when no extension is present.
    if cleaned.casefold().split(".", 1)[0] in {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }:
        cleaned = f"_{cleaned}"
    return cleaned


def safe_export_project_name(value: str) -> str:
    """Keep a display title while preventing user-entered paths in archives."""

    # Project names are free-form UI text rather than filesystem paths.  A
    # slash, backslash or drive marker is nevertheless enough to make the
    # value look like a path when copied into a manifest or EPUB metadata, so
    # collapse those names through the same filename sanitizer used for the
    # output artifact.  Ordinary punctuation (including ``&`` and ``<``) is
    # retained and escaped by the EPUB writer below.
    if any(marker in value for marker in ("/", "\\", ":")):
        return safe_filename_component(value, fallback="漫画")
    return " ".join(value.split())[:120] or "漫画"


def page_filename(page_index: int) -> str:
    return f"page-{page_index:04d}.png"


def resolve_current_rendered(page: Any, project: Any, data_dir: Path) -> Path | None:
    """Resolve a current rendered PNG while enforcing project containment."""

    if page.render_status != "completed" or not page.rendered_path:
        return None
    try:
        project_root = resolve_within(data_dir, project.workspace_path)
        path = resolve_within(data_dir, page.rendered_path)
    except (AppError, OSError, RuntimeError):
        return None
    if (
        path.suffix.casefold() != ".png"
        or not path.is_relative_to(project_root)
        or not path.is_file()
        or not is_valid_png_file(path)
    ):
        return None
    return path


def file_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def copy_file_atomic(source: Path, destination: Path) -> None:
    """Copy a page into staging without exposing a partial file."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_name(f"{destination.name}.part")
    try:
        with source.open("rb") as reader, part.open("wb") as writer:
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(part, destination)
    finally:
        part.unlink(missing_ok=True)


def _write_zip_atomic(
    destination: Path,
    writer,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_name(f"{destination.name}.part")
    try:
        with zipfile.ZipFile(
            part,
            mode="w",
            compression=zipfile.ZIP_STORED,
            allowZip64=True,
        ) as archive:
            writer(archive)
        os.replace(part, destination)
    finally:
        part.unlink(missing_ok=True)


def build_zip_archive(
    staging_dir: Path,
    destination: Path,
    *,
    project_name: str,
    pages: Iterable[ExportPage],
) -> None:
    """Build a deterministic PNG ZIP from already staged page files."""

    page_list = sorted(pages, key=lambda page: page.page_index)
    manifest = {
        "format": "manga-translator-zip-v1",
        "project_name": safe_export_project_name(project_name),
        "page_count": len(page_list),
        "pages": [
            {
                "page_index": page.page_index,
                "filename": page_filename(page.page_index),
                "width": page.width,
                "height": page.height,
            }
            for page in page_list
        ],
    }

    def write(archive: zipfile.ZipFile) -> None:
        archive.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        for page in page_list:
            source = page.path if page.path.is_file() else staging_dir / page_filename(page.page_index)
            if not source.is_file():
                raise FileNotFoundError(source)
            archive.write(source, arcname=page_filename(page.page_index))

    _write_zip_atomic(destination, write)


def _page_xhtml(page: ExportPage) -> str:
    name = page_filename(page.page_index)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops">\n'
        "<head>"
        '<meta charset="utf-8"/>'
        f'<meta name="viewport" content="width={page.width}, height={page.height}"/>'
        "<style>html,body{margin:0;padding:0;background:#000;"
        "width:100%;height:100%;}img{display:block;width:100%;height:100%;"
        "object-fit:contain;}</style>"
        f"<title>第 {page.page_index} 页</title></head>"
        f'<body epub:type="bodymatter"><img src="../images/{name}" '
        f'alt="第 {page.page_index} 页"/></body></html>'
    )


def build_epub_archive(
    staging_dir: Path,
    destination: Path,
    *,
    project_name: str,
    pages: Iterable[ExportPage],
    cover_page_index: int,
    identifier: str = "phase9",
) -> None:
    """Build an EPUB 3 pre-paginated book without loading page bytes."""

    page_list = sorted(pages, key=lambda page: page.page_index)
    title = html.escape(safe_export_project_name(project_name), quote=False)
    identifiers = [
        f"<item id=\"image-{page.page_index}\" href=\"images/{page_filename(page.page_index)}\" "
        'media-type="image/png"'
        + (' properties="cover-image"' if page.page_index == cover_page_index else "")
        + "/>"
        for page in page_list
    ]
    page_items = [
        f'<item id="page-{page.page_index}" href="pages/{page_filename(page.page_index).replace(".png", ".xhtml")}" '
        'media-type="application/xhtml+xml"/>'
        for page in page_list
    ]
    manifest_items = "\n".join([*identifiers, *page_items, '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'])
    spine = "\n".join(
        f'<itemref idref="page-{page.page_index}"/>' for page in page_list
    )
    nav_items = "\n".join(
        f'<li><a href="pages/{page_filename(page.page_index).replace(".png", ".xhtml")}">第 {page.page_index} 页</a></li>'
        for page in page_list
    )
    opf = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
        'unique-identifier="book-id" prefix="rendition: http://www.idpf.org/vocab/rendition/#">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f'<dc:identifier id="book-id">urn:manga-translator:{html.escape(identifier, quote=False)}</dc:identifier>'
        f"<dc:title>{title}</dc:title><dc:language>zh-CN</dc:language>"
        '<meta property="dcterms:modified">2000-01-01T00:00:00Z</meta>'
        '<meta property="rendition:layout">pre-paginated</meta>'
        '<meta property="rendition:spread">none</meta>'
        '</metadata><manifest>'
        f"{manifest_items}</manifest><spine page-progression-direction=\"ltr\">{spine}</spine></package>"
    )
    nav = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops"><head>'
        f"<title>{title}</title></head><body><nav epub:type=\"toc\" id=\"toc\">"
        f"<h1>{title}</h1><ol>{nav_items}</ol></nav></body></html>"
    )
    container = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="OEBPS/package.opf" '
        'media-type="application/oebps-package+xml"/></rootfiles></container>'
    )

    def write(archive: zipfile.ZipFile) -> None:
        # EPUB requires this exact first, uncompressed entry.
        info = zipfile.ZipInfo("mimetype")
        info.compress_type = zipfile.ZIP_STORED
        archive.writestr(info, b"application/epub+zip")
        archive.writestr("META-INF/container.xml", container.encode("utf-8"))
        archive.writestr("OEBPS/package.opf", opf.encode("utf-8"))
        archive.writestr("OEBPS/nav.xhtml", nav.encode("utf-8"))
        for page in page_list:
            filename = page_filename(page.page_index)
            source = page.path if page.path.is_file() else staging_dir / filename
            if not source.is_file():
                raise FileNotFoundError(source)
            archive.write(source, arcname=f"OEBPS/images/{filename}")
            archive.writestr(
                f"OEBPS/pages/{filename.removesuffix('.png')}.xhtml",
                _page_xhtml(page).encode("utf-8"),
            )

    _write_zip_atomic(destination, write)


def write_jpeg_atomic(source: Path, destination: Path) -> tuple[int, int]:
    """Convert one rendered PNG to a full-resolution, white-backed JPEG."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_name(f"{destination.name}.part")
    try:
        with Image.open(source) as opened:
            oriented = ImageOps.exif_transpose(opened)
            if oriented.mode == "RGBA" or "transparency" in oriented.info:
                rgba = oriented.convert("RGBA")
                background = Image.new("RGB", rgba.size, "white")
                background.paste(rgba, mask=rgba.getchannel("A"))
                output = background
            else:
                output = oriented.convert("RGB")
            output.save(part, format="JPEG", quality=95, optimize=True, progressive=True)
            dimensions = output.size
            if output is not oriented:
                output.close()
        os.replace(part, destination)
        return dimensions
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        raise AppError("EXPORT_IMAGE_INVALID", "成品图片无法转换", status_code=422) from exc
    finally:
        part.unlink(missing_ok=True)


def cleanup_export_parts(root: Path) -> None:
    """Remove only atomic temporary files; preserve resumable page staging."""

    if not root.is_dir():
        return
    for part in root.rglob("*.part"):
        try:
            if part.is_file():
                part.unlink(missing_ok=True)
        except OSError:
            continue


def cleanup_export_staging(root: Path, *, keep_task_ids: Iterable[str] = ()) -> None:
    """Remove abandoned export staging directories after a process restart.

    A task that was active at startup keeps its staging directory so already
    copied pages can be reused.  Staging belonging to terminal or deleted
    tasks is safe to remove and otherwise would accumulate indefinitely after
    an interrupted process.  The caller supplies a directory rooted at the
    application's projects workspace; no path from the database is used for
    traversal here.
    """

    if not root.is_dir():
        return
    keep = {str(value) for value in keep_task_ids}
    try:
        root_resolved = root.resolve()
    except OSError:
        return
    for staging in root_resolved.rglob(".staging"):
        if staging.is_symlink() or not staging.is_dir() or not staging.is_relative_to(root_resolved):
            continue
        for child in list(staging.iterdir()):
            if child.name in keep:
                continue
            try:
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink(missing_ok=True)
            except OSError:
                continue
        try:
            staging.rmdir()
        except OSError:
            pass


__all__ = [
    "ExportPage",
    "build_epub_archive",
    "build_zip_archive",
    "cleanup_export_parts",
    "copy_file_atomic",
    "cleanup_export_staging",
    "file_sha256",
    "page_filename",
    "resolve_current_rendered",
    "safe_filename_component",
    "safe_export_project_name",
    "write_jpeg_atomic",
]
