from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from contextlib import closing
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


class Database:
    def __init__(self, database_path: Path) -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.path = database_path
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{database_path.as_posix()}", pool_pre_ping=True
        )
        self.session_factory = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )
        event.listen(self.engine.sync_engine, "connect", self._configure_sqlite)

    @staticmethod
    def _configure_sqlite(dbapi_connection: object, _connection_record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA temp_store=MEMORY")
        cursor.execute("PRAGMA cache_size=-32768")
        cursor.close()

    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            yield session

    async def dispose(self) -> None:
        await self.engine.dispose()


def run_migrations(database_path: Path) -> None:
    # The repository's ``alembic`` migration directory is a namespace package
    # on some Python environments, so import sorting classifies these two
    # runtime-only imports differently.  Keep the lazy imports together.
    from alembic import command  # noqa: I001
    from alembic.config import Config

    backend_root = Path(__file__).resolve().parents[2]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")
    command.upgrade(config, "head")


def optimize_database(database_path: Path) -> None:
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA optimize")
