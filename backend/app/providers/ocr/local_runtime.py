from __future__ import annotations

import asyncio
import importlib
import importlib.metadata
import importlib.util
import json
import multiprocessing
import os
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from PIL import Image

from app.providers.ocr.base import (
    OCRBatchItem,
    OCRBatchResult,
    OCRDependencyError,
    OCRImageInput,
    OCRModelMissingError,
    OCRProviderInfo,
    OCRRecognition,
)

_MANGA_MODELS: dict[str, object] = {}
_PADDLE_MODELS: dict[str, object] = {}


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def manga_model_dir(root: Path) -> Path:
    return root / "mangaocr"


def paddle_model_name(language: str) -> str:
    names = {"ko": "korean_PP-OCRv5_mobile_rec", "en": "en_PP-OCRv5_mobile_rec"}
    try:
        return names[language]
    except KeyError as exc:
        raise ValueError("PaddleOCR 不支持该语言") from exc


def paddle_model_dir(root: Path, language: str) -> Path:
    return root / "paddlex" / "official_models" / f"{paddle_model_name(language)}_onnx"


def _is_ready(path: Path) -> bool:
    return path.is_dir() and (path / ".ready.json").is_file()


def _set_cache_environment(models_root: str) -> Path:
    root = Path(models_root)
    os.environ["HF_HOME"] = str(root / "huggingface")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["PADDLE_PDX_CACHE_HOME"] = str(root / "paddlex")
    os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
    return root


def _manga_recognize(image_path: str, device: str, models_root: str) -> dict[str, object]:
    root = _set_cache_environment(models_root)
    model_dir = manga_model_dir(root)
    if not _is_ready(model_dir):
        raise OCRModelMissingError("MangaOCR 模型尚未准备")
    try:
        import torch
        from manga_ocr import MangaOcr
    except ImportError as exc:
        raise OCRDependencyError("MangaOCR 运行依赖尚未安装") from exc
    if device == "cuda" and not torch.cuda.is_available():
        raise OCRDependencyError("CUDA 不可用")
    use_cuda = device in {"auto", "cuda"} and torch.cuda.is_available()
    actual = "cuda:0" if use_cuda else "cpu"
    cache_key = f"{model_dir}:{actual}"
    try:
        model = _MANGA_MODELS.get(cache_key)
        if model is None:
            model = MangaOcr(str(model_dir), force_cpu=not use_cuda)
            _MANGA_MODELS[cache_key] = model
        text = str(model(image_path))
    except RuntimeError:
        if device != "auto" or not use_cuda:
            raise
        torch.cuda.empty_cache()
        actual = "cpu"
        cache_key = f"{model_dir}:{actual}"
        model = _MANGA_MODELS.get(cache_key)
        if model is None:
            model = MangaOcr(str(model_dir), force_cpu=True)
            _MANGA_MODELS[cache_key] = model
        text = str(model(image_path))
    return {"text": text, "confidence": None, "actual_device": actual}


def _manga_model(models_root: str, target: str):
    root = _set_cache_environment(models_root)
    model_dir = manga_model_dir(root)
    if not _is_ready(model_dir):
        raise OCRModelMissingError("MangaOCR 模型尚未准备")
    try:
        from manga_ocr import MangaOcr
    except ImportError as exc:
        raise OCRDependencyError("MangaOCR 运行依赖尚未安装") from exc
    cache_key = f"{model_dir}:{target}"
    model = _MANGA_MODELS.get(cache_key)
    if model is None:
        model = MangaOcr(str(model_dir), force_cpu=target == "cpu")
        _MANGA_MODELS[cache_key] = model
    return model


def _manga_batch_once(
    inputs: tuple[OCRImageInput, ...], target: str, models_root: str
) -> list[tuple[str, float | None]]:
    try:
        import torch
        from manga_ocr.ocr import post_process
    except ImportError as exc:
        raise OCRDependencyError("MangaOCR 运行依赖尚未安装") from exc
    model = _manga_model(models_root, target)
    images = [
        Image.frombytes("RGB", (item.width, item.height), item.rgb).convert("L").convert("RGB")
        for item in inputs
    ]
    pixels = model.processor(images, return_tensors="pt").pixel_values
    with torch.inference_mode():
        generated = model.model.generate(
            pixels.to(model.model.device),
            max_length=300,
        )
    decoded = model.tokenizer.batch_decode(generated.cpu(), skip_special_tokens=True)
    return [(str(post_process(text)), None) for text in decoded]


def _result_payload(value: object) -> dict[str, object]:
    raw = getattr(value, "json", value)
    if callable(raw):
        raw = raw()
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict):
        raise RuntimeError("PaddleOCR 返回格式无效")
    payload = raw.get("res", raw)
    if not isinstance(payload, dict):
        raise RuntimeError("PaddleOCR 返回格式无效")
    return payload


def _paddle_recognize(
    image_path: str, language: str, device: str, models_root: str
) -> dict[str, object]:
    root = _set_cache_environment(models_root)
    model_dir = paddle_model_dir(root, language)
    if not _is_ready(model_dir):
        raise OCRModelMissingError("PaddleOCR 模型尚未准备")
    try:
        importlib.import_module("torch")  # Preload bundled CUDA/cuDNN DLLs on Windows.
        import onnxruntime as ort
        from paddleocr import TextRecognition
    except ImportError as exc:
        raise OCRDependencyError("PaddleOCR 运行依赖尚未安装") from exc
    cuda_available = "CUDAExecutionProvider" in ort.get_available_providers()
    if device == "cuda" and not cuda_available:
        raise OCRDependencyError("ONNX Runtime CUDA 不可用")
    use_cuda = device in {"auto", "cuda"} and cuda_available
    actual = "cuda:0" if use_cuda else "cpu"

    def recognize_once(target: str) -> tuple[str, float | None]:
        cache_key = f"{model_dir}:{target}"
        model = _PADDLE_MODELS.get(cache_key)
        if model is None:
            model = TextRecognition(
                model_name=paddle_model_name(language),
                model_dir=str(model_dir),
                device="gpu:0" if target.startswith("cuda") else "cpu",
                engine="onnxruntime",
            )
            _PADDLE_MODELS[cache_key] = model
        output = list(model.predict(input=image_path, batch_size=1))
        if not output:
            return "", None
        payload = _result_payload(output[0])
        confidence = payload.get("rec_score")
        score = float(confidence) if confidence is not None else None
        return str(payload.get("rec_text", "")), score

    try:
        text, confidence = recognize_once(actual)
    except RuntimeError:
        if device != "auto" or not use_cuda:
            raise
        actual = "cpu"
        text, confidence = recognize_once(actual)
    return {"text": text, "confidence": confidence, "actual_device": actual}


def _paddle_model(language: str, models_root: str, target: str):
    root = _set_cache_environment(models_root)
    model_dir = paddle_model_dir(root, language)
    if not _is_ready(model_dir):
        raise OCRModelMissingError("PaddleOCR 模型尚未准备")
    try:
        importlib.import_module("torch")
        from paddleocr import TextRecognition
    except ImportError as exc:
        raise OCRDependencyError("PaddleOCR 运行依赖尚未安装") from exc
    cache_key = f"{model_dir}:{target}"
    model = _PADDLE_MODELS.get(cache_key)
    if model is None:
        model = TextRecognition(
            model_name=paddle_model_name(language),
            model_dir=str(model_dir),
            device="gpu:0" if target.startswith("cuda") else "cpu",
            engine="onnxruntime",
        )
        _PADDLE_MODELS[cache_key] = model
    return model


def _paddle_batch_once(
    inputs: tuple[OCRImageInput, ...],
    language: str,
    target: str,
    models_root: str,
) -> list[tuple[str, float | None]]:
    try:
        import numpy as np
    except ImportError as exc:
        raise OCRDependencyError("PaddleOCR 运行依赖尚未安装") from exc
    model = _paddle_model(language, models_root, target)
    # PaddleX's RGB reader assumes in-memory arrays arrived in OpenCV BGR
    # order and converts them to RGB.  Reverse the Pillow RGB bytes here so
    # in-memory and file-path inference remain pixel-identical.
    images = [
        np.frombuffer(item.rgb, dtype=np.uint8)
        .reshape((item.height, item.width, 3))[:, :, ::-1]
        .copy()
        for item in inputs
    ]
    output = list(model.predict(input=images, batch_size=len(images)))
    if len(output) != len(inputs):
        raise RuntimeError("PaddleOCR 返回数量与输入不一致")
    results: list[tuple[str, float | None]] = []
    for value in output:
        payload = _result_payload(value)
        confidence = payload.get("rec_score")
        results.append(
            (
                str(payload.get("rec_text", "")),
                float(confidence) if confidence is not None else None,
            )
        )
    return results


def _cuda_available(provider: str) -> bool:
    if provider == "mangaocr":
        try:
            import torch

            return bool(torch.cuda.is_available())
        except Exception:
            return False
    try:
        importlib.import_module("torch")
        import onnxruntime as ort

        return "CUDAExecutionProvider" in ort.get_available_providers()
    except Exception:
        return False


def _is_cuda_oom(error: BaseException) -> bool:
    try:
        import torch

        if isinstance(error, torch.cuda.OutOfMemoryError):
            return True
    except Exception:
        pass
    return "out of memory" in str(error).casefold()


def _empty_cuda_cache() -> None:
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def recognize_batch_local(
    provider: str,
    inputs: tuple[OCRImageInput, ...],
    language: str,
    device: str,
    models_root: str,
    batch_size: int,
) -> dict[str, object]:
    if provider not in {"mangaocr", "paddleocr"}:
        raise ValueError("OCR Provider 不存在")
    if device == "cuda" and not _cuda_available(provider):
        message = "CUDA 不可用" if provider == "mangaocr" else "ONNX Runtime CUDA 不可用"
        raise OCRDependencyError(message)

    requested = max(1, batch_size)
    target = "cuda:0" if device in {"auto", "cuda"} and _cuda_available(provider) else "cpu"
    run_size = requested if target.startswith("cuda") else 1
    effective = min(run_size, len(inputs)) if inputs else run_size
    started = time.perf_counter()

    def infer(group: tuple[OCRImageInput, ...], actual: str) -> list[tuple[str, float | None]]:
        if provider == "mangaocr":
            return _manga_batch_once(group, actual, models_root)
        return _paddle_batch_once(group, language, actual, models_root)

    def resilient(
        group: tuple[OCRImageInput, ...], actual: str
    ) -> list[dict[str, object]]:
        nonlocal effective
        effective = min(effective, len(group))
        try:
            recognized = infer(group, actual)
            if len(recognized) != len(group):
                raise RuntimeError("OCR 返回数量与输入不一致")
            return [
                {
                    "region_id": item.region_id,
                    "text": text,
                    "confidence": confidence,
                    "actual_device": actual,
                    "error": None,
                }
                for item, (text, confidence) in zip(group, recognized, strict=True)
            ]
        except (OCRDependencyError, OCRModelMissingError):
            raise
        except Exception as exc:
            if _is_cuda_oom(exc):
                _empty_cuda_cache()
            if len(group) > 1:
                midpoint = max(1, len(group) // 2)
                return resilient(group[:midpoint], actual) + resilient(group[midpoint:], actual)
            if actual.startswith("cuda") and device == "auto":
                effective = 1
                return resilient(group, "cpu")
            return [
                {
                    "region_id": group[0].region_id,
                    "text": "",
                    "confidence": None,
                    "actual_device": actual,
                    "error": "本区域 OCR 失败",
                }
            ]

    items: list[dict[str, object]] = []
    for offset in range(0, len(inputs), run_size):
        items.extend(resilient(inputs[offset : offset + run_size], target))
    return {
        "items": items,
        "requested_batch_size": requested,
        "effective_batch_size": effective,
        "inference_ms": round((time.perf_counter() - started) * 1000),
    }


def recognize_local(
    provider: str,
    image_path: str,
    language: str,
    device: str,
    models_root: str,
) -> dict[str, object]:
    if provider == "mangaocr":
        return _manga_recognize(image_path, device, models_root)
    if provider == "paddleocr":
        return _paddle_recognize(image_path, language, device, models_root)
    raise ValueError("OCR Provider 不存在")


class LocalOCRRuntime:
    def __init__(self, models_root: Path) -> None:
        self.models_root = models_root
        context = multiprocessing.get_context("spawn")
        self._executors = {
            "mangaocr": ProcessPoolExecutor(max_workers=1, mp_context=context),
            "paddleocr": ProcessPoolExecutor(max_workers=1, mp_context=context),
        }
        self._locks = {name: asyncio.Lock() for name in self._executors}

    async def providers(self) -> list[OCRProviderInfo]:
        manga_version = _package_version("manga-ocr")
        paddle_version = _package_version("paddleocr")
        ort_installed = importlib.util.find_spec("onnxruntime") is not None
        cuda_available = False
        torch_cuda_available = False
        if importlib.util.find_spec("torch") is not None:
            try:
                import torch

                torch_cuda_available = torch.cuda.is_available()
            except Exception:
                pass
        if ort_installed:
            try:
                importlib.import_module("torch")
                import onnxruntime as ort

                cuda_available = "CUDAExecutionProvider" in ort.get_available_providers()
            except Exception:
                pass
        return [
            OCRProviderInfo(
                id="mangaocr",
                name="MangaOCR",
                supported_languages=("ja",),
                installed=manga_version is not None,
                version=manga_version,
                model_ready=_is_ready(manga_model_dir(self.models_root)),
                model_names=("kha-white/manga-ocr-base",),
                cuda_available=torch_cuda_available,
            ),
            OCRProviderInfo(
                id="paddleocr",
                name="PaddleOCR",
                supported_languages=("ko", "en"),
                installed=paddle_version is not None and ort_installed,
                version=paddle_version,
                model_ready=all(
                    _is_ready(paddle_model_dir(self.models_root, language))
                    for language in ("ko", "en")
                ),
                model_names=(paddle_model_name("ko"), paddle_model_name("en")),
                cuda_available=cuda_available,
            ),
        ]

    async def recognize(
        self,
        provider: str,
        image_path: Path,
        *,
        language: str,
        device: str,
    ) -> OCRRecognition:
        if provider not in self._executors:
            raise ValueError("OCR Provider 不存在")
        loop = asyncio.get_running_loop()
        async with self._locks[provider]:
            result = await loop.run_in_executor(
                self._executors[provider],
                recognize_local,
                provider,
                str(image_path),
                language,
                device,
                str(self.models_root),
            )
        return OCRRecognition(
            text=str(result["text"]),
            confidence=(
                float(result["confidence"]) if result.get("confidence") is not None else None
            ),
            actual_device=str(result["actual_device"]),
        )

    async def recognize_batch(
        self,
        provider: str,
        images: tuple[OCRImageInput, ...],
        *,
        language: str,
        device: str,
        batch_size: int,
    ) -> OCRBatchResult:
        if provider not in self._executors:
            raise ValueError("OCR Provider 不存在")
        loop = asyncio.get_running_loop()
        async with self._locks[provider]:
            result = await loop.run_in_executor(
                self._executors[provider],
                recognize_batch_local,
                provider,
                images,
                language,
                device,
                str(self.models_root),
                batch_size,
            )
        items = tuple(
            OCRBatchItem(
                region_id=str(item["region_id"]),
                recognition=(
                    OCRRecognition(
                        text=str(item["text"]),
                        confidence=(
                            float(item["confidence"])
                            if item.get("confidence") is not None
                            else None
                        ),
                        actual_device=str(item["actual_device"]),
                    )
                    if item.get("error") is None
                    else None
                ),
                error=str(item["error"]) if item.get("error") is not None else None,
            )
            for item in result["items"]
        )
        return OCRBatchResult(
            items=items,
            requested_batch_size=int(result["requested_batch_size"]),
            effective_batch_size=int(result["effective_batch_size"]),
            inference_ms=int(result["inference_ms"]),
        )

    async def close(self) -> None:
        await asyncio.gather(
            *(
                asyncio.to_thread(executor.shutdown, wait=True, cancel_futures=True)
                for executor in self._executors.values()
            )
        )
