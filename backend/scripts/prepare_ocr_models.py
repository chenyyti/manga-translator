from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import get_settings
from app.providers.ocr.local_runtime import manga_model_dir, paddle_model_dir, paddle_model_name


def _write_ready(path: Path, provider: str, model: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    target = path / ".ready.json"
    temporary = path / ".ready.json.part"
    temporary.write_text(
        json.dumps(
            {
                "provider": provider,
                "model": model,
                "prepared_at": datetime.now(UTC).isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    os.replace(temporary, target)


def _device_name(requested: str) -> tuple[str, bool]:
    import torch

    cuda = torch.cuda.is_available()
    if requested == "cuda" and not cuda:
        raise RuntimeError("请求使用 CUDA，但当前 PyTorch CUDA 不可用")
    use_cuda = requested in {"auto", "cuda"} and cuda
    return ("gpu:0" if use_cuda else "cpu"), use_cuda


def prepare_manga(root: Path, requested_device: str) -> None:
    from huggingface_hub import snapshot_download
    from manga_ocr import MangaOcr

    target = manga_model_dir(root)
    print(f"准备 MangaOCR: {target}")
    snapshot_download(
        repo_id="kha-white/manga-ocr-base",
        local_dir=target,
    )
    _, use_cuda = _device_name(requested_device)
    MangaOcr(str(target), force_cpu=not use_cuda)
    _write_ready(target, "mangaocr", "kha-white/manga-ocr-base")
    print(f"MangaOCR 已就绪（{'CUDA' if use_cuda else 'CPU'}）")


def prepare_paddle(root: Path, requested_device: str) -> None:
    import torch  # noqa: F401 - preload PyTorch CUDA/cuDNN DLLs before ONNX Runtime.
    from paddleocr import TextRecognition

    device, _ = _device_name(requested_device)
    for language in ("ko", "en"):
        name = paddle_model_name(language)
        target = paddle_model_dir(root, language)
        print(f"准备 PaddleOCR {language}: {target}")
        TextRecognition(model_name=name, device=device, engine="onnxruntime")
        if not target.is_dir():
            raise RuntimeError(f"PaddleOCR 未在预期缓存目录生成模型：{target}")
        _write_ready(target, "paddleocr", name)
    print(f"PaddleOCR 已就绪（{device}）")


def main() -> None:
    parser = argparse.ArgumentParser(description="显式下载并验证 Phase 3 OCR 模型")
    parser.add_argument(
        "--provider",
        choices=("all", "mangaocr", "paddleocr"),
        default="all",
    )
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    args = parser.parse_args()

    settings = get_settings()
    settings.ensure_directories()
    root = settings.ocr_models_dir.resolve()
    os.environ["HF_HOME"] = str(root / "huggingface")
    os.environ.pop("HF_HUB_OFFLINE", None)
    os.environ["PADDLE_PDX_CACHE_HOME"] = str(root / "paddlex")
    os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
    print(f"OCR 模型缓存：{root}")
    if args.provider in {"all", "mangaocr"}:
        prepare_manga(root, args.device)
    if args.provider in {"all", "paddleocr"}:
        prepare_paddle(root, args.device)


if __name__ == "__main__":
    main()
