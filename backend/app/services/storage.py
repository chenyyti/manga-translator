from __future__ import annotations

import os
import re
import shutil
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import BinaryIO

from app.core.errors import AppError

PROJECT_SUBDIRECTORIES = (
    "original",
    "thumbnails",
    "preview",
    "masks",
    "crops",
    "rendered",
    "export",
    "cache",
    "metadata",
)
SUPPORTED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
_NATURAL_PART = re.compile(r"(\d+)")


def normalize_relative_path(value: str) -> str:
    normalized = value.replace("\\", "/").strip()
    if not normalized or "\x00" in normalized:
        raise AppError("INVALID_PATH", "文件相对路径无效", status_code=422)
    if PureWindowsPath(normalized).drive or normalized.startswith("/"):
        raise AppError("INVALID_PATH", "文件路径不能包含盘符或绝对路径", status_code=422)
    path = PurePosixPath(normalized)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise AppError("INVALID_PATH", "文件路径包含不安全的目录片段", status_code=422)
    return path.as_posix()


def natural_sort_key(value: str) -> tuple[tuple[int, object], ...]:
    normalized = value.replace("\\", "/").casefold()
    parts = _NATURAL_PART.split(normalized)
    return tuple((1, int(part)) if part.isdigit() else (0, part) for part in parts)


def is_supported_image(path: str | Path) -> bool:
    return Path(path).suffix.casefold() in SUPPORTED_IMAGE_SUFFIXES


def _absolute_path(path: Path) -> Path:
    """Return a lexical absolute path without following reparse points.

    On Windows, ``Path.resolve()`` can return a different, virtualized path
    when the application is running in a sandbox.  We still need the lexical
    path for database-relative paths and for atomic writes, while ``resolve``
    remains part of the containment check below.
    """

    return Path(os.path.abspath(os.fspath(path)))


def _resolve_contained(root: Path, candidate: Path) -> Path:
    """Validate *candidate* is contained by *root* and return a usable path.

    The normal path is the fully resolved path, which prevents symlinks and
    junctions from escaping the workspace.  Some Windows sandbox providers
    expose existing children through a virtual alias (for example a package
    cache path) while leaving the configured root at its logical path.  In
    that case ``samefile`` proves the resolved ancestor is the configured
    root, and we can safely return the lexical path after verifying the exact
    relative suffix.
    """

    root_abs = _absolute_path(root)
    candidate_abs = _absolute_path(candidate)
    try:
        lexical_relative = candidate_abs.relative_to(root_abs)
    except ValueError as exc:
        raise AppError("UNSAFE_PATH", "请求的文件路径超出工作区", status_code=422) from exc

    root_resolved = root_abs.resolve()
    candidate_resolved = candidate_abs.resolve()
    try:
        if candidate_resolved.is_relative_to(root_resolved):
            return candidate_resolved
    except (OSError, RuntimeError):
        # A broken link or an unavailable reparse point is unsafe.  The
        # sandbox alias fallback below is only considered after this check.
        pass

    # Windows sandbox/reparse aliases can make a child resolve beneath a
    # different textual root.  Permit that only when the resolved ancestor is
    # the exact same directory as the configured root and the suffix did not
    # change.  A symlink to an outside directory cannot satisfy both checks.
    lexical_key = lexical_relative.as_posix().casefold()
    for ancestor in (candidate_resolved, *candidate_resolved.parents):
        try:
            if not ancestor.exists() or not os.path.samefile(ancestor, root_abs):
                continue
            suffix = candidate_resolved.relative_to(ancestor)
            if suffix.as_posix().casefold() == lexical_key:
                return candidate_abs
        except (OSError, RuntimeError, ValueError):
            continue

    raise AppError("UNSAFE_PATH", "请求的文件路径超出工作区", status_code=422)


def resolve_within(root: Path, relative_path: str | Path) -> Path:
    relative = Path(relative_path)
    if relative.is_absolute():
        raise AppError("UNSAFE_PATH", "请求的文件路径必须是工作区相对路径", status_code=422)
    return _resolve_contained(root, _absolute_path(root) / relative)


def relative_to_root(root: Path, path: Path) -> str:
    root_abs = _absolute_path(root)
    candidate_abs = _absolute_path(path)
    _resolve_contained(root_abs, candidate_abs)
    return candidate_abs.relative_to(root_abs).as_posix()


def create_project_tree(project_root: Path) -> None:
    project_root.mkdir(parents=True, exist_ok=False)
    for name in PROJECT_SUBDIRECTORIES:
        (project_root / name).mkdir()


def atomic_copy(source: Path, destination: Path, chunk_size: int = 1024 * 1024) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    part_path = destination.with_name(f"{destination.name}.part")
    try:
        with source.open("rb") as reader, part_path.open("wb") as writer:
            shutil.copyfileobj(reader, writer, length=chunk_size)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(part_path, destination)
    finally:
        part_path.unlink(missing_ok=True)


def stream_to_atomic_file(
    source: BinaryIO,
    destination: Path,
    *,
    max_bytes: int,
    chunk_size: int = 1024 * 1024,
) -> tuple[int, str]:
    import hashlib

    destination.parent.mkdir(parents=True, exist_ok=True)
    part_path = destination.with_name(f"{destination.name}.part")
    size = 0
    digest = hashlib.sha256()
    try:
        with part_path.open("wb") as writer:
            while chunk := source.read(chunk_size):
                size += len(chunk)
                if size > max_bytes:
                    raise AppError("IMPORT_TOO_LARGE", "导入内容超过允许的容量", status_code=413)
                digest.update(chunk)
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(part_path, destination)
    finally:
        part_path.unlink(missing_ok=True)
    return size, digest.hexdigest()


def remove_project_tree(projects_dir: Path, workspace_path: str) -> None:
    target = resolve_within(projects_dir.parent.parent, workspace_path)
    projects_root = projects_dir.resolve()
    if target.parent != projects_root or not target.name:
        raise AppError("UNSAFE_PATH", "项目目录校验失败", status_code=422)
    if target.exists():
        shutil.rmtree(target)
