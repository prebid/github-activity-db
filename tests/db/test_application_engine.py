"""The application's own engine and sessions apply the SQLite connection settings."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from github_activity_db.config import get_settings
from github_activity_db.db.engine import (
    SQLITE_BUSY_TIMEOUT_MS,
    create_tables,
    dispose_engine,
    get_session,
)


@pytest.fixture
async def application_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[None]:
    """Point the settings at a new database file; reset the cached engine before and after."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'application.db'}")
    get_settings.cache_clear()
    await dispose_engine()
    try:
        await create_tables()
        yield
    finally:
        await dispose_engine()
        get_settings.cache_clear()


async def test_every_application_session_gets_the_connection_settings(
    application_database: None,
) -> None:
    # The engine opens a new connection per session, so check several sessions.
    for _ in range(3):
        async with get_session() as session:
            assert (await session.execute(text("PRAGMA foreign_keys"))).scalar() == 1
            assert (
                await session.execute(text("PRAGMA busy_timeout"))
            ).scalar() == SQLITE_BUSY_TIMEOUT_MS
            assert (await session.execute(text("PRAGMA journal_mode"))).scalar() == "delete"


async def test_application_session_rejects_an_orphan_row(application_database: None) -> None:
    with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
        async with get_session() as session:
            await session.execute(
                text("INSERT INTO pr_user_tags (pr_id, user_tag_id) VALUES (999, 999)")
            )
