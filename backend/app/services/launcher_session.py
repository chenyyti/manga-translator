from __future__ import annotations

import hmac
import time
from collections.abc import Callable

from app.core.errors import AppError


class LauncherSession:
    """In-memory liveness state for a browser managed by the local launcher."""

    def __init__(
        self,
        session_id: str | None,
        *,
        heartbeat_interval_seconds: int,
        heartbeat_timeout_seconds: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        normalized = session_id.strip() if session_id else ""
        self._session_id = normalized or None
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.heartbeat_timeout_seconds = heartbeat_timeout_seconds
        self._clock = clock
        self._last_seen: float | None = None

    @property
    def managed(self) -> bool:
        return self._session_id is not None

    def info(self) -> dict[str, object]:
        return {
            "managed": self.managed,
            "session_id": self._session_id,
            "heartbeat_interval_seconds": self.heartbeat_interval_seconds,
            "heartbeat_timeout_seconds": self.heartbeat_timeout_seconds,
        }

    def heartbeat(self, session_id: str) -> None:
        self._require_managed()
        self._validate_session(session_id)
        self._last_seen = self._clock()

    def status(self, session_id: str) -> dict[str, object]:
        if not self.managed:
            return {"managed": False, "active": False}
        self._validate_session(session_id)
        active = (
            self._last_seen is not None
            and self._clock() - self._last_seen <= self.heartbeat_timeout_seconds
        )
        return {"managed": True, "active": active}

    def _require_managed(self) -> None:
        if not self.managed:
            raise AppError(
                "LAUNCHER_NOT_MANAGED",
                "当前服务未由一键启动器管理",
                status_code=409,
            )

    def _validate_session(self, session_id: str) -> None:
        if not hmac.compare_digest(session_id, self._session_id or ""):
            raise AppError("LAUNCHER_SESSION_INVALID", "启动会话无效", status_code=403)
