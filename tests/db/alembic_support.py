"""Running the real migrations from tests, through the repository's alembic.ini and env.py."""

from __future__ import annotations

import os
import sqlite3
import textwrap
from pathlib import Path

from alembic import command
from alembic.config import Config

from tests.db.schema_support import user_objects

REPO_ROOT = Path(__file__).resolve().parents[2]
VERSIONS = REPO_ROOT / "alembic" / "versions"
FIRST_SCHEMA_HEAD = "b3c4d5e6f7g8"
# The revisions of Step 3a, of Step 3b-1, and the one after it, which adds
# sync_failures.last_run_id.
STEP3A = "33f8570f6540"
STEP3B1 = "94a52e4f1d57"
LAST_RUN_ID = "2d2ba76b177c"


def alembic_config(
    database: Path, *, test_versions: Path | None = None, script_location: Path | None = None
) -> Config:
    """A config for ``database``; ``test_versions`` adds a directory of test-only revisions."""
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.attributes["database_url"] = f"sqlite+aiosqlite:///{database}"
    config.attributes["configure_logger"] = False
    if script_location is not None:
        config.set_main_option("script_location", str(script_location))
    if test_versions is not None:
        versions = (script_location / "versions") if script_location is not None else VERSIONS
        config.set_main_option(
            "version_locations", os.pathsep.join([str(versions), str(test_versions)])
        )
    return config


def upgrade(database: Path, revision: str = "head", **options: Path) -> None:
    command.upgrade(alembic_config(database, **options), revision)


def write_revision(directory: Path, revision: str, down_revision: str, upgrade_body: str) -> None:
    """A test-only revision file whose ``upgrade()`` runs ``upgrade_body``."""
    directory.mkdir(exist_ok=True)
    source = (
        f'"""Test-only revision {revision}."""\n\n'
        "import sqlalchemy as sa\n"
        "from alembic import op\n\n"
        f"revision = {revision!r}\n"
        f"down_revision = {down_revision!r}\n"
        "branch_labels = None\n"
        "depends_on = None\n\n\n"
        "def upgrade() -> None:\n"
        f"{textwrap.indent(textwrap.dedent(upgrade_body).strip(), '    ')}\n\n\n"
        "def downgrade() -> None:\n"
        "    raise RuntimeError('test-only revision')\n"
    )
    (directory / f"{revision}_test_only.py").write_text(source)


def revision_of(database: Path) -> str | None:
    connection = sqlite3.connect(database)
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        if "alembic_version" not in tables:
            return None
        rows = connection.execute("SELECT version_num FROM alembic_version").fetchall()
    finally:
        connection.close()
    assert len(rows) == 1, rows
    return str(rows[0][0])


def query(database: Path, sql: str) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(database)
    try:
        return [tuple(row) for row in connection.execute(sql).fetchall()]
    finally:
        connection.close()


def table_names(database: Path) -> set[str]:
    return set(user_objects(database, "table"))
