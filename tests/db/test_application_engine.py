"""The application's own engine and sessions apply the SQLite connection settings."""

import shutil
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from github_activity_db.config import get_settings
from github_activity_db.db.engine import (
    SQLITE_BUSY_TIMEOUT_MS,
    dispose_engine,
    get_engine,
    get_session,
)


@pytest.fixture
async def application_database(
    migrated_database: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> AsyncIterator[Path]:
    """Point the settings at a database migrated to head; reset the cached engine around it."""
    monkeypatch.chdir(tmp_path)  # the settings read no .env but this test's
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{migrated_database}")
    get_settings.cache_clear()
    await dispose_engine()
    try:
        yield migrated_database
    finally:
        await dispose_engine()
        get_settings.cache_clear()


async def test_every_application_session_gets_the_connection_settings(
    application_database: Path,
) -> None:
    # The engine opens a new connection per session, so check several sessions.
    for _ in range(3):
        async with get_session() as session:
            assert (await session.execute(text("PRAGMA foreign_keys"))).scalar() == 1
            assert (
                await session.execute(text("PRAGMA busy_timeout"))
            ).scalar() == SQLITE_BUSY_TIMEOUT_MS
            assert (await session.execute(text("PRAGMA journal_mode"))).scalar() == "delete"


async def test_application_session_rejects_an_orphan_row(application_database: Path) -> None:
    with pytest.raises(IntegrityError) as error:
        async with get_session() as session:
            await session.execute(
                text("INSERT INTO pr_user_tags (pr_id, user_tag_id) VALUES (999, 999)")
            )
    driver_error = error.value.orig
    assert isinstance(driver_error, sqlite3.IntegrityError), repr(driver_error)
    assert driver_error.sqlite_errorname == "SQLITE_CONSTRAINT_FOREIGNKEY"


async def test_dispose_engine_makes_the_next_engine_read_the_settings_again(
    application_database: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first = get_engine()
    assert get_engine() is first
    other = tmp_path / "other.db"
    shutil.copyfile(application_database, other)
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{other}")
    get_settings.cache_clear()
    assert get_engine() is first  # cached until disposed

    await dispose_engine()

    second = get_engine()
    assert second is not first
    assert second.url.database == str(other)
