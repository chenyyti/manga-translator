from __future__ import annotations

import os
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from app.core.config import Settings
from app.core.errors import UnsafeArchiveError
from app.services.storage import (
    is_supported_image,
    natural_sort_key,
    normalize_relative_path,
)


@dataclass(frozen=True)
class ArchiveImage:
    relative_path: str
    extracted_path: Path


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = info.external_attr >> 16
    return stat.S_ISLNK(mode)


def extract_archive_images(
    archive_path: Path,
    destination: Path,
    settings: Settings,
) -> list[ArchiveImage]:
    destination.mkdir(parents=True, exist_ok=True)
    images: list[ArchiveImage] = []
    normalized_seen: set[str] = set()
    total_uncompressed = 0

    try:
        archive = zipfile.ZipFile(archive_path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise UnsafeArchiveError("ZIP 文件无法读取") from exc

    with archive:
        infos = archive.infolist()
        if len(infos) > settings.max_archive_entries:
            raise UnsafeArchiveError("ZIP 条目数量超过安全限制")

        for info in infos:
            try:
                relative_path = normalize_relative_path(info.filename)
            except Exception as exc:
                raise UnsafeArchiveError("ZIP 包含不安全路径") from exc
            normalized_key = relative_path.casefold()
            if normalized_key in normalized_seen:
                raise UnsafeArchiveError("ZIP 包含规范化后重名的路径")
            normalized_seen.add(normalized_key)
            if _is_symlink(info):
                raise UnsafeArchiveError("ZIP 不允许包含符号链接")
            if info.flag_bits & 0x1:
                raise UnsafeArchiveError("ZIP 不允许包含加密条目")
            if info.is_dir():
                continue

            total_uncompressed += info.file_size
            if total_uncompressed > settings.max_import_bytes:
                raise UnsafeArchiveError("ZIP 解压总量超过安全限制")
            if info.file_size and info.compress_size == 0:
                raise UnsafeArchiveError("ZIP 压缩信息异常")
            if (
                info.compress_size
                and info.file_size / info.compress_size > settings.max_compression_ratio
            ):
                raise UnsafeArchiveError("ZIP 条目压缩比超过安全限制")
            if not is_supported_image(relative_path):
                continue
            if relative_path.startswith("__MACOSX/") or "/.__" in relative_path:
                continue

            extracted_path = destination / f"{uuid4().hex}{Path(relative_path).suffix.casefold()}"
            part_path = extracted_path.with_name(f"{extracted_path.name}.part")
            written = 0
            try:
                with archive.open(info, "r") as source, part_path.open("wb") as target:
                    while chunk := source.read(1024 * 1024):
                        written += len(chunk)
                        if written > info.file_size or written > settings.max_import_bytes:
                            raise UnsafeArchiveError("ZIP 解压数据超过声明大小")
                        target.write(chunk)
                    target.flush()
                    os.fsync(target.fileno())
                os.replace(part_path, extracted_path)
            finally:
                part_path.unlink(missing_ok=True)
            images.append(ArchiveImage(relative_path, extracted_path))

    images.sort(key=lambda item: natural_sort_key(item.relative_path))
    return images
