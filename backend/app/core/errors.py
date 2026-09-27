from __future__ import annotations

from typing import Any


class AppError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details


class NotFoundError(AppError):
    def __init__(self, message: str = "请求的资源不存在") -> None:
        super().__init__("NOT_FOUND", message, status_code=404)


class ConflictError(AppError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, status_code=409)


class UnsafeArchiveError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__("UNSAFE_ARCHIVE", message, status_code=422)
