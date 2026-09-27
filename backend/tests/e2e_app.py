from __future__ import annotations

import shutil
from pathlib import Path

from app.core.config import get_settings
from app.main import create_app
from app.providers.detection.base import (
    DetectionModelInfo,
    DetectionPrediction,
    DetectionRuntimeInfo,
)
from app.providers.inpainting.base import InpaintingProviderInfo, InpaintingResult
from app.providers.llm.base import (
    LLMProviderInfo,
    TranslationResult,
)
from app.providers.llm.secrets import MemorySecretStore
from app.providers.ocr.base import OCRProviderInfo, OCRRecognition


class E2EOCRRuntime:
    async def providers(self) -> list[OCRProviderInfo]:
        return [
            OCRProviderInfo(
                "mangaocr",
                "MangaOCR",
                ("ja",),
                True,
                "e2e",
                True,
                ("e2e-ja",),
                False,
            ),
            OCRProviderInfo(
                "paddleocr",
                "PaddleOCR",
                ("ko", "en"),
                True,
                "e2e",
                True,
                ("e2e-ko", "e2e-en"),
                False,
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
        assert image_path.is_file()
        text = {"ja": "テスト原文", "ko": "테스트 원문", "en": "Test source"}[language]
        confidence = None if provider == "mangaocr" else 0.98
        return OCRRecognition(text, confidence, "cpu")

    async def close(self) -> None:
        return None


class E2EDetectionRuntime:
    async def status(self) -> DetectionRuntimeInfo:
        return DetectionRuntimeInfo("e2e", "e2e", False, None, None)

    async def inspect(self, model_path: Path) -> DetectionModelInfo:
        raise RuntimeError("E2E 不导入检测模型")

    async def predict(
        self,
        model_path: Path,
        image_path: Path,
        *,
        confidence: float,
        image_size: int,
        device: str,
    ) -> DetectionPrediction:
        raise RuntimeError("E2E 不执行检测推理")

    async def close(self) -> None:
        return None


class E2ELLMRuntime:
    async def providers(self) -> list[LLMProviderInfo]:
        return [
            LLMProviderInfo(
                provider,
                provider,
                ("ja", "ko", "en"),
                None,
                True,
                "e2e",
                "chat_completions",
                True,
                ("remote",),
            )
            for provider in ("openai", "openai_compatible", "deepseek", "qwen", "claude", "gemini")
        ]

    async def translate_page(self, profile, payload):
        return TranslationResult(
            {region.region_id: f"测试译文-{region.region_id}" for region in payload.regions},
            profile.provider,
            profile.model,
        )

    async def test_connection(self, profile):
        return {"provider": profile.provider, "model": profile.model, "latency_ms": 1}

    async def close(self) -> None:
        return None


class E2EInpaintingRuntime:
    async def providers(self) -> list[InpaintingProviderInfo]:
        return [
            InpaintingProviderInfo("auto", "自动", True, "e2e", True, None, ("cpu",), "e2e"),
            InpaintingProviderInfo("fast", "FAST", True, "e2e", True, None, ("cpu",), "e2e"),
            InpaintingProviderInfo("opencv", "OpenCV", True, "e2e", True, None, ("cpu",), "e2e"),
            InpaintingProviderInfo("lama", "Big-LaMa", True, "e2e", True, "big-lama", ("cpu",), "e2e"),
        ]

    async def inpaint(self, provider, image_path, mask_path, output_path, *, device, max_edge, opencv_radius):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(image_path, output_path)
        return InpaintingResult(output_path, provider, "cpu")

    async def close(self) -> None:
        return None


app = create_app(
    get_settings(),
    detection_runtime=E2EDetectionRuntime(),
    ocr_runtime=E2EOCRRuntime(),
    llm_runtime=E2ELLMRuntime(),
    inpainting_runtime=E2EInpaintingRuntime(),
    secret_store=MemorySecretStore(),
)
