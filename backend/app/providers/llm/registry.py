from __future__ import annotations

from app.providers.llm.base import LLM_PROVIDER_IDS, LLMProviderInfo

PROVIDER_LANGUAGES: dict[str, tuple[str, ...]] = {
    "openai": ("ja", "ko", "en"),
    "openai_compatible": ("ja", "ko", "en"),
    "deepseek": ("ja", "ko", "en"),
    "qwen": ("ja", "ko", "en"),
    "claude": ("ja", "ko", "en"),
    "gemini": ("ja", "ko", "en"),
}

DEFAULT_BASE_URLS: dict[str, str | None] = {
    "openai": "https://api.openai.com/v1",
    "openai_compatible": None,
    "deepseek": "https://api.deepseek.com",
    "qwen": None,
    "claude": "https://api.anthropic.com",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
}

PROVIDER_NAMES = {
    "openai": "OpenAI",
    "openai_compatible": "OpenAI Compatible",
    "deepseek": "DeepSeek",
    "qwen": "Qwen",
    "claude": "Claude",
    "gemini": "Gemini",
}


def resolve_provider(provider: str) -> str:
    if provider not in LLM_PROVIDER_IDS:
        raise ValueError(f"不支持的 LLM Provider: {provider}")
    return provider


def provider_infos(*, installed: bool = True) -> list[LLMProviderInfo]:
    return [
        LLMProviderInfo(
            id=provider,
            name=PROVIDER_NAMES[provider],
            supported_languages=PROVIDER_LANGUAGES[provider],
            default_base_url=DEFAULT_BASE_URLS[provider],
            installed=installed,
            version="httpx",
            protocol=(
                "claude_messages"
                if provider == "claude"
                else "gemini_generate_content"
                if provider == "gemini"
                else "openai_chat_completions"
            ),
            model_ready=True,
            available_devices=("remote",),
        )
        for provider in LLM_PROVIDER_IDS
    ]
