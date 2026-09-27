from __future__ import annotations

import asyncio
from typing import Protocol

from app.providers.llm.base import LLMSecretStoreError


class SecretStore(Protocol):
    async def get(self, target: str) -> str | None: ...

    async def set(self, target: str, secret: str) -> None: ...

    async def delete(self, target: str) -> None: ...


class WindowsCredentialStore:
    """Small injectable wrapper around keyring/Windows Credential Manager."""

    service_name = "MangaTranslator"

    def __init__(self, keyring_module=None) -> None:
        if keyring_module is None:
            try:
                import keyring as keyring_module  # type: ignore[import-not-found]
            except Exception as exc:  # pragma: no cover - optional install path
                raise LLMSecretStoreError("凭据存储依赖不可用") from exc
        self._keyring = keyring_module

    @classmethod
    def from_optional(cls) -> WindowsCredentialStore | None:
        try:
            return cls()
        except LLMSecretStoreError:
            return None

    async def get(self, target: str) -> str | None:
        try:
            return await asyncio.to_thread(self._keyring.get_password, self.service_name, target)
        except Exception as exc:
            raise LLMSecretStoreError("Windows 凭据存储不可用") from exc

    async def set(self, target: str, secret: str) -> None:
        try:
            await asyncio.to_thread(self._keyring.set_password, self.service_name, target, secret)
        except Exception as exc:
            raise LLMSecretStoreError("Windows 凭据存储不可用") from exc

    async def delete(self, target: str) -> None:
        try:
            await asyncio.to_thread(self._keyring.delete_password, self.service_name, target)
        except Exception as exc:
            # keyring raises PasswordDeleteError for an already absent item.
            if exc.__class__.__name__ not in {"PasswordDeleteError", "ItemNotFoundException"}:
                raise LLMSecretStoreError("Windows 凭据存储不可用") from exc


class MemorySecretStore:
    """Deterministic store for tests and injected local runtimes."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, target: str) -> str | None:
        return self.values.get(target)

    async def set(self, target: str, secret: str) -> None:
        self.values[target] = secret

    async def delete(self, target: str) -> None:
        self.values.pop(target, None)
