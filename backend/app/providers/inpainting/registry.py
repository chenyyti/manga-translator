from __future__ import annotations

from .base import INPAINTING_PROVIDER_IDS, InpaintingProviderInfo

PROVIDER_NAMES = {
    "auto": "Auto",
    "fast": "FAST 背景填充",
    "opencv": "OpenCV Telea",
    "lama": "Big-LaMa",
}


def provider_infos(
    *,
    opencv_installed: bool,
    lama_installed: bool,
    lama_model_ready: bool,
    opencv_version: str | None,
    torch_version: str | None,
    cuda_available: bool,
) -> list[InpaintingProviderInfo]:
    devices = ("cpu", "cuda") if cuda_available else ("cpu",)
    return [
        InpaintingProviderInfo(
            id="auto",
            name=PROVIDER_NAMES["auto"],
            installed=True,
            version=None,
            model_ready=True,
            model_name=None,
            available_devices=devices,
            description="按背景复杂度选择 FAST、OpenCV 或 Big-LaMa",
        ),
        InpaintingProviderInfo(
            id="fast",
            name=PROVIDER_NAMES["fast"],
            installed=True,
            version=None,
            model_ready=True,
            model_name=None,
            available_devices=("cpu",),
            description="使用区域外缘的稳健背景色填充",
        ),
        InpaintingProviderInfo(
            id="opencv",
            name=PROVIDER_NAMES["opencv"],
            installed=opencv_installed,
            version=opencv_version,
            model_ready=opencv_installed,
            model_name="Telea",
            available_devices=("cpu",),
            description="使用 OpenCV Telea 算法修复普通背景",
        ),
        InpaintingProviderInfo(
            id="lama",
            name=PROVIDER_NAMES["lama"],
            installed=lama_installed,
            version=torch_version,
            model_ready=lama_model_ready,
            model_name="big-lama.pt",
            available_devices=devices,
            description="使用本地 Big-LaMa TorchScript 模型",
        ),
    ]


def resolve_provider(provider: str) -> str:
    if provider not in INPAINTING_PROVIDER_IDS:
        raise ValueError(f"不支持的图像修复模式: {provider}")
    return provider
