from __future__ import annotations

import argparse
import asyncio

from app.core.config import get_settings
from app.db.models import APIProfile
from app.db.session import Database
from app.providers.llm.base import LLMProfileConfig, TranslationInput, TranslationRegion
from app.providers.llm.http_runtime import HttpLLMRuntime
from app.providers.llm.secrets import WindowsCredentialStore


async def validate(profile_id: str) -> None:
    settings = get_settings()
    database = Database(settings.database_path)
    store = WindowsCredentialStore()
    async with database.session_factory() as session:
        profile = await session.get(APIProfile, profile_id)
    if profile is None:
        raise RuntimeError("未找到指定 Profile")
    key = await store.get(profile.credential_target)
    if not key:
        raise RuntimeError("指定 Profile 没有可用 API Key")
    runtime = HttpLLMRuntime()
    result = await runtime.translate_page(
        LLMProfileConfig(
            id=profile.id,
            provider=profile.provider,
            base_url=profile.base_url,
            model=profile.model,
            api_key=key,
            temperature=profile.temperature,
            max_tokens=profile.max_tokens,
            timeout_seconds=profile.timeout_seconds,
            max_concurrency=profile.max_concurrency,
        ),
        TranslationInput(
            source_language="ja",
            target_language="zh-CN",
            regions=(TranslationRegion("sample", "こんにちは", "dialogue", 1),),
        ),
    )
    print(f"provider={result.provider} model={result.model}")
    print(f"translation={result.translations['sample']}")
    await database.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="对指定 LLM Profile 执行一次真实 Provider 验收请求"
    )
    parser.add_argument("profile_id", help="设置页显示的 Profile UUID")
    args = parser.parse_args()
    asyncio.run(validate(args.profile_id))


if __name__ == "__main__":
    main()
