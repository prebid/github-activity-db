"""Shared test fixtures."""

import pytest

from github_activity_db.db.engine import build_engine
from github_activity_db.db.models import Base


@pytest.fixture
async def test_engine(tmp_path):
    """A SQLite engine with the application's connection settings and every table created.

    A file (not :memory:) is required because the application engine opens a new connection
    per session, and foreign-key enforcement must match production.
    """
    engine = build_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()
