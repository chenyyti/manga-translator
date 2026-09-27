"""Finish filesystem and credential cleanup queued by database migrations."""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from sqlalchemy import text

from app.db.session import Database
from app.providers.llm.secrets import SecretStore
from app.services.storage import resolve_within

logger = logging.getLogger(__name__)


def _remove_directory(path: Path) -> None:
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass


async def finish_feature_cleanup(
    database: Database, data_dir: Path, secret_store: SecretStore | None
) -> None:
    async with database.session_factory() as session:
        pending = list(
            (await session.execute(text("SELECT kind, target FROM feature_cleanup ORDER BY kind, target"))).all()
        )
    for kind, target in pending:
        try:
            if kind == "credential":
                if secret_store is None:
                    raise RuntimeError("凭据存储不可用")
                await secret_store.delete(target)
            elif kind in {"directory", "file"}:
                relative = Path(target.replace("\\", "/"))
                parts = relative.parts
                if len(parts) < 4 or parts[:2] != ("workspace", "projects") or ".." in parts:
                    raise RuntimeError("旧精翻文件清理路径无效")
                project_root = resolve_within(data_dir, Path(*parts[:3]))
                path = resolve_within(project_root, Path(*parts[3:]))
                if kind == "directory":
                    allowed = {"masks", "rendered", "export", "cache/inpainted"}
                    if Path(*parts[3:]).as_posix() not in allowed:
                        raise RuntimeError("旧精翻目录清理路径无效")
                    await asyncio.to_thread(_remove_directory, path)
                else:
                    if parts[3] not in {"characters", "avatars", "cache"}:
                        raise RuntimeError("旧人物图片清理路径无效")
                    await asyncio.to_thread(path.unlink, True)
            else:
                raise RuntimeError("未知的旧功能清理任务")
        except Exception:
            logger.warning("旧功能清理失败，待下次启动重试（%s）", kind)
            continue
        async with database.session_factory() as session:
            await session.execute(
                text("DELETE FROM feature_cleanup WHERE kind = :kind AND target = :target"),
                {"kind": kind, "target": target},
            )
            await session.commit()
