from __future__ import annotations

import asyncio
import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.core.config import Settings
from app.main import create_app
from app.providers.llm.base import (
    LLMProfileConfig,
    LLMProtocolError,
    TranslationInput,
    TranslationRegion,
)
from app.providers.llm.http_runtime import (
    HttpLLMRuntime,
    build_translation_prompt,
    validate_base_url,
)
from app.providers.llm.secrets import MemorySecretStore
from tests.helpers import wait_for_task


@pytest.mark.parametrize("provider", ["deepseek", "openai", "openai_compatible"])
def test_output_budget_uses_server_default_only_for_deepseek(provider: str) -> None:
    config = LLMProfileConfig(
        id="budget-test", provider=provider, base_url="https://example.com/v1",
        model="test", api_key="test", max_tokens=2048,
    )
    body = HttpLLMRuntime._openai_body(config, "system", "user")
    if provider == "deepseek":
        assert "max_tokens" not in body
        assert "max_completion_tokens" not in body
    else:
        assert body["max_tokens"] == 2048


def profile(
    provider: str, *, profile_id: str = "p", max_concurrency: int = 3
) -> LLMProfileConfig:
    return LLMProfileConfig(
        id=profile_id,
        provider=provider,
        base_url="https://example.test/v1",
        model="model",
        api_key="secret",
        max_concurrency=max_concurrency,
    )


def request_input() -> TranslationInput:
    return TranslationInput(
        source_language="ja",
        target_language="zh-CN",
        regions=(TranslationRegion("r1", "こんにちは", "dialogue", 1),),
    )


def test_prompt_distinguishes_ordinary_text_from_sound_effects() -> None:
    payload = TranslationInput(
        source_language="ja",
        target_language="zh-CN",
        regions=(
            TranslationRegion("dialogue", "お前と二人で？", "text", 1),
            TranslationRegion("sfx", "ドン", "sfx", 2, "preserve"),
        ),
    )
    system, user = build_translation_prompt(payload)
    regions = json.loads(user)["regions"]
    assert regions[0]["sfx_strategy"] is None
    assert regions[1]["sfx_strategy"] == "preserve"
    assert "sfx_strategy=null means ordinary text" in system


@pytest.mark.asyncio
async def test_copied_japanese_dialogue_is_retried_before_accepting_translation() -> None:
    replies = ["こんにちは", "你好"]
    requests: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        translation = replies.pop(0)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"translations": [{"region_id": "r1", "translation": translation}]},
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
        )

    runtime = HttpLLMRuntime(transport=httpx.MockTransport(handler), max_retries=0)
    result = await runtime.translate_page(profile("deepseek"), request_input())
    assert result.translations == {"r1": "你好"}
    assert len(requests) == 2
    assert "普通文本仍是日文原文" in json.loads(requests[1]["messages"][1]["content"])[
        "validation_error"
    ]


@pytest.mark.asyncio
async def test_copied_japanese_dialogue_is_rejected_after_retry() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"translations":[{"region_id":"r1","translation":"こんにちは"}]}'
                        }
                    }
                ]
            },
        )

    runtime = HttpLLMRuntime(transport=httpx.MockTransport(handler), max_retries=0)
    with pytest.raises(LLMProtocolError, match="普通文本仍是日文原文"):
        await runtime.translate_page(profile("deepseek"), request_input())


@pytest.mark.asyncio
async def test_preserved_sound_effect_can_match_its_source() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"translations":[{"region_id":"sfx","translation":"ドン"}]}'
                        }
                    }
                ]
            },
        )

    payload = TranslationInput(
        source_language="ja",
        target_language="zh-CN",
        regions=(TranslationRegion("sfx", "ドン", "sfx", 1, "preserve"),),
    )
    runtime = HttpLLMRuntime(transport=httpx.MockTransport(handler), max_retries=0)
    result = await runtime.translate_page(profile("deepseek"), payload)
    assert result.translations == {"sfx": "ドン"}


@pytest.mark.asyncio
async def test_openai_schema_and_response_are_strictly_validated() -> None:
    seen: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append({})
        payload = {
            "choices": [
                {
                    "message": {
                        "content": '{"translations":[{"region_id":"r1","translation":"你好"}]}'
                    }
                }
            ]
        }
        seen[-1] = json.loads(request.content)
        return httpx.Response(200, json=payload)

    runtime = HttpLLMRuntime(transport=httpx.MockTransport(handler), max_retries=0)
    result = await runtime.translate_page(profile("openai"), request_input())
    assert result.translations == {"r1": "你好"}
    assert seen[0]["response_format"]["type"] == "json_schema"
    assert seen[0]["response_format"]["json_schema"]["strict"] is True


@pytest.mark.asyncio
async def test_claude_tool_and_gemini_structured_response() -> None:
    async def claude_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "tool_use",
                        "name": "submit_translations",
                        "input": {"translations": [{"region_id": "r1", "translation": "你好"}]},
                    }
                ]
            },
        )

    async def gemini_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": (
                                        '{"translations":[{"region_id":"r1","translation":"你好"}]}'
                                    )
                                }
                            ]
                        }
                    }
                ]
            },
        )

    claude = HttpLLMRuntime(transport=httpx.MockTransport(claude_handler), max_retries=0)
    gemini = HttpLLMRuntime(transport=httpx.MockTransport(gemini_handler), max_retries=0)
    assert (await claude.translate_page(profile("claude"), request_input())).translations[
        "r1"
    ] == "你好"
    assert (await gemini.translate_page(profile("gemini"), request_input())).translations[
        "r1"
    ] == "你好"


@pytest.mark.asyncio
async def test_provider_json_wrappers_and_content_blocks_are_supported() -> None:
    wrapped = (
        "<think>先分析输入，但不要把分析当作结果。</think>\n"
        "下面是结果：\n"
        "```json\n"
        '{"translations":[{"region_id":"r1","translation":"你好"}]}\n'
        "```\n"
        ""
    )

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": [
                                {"type": "text", "text": wrapped},
                            ]
                        }
                    }
                ]
            },
        )

    runtime = HttpLLMRuntime(transport=httpx.MockTransport(handler), max_retries=0)
    result = await runtime.translate_page(profile("deepseek"), request_input())
    assert result.translations == {"r1": "你好"}


@pytest.mark.asyncio
async def test_single_region_plain_text_is_accepted_after_structured_retry() -> None:
    requests: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "你在干什么啊？"}}]},
        )

    runtime = HttpLLMRuntime(transport=httpx.MockTransport(handler), max_retries=0)
    result = await runtime.translate_page(profile("deepseek"), request_input())

    assert result.translations == {"r1": "你在干什么啊？"}
    assert len(requests) == 2
    assert '"translations"' in requests[0]["messages"][0]["content"]
    repair_prompt = json.loads(requests[1]["messages"][1]["content"])
    assert repair_prompt["validation_error"] == "Provider 返回的内容不是有效 JSON"


@pytest.mark.asyncio
async def test_single_region_truncated_json_is_not_saved_as_plain_text() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"translations":[{"region_id":"r1","translation":"你好"}'
                        }
                    }
                ]
            },
        )

    runtime = HttpLLMRuntime(transport=httpx.MockTransport(handler), max_retries=0)

    with pytest.raises(LLMProtocolError):
        await runtime.translate_page(profile("deepseek"), request_input())


@pytest.mark.asyncio
async def test_large_invalid_provider_response_is_retried_in_smaller_batches() -> None:
    request_sizes: list[int] = []
    active_small_requests = 0
    max_active_small_requests = 0
    payload = TranslationInput(
        source_language="ja",
        target_language="zh-CN",
        regions=tuple(
            TranslationRegion(f"r{index}", f"文本{index}", "dialogue", index)
            for index in range(4)
        ),
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active_small_requests, max_active_small_requests
        body = json.loads(request.content)
        user = json.loads(body["messages"][1]["content"])
        regions = user["regions"]
        request_sizes.append(len(regions))
        if len(regions) > 2:
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "输出被截断"}}]},
            )
        active_small_requests += 1
        max_active_small_requests = max(max_active_small_requests, active_small_requests)
        await asyncio.sleep(0.02)
        active_small_requests -= 1
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "translations": [
                                        {
                                            "region_id": region["region_id"],
                                            "translation": f"译文-{region['region_id']}",
                                        }
                                        for region in regions
                                    ]
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
        )

    runtime = HttpLLMRuntime(transport=httpx.MockTransport(handler), max_retries=0)
    result = await runtime.translate_page(profile("deepseek"), payload)

    assert result.translations == {f"r{index}": f"译文-r{index}" for index in range(4)}
    assert request_sizes == [4, 4, 2, 2]
    assert max_active_small_requests == 2
    assert result.diagnostics["protocol_repairs"] == 1
    assert result.diagnostics["split_batches"] == 1
    assert result.diagnostics["provider_requests"] == 4
    await runtime.close()


@pytest.mark.asyncio
async def test_profile_concurrency_is_enforced_without_provider_wide_serialization() -> None:
    active = 0
    max_active = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"translations":[{"region_id":"r1","translation":"你好"}]}'
                        }
                    }
                ]
            },
        )

    runtime = HttpLLMRuntime(transport=httpx.MockTransport(handler), max_retries=0)
    limited = profile("openai", profile_id="limited", max_concurrency=2)
    await asyncio.gather(
        *(runtime.translate_page(limited, request_input()) for _ in range(4))
    )
    assert max_active == 2

    active = 0
    max_active = 0
    await asyncio.gather(
        runtime.translate_page(
            profile("openai", profile_id="one", max_concurrency=1), request_input()
        ),
        runtime.translate_page(
            profile("openai", profile_id="two", max_concurrency=1), request_input()
        ),
    )
    assert max_active == 2
    client = runtime._client
    await runtime.close()
    assert client.is_closed


def test_base_url_rejects_unsafe_addresses() -> None:
    with pytest.raises(ValueError):
        validate_base_url("http://remote.example/api")
    with pytest.raises(ValueError):
        validate_base_url("https://user:pass@example.test/api")
    with pytest.raises(ValueError):
        validate_base_url("https://example.test/api?token=secret")


class FakeRuntime:
    def __init__(self):
        self.last_payload = None
        self.copy_source = False

    async def providers(self):
        return []

    async def close(self):
        return None

    async def test_connection(self, profile):
        return {"provider": profile.provider, "model": profile.model, "latency_ms": 1}

    async def translate_page(self, profile, payload):
        from app.providers.llm.base import TranslationResult

        self.last_payload = payload
        return TranslationResult(
            {
                region.region_id: (
                    region.source_text if self.copy_source else f"译文-{region.region_id}"
                )
                for region in payload.regions
            },
            profile.provider,
            profile.model,
        )


def test_profile_and_current_page_translation_with_injected_runtime(tmp_path) -> None:
    settings = Settings(data_dir=tmp_path / "data", project_root=tmp_path / "project")
    store = MemorySecretStore()
    runtime = FakeRuntime()
    with TestClient(create_app(settings, llm_runtime=runtime, secret_store=store)) as client:
        profile_response = client.post(
            "/api/llm-profiles",
            json={
                "name": "测试 Profile",
                "provider": "openai",
                "base_url": "https://example.test/v1",
                "model": "test-model",
                "api_key": "secret-key-83a2",
            },
        )
        assert profile_response.status_code == 201
        profile_data = profile_response.json()["data"]
        assert profile_data["api_key_hint"] == "••••83a2"
        assert "secret-key" not in profile_response.text

        image = Image.new("RGB", (100, 120), "white")
        import io

        stream = io.BytesIO()
        image.save(stream, format="PNG")
        created = client.post(
            "/api/import-sessions",
            json={
                "project_name": "测试漫画",
                "source_language": "ja",
                "translation_mode": "quick",
                "source_type": "single",
                "ocr_provider": "auto",
                "llm_profile_id": profile_data["id"],
            },
        ).json()["data"]
        client.post(
            f"/api/import-sessions/{created['id']}/files",
            data={"relative_path": "001.png"},
            files={"file": ("001.png", stream.getvalue(), "image/png")},
        )
        commit = client.post(f"/api/import-sessions/{created['id']}/commit").json()["data"]
        for _ in range(100):
            task = client.get(f"/api/tasks/{commit['task_id']}").json()["data"]
            if task["status"] in {"completed", "failed"}:
                break
        page = client.get(f"/api/projects/{commit['project_id']}/pages").json()["data"]["items"][0]
        client.put(
            f"/api/pages/{page['id']}/regions",
            json={
                "expected_revision": 0,
                "regions": [
                    {
                        "id": "r1",
                        "class_id": 0,
                        "class_name": "dialogue",
                        "confidence": None,
                        "x1": 1,
                        "y1": 1,
                        "x2": 80,
                        "y2": 40,
                        "source": "manual",
                        "is_manual_edited": True,
                    }
                ],
            },
        ).json()["data"]
        client.post(f"/api/pages/{page['id']}/review")
        client.put(
            "/api/regions/r1/ocr-text",
            json={"text": "こんにちは", "expected_ocr_revision": 0},
        )
        result = client.post(f"/api/pages/{page['id']}/translate").json()["data"]
        for _ in range(100):
            task = client.get(f"/api/tasks/{result['task_id']}").json()["data"]
            if task["status"] in {"completed", "failed", "cancelled"}:
                break
        assert task["status"] == "completed"
        with sqlite3.connect(settings.database_path) as connection:
            raw_result = connection.execute(
                "SELECT result_json FROM tasks WHERE id = ?", (result["task_id"],)
            ).fetchone()[0]
        diagnostics = json.loads(raw_result)
        assert diagnostics["provider"] == "openai"
        assert diagnostics["regions"] == 1
        assert set(diagnostics["timings_ms"]) == {
            "context", "profile_queue", "provider", "persist", "total"
        }
        region = client.get(f"/api/pages/{page['id']}/regions").json()["data"]["regions"][0]
        assert region["target_text"] == "译文-r1"
        assert region["translation_status"] == "completed"
        assert runtime.last_payload.regions[0].sfx_strategy is None

        runtime.copy_source = True
        retried = client.post(
            "/api/regions/r1/translate",
            json={
                "overwrite_completed": True,
                "expected_translation_revision": region["translation_revision"],
            },
        )
        assert retried.status_code == 202
        assert wait_for_task(client, retried.json()["data"]["task_id"])["status"] == "completed"
        copied = client.get(f"/api/pages/{page['id']}/regions").json()["data"]["regions"][0]
        assert copied["target_text"] == copied["source_text"]
        assert copied["translation_status"] == "completed"

        preview = client.post(
            f"/api/projects/{commit['project_id']}/batch-tasks/preview",
            json={"start_page": 1, "end_page": 1, "run_ocr": False, "run_render": False},
        )
        assert preview.status_code == 200
        assert preview.json()["data"]["items"][0]["needs_llm"] is True

        runtime.copy_source = False
        repaired = client.post(f"/api/pages/{page['id']}/translate")
        assert repaired.status_code == 202
        assert wait_for_task(client, repaired.json()["data"]["task_id"])["status"] == "completed"
        final_region = client.get(f"/api/pages/{page['id']}/regions").json()["data"]["regions"][0]
        assert final_region["target_text"] == "译文-r1"
