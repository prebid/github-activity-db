"""Tests for database engine and session management."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from github_activity_db.db.models import Base, Repository


class TestDatabaseEngine:
    """Tests for async SQLAlchemy engine operations."""

    async def test_create_tables(self, test_engine):
        """Test that all tables are created successfully."""
        async with test_engine.connect() as conn:
            # Query SQLite to list tables
            result = await conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
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
        try:
            async with session_factory() as session:
                repo = Repository(
                    owner="prebid",
                    name="rollback-test",
                    full_name="prebid/rollback-test",
                )
                session.add(repo)
                await session.flush()  # Write to DB but don't commit
                raise ValueError("Simulated error")
        except ValueError:
            pass

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
