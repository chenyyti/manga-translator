from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

INPAINTING_PROVIDER_IDS = ("auto", "fast", "opencv", "lama")


class InpaintingError(RuntimeError):
    """Safe, user-facing local repair error."""

    code = "INPAINTING_FAILED"


class InpaintingDependencyError(InpaintingError):
    code = "INPAINTING_DEPENDENCY_MISSING"


class InpaintingModelMissingError(InpaintingError):
    code = "INPAINTING_MODEL_MISSING"


@dataclass(frozen=True)
class InpaintingProviderInfo:
    id: str
    name: str
    installed: bool
    version: str | None
    model_ready: bool
    model_name: str | None
    available_devices: tuple[str, ...]
    description: str


@dataclass(frozen=True)
class InpaintingResult:
    image_path: Path
    provider: str
    actual_device: str
    warning: str | None = None


class InpaintingRuntime(Protocol):
    async def providers(self) -> list[InpaintingProviderInfo]: ...

    async def inpaint(
        self,
        provider: str,
        image_path: Path,
        mask_path: Path,
        output_path: Path,
        *,
        device: str,
        max_edge: int,
        opencv_radius: float,
    ) -> InpaintingResult: ...

    async def close(self) -> None: ...


# Public protocol alias used by integration code and injected test runtimes.
InpaintingProvider = InpaintingRuntime
