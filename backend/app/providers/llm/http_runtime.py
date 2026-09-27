from __future__ import annotations

import asyncio
import json
import random
import re
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from ipaddress import ip_address
from urllib.parse import urlsplit

import httpx

from app.providers.json_response import parse_json_response
from app.providers.llm.base import (
    LLMAuthError,
    LLMError,
    LLMProfileConfig,
    LLMProtocolError,
    LLMProviderInfo,
    LLMRuntime,
    LLMTransientError,
    TranslationInput,
    TranslationRegion,
    TranslationResult,
    is_copied_japanese_text,
)
from app.providers.llm.registry import provider_infos

TRANSLATION_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "translations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "region_id": {"type": "string"},
                    "translation": {"type": "string"},
                },
                "required": ["region_id", "translation"],
            },
        }
    },
    "required": ["translations"],
}

_REASONING_BLOCK = re.compile(
    r"<(?P<tag>think|analysis|reasoning)\b[^>]*>.*?</(?P=tag)\s*>",
    re.IGNORECASE | re.DOTALL,
)
_PLAIN_TEXT_FENCE = re.compile(
    r"\A```(?:text|plaintext)?[ \t]*\n?(?P<text>.*?)\n?```\Z",
    re.IGNORECASE | re.DOTALL,
)


@dataclass
class _TranslationMetrics:
    queue_wait_ms: float = 0.0
    provider_ms: float = 0.0
    retry_sleep_ms: float = 0.0
    provider_requests: int = 0
    http_attempts: int = 0
    transport_retries: int = 0
    protocol_repairs: int = 0
    split_batches: int = 0
    prompt_chars: int = 0


def validate_base_url(value: str) -> str:
    """Validate a profile URL before it can be used for an outbound request."""
    text = value.strip().rstrip("/")
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Base URL 必须是 http 或 https 地址")
    # Accessing ``port`` also validates malformed ports (for example
    # ``:not-a-port``) before a profile can be persisted or used.
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("Base URL 端口无效") from exc
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Base URL 不得包含用户信息、查询参数或 fragment")
    host = parsed.hostname.casefold()
    loopback = host in {"localhost", "127.0.0.1", "::1"}
    if not loopback:
        try:
            loopback = ip_address(host).is_loopback
        except ValueError:
            pass
    if parsed.scheme == "http" and not loopback:
        raise ValueError("远程 Provider 必须使用 HTTPS")
    return text


def _json_text(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _parse_translations(raw: object, expected_ids: set[str]) -> dict[str, str]:
    try:
        payload = parse_json_response(raw)
    except ValueError as exc:
        raise LLMProtocolError("Provider 返回的内容不是有效 JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("translations"), list):
        raise LLMProtocolError("Provider 返回缺少 translations 数组")
    result: dict[str, str] = {}
    for item in payload["translations"]:
        if not isinstance(item, dict):
            raise LLMProtocolError("Provider 返回了无效区域项")
        region_id = item.get("region_id")
        translation = item.get("translation")
        if not isinstance(region_id, str) or not isinstance(translation, str):
            raise LLMProtocolError("Provider 区域项字段类型错误")
        if region_id not in expected_ids or region_id in result:
            raise LLMProtocolError("Provider 返回了未知或重复的区域 ID")
        if len(translation) > 10_000:
            raise LLMProtocolError("Provider 返回的译文超过 10,000 字符")
        normalized_translation = translation.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not normalized_translation:
            raise LLMProtocolError("Provider 返回了空译文")
        result[region_id] = normalized_translation
    if set(result) != expected_ids:
        raise LLMProtocolError("Provider 未准确覆盖所有请求区域")
    return result


def _parse_single_plain_translation(raw: object, region_id: str) -> dict[str, str]:
    """Accept a clean plain-text fallback for one region only.

    Some OpenAI-compatible providers ignore ``response_format`` for very short
    inputs and return only the translated sentence.  This fallback is kept
    deliberately narrow: structured-looking or fenced non-text output is still
    rejected instead of being written into the translation field.
    """
    if not isinstance(raw, str):
        raise LLMProtocolError("Provider 返回的内容不是有效 JSON")

    text = _REASONING_BLOCK.sub("", raw).strip()
    try:
        decoded = parse_json_response(text)
    except ValueError:
        decoded = None
    if isinstance(decoded, str):
        text = decoded.strip()
    elif decoded is not None:
        raise LLMProtocolError("Provider 返回缺少 translations 数组")

    fenced = _PLAIN_TEXT_FENCE.fullmatch(text)
    if fenced:
        text = fenced.group("text").strip()

    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if (
        not text
        or len(text) > 10_000
        or text.startswith(("{", "["))
        or "```" in text
        or _REASONING_BLOCK.search(text)
    ):
        raise LLMProtocolError("Provider 返回的内容不是有效 JSON")
    return {region_id: text}


def _validate_translation_content(
    payload: TranslationInput, translations: dict[str, str]
) -> dict[str, str]:
    for region in payload.regions:
        if (
            region.sfx_strategy is None
            and is_copied_japanese_text(
                region.source_text,
                translations[region.region_id],
                payload.source_language,
                payload.target_language,
            )
        ):
            raise LLMProtocolError("普通文本仍是日文原文，请翻译后返回")
    return translations


def build_translation_prompt(payload: TranslationInput) -> tuple[str, str]:
    regions = [
        {
            "region_id": item.region_id,
            "source_text": item.source_text,
            "class": item.class_name,
            "reading_order": item.reading_order,
            "sfx_strategy": item.sfx_strategy,
        }
        for item in payload.regions
    ]
    context = list(payload.context)
    system = (
        "You are a careful manga translator. Translate only the supplied text into "
        f"{payload.target_language}. Preserve region IDs exactly. Return exactly one "
        "JSON object; do not output reasoning, <think> tags, "
        "Markdown fences, or explanatory text outside the object. Use exactly this "
        'shape: {"translations":[{"region_id":"the supplied id","translation":"translated text"}]}. '
        "do not invent missing text. sfx_strategy=null means ordinary text: translate it "
        "and do not apply sound-effect preservation. Only for sound effects: "
        "for sfx_strategy=preserve, keep the source text; for replace, translate the "
        "sound effect naturally; for bilingual, include the "
        "source sound effect followed by its Chinese rendering in the same region."
    )
    user: dict[str, object] = {
        "source_language": payload.source_language,
        "target_language": payload.target_language,
        "regions": regions,
        "previous_pages": context,
    }
    if payload.repair_error:
        user["validation_error"] = payload.repair_error
        user["instruction"] = "修复上一响应，只返回完整且不重复的 translations。"
    return system, json.dumps(user, ensure_ascii=False)


def _gemini_schema(value: object) -> object:
    if isinstance(value, dict):
        converted = {
            key: _gemini_schema(item)
            for key, item in value.items()
            if key != "additionalProperties"
        }
        schema_type = converted.get("type")
        if isinstance(schema_type, str):
            converted["type"] = {
                "object": "OBJECT",
                "array": "ARRAY",
                "string": "STRING",
                "number": "NUMBER",
                "integer": "INTEGER",
                "boolean": "BOOLEAN",
            }.get(schema_type.casefold(), schema_type)
        return converted
    if isinstance(value, list):
        return [_gemini_schema(item) for item in value]
    return value


class HttpLLMRuntime(LLMRuntime):
    """Direct, non-streaming HTTP adapters for all configured LLM providers."""

    def __init__(
        self,
        *,
        max_retries: int = 3,
        retry_base_seconds: float = 1.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.max_retries = max(0, min(max_retries, 3))
        self.retry_base_seconds = max(0.1, retry_base_seconds)
        self.transport = transport
        self._profile_semaphores: dict[tuple[str, int], asyncio.Semaphore] = {}
        self._client = httpx.AsyncClient(
            follow_redirects=False,
            transport=transport,
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
        )

    async def providers(self) -> list[LLMProviderInfo]:
        return provider_infos(installed=True)

    async def close(self) -> None:
        await self._client.aclose()

    async def translate_page(
        self, profile: LLMProfileConfig, payload: TranslationInput
    ) -> TranslationResult:
        if profile.provider not in {item.id for item in await self.providers()}:
            raise LLMError("不支持的 LLM Provider")
        expected_ids = {region.region_id for region in payload.regions}
        if not expected_ids:
            return TranslationResult({}, profile.provider, profile.model)
        started = time.perf_counter()
        metrics = _TranslationMetrics()
        translations = await self._translate_regions_locked(
            profile, payload, expected_ids, metrics
        )
        total_ms = (time.perf_counter() - started) * 1000
        return TranslationResult(
            translations=translations,
            provider=profile.provider,
            model=profile.model,
            latency_ms=int(total_ms),
            diagnostics={
                "regions": len(payload.regions),
                "profile_max_concurrency": profile.max_concurrency,
                "queue_wait_ms": round(metrics.queue_wait_ms, 2),
                "provider_ms": round(metrics.provider_ms, 2),
                "retry_sleep_ms": round(metrics.retry_sleep_ms, 2),
                "provider_requests": metrics.provider_requests,
                "http_attempts": metrics.http_attempts,
                "transport_retries": metrics.transport_retries,
                "protocol_repairs": metrics.protocol_repairs,
                "split_batches": metrics.split_batches,
                "prompt_chars": metrics.prompt_chars,
                "total_ms": round(total_ms, 2),
            },
        )

    async def _translate_regions_locked(
        self,
        profile: LLMProfileConfig,
        payload: TranslationInput,
        expected_ids: set[str],
        metrics: _TranslationMetrics,
    ) -> dict[str, str]:
        """Translate one request and recover from provider output truncation.

        The first retry asks the provider to repair its JSON.  If that still
        fails for a multi-region page, split the request into two smaller
        requests.  Reasoning models often spend the token budget on analysis,
        so this fallback prevents a large page from failing only because its
        final JSON was truncated.  The caller still validates every response
        before any translation is persisted.
        """
        system, user = build_translation_prompt(payload)
        metrics.prompt_chars += len(system) + len(user)
        raw = await self._request_provider(profile, system, user, metrics)
        try:
            return _validate_translation_content(
                payload, self._extract_and_parse(profile.provider, raw, expected_ids)
            )
        except LLMProtocolError as first_error:
            metrics.protocol_repairs += 1
            repair_payload = replace(payload, repair_error=str(first_error))
            repair_system, repair_user = build_translation_prompt(repair_payload)
            metrics.prompt_chars += len(repair_system) + len(repair_user)
            repaired = await self._request_provider(
                profile, repair_system, repair_user, metrics
            )
            try:
                return _validate_translation_content(
                    payload, self._extract_and_parse(profile.provider, repaired, expected_ids)
                )
            except LLMProtocolError as second_error:
                if len(payload.regions) <= 1:
                    region_id = payload.regions[0].region_id
                    content = self._extract_content(profile.provider, repaired)
                    try:
                        plain_translation = _parse_single_plain_translation(content, region_id)
                    except LLMProtocolError:
                        raise second_error from None
                    return _validate_translation_content(payload, plain_translation)
                midpoint = len(payload.regions) // 2
                left_payload = replace(payload, regions=payload.regions[:midpoint], repair_error=None)
                right_payload = replace(payload, regions=payload.regions[midpoint:], repair_error=None)
                metrics.split_batches += 1
                left, right = await asyncio.gather(
                    self._translate_regions_locked(
                        profile,
                        left_payload,
                        {region.region_id for region in left_payload.regions},
                        metrics,
                    ),
                    self._translate_regions_locked(
                        profile,
                        right_payload,
                        {region.region_id for region in right_payload.regions},
                        metrics,
                    ),
                )
                return left | right

    async def test_connection(self, profile: LLMProfileConfig) -> dict[str, object]:
        tiny = TranslationInput(
            source_language="en",
            target_language="zh-CN",
            regions=(
                # This request is deliberately tiny; users are warned in the UI.
                TranslationRegion(
                    region_id="test", source_text="Hello", class_name="dialogue", reading_order=1
                ),
            ),
        )
        result = await self.translate_page(
            replace(profile, max_tokens=min(profile.max_tokens, 64)), tiny
        )
        return {"provider": result.provider, "model": result.model, "latency_ms": result.latency_ms}

    async def _request_provider(
        self,
        profile: LLMProfileConfig,
        system: str,
        user: str,
        metrics: _TranslationMetrics,
    ) -> object:
        validate_base_url(profile.base_url)
        headers = {"Content-Type": "application/json"}
        if profile.provider == "gemini":
            headers["x-goog-api-key"] = profile.api_key
            url = f"{profile.base_url.rstrip('/')}/models/{profile.model}:generateContent"
            body = self._gemini_body(profile, system, user)
        elif profile.provider == "claude":
            headers.update({"x-api-key": profile.api_key, "anthropic-version": "2023-06-01"})
            base = profile.base_url.rstrip("/")
            url = f"{base}/messages" if base.endswith("/v1") else f"{base}/v1/messages"
            body = self._claude_body(profile, system, user)
        else:
            headers["Authorization"] = f"Bearer {profile.api_key}"
            url = f"{profile.base_url.rstrip('/')}/chat/completions"
            body = self._openai_body(profile, system, user)
        concurrency = max(1, profile.max_concurrency)
        key = (profile.id, concurrency)
        semaphore = self._profile_semaphores.setdefault(
            key, asyncio.Semaphore(concurrency)
        )
        wait_started = time.perf_counter()
        async with semaphore:
            metrics.queue_wait_ms += (time.perf_counter() - wait_started) * 1000
            provider_started = time.perf_counter()
            try:
                metrics.provider_requests += 1
                try:
                    return await self._request_json(
                        url, headers, body, profile.timeout_seconds, metrics
                    )
                except LLMError as exc:
                    # Some OpenAI-compatible gateways reject response_format.
                    # Retry once without it while retaining strict validation.
                    if (
                        profile.provider in {"openai_compatible", "deepseek", "qwen"}
                        and "response_format" in body
                        and not isinstance(
                            exc, LLMAuthError | LLMTransientError | LLMProtocolError
                        )
                    ):
                        body.pop("response_format", None)
                        metrics.provider_requests += 1
                        return await self._request_json(
                            url, headers, body, profile.timeout_seconds, metrics
                        )
                    raise
            finally:
                metrics.provider_ms += (time.perf_counter() - provider_started) * 1000

    @staticmethod
    def _openai_body(profile: LLMProfileConfig, system: str, user: str) -> dict[str, object]:
        body: dict[str, object] = {
            "model": profile.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        # DeepSeek's reasoning shares the output budget with the final answer.
        # Use its server default instead of the legacy 2048-token profile cap.
        if profile.provider != "deepseek":
            body["max_tokens"] = profile.max_tokens
        if profile.temperature is not None:
            body["temperature"] = profile.temperature
        if profile.provider == "openai":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "manga_translations",
                    "strict": True,
                    "schema": TRANSLATION_SCHEMA,
                },
            }
        else:
            body["response_format"] = {"type": "json_object"}
        return body

    @staticmethod
    def _claude_body(profile: LLMProfileConfig, system: str, user: str) -> dict[str, object]:
        body: dict[str, object] = {
            "model": profile.model,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "max_tokens": profile.max_tokens,
            "tools": [
                {
                    "name": "submit_translations",
                    "description": "Return all translations",
                    "input_schema": TRANSLATION_SCHEMA,
                }
            ],
            "tool_choice": {"type": "tool", "name": "submit_translations"},
        }
        if profile.temperature is not None:
            body["temperature"] = profile.temperature
        return body

    @staticmethod
    def _gemini_body(profile: LLMProfileConfig, system: str, user: str) -> dict[str, object]:
        body: dict[str, object] = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "maxOutputTokens": profile.max_tokens,
                "responseMimeType": "application/json",
                "responseSchema": _gemini_schema(TRANSLATION_SCHEMA),
            },
        }
        if profile.temperature is not None:
            body["generationConfig"]["temperature"] = profile.temperature  # type: ignore[index]
        return body

    async def _request_json(
        self,
        url: str,
        headers: dict[str, str],
        body: dict[str, object],
        timeout: int,
        metrics: _TranslationMetrics,
    ) -> object:
        for attempt in range(self.max_retries + 1):
            metrics.http_attempts += 1
            try:
                response = await self._client.post(
                    url,
                    headers=headers,
                    json=body,
                    timeout=httpx.Timeout(timeout),
                )
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                if attempt < self.max_retries:
                    metrics.transport_retries += 1
                    sleep_started = time.perf_counter()
                    await asyncio.sleep(self._retry_delay(attempt, None))
                    metrics.retry_sleep_ms += (time.perf_counter() - sleep_started) * 1000
                    continue
                raise LLMTransientError("Provider 请求超时或连接失败") from exc
            if response.status_code in {401, 403}:
                raise LLMAuthError("Provider 认证失败")
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < self.max_retries:
                    metrics.transport_retries += 1
                    sleep_started = time.perf_counter()
                    await asyncio.sleep(
                        self._retry_delay(attempt, response.headers.get("Retry-After"))
                    )
                    metrics.retry_sleep_ms += (time.perf_counter() - sleep_started) * 1000
                    continue
                raise LLMTransientError("Provider 暂时不可用")
            if response.status_code >= 400:
                raise LLMError(f"Provider 请求被拒绝（HTTP {response.status_code}）")
            try:
                return response.json()
            except ValueError as exc:
                raise LLMProtocolError("Provider 返回不是 JSON") from exc
        raise LLMTransientError("Provider 请求失败")

    def _retry_delay(self, attempt: int, retry_after: str | None) -> float:
        try:
            server_delay = min(60.0, max(0.0, float(retry_after or "0")))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_after or "")
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=UTC)
                server_delay = min(60.0, max(0.0, (retry_at - datetime.now(UTC)).total_seconds()))
            except (TypeError, ValueError, OverflowError):
                server_delay = 0.0
        base = min(60.0, self.retry_base_seconds * (2**attempt))
        return min(60.0, max(base, server_delay) + random.uniform(0, 0.25))

    @staticmethod
    def _extract_content(provider: str, raw: object) -> object:
        content: object
        if not isinstance(raw, dict):
            raise LLMProtocolError("Provider 响应格式错误")
        if provider == "claude":
            blocks = raw.get("content")
            if not isinstance(blocks, list):
                raise LLMProtocolError("Claude 响应缺少 content")
            tool = next(
                (
                    item
                    for item in blocks
                    if (
                        isinstance(item, dict)
                        and item.get("type") == "tool_use"
                        and item.get("name") == "submit_translations"
                    )
                ),
                None,
            )
            if not isinstance(tool, dict) or not isinstance(tool.get("input"), dict):
                raise LLMProtocolError("Claude 未返回 submit_translations 工具结果")
            content = tool["input"]
        elif provider == "gemini":
            candidates = raw.get("candidates")
            if not isinstance(candidates, list) or not candidates:
                raise LLMProtocolError("Gemini 响应缺少 candidates")
            candidate = candidates[0] if isinstance(candidates[0], dict) else {}
            content = candidate.get("content") if isinstance(candidate, dict) else None
            parts = content.get("parts", []) if isinstance(content, dict) else []
            text = next(
                (
                    part.get("text")
                    for part in parts
                    if isinstance(part, dict) and isinstance(part.get("text"), str)
                ),
                None,
            )
            if text is None:
                raise LLMProtocolError("Gemini 响应缺少文本")
            content = text
        else:
            choices = raw.get("choices")
            if not isinstance(choices, list) or not choices:
                raise LLMProtocolError("Chat Completions 响应缺少 choices")
            message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                text_parts = [
                    item
                    if isinstance(item, str)
                    else item.get("text")
                    for item in content
                    if isinstance(item, str)
                    or (isinstance(item, dict) and isinstance(item.get("text"), str))
                ]
                content = "\n".join(text_parts) if text_parts else None
            if content is None:
                raise LLMProtocolError("Chat Completions 响应缺少 content")
        return content

    @staticmethod
    def _extract_and_parse(provider: str, raw: object, expected_ids: set[str]) -> dict[str, str]:
        content = HttpLLMRuntime._extract_content(provider, raw)
        return _parse_translations(content, expected_ids)
