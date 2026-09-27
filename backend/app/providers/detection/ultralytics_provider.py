from __future__ import annotations

import asyncio
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from app.providers.detection.base import (
    DetectionDependencyError,
    DetectionModelInfo,
    DetectionPrediction,
    DetectionRuntimeInfo,
)

_CACHED_MODEL_PATH: str | None = None
_CACHED_MODEL: object | None = None
_CACHED_MODEL_STAMP: tuple[int, int] | None = None


def _dependencies():
    try:
        import torch
        import ultralytics
        from ultralytics import YOLO
    except ImportError as exc:
        raise DetectionDependencyError(
            "检测运行环境尚未安装，请先运行 scripts/install-detection-gpu.ps1 或 CPU 安装脚本"
        ) from exc
    return torch, ultralytics, YOLO


def _load_model(path: str):
    global _CACHED_MODEL, _CACHED_MODEL_PATH, _CACHED_MODEL_STAMP
    _torch, _ultralytics, yolo = _dependencies()
    stat = Path(path).stat()
    stamp = (stat.st_size, stat.st_mtime_ns)
    if _CACHED_MODEL is None or _CACHED_MODEL_PATH != path or _CACHED_MODEL_STAMP != stamp:
        _CACHED_MODEL = yolo(path)
        _CACHED_MODEL_PATH = path
        _CACHED_MODEL_STAMP = stamp
    return _CACHED_MODEL


def _normalize_names(value: object) -> dict[int, str]:
    if isinstance(value, dict):
        return {int(key): str(name) for key, name in value.items()}
    if isinstance(value, list | tuple):
        return {index: str(name) for index, name in enumerate(value)}
    raise RuntimeError("模型没有可用的类别映射")


def inspect_model_file(path: str) -> dict[str, object]:
    _torch, ultralytics, _yolo = _dependencies()
    model = _load_model(path)
    names = _normalize_names(getattr(model, "names", None))
    return {
        "class_names": names,
        "framework_version": str(ultralytics.__version__),
        "task_name": str(getattr(model, "task", "detect")),
    }


def inspect_runtime() -> dict[str, object]:
    torch, ultralytics, _yolo = _dependencies()
    cuda_available = bool(torch.cuda.is_available())
    properties = torch.cuda.get_device_properties(0) if cuda_available else None
    return {
        "torch_version": str(torch.__version__),
        "ultralytics_version": str(ultralytics.__version__),
        "cuda_available": cuda_available,
        "gpu_name": str(properties.name) if properties else None,
        "total_vram_mb": int(properties.total_memory // 1024**2) if properties else None,
    }


def _predict_once(
    model_path: str,
    image_path: str,
    confidence: float,
    image_size: int,
    device: str,
) -> list[dict[str, object]]:
    import numpy as np
    from PIL import Image, ImageOps

    model = _load_model(model_path)
    with Image.open(image_path) as opened:
        image = np.asarray(ImageOps.exif_transpose(opened).convert("RGB"))
    result = model.predict(
        source=image,
        conf=confidence,
        imgsz=image_size,
        device=device,
        verbose=False,
    )[0]
    names = _normalize_names(result.names)
    if result.boxes is None:
        return []
    coordinates = result.boxes.xyxy.detach().cpu().tolist()
    classes = result.boxes.cls.detach().cpu().tolist()
    confidences = result.boxes.conf.detach().cpu().tolist()
    return [
        {
            "class_id": int(class_id),
            "class_name": names[int(class_id)],
            "confidence": float(score),
            "x1": float(box[0]),
            "y1": float(box[1]),
            "x2": float(box[2]),
            "y2": float(box[3]),
        }
        for box, class_id, score in zip(coordinates, classes, confidences, strict=True)
    ]


def predict_image_file(
    model_path: str,
    image_path: str,
    confidence: float,
    image_size: int,
    requested_device: str,
) -> dict[str, object]:
    torch, _ultralytics, _yolo = _dependencies()
    if requested_device == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA 不可用")
    use_cuda = requested_device in {"auto", "cuda"} and torch.cuda.is_available()
    actual_device = "cuda:0" if use_cuda else "cpu"
    try:
        regions = _predict_once(model_path, image_path, confidence, image_size, actual_device)
    except RuntimeError:
        if requested_device != "auto" or actual_device == "cpu":
            raise
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        regions = _predict_once(model_path, image_path, confidence, image_size, "cpu")
        actual_device = "cpu"
    return {"regions": regions, "actual_device": actual_device}


class UltralyticsDetectionRuntime:
    def __init__(self) -> None:
        self._executor = ProcessPoolExecutor(
            max_workers=1,
            mp_context=multiprocessing.get_context("spawn"),
        )

    async def status(self) -> DetectionRuntimeInfo:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(self._executor, inspect_runtime)
        return DetectionRuntimeInfo(
            torch_version=str(result["torch_version"]),
            ultralytics_version=str(result["ultralytics_version"]),
            cuda_available=bool(result["cuda_available"]),
            gpu_name=str(result["gpu_name"]) if result["gpu_name"] else None,
            total_vram_mb=(
                int(result["total_vram_mb"]) if result["total_vram_mb"] is not None else None
            ),
        )

    async def inspect(self, model_path: Path) -> DetectionModelInfo:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(self._executor, inspect_model_file, str(model_path))
        return DetectionModelInfo(
            class_names={int(key): str(value) for key, value in result["class_names"].items()},
            framework_version=str(result["framework_version"]),
            task_name=str(result["task_name"]),
        )

    async def predict(
        self,
        model_path: Path,
        image_path: Path,
        *,
        confidence: float,
        image_size: int,
        device: str,
    ) -> DetectionPrediction:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            self._executor,
            predict_image_file,
            str(model_path),
            str(image_path),
            confidence,
            image_size,
            device,
        )
        return DetectionPrediction(
            regions=list(result["regions"]),
            actual_device=str(result["actual_device"]),
        )

    async def close(self) -> None:
        # Python 3.11 has no public terminate_workers API. Capture workers
        # before shutdown so a cancelled model load cannot hold exit open.
        workers = list((self._executor._processes or {}).values())
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
        await asyncio.to_thread(self._executor.shutdown, wait=True, cancel_futures=True)
        for worker in workers:
            await asyncio.to_thread(worker.join, 5)
