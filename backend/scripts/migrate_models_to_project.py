from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from pathlib import Path

from sqlalchemy import select

from app.core.config import Settings
from app.db.models import DetectionModel
from app.db.session import Database, run_migrations
from app.services.detection_models import (
    DATA_STORAGE,
    MIGRATED_ORIGIN,
    PROJECT_STORAGE,
    legacy_model_path,
    project_model_relative_path,
    sha256_file,
)
from app.services.storage import atomic_copy

OCR_MODEL_DIRS = (
    "mangaocr",
    "paddlex/official_models/en_PP-OCRv5_mobile_rec_onnx",
    "paddlex/official_models/korean_PP-OCRv5_mobile_rec_onnx",
)


def _copy_if_needed(source: Path, destination: Path) -> bool:
    if not source.is_file():
        return False
    if destination.is_file() and sha256_file(destination) == sha256_file(source):
        return False
    if destination.exists():
        raise RuntimeError(f"目标文件已存在但校验不一致：{destination}")
    atomic_copy(source, destination)
    if sha256_file(destination) != sha256_file(source):
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"复制后校验失败：{destination}")
    return True


def migrate_ocr(settings: Settings) -> list[str]:
    source_root = settings.data_dir / "models" / "ocr"
    destination_root = settings.ocr_models_dir
    copied: list[str] = []
    for relative_dir in OCR_MODEL_DIRS:
        source_dir = source_root / relative_dir
        if not source_dir.is_dir():
            continue
        destination_dir = destination_root / relative_dir
        for source in source_dir.iterdir():
            # Hugging Face caches and lock files are disposable runtime state;
            # only the model directory's direct payload is bundled.
            if not source.is_file() or source.name in {".lock"}:
                continue
            destination = destination_dir / source.name
            if _copy_if_needed(source, destination):
                copied.append(destination.as_posix())
    return copied


def migrate_inpainting(settings: Settings) -> list[str]:
    source_root = settings.data_dir / "models" / "inpainting"
    copied: list[str] = []
    for name in ("big-lama.pt", ".ready.json"):
        destination = settings.inpainting_models_dir / name
        if _copy_if_needed(source_root / name, destination):
            copied.append(destination.as_posix())
    return copied


def write_manifest(settings: Settings) -> Path:
    """Write a deterministic inventory of bundled OCR and inpainting files."""

    files: list[dict[str, object]] = []
    for root in (settings.ocr_models_dir, settings.inpainting_models_dir):
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
            if not path.is_file() or ".cache" in path.parts:
                continue
            files.append(
                {
                    "path": path.relative_to(settings.models_dir).as_posix(),
                    "size": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )
    manifest = {
        "version": 1,
        "source": "project",
        "required": [
            "ocr/mangaocr",
            "ocr/paddlex/official_models/korean_PP-OCRv5_mobile_rec_onnx",
            "ocr/paddlex/official_models/en_PP-OCRv5_mobile_rec_onnx",
            "inpainting/big-lama.pt",
        ],
        "files": files,
    }
    destination = settings.models_dir / "MODEL_MANIFEST.json"
    temporary = destination.with_name(f"{destination.name}.part")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with temporary.open("w", encoding="utf-8", newline="\n") as output:
        json.dump(manifest, output, ensure_ascii=False, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, destination)
    return destination


def _safe_filename(value: str) -> str:
    filename = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(value).name)
    return filename or "model.pt"


def _existing_project_copy(
    settings: Settings,
    digest: str,
    occupied_paths: set[str],
) -> Path | None:
    for candidate in sorted(settings.yolo_models_dir.glob("*.pt"), key=lambda item: item.name.casefold()):
        relative = project_model_relative_path(settings, candidate)
        if relative.casefold() in occupied_paths:
            continue
        try:
            if sha256_file(candidate) == digest:
                return candidate
        except OSError:
            continue
    return None


async def migrate_yolo(settings: Settings) -> tuple[list[str], list[str]]:
    run_migrations(settings.database_path)
    database = Database(settings.database_path)
    copied: list[str] = []
    failures: list[str] = []
    try:
        async with database.session_factory() as session:
            models = list(
                (
                    await session.scalars(
                        select(DetectionModel).where(
                            DetectionModel.storage_scope == DATA_STORAGE
                        )
                    )
                ).all()
            )
            occupied_paths = {
                model.relative_path.casefold()
                for model in (
                    await session.scalars(
                        select(DetectionModel).where(
                            DetectionModel.storage_scope == PROJECT_STORAGE
                        )
                    )
                ).all()
            }
            for model in models:
                try:
                    source = legacy_model_path(settings, model)
                    if not source.is_file():
                        raise FileNotFoundError(source)
                    source_digest = sha256_file(source)
                    destination = _existing_project_copy(settings, source_digest, occupied_paths)
                    if destination is None:
                        filename = f"{model.id}_{_safe_filename(model.filename)}"
                        destination = settings.yolo_models_dir / filename
                        copied_file = _copy_if_needed(source, destination)
                    else:
                        copied_file = False
                    if copied_file:
                        copied.append(destination.as_posix())
                    model.relative_path = project_model_relative_path(settings, destination)
                    occupied_paths.add(model.relative_path.casefold())
                    model.storage_scope = "project"
                    model.origin = MIGRATED_ORIGIN
                    model.status = "ready"
                    model.error_message = None
                except Exception as exc:  # report one bad legacy model and continue
                    model.status = "missing"
                    model.error_message = f"迁移失败：{exc}"
                    failures.append(f"{model.id}: {exc}")
            await session.commit()
    finally:
        await database.dispose()
    return copied, failures


def main() -> int:
    parser = argparse.ArgumentParser(description="将模型缓存迁移到项目 models 目录")
    parser.add_argument(
        "--only",
        choices=["all", "ocr", "inpainting", "yolo"],
        default="all",
    )
    args = parser.parse_args()

    settings = Settings()
    settings.ensure_directories()
    copied: list[str] = []
    failures: list[str] = []
    if args.only in {"all", "ocr"}:
        copied.extend(migrate_ocr(settings))
    if args.only in {"all", "inpainting"}:
        copied.extend(migrate_inpainting(settings))
    if args.only in {"all", "yolo"}:
        yolo_copied, yolo_failures = asyncio.run(migrate_yolo(settings))
        copied.extend(yolo_copied)
        failures.extend(yolo_failures)
    manifest = write_manifest(settings)

    print(f"已迁移 {len(copied)} 个模型文件到 {settings.models_dir}")
    print(f"模型清单：{manifest}")
    if failures:
        print("以下模型未完成迁移：")
        for failure in failures:
            print(f"- {failure}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
