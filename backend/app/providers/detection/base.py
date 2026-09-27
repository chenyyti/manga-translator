from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class DetectionDependencyError(RuntimeError):
    pass


@dataclass(frozen=True)
class DetectionModelInfo:
    class_names: dict[int, str]
    framework_version: str
    task_name: str


@dataclass(frozen=True)
class DetectionPrediction:
    regions: list[dict[str, object]]
    actual_device: str


@dataclass(frozen=True)
class DetectionRuntimeInfo:
    torch_version: str
    ultralytics_version: str
    cuda_available: bool
    gpu_name: str | None
    total_vram_mb: int | None


class DetectionRuntime(Protocol):
    async def status(self) -> DetectionRuntimeInfo: ...

    async def inspect(self, model_path: Path) -> DetectionModelInfo: ...

    async def predict(
        self,
        model_path: Path,
        image_path: Path,
        *,
        confidence: float,
        image_size: int,
        device: str,
    ) -> DetectionPrediction: ...

    async def close(self) -> None: ...
