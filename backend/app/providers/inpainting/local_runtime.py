from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import importlib.util
import io
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from statistics import median

from PIL import Image, ImageOps, UnidentifiedImageError

from .base import (
    InpaintingDependencyError,
    InpaintingError,
    InpaintingModelMissingError,
    InpaintingProviderInfo,
    InpaintingResult,
)
from .registry import provider_infos, resolve_provider


def lama_model_path(root: Path) -> Path:
    return root / "big-lama.pt"


def lama_manifest_path(root: Path) -> Path:
    return root / ".ready.json"


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def lama_model_ready(root: Path) -> bool:
    return lama_model_path(root).is_file() and lama_manifest_path(root).is_file()


def _load_rgb(path: str) -> Image.Image:
    try:
        with Image.open(path) as opened:
            return ImageOps.exif_transpose(opened).convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        raise InpaintingError("图片无法解码") from exc


def _load_mask(path: str, size: tuple[int, int]) -> Image.Image:
    try:
        with Image.open(path) as opened:
            mask = opened.convert("L")
            if mask.size != size:
                raise InpaintingError("掩膜尺寸与原图不一致")
            return mask
    except InpaintingError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        raise InpaintingError("掩膜无法解码") from exc


def _atomic_save(image: Image.Image, output: str) -> None:
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_name(f"{destination.name}.part")
    try:
        image.save(part, "PNG", optimize=True)
        os.replace(part, destination)
    finally:
        part.unlink(missing_ok=True)


def _mask_bbox(mask: Image.Image) -> tuple[int, int, int, int] | None:
    return mask.getbbox()


def _fast_inpaint(image_path: str, mask_path: str, output_path: str) -> str:
    image = _load_rgb(image_path)
    mask = _load_mask(mask_path, image.size)
    bbox = _mask_bbox(mask)
    if bbox is None:
        _atomic_save(image, output_path)
        return "cpu"
    x1, y1, x2, y2 = bbox
    ring = max(4, min(32, round(min(x2 - x1, y2 - y1) * 0.08)))
    left, top = max(0, x1 - ring), max(0, y1 - ring)
    right, bottom = min(image.width, x2 + ring), min(image.height, y2 + ring)
    pixels: list[tuple[int, int, int]] = []
    image_pixels = image.load()
    mask_pixels = mask.load()
    for y in range(top, bottom):
        for x in range(left, right):
            if mask_pixels[x, y] == 0:
                pixel = image_pixels[x, y]
                pixels.append((int(pixel[0]), int(pixel[1]), int(pixel[2])))
    if not pixels:
        raise InpaintingError("掩膜外没有可用背景像素")
    background = tuple(int(median(channel)) for channel in zip(*pixels, strict=True))
    repaired = image.copy()
    fill = Image.new("RGB", (x2 - x1, y2 - y1), background)
    repaired.paste(fill, (x1, y1), mask.crop((x1, y1, x2, y2)))
    _atomic_save(repaired, output_path)
    return "cpu"


def _opencv_inpaint(
    image_path: str, mask_path: str, output_path: str, radius: float
) -> str:
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise InpaintingDependencyError("OpenCV 修复依赖尚未安装") from exc
    image = _load_rgb(image_path)
    mask = _load_mask(mask_path, image.size)
    source = np.asarray(image)
    binary = np.asarray(mask)
    result = cv2.inpaint(cv2.cvtColor(source, cv2.COLOR_RGB2BGR), binary, radius, cv2.INPAINT_TELEA)
    repaired = Image.fromarray(cv2.cvtColor(result, cv2.COLOR_BGR2RGB), mode="RGB")
    # OpenCV normally leaves unmasked pixels untouched; explicitly composite
    # to make that invariant true for every OpenCV build.
    repaired = Image.composite(repaired, image, mask)
    _atomic_save(repaired, output_path)
    return "cpu"


_LAMA_MODELS: dict[str, object] = {}


def _lama_inpaint(
    image_path: str,
    mask_path: str,
    output_path: str,
    device: str,
    max_edge: int,
    model_root: str,
) -> str:
    root = Path(model_root)
    model_path = lama_model_path(root)
    if not lama_model_ready(root):
        raise InpaintingModelMissingError("Big-LaMa 模型尚未准备")
    try:
        import numpy as np
        import torch
    except ImportError as exc:
        raise InpaintingDependencyError("LaMa 运行依赖尚未安装") from exc
    if device == "cuda" and not torch.cuda.is_available():
        raise InpaintingDependencyError("CUDA 不可用")
    use_cuda = device in {"auto", "cuda"} and torch.cuda.is_available()
    actual = "cuda:0" if use_cuda else "cpu"
    image = _load_rgb(image_path)
    mask = _load_mask(mask_path, image.size)
    original_size = image.size
    if max(image.size) > max_edge:
        scale = max_edge / max(image.size)
        target_size = (max(8, round(image.width * scale)), max(8, round(image.height * scale)))
        image = image.resize(target_size, Image.Resampling.LANCZOS)
        mask = mask.resize(target_size, Image.Resampling.NEAREST)
    cache_key = f"{model_path}:{actual}"
    try:
        model = _LAMA_MODELS.get(cache_key)
        if model is None:
            # Loading from bytes avoids TorchScript path handling problems on
            # Windows installations whose user/data directory contains CJK.
            model = torch.jit.load(io.BytesIO(model_path.read_bytes()), map_location=actual)
            model.eval()
            model.to(actual)
            _LAMA_MODELS[cache_key] = model
        image_array = np.asarray(image).astype("float32") / 255.0
        mask_array = (np.asarray(mask) > 0).astype("float32")
        image_tensor = torch.from_numpy(image_array).permute(2, 0, 1).unsqueeze(0).to(actual)
        mask_tensor = torch.from_numpy(mask_array).unsqueeze(0).unsqueeze(0).to(actual)
        with torch.inference_mode():
            output = model(image_tensor, mask_tensor)
        rendered = output[0].permute(1, 2, 0).detach().cpu().numpy()
        rendered = np.clip(rendered * 255.0, 0, 255).astype("uint8")
        repaired = Image.fromarray(rendered, mode="RGB")
    except RuntimeError:
        if device != "auto" or not use_cuda:
            raise
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        actual = "cpu"
        cache_key = f"{model_path}:{actual}"
        model = _LAMA_MODELS.get(cache_key)
        if model is None:
            model = torch.jit.load(io.BytesIO(model_path.read_bytes()), map_location="cpu")
            model.eval()
            _LAMA_MODELS[cache_key] = model
        image_array = np.asarray(image).astype("float32") / 255.0
        mask_array = (np.asarray(mask) > 0).astype("float32")
        image_tensor = torch.from_numpy(image_array).permute(2, 0, 1).unsqueeze(0)
        mask_tensor = torch.from_numpy(mask_array).unsqueeze(0).unsqueeze(0)
        with torch.inference_mode():
            output = model(image_tensor, mask_tensor)
        rendered = output[0].permute(1, 2, 0).detach().cpu().numpy()
        rendered = np.clip(rendered * 255.0, 0, 255).astype("uint8")
        repaired = Image.fromarray(rendered, mode="RGB")
    if repaired.size != original_size:
        repaired = repaired.resize(original_size, Image.Resampling.LANCZOS)
    repaired = Image.composite(repaired, _load_rgb(image_path), _load_mask(mask_path, original_size))
    _atomic_save(repaired, output_path)
    return actual


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _result(provider: str, output_path: Path, actual: str, warning: str | None = None) -> InpaintingResult:
    return InpaintingResult(output_path, provider, actual, warning)


class LocalInpaintingRuntime:
    """Runs lightweight repair locally and isolates Big-LaMa in a subprocess."""

    def __init__(self, models_root: Path) -> None:
        self.models_root = models_root
        context = multiprocessing.get_context("spawn")
        self._lama_executor = ProcessPoolExecutor(max_workers=1, mp_context=context)
        self._locks = {provider: asyncio.Lock() for provider in ("fast", "opencv", "lama")}

    async def providers(self) -> list[InpaintingProviderInfo]:
        opencv_version = _package_version("opencv-python-headless") or _package_version("opencv-python")
        opencv_installed = importlib.util.find_spec("cv2") is not None
        torch_version = _package_version("torch")
        cuda_available = False
        if torch_version and importlib.util.find_spec("torch") is not None:
            try:
                import torch

                cuda_available = bool(torch.cuda.is_available())
            except Exception:
                pass
        return provider_infos(
            opencv_installed=opencv_installed,
            lama_installed=torch_version is not None,
            lama_model_ready=lama_model_ready(self.models_root),
            opencv_version=opencv_version,
            torch_version=torch_version,
            cuda_available=cuda_available,
        )

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
    ) -> InpaintingResult:
        provider = resolve_provider(provider)
        if provider == "auto":
            raise ValueError("Auto 模式必须先由任务根据背景复杂度选择实际算法")
        if provider == "fast":
            async with self._locks[provider]:
                actual = await asyncio.to_thread(_fast_inpaint, str(image_path), str(mask_path), str(output_path))
            return _result(provider, output_path, actual)
        if provider == "opencv":
            async with self._locks[provider]:
                actual = await asyncio.to_thread(
                    _opencv_inpaint,
                    str(image_path),
                    str(mask_path),
                    str(output_path),
                    opencv_radius,
                )
            return _result(provider, output_path, actual)
        async with self._locks[provider]:
            loop = asyncio.get_running_loop()
            actual = await loop.run_in_executor(
                self._lama_executor,
                _lama_inpaint,
                str(image_path),
                str(mask_path),
                str(output_path),
                device,
                max_edge,
                str(self.models_root),
            )
        return _result(provider, output_path, str(actual))

    async def close(self) -> None:
        await asyncio.to_thread(self._lama_executor.shutdown, wait=True, cancel_futures=True)


__all__ = [
    "LocalInpaintingRuntime",
    "lama_manifest_path",
    "lama_model_path",
    "lama_model_ready",
]
