from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class OCRDependencyError(RuntimeError):
    pass


class OCRModelMissingError(RuntimeError):
    pass


@dataclass(frozen=True)
class OCRRecognition:
    text: str
    confidence: float | None
    actual_device: str


@dataclass(frozen=True)
class OCRImageInput:
    """One RGB crop transported to the isolated OCR worker."""

    region_id: str
    width: int
    height: int
    rgb: bytes


@dataclass(frozen=True)
class OCRBatchItem:
    region_id: str
    recognition: OCRRecognition | None = None
    error: str | None = None


@dataclass(frozen=True)
class OCRBatchResult:
    items: tuple[OCRBatchItem, ...]
    requested_batch_size: int
    effective_batch_size: int
    inference_ms: int


@dataclass(frozen=True)
class OCRProviderInfo:
    id: str
    name: str
    supported_languages: tuple[str, ...]
    installed: bool
    version: str | None
    model_ready: bool
    model_names: tuple[str, ...]
    cuda_available: bool


class OCRRuntime(Protocol):
    async def providers(self) -> list[OCRProviderInfo]: ...

    async def recognize(
        self,
        provider: str,
        image_path: Path,
        *,
        language: str,
        device: str,
    ) -> OCRRecognition: ...

    async def recognize_batch(
        self,
        provider: str,
        images: tuple[OCRImageInput, ...],
        *,
        language: str,
        device: str,
        batch_size: int,
    ) -> OCRBatchResult: ...

    async def close(self) -> None: ...
