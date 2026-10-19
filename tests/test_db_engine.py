"""Tests for database engine and session management."""

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import Connection, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from github_activity_db.db.engine import (
    SQLITE_BUSY_TIMEOUT_MS,
    _apply_sqlite_pragmas,
    build_engine,
)
from github_activity_db.db.models import UserTag
from tests.db.seed import Seed


class TestDatabaseEngine:
    """Sessions over the application engine, on a database migrated to head."""

    async def test_session_commits_on_success(self, test_engine):
        session_factory = async_sessionmaker(
            test_engine, class_=AsyncSession, expire_on_commit=False
        )
        async with session_factory() as session:
            tag = UserTag(name="committed")
            session.add(tag)
            await session.commit()
            tag_id = tag.id

        async with session_factory() as session:
            stored = await session.get(UserTag, tag_id)
            assert stored is not None
            assert stored.name == "committed"

    async def test_session_rollbacks_on_error(self, test_engine):
        session_factory = async_sessionmaker(
            test_engine, class_=AsyncSession, expire_on_commit=False
        )
        with pytest.raises(ValueError, match="Simulated error"):
            async with session_factory() as session:
                session.add(UserTag(name="rolled back"))
                await session.flush()  # written to the database, not committed
                raise ValueError("Simulated error")

        async with session_factory() as session:
            names = (await session.execute(select(UserTag.name))).scalars().all()
            assert names == []


class TestSqliteConnectionSettings:
    """The application engine configures every SQLite connection."""

    async def test_pragmas_applied_to_new_connections(self, tmp_path: Path) -> None:
        engine = build_engine(f"sqlite+aiosqlite:///{tmp_path / 'pragmas.db'}")
        try:
            async with engine.connect() as conn:
                foreign_keys = (await conn.execute(text("PRAGMA foreign_keys"))).scalar()
                journal_mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar()
                busy_timeout = (await conn.execute(text("PRAGMA busy_timeout"))).scalar()
        finally:
            await engine.dispose()

        assert foreign_keys == 1
        assert busy_timeout == SQLITE_BUSY_TIMEOUT_MS
        # Rollback journal while github_activity.db is tracked in git (see _apply_sqlite_pragmas).
        assert journal_mode == "delete"

    def test_busy_timeout_differs_from_the_driver_default(self, tmp_path: Path) -> None:
        # The pragma assertions can only detect a missing busy_timeout setting while the
        # configured value differs from what sqlite3 reports without it.
        connection = sqlite3.connect(tmp_path / "default.db")
        try:
            driver_default = connection.execute("PRAGMA busy_timeout").fetchone()[0]
        finally:
            connection.close()
        assert driver_default != SQLITE_BUSY_TIMEOUT_MS

    async def test_connect_needs_no_database_lock(self, tmp_path: Path) -> None:
        # A connect-time statement that needs a lock fails while another process holds
        # the database, and the failed connection keeps the process alive (see
        # _apply_sqlite_pragmas). Connecting must succeed under an exclusive lock.
        path = tmp_path / "held.db"
        holder = sqlite3.connect(path, isolation_level=None)
        try:
            holder.execute("CREATE TABLE t (x INTEGER)")
            holder.execute("BEGIN EXCLUSIVE")
            engine = build_engine(f"sqlite+aiosqlite:///{path}")
            try:
                async with engine.connect() as conn:
                    foreign_keys = (await conn.execute(text("PRAGMA foreign_keys"))).scalar()
            finally:
                await engine.dispose()
        finally:
            holder.close()

        assert foreign_keys == 1

    def test_listener_failure_closes_the_connection(self) -> None:
        class FailingCursor:
            def execute(self, statement: str) -> None:
                raise sqlite3.OperationalError("database is locked")

            def close(self) -> None:
                pass

        class Connection:
            closed = False

            def cursor(self) -> FailingCursor:
                return FailingCursor()

            def close(self) -> None:
                self.closed = True

        connection = Connection()
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            _apply_sqlite_pragmas(connection, None)
        assert connection.closed

    async def test_foreign_key_violation_is_rejected(self, test_engine) -> None:
        with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
            async with test_engine.begin() as conn:
                await conn.execute(
                    text("INSERT INTO pr_user_tags (pr_id, user_tag_id) VALUES (999, 999)")
                )

    async def test_on_delete_cascade_removes_children(self, test_engine) -> None:
        async with test_engine.begin() as conn:
            repository = await conn.run_sync(lambda sync: Seed(sync).repository("prebid/c"))
            await conn.execute(
                text(
                    "INSERT INTO sync_failures (repository_id, item_kind, number, attempts, "
                    "last_error, first_failed_at, last_failed_at) VALUES (:repository, "
                    "'pull_request', 7, 1, 'e', '2026-01-01 00:00:00.000000', "
                    "'2026-01-01 00:00:00.000000')"
                ),
                {"repository": repository},
            )
            await conn.execute(text("DELETE FROM repositories"))
            remaining = (await conn.execute(text("SELECT COUNT(*) FROM sync_failures"))).scalar()

        assert remaining == 0

    async def test_on_delete_set_null_keeps_what_an_account_authored(self, test_engine) -> None:
        def seed(sync: Connection) -> None:
            rows = Seed(sync)
            author = rows.user("someone")
            rows.pull_request(rows.repository("prebid/r"), 5, author)

        async with test_engine.begin() as conn:
            await conn.run_sync(seed)
            await conn.execute(text("DELETE FROM github_users"))
            pull_requests = (
                await conn.execute(text("SELECT number, author_id FROM pull_requests"))
            ).all()

        assert [tuple(row) for row in pull_requests] == [(5, None)]
