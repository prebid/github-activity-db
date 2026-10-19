"""Async SQLAlchemy engine and session management."""

import contextlib
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import event, pool
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from github_activity_db.config import get_settings

# Module-level engine instance (initialized lazily)
_engine: AsyncEngine | None = None
_async_session_factory: async_sessionmaker[AsyncSession] | None = None

# How long a connection waits on a locked database before raising "database is locked".
# Python's sqlite3 waits 5 s by default (timeout=5.0). In rollback-journal mode a reader's
# lock blocks a writer's commit; the sync is the only writer, and 30 s lets it outlast a
# concurrent read. A lock held longer makes the writer's statement or commit raise, which ends
# the run (redesign plan 5.3 step 4, gap 77).
SQLITE_BUSY_TIMEOUT_MS = 30_000


def _apply_sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
    """Configure every new SQLite connection.

    foreign_keys is per connection and off by default in SQLite, so declared
    ON DELETE CASCADE constraints only hold when it is set on each connection.

    Statements here should not need a database lock: one that does, such as
    `PRAGMA journal_mode=DELETE`, makes every connection fail with "database is
    locked" while another process has the database open in WAL mode, or wait out
    the busy timeout under an exclusive lock. The journal mode is therefore left
    as the file has it; D20 (rollback journal for the tracked file) is checked by
    scripts/check_db_journal.py as a pre-commit hook and by a test.

    If a statement fails, the connection is closed before re-raising: SQLAlchemy
    does not close a connection whose connect listener raised, and the open
    aiosqlite connection's worker thread would keep the process from exiting.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    except BaseException:
        with contextlib.suppress(Exception):
            cursor.close()
        with contextlib.suppress(Exception):
            dbapi_connection.close()
        raise
    cursor.close()


def build_engine(database_url: str) -> AsyncEngine:
    """Create an async engine with the application's connection settings."""
    engine = create_async_engine(
        database_url,
        echo=False,  # SQLAlchemy logging controlled via loguru interception
        future=True,
        poolclass=pool.NullPool,  # Required for SQLite to prevent "database is locked"
    )
    if engine.dialect.name == "sqlite":
        event.listen(engine.sync_engine, "connect", _apply_sqlite_pragmas)
    return engine


def get_engine() -> AsyncEngine:
    """Get or create the async SQLAlchemy engine."""
    global _engine
    if _engine is None:
        _engine = build_engine(get_settings().database_url)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Get or create the async session factory."""
    global _async_session_factory
    if _async_session_factory is None:
        engine = get_engine()
        _async_session_factory = async_sessionmaker(
            bind=engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autocommit=False,
            autoflush=False,
        )
    return _async_session_factory


@asynccontextmanager
async def get_session(auto_commit: bool = True) -> AsyncGenerator[AsyncSession, None]:
    """Get an async database session with automatic cleanup.

    Args:
        auto_commit: If True (default), commits on successful exit. When False, the caller
                     commits; changes it has not committed are rolled back on exit.

    Usage:
        async with get_session() as session:
            result = await session.execute(select(PullRequest))
    """
    session_factory = get_session_factory()
    async with session_factory() as session:
        try:
            yield session
            if auto_commit:
                await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Dispose the engine and close all connections.

    Call this when shutting down the application.
    """
    global _engine, _async_session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _async_session_factory = None
