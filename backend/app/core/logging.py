from __future__ import annotations

import logging
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from typing import Literal

from app.core.config import Settings

TASK_PROGRESS_LOGGER = "app.task_progress"
TASK_PROGRESS_MARKER = "[TASK]"
_TASK_PROGRESS_LEVEL = logging.INFO

_STAGE_LABELS = {
    "preparing": "准备任务",
    "ocr": "OCR",
    "translation": "翻译",
    "translating": "翻译",
    "repair": "图像修复",
    "render": "文字排版",
    "export": "导出",
}

_TASK_LABELS = {
    "batch_pipeline": "批处理",
    "quick_translation": "翻译",
}

_EVENT_LABELS = {
    "started": "开始",
    "resumed": "继续",
    "pause_requested": "正在暂停",
    "paused": "已暂停",
    "cancel_requested": "正在取消",
    "cancelled": "已取消",
    "completed": "完成",
    "warning": "完成（有异常）",
    "skipped": "跳过",
    "failed": "失败",
    "progress": "进度",
}

TaskProgressEvent = Literal[
    "started",
    "resumed",
    "pause_requested",
    "paused",
    "cancel_requested",
    "cancelled",
    "completed",
    "warning",
    "skipped",
    "failed",
    "progress",
]


def _write_task_console(message: str) -> None:
    try:
        print(
            f"{TASK_PROGRESS_MARKER} [{datetime.now().astimezone():%H:%M:%S}] {message}",
            file=sys.stdout,
            flush=True,
        )
    except (OSError, ValueError):
        pass


def configure_logging(settings: Settings) -> None:
    global _TASK_PROGRESS_LEVEL

    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s request_id=%(request_id)s %(message)s"
    )
    root = logging.getLogger()
    root.setLevel(settings.log_level.upper())
    _TASK_PROGRESS_LEVEL = root.level
    # Uvicorn's reload mode can briefly start a second process while the
    # previous process still owns the Windows log file.  A locked or
    # read-only log file must not prevent the API from starting; fall back to
    # stderr in that case and keep the same redacted formatter/filter.
    if not any(getattr(item, "_manga_translator_handler", False) for item in root.handlers):
        try:
            handler: logging.Handler = RotatingFileHandler(
                settings.logs_dir / "app.log",
                maxBytes=5 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
        except OSError:
            handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        handler.addFilter(RequestIdFilter())
        # Keep an explicit marker instead of checking only RotatingFileHandler:
        # the fallback stream handler is also installed exactly once across
        # repeated lifespan setup calls in development reloads.
        handler._manga_translator_handler = True  # type: ignore[attr-defined]
        root.addHandler(handler)

    # Test clients and Uvicorn may apply a dictConfig that disables loggers
    # created earlier. Re-enable this dedicated channel whenever the app
    # configures logging so task output cannot silently disappear.
    progress_logger = logging.getLogger(TASK_PROGRESS_LOGGER)
    progress_logger.disabled = False
    progress_logger.setLevel(_TASK_PROGRESS_LEVEL)
    progress_logger.propagate = True
    if root.isEnabledFor(logging.INFO) and not getattr(
        progress_logger, "_manga_translator_ready_emitted", False
    ):
        message = "任务日志已启用，等待翻译任务"
        _write_task_console(message)
        progress_logger.info(message, extra={"request_id": "-"})
        progress_logger._manga_translator_ready_emitted = True  # type: ignore[attr-defined]


def _safe_fragment(value: object | None, *, limit: int = 500) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).splitlines()).strip()
    return text[:limit] if text else None


def task_progress(
    task_id: str,
    event: TaskProgressEvent,
    *,
    task_type: str | None = None,
    stage: str | None = None,
    translation_mode: str | None = None,
    page_index: int | None = None,
    total: int | None = None,
    completed: int | None = None,
    failed: int | None = None,
    skipped: int | None = None,
    provider: str | None = None,
    model: str | None = None,
    error_code: str | None = None,
    message: str | None = None,
) -> None:
    """Emit one bounded, human-readable task event without task content."""

    context = [f"[任务 {task_id[:8]}]"]
    if page_index is not None:
        context.append(f"[第 {page_index} 页]")

    subject = _STAGE_LABELS.get(stage or "") or _TASK_LABELS.get(task_type or "") or "任务"

    parts = [" ".join(context), f"{subject}：{_EVENT_LABELS[event]}"]
    if completed is not None or failed is not None or skipped is not None:
        progress = []
        if completed is not None:
            progress.append(f"完成 {completed}/{total}" if total is not None else f"完成 {completed}")
        if failed is not None:
            progress.append(f"失败 {failed}")
        if skipped is not None:
            progress.append(f"跳过 {skipped}")
        parts.append("，".join(progress))
    elif total is not None:
        parts.append(f"共 {total} 项")

    safe_provider = _safe_fragment(provider, limit=32)
    safe_model = _safe_fragment(model, limit=200)
    safe_code = _safe_fragment(error_code, limit=80)
    safe_message = _safe_fragment(message)
    if safe_provider:
        parts.append(f"供应商={safe_provider}")
    if safe_model:
        parts.append(f"模型={safe_model}")
    if safe_code:
        parts.append(f"code={safe_code}")
    if safe_message:
        parts.append(safe_message)

    level = (
        logging.ERROR
        if event == "failed"
        else logging.WARNING
        if event in {"warning", "skipped"}
        else logging.INFO
    )
    rendered = " ".join(parts)
    progress_logger = logging.getLogger(TASK_PROGRESS_LOGGER)
    progress_logger.disabled = False
    # Alembic configures logging while migrations run and may reset the root
    # logger to WARNING or disable existing named loggers. Restore the
    # application's configured task level on every event so normal progress
    # remains visible after startup.
    progress_logger.setLevel(_TASK_PROGRESS_LEVEL)
    if not progress_logger.isEnabledFor(level):
        return
    # Write the launcher channel directly instead of relying on a logging
    # StreamHandler. Uvicorn and third-party dictConfig calls can replace or
    # disable handlers after application startup; stdout itself remains the
    # stable redirected stream owned by launcher.ps1.
    _write_task_console(rendered)
    progress_logger.log(level, rendered, extra={"request_id": "-"})


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = "-"
        return True
