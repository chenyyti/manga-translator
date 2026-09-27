from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

LLM_PROVIDER_IDS = (
    "openai",
    "openai_compatible",
    "deepseek",
    "qwen",
    "claude",
    "gemini",
)
_JAPANESE_KANA = re.compile(r"[\u3040-\u30ff]")


def is_copied_japanese_text(
    source_text: str, target_text: str | None, source_language: str, target_language: str
) -> bool:
    return (
        source_language.lower().split("-", 1)[0] == "ja"
        and target_language.lower().split("-", 1)[0] != "ja"
        and target_text is not None
        and bool(_JAPANESE_KANA.search(source_text))
        and source_text.strip() == target_text.strip()
    )


class LLMError(RuntimeError):
    """Base class for safe, user-facing provider errors."""

    code = "LLM_FAILED"
    retryable = False


class LLMDependencyError(LLMError):
    code = "LLM_DEPENDENCY_MISSING"


class LLMSecretStoreError(LLMError):
    code = "SECRET_STORE_UNAVAILABLE"


class LLMAuthError(LLMError):
    code = "LLM_AUTH_FAILED"


class LLMProtocolError(LLMError):
    code = "LLM_INVALID_RESPONSE"


class LLMTransientError(LLMError):
    code = "LLM_TRANSIENT_ERROR"
    retryable = True


@dataclass(frozen=True)
class LLMProviderInfo:
    id: str
    name: str
    supported_languages: tuple[str, ...]
    default_base_url: str | None
    installed: bool
    version: str | None
    protocol: str
    model_ready: bool = True
    available_devices: tuple[str, ...] = ("remote",)


@dataclass(frozen=True)
class TranslationRegion:
    region_id: str
    source_text: str
    class_name: str
    reading_order: int
    sfx_strategy: str | None = None


@dataclass(frozen=True)
class TranslationInput:
    source_language: str
    target_language: str
    regions: tuple[TranslationRegion, ...]
    context: tuple[dict[str, object], ...] = field(default_factory=tuple)
    repair_error: str | None = None


@dataclass(frozen=True)
class TranslationResult:
    translations: dict[str, str]
    provider: str
    model: str
    latency_ms: int | None = None
    diagnostics: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class LLMProfileConfig:
    id: str
    provider: str
    base_url: str
    model: str
    api_key: str
    temperature: float | None = None
    max_tokens: int = 2048
    timeout_seconds: int = 60
    max_concurrency: int = 3


class LLMRuntime(Protocol):
    async def providers(self) -> list[LLMProviderInfo]: ...

    async def translate_page(
        self, profile: LLMProfileConfig, payload: TranslationInput
    ) -> TranslationResult: ...

    async def test_connection(self, profile: LLMProfileConfig) -> dict[str, object]: ...

    async def close(self) -> None: ...


# Public protocol name used by the application/provider registry.
LLMProvider = LLMRuntime
