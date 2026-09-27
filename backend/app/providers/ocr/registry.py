from __future__ import annotations

from app.db.models import OCRSettings

PROVIDER_LANGUAGES: dict[str, frozenset[str]] = {
    "mangaocr": frozenset({"ja"}),
    "paddleocr": frozenset({"ko", "en"}),
}

BUILTIN_DEFAULTS = {"ja": "mangaocr", "ko": "paddleocr", "en": "paddleocr"}


def fixed_provider_for_language(language: str) -> str:
    try:
        return BUILTIN_DEFAULTS[language]
    except KeyError as exc:
        raise ValueError("不支持的 OCR 语言") from exc


def resolve_provider(
    language: str,
    project_provider: str,
    configured: OCRSettings,
    requested: str | None = None,
) -> str:
    del configured  # Retained in the signature for persisted-task compatibility.
    fixed = fixed_provider_for_language(language)
    for choice in (project_provider, requested):
        if choice not in {None, "auto", fixed}:
            raise ValueError("OCR Provider 已按项目源语言固定")
    if fixed not in PROVIDER_LANGUAGES:
        raise ValueError("OCR Provider 不存在")
    return fixed
