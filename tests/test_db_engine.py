"""Tests for database engine and session management."""

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from github_activity_db.db.engine import (
    SQLITE_BUSY_TIMEOUT_MS,
    _apply_sqlite_pragmas,
    build_engine,
)
from github_activity_db.db.models import Base, Repository


class TestDatabaseEngine:
    """Tests for async SQLAlchemy engine operations."""

    async def test_create_tables(self, test_engine):
        """Test that all tables are created successfully."""
        async with test_engine.connect() as conn:
            # Query SQLite to list tables
            result = await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
            tables = {row[0] for row in result.fetchall()}

        # Verify expected tables exist
        assert "repositories" in tables
        assert "pull_requests" in tables
        assert "user_tags" in tables
        assert "pr_user_tags" in tables

    async def test_session_commits_on_success(self, test_engine):
        """Test that session commits changes on successful operations."""
        session_factory = async_sessionmaker(
            test_engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

        # Create and commit a repository
        async with session_factory() as session:
            repo = Repository(
                owner="prebid",
                name="test-repo",
                full_name="prebid/test-repo",
            )
            session.add(repo)
            await session.commit()
            repo_id = repo.id

        # Verify in a new session
        async with session_factory() as session:
            result = await session.get(Repository, repo_id)
            assert result is not None
            assert result.full_name == "prebid/test-repo"

    async def test_session_rollbacks_on_error(self, test_engine):
        """Test that session rolls back on exception."""
        session_factory = async_sessionmaker(
            test_engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

        # Attempt to create a repo but roll back
        with pytest.raises(ValueError, match="Simulated error"):
            async with session_factory() as session:
                repo = Repository(
                    owner="prebid",
                    name="rollback-test",
                    full_name="prebid/rollback-test",
                )
                session.add(repo)
                await session.flush()  # Write to DB but don't commit
                raise ValueError("Simulated error")

        # Verify repo was not persisted
        async with session_factory() as session:
            result = await session.execute(
                text("SELECT COUNT(*) FROM repositories WHERE full_name = 'prebid/rollback-test'")
            )
            count = result.scalar()
            assert count == 0

    async def test_dispose_engine(self):
        """Test that engine can be disposed cleanly."""
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        # Dispose should not raise
        await engine.dispose()

        # Engine should be disposed (pool closed)
        # Creating a new connection after dispose should fail or create new pool
        # Just verify dispose completed without error
        assert True


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
            await conn.execute(
                text(
                    "INSERT INTO repositories (id, owner, name, full_name, is_active, created_at) "
                    "VALUES (1, 'prebid', 'cascade', 'prebid/cascade', 1, CURRENT_TIMESTAMP)"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO sync_failures (repository_id, pr_number, error_message, "
                    "error_type, retry_count, status, failed_at, created_at) "
                    "VALUES (1, 7, 'e', 'ValueError', 0, 'PENDING', CURRENT_TIMESTAMP, "
                    "CURRENT_TIMESTAMP)"
                )
            )
            await conn.execute(text("DELETE FROM repositories WHERE id = 1"))
            remaining = (await conn.execute(text("SELECT COUNT(*) FROM sync_failures"))).scalar()

        assert remaining == 0
