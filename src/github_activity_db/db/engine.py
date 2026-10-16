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
from github_activity_db.db.models import Base

# Module-level engine instance (initialized lazily)
_engine: AsyncEngine | None = None
_async_session_factory: async_sessionmaker[AsyncSession] | None = None

# How long a connection waits on a locked database before raising "database is locked".
# Python's sqlite3 waits 5 s by default (timeout=5.0). In rollback-journal mode a reader's
# lock blocks a writer's commit; the sync is the only writer, and 30 s lets it outlast a
# concurrent read instead of failing the item.
SQLITE_BUSY_TIMEOUT_MS = 30_000


def _apply_sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
    """Configure every new SQLite connection.

    foreign_keys is per connection and off by default in SQLite, so declared
    ON DELETE CASCADE constraints only hold when it is set on each connection.

    Statements here must not need a database lock. SQLAlchemy does not close a
    connection whose connect listener raised, and the open aiosqlite connection's
    worker thread then keeps the process from exiting; a lock-taking statement such
    as `PRAGMA journal_mode=DELETE` fails that way while another process holds the
    database. The journal mode is left at SQLite's default rollback journal (D20);
    tests/db/test_tracked_database.py checks the tracked file's header.
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
        auto_commit: If True (default), commits on successful exit.
                     Set to False when using CommitManager for manual commits.
                     When False, uncommitted changes are rolled back on exit.

    Usage:
        # Standard usage - auto-commits on exit
        async with get_session() as session:
            result = await session.execute(select(PullRequest))

        # With CommitManager - manual commits
        async with get_session(auto_commit=False) as session:
            commit_manager = CommitManager(session, batch_size=25)
            # ... operations with commit_manager.record_success() ...
            await commit_manager.finalize()
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


async def create_tables() -> None:
    """Create all database tables.

    Use this for testing or initial setup. In production, use Alembic migrations.
    """
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def drop_tables() -> None:
    """Drop all database tables.

    WARNING: This will delete all data. Use only for testing.
    """
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def dispose_engine() -> None:
    """Dispose the engine and close all connections.

    Call this when shutting down the application.
    """
    global _engine, _async_session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _async_session_factory = None
