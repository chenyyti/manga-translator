from __future__ import annotations

import io
import logging
import sys

from app.core.config import Settings
from app.core.logging import TASK_PROGRESS_LOGGER, configure_logging, task_progress


def test_task_progress_is_emitted_once_to_console_and_file(tmp_path, monkeypatch) -> None:
    root = logging.getLogger()
    progress_logger = logging.getLogger(TASK_PROGRESS_LOGGER)
    old_root_level = root.level
    old_progress_level = progress_logger.level
    old_progress_disabled = progress_logger.disabled
    had_ready_marker = hasattr(progress_logger, "_manga_translator_ready_emitted")
    old_ready_marker = getattr(progress_logger, "_manga_translator_ready_emitted", None)
    if had_ready_marker:
        del progress_logger._manga_translator_ready_emitted  # type: ignore[attr-defined]
    old_root_handlers = [
        handler
        for handler in root.handlers
        if getattr(handler, "_manga_translator_handler", False)
    ]
    old_progress_handlers = [
        handler
        for handler in progress_logger.handlers
        if getattr(handler, "_manga_translator_progress_handler", False)
    ]
    for handler in old_root_handlers:
        root.removeHandler(handler)
    for handler in old_progress_handlers:
        progress_logger.removeHandler(handler)

    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    settings = Settings(data_dir=tmp_path / "data", project_root=tmp_path / "project")

    try:
        configure_logging(settings)
        configure_logging(settings)
        # Alembic's migration logging setup can reset the root logger and
        # disable named loggers after application logging is configured.
        root.setLevel(logging.WARNING)
        progress_logger.setLevel(logging.NOTSET)
        progress_logger.disabled = True
        task_progress(
            "a1b2c3d4-full-id",
            "started",
            task_type="batch_pipeline",
            stage="translation",
            translation_mode="quick",
            page_index=3,
            provider="openai",
            model="gpt-5.1-mini",
        )
        for handler in root.handlers + progress_logger.handlers:
            handler.flush()

        console = output.getvalue()
        assert console.count("任务日志已启用，等待翻译任务") == 1
        assert console.count("[任务 a1b2c3d4]") == 1
        assert "[任务 a1b2c3d4] [第 3 页] 翻译：开始" in console
        assert "供应商=openai 模型=gpt-5.1-mini" in console

        file_text = (settings.logs_dir / "app.log").read_text(encoding="utf-8")
        assert file_text.count("[任务 a1b2c3d4]") == 1
    finally:
        new_root_handlers = [
            handler
            for handler in root.handlers
            if getattr(handler, "_manga_translator_handler", False)
        ]
        new_progress_handlers = [
            handler
            for handler in progress_logger.handlers
            if getattr(handler, "_manga_translator_progress_handler", False)
        ]
        for handler in new_root_handlers:
            root.removeHandler(handler)
            handler.close()
        for handler in new_progress_handlers:
            progress_logger.removeHandler(handler)
            handler.close()
        for handler in old_root_handlers:
            root.addHandler(handler)
        for handler in old_progress_handlers:
            progress_logger.addHandler(handler)
        if had_ready_marker:
            progress_logger._manga_translator_ready_emitted = old_ready_marker  # type: ignore[attr-defined]
        elif hasattr(progress_logger, "_manga_translator_ready_emitted"):
            del progress_logger._manga_translator_ready_emitted  # type: ignore[attr-defined]
        root.setLevel(old_root_level)
        progress_logger.setLevel(old_progress_level)
        progress_logger.disabled = old_progress_disabled


def test_task_progress_bounds_and_flattens_error_text(caplog) -> None:
    caplog.set_level(logging.ERROR, logger=TASK_PROGRESS_LOGGER)

    task_progress(
        "safe-task-id",
        "failed",
        task_type="quick_translation",
        stage="translation",
        page_index=9,
        provider="openai\r\nforged-provider",
        model="safe-model\nforged-model",
        error_code="LLM_FAILED\r\nforged",
        message="安全提示\n下一行" + ("x" * 600),
    )

    message = caplog.records[-1].getMessage()
    assert "[第 9 页] 翻译：失败" in message
    assert "code=LLM_FAILED forged" in message
    assert "供应商=openai forged-provider" in message
    assert "模型=safe-model forged-model" in message
    assert "安全提示 下一行" in message
    assert "\n" not in message
    assert len(message) < 700
