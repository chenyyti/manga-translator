from __future__ import annotations

import json
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, update

from app.api.routes import database, ok
from app.api.schemas import (
    LLMProfileCreate,
    LLMProfileUpdate,
    ProjectLLMProfileWrite,
    TranslationSettingsWrite,
)
from app.core.errors import AppError, ConflictError, NotFoundError
from app.db.models import APIProfile, Page, Project, Task, TranslationSettings
from app.providers.llm.base import LLMError, LLMProfileConfig, LLMSecretStoreError
from app.providers.llm.http_runtime import validate_base_url
from app.providers.llm.registry import PROVIDER_LANGUAGES
from app.services.tasks import utc_now

router = APIRouter(prefix="/api")


def runtime(request: Request):
    return request.app.state.llm_runtime


def secret_store(request: Request):
    return request.app.state.secret_store


def _hint(value: str | None) -> str | None:
    return f"••••{value[-4:]}" if value else None


def _profile_read(profile: APIProfile, *, has_key: bool) -> dict[str, object]:
    return {
        "id": profile.id,
        "name": profile.name,
        "provider": profile.provider,
        "base_url": profile.base_url,
        "model": profile.model,
        "temperature": profile.temperature,
        "max_tokens": profile.max_tokens,
        "timeout_seconds": profile.timeout_seconds,
        "max_concurrency": profile.max_concurrency,
        "api_key_hint": profile.api_key_hint if has_key else None,
        "has_api_key": has_key,
        "created_at": profile.created_at,
        "updated_at": profile.updated_at,
    }


def _validate_provider_url(provider: str, base_url: str) -> str:
    if provider not in PROVIDER_LANGUAGES:
        raise AppError("LLM_PROVIDER_NOT_FOUND", "LLM Provider 不存在", status_code=422)
    try:
        return validate_base_url(base_url)
    except ValueError as exc:
        raise AppError("INVALID_BASE_URL", str(exc), status_code=422) from exc


async def _active_profile_tasks(request: Request, profile_id: str) -> bool:
    async with database(request).session_factory() as session:
        return bool(
            await session.scalar(
                select(Task.id)
                .where(
                    Task.task_type.in_(
                        [
                            "quick_translation",
                            "batch_pipeline",
                        ]
                    ),
                    Task.status.in_(["pending", "running", "pausing", "paused"]),
                    (Task.llm_profile_id == profile_id) | Task.parameters_json.contains(profile_id),
                )
                .limit(1)
            )
        )


@router.get("/providers/llm")
async def list_llm_providers(request: Request) -> JSONResponse:
    return ok({"items": [item.__dict__ for item in await runtime(request).providers()]})


@router.get("/llm-profiles")
async def list_profiles(request: Request) -> JSONResponse:
    async with database(request).session_factory() as session:
        profiles = list(
            (
                await session.scalars(
                    select(APIProfile)
                    .where(APIProfile.profile_type == "llm")
                    .order_by(APIProfile.created_at)
                )
            ).all()
        )
    store = secret_store(request)
    items: list[dict[str, object]] = []
    for profile in profiles:
        # Listing metadata remains safe when the credential backend is
        # unavailable.  Save/test/use operations still fail explicitly, but
        # the settings page can start and tell the user that keys are not
        # currently accessible.
        if store is None:
            key = None
        else:
            try:
                key = await store.get(profile.credential_target)
            except Exception:
                key = None
        items.append(_profile_read(profile, has_key=bool(key)))
    return ok({"items": items})


@router.post("/llm-profiles")
async def create_profile(payload: LLMProfileCreate, request: Request) -> JSONResponse:
    store = secret_store(request)
    if store is None:
        raise ConflictError("SECRET_STORE_UNAVAILABLE", "Windows 凭据存储不可用")
    base_url = _validate_provider_url(payload.provider.value, payload.base_url)
    profile_id = str(uuid4())
    target = f"MangaTranslator/llm/{profile_id}"
    try:
        await store.set(target, payload.api_key.get_secret_value())
    except Exception as exc:
        raise ConflictError("SECRET_STORE_UNAVAILABLE", "Windows 凭据存储不可用") from exc
    profile = APIProfile(
        id=profile_id,
        name=" ".join(payload.name.split()),
        profile_type="llm",
        provider=payload.provider.value,
        base_url=base_url,
        model=payload.model,
        temperature=payload.temperature,
        max_tokens=payload.max_tokens,
        timeout_seconds=payload.timeout_seconds,
        max_concurrency=payload.max_concurrency,
        api_key_hint=_hint(payload.api_key.get_secret_value()),
        credential_target=target,
    )
    async with database(request).session_factory() as session:
        session.add(profile)
        await session.commit()
        await session.refresh(profile)
    return ok(_profile_read(profile, has_key=True), status_code=201)


@router.put("/llm-profiles/{profile_id}")
async def update_profile(
    profile_id: str, payload: LLMProfileUpdate, request: Request
) -> JSONResponse:
    if await _active_profile_tasks(request, profile_id):
        raise ConflictError("PROFILE_IN_USE", "Profile 正在执行翻译任务，暂时不能编辑")
    store = secret_store(request)
    if store is None:
        raise ConflictError("SECRET_STORE_UNAVAILABLE", "Windows 凭据存储不可用")
    async with database(request).session_factory() as session:
        profile = await session.get(APIProfile, profile_id)
        if profile is None or profile.profile_type != "llm":
            raise NotFoundError("翻译 Profile 不存在")
        provider = payload.provider.value if payload.provider else profile.provider
        base_url = payload.base_url if payload.base_url is not None else profile.base_url
        normalized_url = _validate_provider_url(provider, base_url)
        secret_value = payload.api_key.get_secret_value() if payload.api_key else None
        try:
            if payload.clear_api_key:
                await store.delete(profile.credential_target)
                profile.api_key_hint = None
            elif secret_value:
                await store.set(profile.credential_target, secret_value)
                profile.api_key_hint = _hint(secret_value)
        except Exception as exc:
            await session.rollback()
            raise ConflictError("SECRET_STORE_UNAVAILABLE", "Windows 凭据存储不可用") from exc
        if payload.name is not None:
            profile.name = " ".join(payload.name.split())
        profile.provider = provider
        profile.base_url = normalized_url
        if payload.model is not None:
            profile.model = payload.model
        if "temperature" in payload.model_fields_set:
            profile.temperature = payload.temperature
        if payload.max_tokens is not None:
            profile.max_tokens = payload.max_tokens
        if payload.timeout_seconds is not None:
            profile.timeout_seconds = payload.timeout_seconds
        if payload.max_concurrency is not None:
            profile.max_concurrency = payload.max_concurrency
        profile.updated_at = utc_now()
        await session.commit()
        await session.refresh(profile)
    try:
        has_key = bool(await store.get(profile.credential_target))
    except Exception as exc:
        raise ConflictError("SECRET_STORE_UNAVAILABLE", "Windows 凭据存储不可用") from exc
    return ok(_profile_read(profile, has_key=has_key))


@router.delete("/llm-profiles/{profile_id}")
async def delete_profile(profile_id: str, request: Request) -> JSONResponse:
    if await _active_profile_tasks(request, profile_id):
        raise ConflictError("PROFILE_IN_USE", "Profile 正在执行翻译任务，暂时不能删除")
    store = secret_store(request)
    if store is None:
        raise ConflictError("SECRET_STORE_UNAVAILABLE", "Windows 凭据存储不可用")
    async with database(request).session_factory() as session:
        profile = await session.get(APIProfile, profile_id)
        if profile is None or profile.profile_type != "llm":
            raise NotFoundError("翻译 Profile 不存在")
        target = profile.credential_target
        await session.execute(
            update(TranslationSettings)
            .where(TranslationSettings.default_profile_id == profile_id)
            .values(default_profile_id=None)
        )
        try:
            await store.delete(target)
        except Exception as exc:
            await session.rollback()
            raise ConflictError("SECRET_STORE_UNAVAILABLE", "Windows 凭据存储不可用") from exc
        await session.delete(profile)
        await session.commit()
    return ok({"deleted": True})


@router.post("/llm-profiles/{profile_id}/test")
async def test_profile(profile_id: str, request: Request) -> JSONResponse:
    store = secret_store(request)
    if store is None:
        raise ConflictError("SECRET_STORE_UNAVAILABLE", "Windows 凭据存储不可用")
    async with database(request).session_factory() as session:
        profile = await session.get(APIProfile, profile_id)
    if profile is None or profile.profile_type != "llm":
        raise NotFoundError("翻译 Profile 不存在")
    try:
        key = await store.get(profile.credential_target)
    except Exception as exc:
        raise ConflictError("SECRET_STORE_UNAVAILABLE", "Windows 凭据存储不可用") from exc
    if not key:
        raise ConflictError("PROFILE_KEY_MISSING", "Profile 尚未配置 API Key")
    try:
        result = await runtime(request).test_connection(
            LLMProfileConfig(
                id=profile.id,
                provider=profile.provider,
                base_url=profile.base_url,
                model=profile.model,
                api_key=key,
                temperature=profile.temperature,
                max_tokens=min(profile.max_tokens, 64),
                timeout_seconds=profile.timeout_seconds,
                max_concurrency=profile.max_concurrency,
            )
        )
    except LLMSecretStoreError as exc:
        raise ConflictError(exc.code, str(exc)) from exc
    except LLMError as exc:
        raise AppError(exc.code, str(exc), status_code=502) from exc
    except Exception as exc:
        raise AppError("LLM_TEST_FAILED", "Provider 连接测试失败", status_code=502) from exc
    return ok(result)


async def _get_settings(request: Request) -> TranslationSettings:
    async with database(request).session_factory() as session:
        value = await session.get(TranslationSettings, 1)
        if value is None:
            value = TranslationSettings(id=1)
            session.add(value)
            await session.commit()
            await session.refresh(value)
        return value


def _settings_read(value: TranslationSettings) -> dict[str, object]:
    return {
        "default_profile_id": value.default_profile_id,
        "sfx_strategy": value.sfx_strategy,
        "sfx_class_names": json.loads(value.sfx_class_names_json),
    }


@router.get("/settings/translation")
async def get_translation_settings(request: Request) -> JSONResponse:
    return ok(_settings_read(await _get_settings(request)))


@router.put("/settings/translation")
async def update_translation_settings(
    payload: TranslationSettingsWrite, request: Request
) -> JSONResponse:
    async with database(request).session_factory() as session:
        active_batch = await session.scalar(
            select(Task.id)
            .where(
                Task.task_type == "batch_pipeline",
                Task.status.in_(["pending", "running", "pausing", "paused"]),
            )
            .limit(1)
        )
        if active_batch:
            raise ConflictError("BATCH_SETTINGS_IN_USE", "批处理任务活动期间不能修改翻译设置")
        if payload.default_profile_id:
            profile = await session.get(APIProfile, payload.default_profile_id)
            if profile is None or profile.profile_type != "llm":
                raise NotFoundError("翻译 Profile 不存在")
        value = await session.get(TranslationSettings, 1)
        if value is None:
            value = TranslationSettings(id=1)
            session.add(value)
            previous_strategy = "preserve"
            previous_names: list[str] = []
        else:
            previous_strategy = value.sfx_strategy
            try:
                previous_names = json.loads(value.sfx_class_names_json)
            except (TypeError, ValueError):
                previous_names = []
        value.default_profile_id = payload.default_profile_id
        value.sfx_strategy = payload.sfx_strategy.value
        value.sfx_class_names_json = json.dumps(payload.sfx_class_names, ensure_ascii=False)
        value.updated_at = utc_now()
        sfx_changed = previous_strategy != value.sfx_strategy or previous_names != payload.sfx_class_names
        if sfx_changed:
            # SFX policy controls whether a region is masked and re-typeset,
            # so every ready page must invalidate both repair and rendering
            # inputs.  A default Profile change alone does not affect assets.
            from app.services.render_state import mark_page_render_outdated

            pages = list(
                (
                    await session.scalars(
                        select(Page).where(Page.import_status == "ready")
                    )
                ).all()
            )
            for page in pages:
                await mark_page_render_outdated(
                    session,
                    page,
                    repair=True,
                    reason="SFX 策略已更新，需要重新生成成品",
                )
        await session.commit()
        await session.refresh(value)
    return ok(_settings_read(value))


@router.put("/projects/{project_id}/llm-profile")
async def update_project_profile(
    project_id: str, payload: ProjectLLMProfileWrite, request: Request
) -> JSONResponse:
    async with database(request).session_factory() as session:
        project = await session.get(Project, project_id)
        if project is None:
            raise NotFoundError("项目不存在")
        active_batch = await session.scalar(
            select(Task.id)
            .where(
                Task.project_id == project_id,
                Task.task_type == "batch_pipeline",
                Task.status.in_(["pending", "running", "pausing", "paused"]),
            )
            .limit(1)
        )
        if active_batch:
            raise ConflictError("BATCH_SETTINGS_IN_USE", "该项目批处理期间不能切换翻译 Profile")
        if payload.profile_id:
            profile = await session.get(APIProfile, payload.profile_id)
            if profile is None or profile.profile_type != "llm":
                raise NotFoundError("翻译 Profile 不存在")
        project.llm_profile_id = payload.profile_id
        project.updated_at = utc_now()
        await session.commit()
    return ok({"project_id": project_id, "llm_profile_id": payload.profile_id})
