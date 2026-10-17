"""Shared test fixtures.

Databases come from one template per test session, built by ``alembic upgrade head`` and copied
for each test (redesign plan Step 3); ``metadata.create_all`` is used only by the schema
comparison in ``tests/db/test_schema.py``.
"""

from __future__ import annotations

import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from github_activity_db.db.engine import build_engine
from tests.db.alembic_support import upgrade


@pytest.fixture(scope="session")
def head_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A database migrated to head; read-only, so no test changes it in place."""
    path = tmp_path_factory.mktemp("template") / "head.db"
    upgrade(path)
    path.chmod(0o444)
    return path


@pytest.fixture
def migrated_database(head_template: Path, tmp_path: Path) -> Path:
    """A writable copy of the head template."""
    path = tmp_path / "test.db"
    shutil.copyfile(head_template, path)
    return path


@pytest.fixture
async def test_engine(migrated_database: Path) -> AsyncIterator[AsyncEngine]:
    """The application engine over a fresh copy of the head template.

    A file (not :memory:) because the application engine opens a new connection per session,
    and foreign-key enforcement must match production.
    """
    engine = build_engine(f"sqlite+aiosqlite:///{migrated_database}")
    yield engine
    await engine.dispose()
