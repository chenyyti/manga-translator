from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sys
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from app.core.config import Settings
from app.core.errors import AppError
from app.db.models import DetectionModel, DetectionSettings
from app.db.session import Database
from app.providers.detection.base import DetectionDependencyError, DetectionRuntime
from app.services.storage import relative_to_root, resolve_within

logger = logging.getLogger(__name__)

PROJECT_STORAGE = "project"
DATA_STORAGE = "data"
SCANNED_ORIGIN = "scanned"
UPLOADED_ORIGIN = "uploaded"
MIGRATED_ORIGIN = "migrated"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_detection_model_path(settings: Settings, model: DetectionModel) -> Path:
    """Resolve a registered detector model from its declared storage scope."""

    if model.storage_scope == PROJECT_STORAGE:
        path = resolve_within(settings.models_dir, model.relative_path)
        root = settings.yolo_models_dir.resolve()
        if not path.is_relative_to(root):
            raise AppError("UNSAFE_PATH", "项目 YOLO 模型路径校验失败", status_code=422)
        return path
    if model.storage_scope == DATA_STORAGE:
        path = resolve_within(settings.data_dir, model.relative_path)
        root = (settings.data_dir / "models").resolve()
        if not path.is_relative_to(root):
            raise AppError("UNSAFE_PATH", "旧版检测模型路径校验失败", status_code=422)
        return path
    raise AppError("UNSAFE_PATH", "检测模型存储范围无效", status_code=422)


def project_model_relative_path(settings: Settings, path: Path) -> str:
    relative = relative_to_root(settings.models_dir, path)
    if not relative.casefold().startswith("yolo/"):
        raise AppError("UNSAFE_PATH", "项目 YOLO 模型必须位于 models/yolo 目录", status_code=422)
    return relative


class DetectionModelCatalog:
    """Synchronize direct ``models/yolo/*.pt`` files with the model catalog."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        runtime: DetectionRuntime,
    ) -> None:
        self.database = database
        self.settings = settings
        self.runtime = runtime
        self._fingerprint: tuple[tuple[str, int, int], ...] | None = None
        self._lock = asyncio.Lock()
        self._worker: asyncio.Task[None] | None = None
        self._rescan = False
        self._closed = False
        self._model_events: dict[str, asyncio.Event] = {}
        self.status = "preparing"
        self.elapsed_ms = 0.0
        self.error: str | None = None
        versions = []
        for name in ("torch", "ultralytics"):
            try:
                versions.append(version(name))
            except PackageNotFoundError:
                versions.append("missing")
        self.environment = hashlib.sha256(
            repr(
                (sys.executable, sys.version, versions, str(settings.yolo_models_dir.resolve()))
            ).encode()
        ).hexdigest()

    async def close(self) -> None:
        self._closed = True
        if self._worker and not self._worker.done():
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)

    def snapshot(self) -> dict[str, object]:
        return {"status": self.status, "elapsed_ms": self.elapsed_ms, "error": self.error}

    async def wait_ready(self) -> None:
        await self.sync()

    async def wait_for_model(self, model_id: str) -> None:
        await self.sync(wait=False)
        if self._rescan:
            await self.wait_ready()
        event = self._model_events.get(model_id)
        if event is not None:
            await event.wait()

    def invalidate(self) -> None:
        self._fingerprint = None

    async def publish_upload(self, model: DetectionModel, staged: Path, destination: Path) -> None:
        # Inspection happens in a hidden staging directory. Publish and register
        # under the inventory lock so a concurrent list cannot register it twice.
        async with self._lock:
            staged.replace(destination)
            try:
                async with self.database.session_factory() as session:
                    session.add(model)
                    configured = await session.get(DetectionSettings, 1)
                    if configured is None:
                        session.add(DetectionSettings(id=1, default_model_id=model.id))
                    elif configured.default_model_id is None:
                        configured.default_model_id = model.id
                    await session.commit()
                    await session.refresh(model)
            except Exception:
                destination.unlink(missing_ok=True)
                raise
            self.invalidate()

    def _files(self) -> list[tuple[Path, int, int]]:
        root = self.settings.yolo_models_dir
        if not root.is_dir():
            return []
        result: list[tuple[Path, int, int]] = []
        for path in root.iterdir():
            if not path.is_file() or path.suffix.casefold() != ".pt":
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            result.append((path, stat.st_size, stat.st_mtime_ns))
        return sorted(result, key=lambda item: item[0].name.casefold())

    async def sync(self, *, wait: bool = True) -> None:
        # Keep only inventory and registration on the request path.
        # Model loading never holds a database transaction.
        async with self._lock:
            if self._closed:
                return
            files = await asyncio.to_thread(self._files)
            fingerprint = tuple((p.name.casefold(), size, mtime) for p, size, mtime in files)
            if self._worker and not self._worker.done():
                worker = self._worker
                if fingerprint != self._fingerprint:
                    self._rescan = True
            else:
                if fingerprint == self._fingerprint:
                    return
                pending: list[tuple[str, Path, int, int]] = []
                async with self.database.session_factory() as session:
                    models = list(
                        (
                            await session.scalars(
                                select(DetectionModel).where(
                                    DetectionModel.storage_scope == PROJECT_STORAGE
                                )
                            )
                        ).all()
                    )
                    by_path = {m.relative_path.casefold(): m for m in models}
                    seen: set[str] = set()
                    for path, size, mtime in files:
                        try:
                            relative = project_model_relative_path(self.settings, path)
                        except AppError:
                            continue
                        seen.add(relative.casefold())
                        model = by_path.get(relative.casefold())
                        if model is None:
                            model = DetectionModel(
                                id=str(uuid4()),
                                name=path.stem,
                                filename=path.name,
                                relative_path=relative,
                                sha256="",
                                size=size,
                                status="preparing",
                                class_names_json="{}",
                                storage_scope=PROJECT_STORAGE,
                                origin=SCANNED_ORIGIN,
                            )
                            session.add(model)
                        cached = (
                            model.status == "ready"
                            and model.size == size
                            and model.file_mtime_ns == str(mtime)
                            and model.validation_environment == self.environment
                            and model.validation_version == 1
                            and len(model.sha256) == 64
                            and model.task_name == "detect"
                        )
                        try:
                            names = json.loads(model.class_names_json)
                            cached = cached and isinstance(names, dict) and bool(names)
                            cached = cached and all(
                                str(k).isdigit() and isinstance(v, str) for k, v in names.items()
                            )
                            if cached:
                                int(model.sha256, 16)
                        except (TypeError, ValueError):
                            cached = False
                        if not cached:
                            model.status = "preparing"
                            model.error_message = None
                            model.class_names_json = "{}"
                            pending.append((model.id, path, size, mtime))
                            self._model_events[model.id] = asyncio.Event()
                    for model in models:
                        if model.relative_path.casefold() not in seen:
                            model.status = "missing"
                            model.error_message = (
                                "模型文件不存在，请将 .pt 文件放入项目 models/yolo 目录"
                            )
                    await session.commit()
                self._fingerprint = fingerprint
                self.status = "preparing"
                self.error = None
                self._worker = asyncio.create_task(self._validate(pending), name="yolo-catalog")
                worker = self._worker
        if wait:
            await asyncio.shield(worker)
            if self._worker is not worker:
                await self.wait_ready()

    async def _validate(self, pending: list[tuple[str, Path, int, int]]) -> None:
        started = time.perf_counter()
        try:
            for model_id, path, size, mtime in pending:
                values: dict[str, object] = {"status": "ready", "error_message": None}
                try:
                    digest = await asyncio.to_thread(sha256_file, path)
                    values.update(sha256=digest, size=size)
                    info = await self.runtime.inspect(path)
                    if info.task_name != "detect" or not info.class_names:
                        raise ValueError("模型不是有效的目标检测模型")
                    stat = path.stat()
                    if (stat.st_size, stat.st_mtime_ns) != (size, mtime):
                        raise ValueError("模型校验期间文件发生变化，请刷新重试")
                    values.update(
                        sha256=digest,
                        size=size,
                        file_mtime_ns=str(mtime),
                        validation_environment=self.environment,
                        validation_version=1,
                        class_names_json=json.dumps(info.class_names, ensure_ascii=False),
                        framework_version=info.framework_version,
                        task_name=info.task_name,
                    )
                except DetectionDependencyError as exc:
                    values.update(status="unavailable", error_message=str(exc))
                except Exception as exc:
                    values.update(status="invalid", error_message=str(exc) or "模型校验失败")
                async with self.database.session_factory() as session:
                    model = await session.get(DetectionModel, model_id)
                    if model is not None:
                        if values["status"] == "ready":
                            try:
                                stat = path.stat()
                                if (stat.st_size, stat.st_mtime_ns) != (size, mtime):
                                    raise ValueError("模型校验期间文件发生变化，请刷新重试")
                            except (OSError, ValueError) as exc:
                                values.update(status="invalid", error_message=str(exc))
                        for key, value in values.items():
                            setattr(model, key, value)
                        await session.commit()
                self._model_events[model_id].set()
            async with self.database.session_factory() as session:
                ready = await session.scalar(
                    select(DetectionModel)
                    .where(
                        DetectionModel.storage_scope == PROJECT_STORAGE,
                        DetectionModel.status == "ready",
                    )
                    .order_by(DetectionModel.created_at)
                    .limit(1)
                )
                if ready:
                    configured = await session.get(DetectionSettings, 1)
                    if configured is None:
                        session.add(DetectionSettings(id=1, default_model_id=ready.id))
                    elif configured.default_model_id is None:
                        configured.default_model_id = ready.id
                    await session.commit()
                invalid = await session.scalar(
                    select(DetectionModel.id)
                    .where(
                        DetectionModel.storage_scope == PROJECT_STORAGE,
                        DetectionModel.status.in_(["invalid", "unavailable", "missing"]),
                    )
                    .limit(1)
                )
                self.status = "completed_with_warnings" if invalid else "ready"
        except Exception:
            logger.exception("YOLO catalog initialization failed")
            self.status = "failed"
            self.error = "检测模型准备失败，请刷新设置页重试"
            self.invalidate()
        finally:
            self.elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            for model_id, *_ in pending:
                self._model_events[model_id].set()
        if self._rescan and not self._closed:
            self._rescan = False
            self._worker = None
            self.invalidate()
            await self.sync(wait=False)


def model_is_readonly(model: DetectionModel) -> bool:
    # Models discovered directly in ``models/yolo`` used to be read-only in the
    # UI.  They now follow the same managed lifecycle as uploaded models: the
    # delete endpoint removes both the catalog row and the source file.
    return False


def legacy_model_path(settings: Settings, model: DetectionModel) -> Path:
    if model.storage_scope != DATA_STORAGE:
        raise AppError("INVALID_MODEL_SCOPE", "该模型不是旧版数据目录模型", status_code=422)
    path = resolve_detection_model_path(settings, model)
    root = (settings.data_dir / "models").resolve()
    if not path.is_relative_to(root) or path.parent.name != model.id:
        raise AppError("UNSAFE_PATH", "旧版检测模型路径校验失败", status_code=422)
    return path
