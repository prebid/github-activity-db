"""Schema identity the installed package can read without ``alembic/`` (gap 74).

The wheel ships ``src/github_activity_db`` only, so code that must refuse a database not at the
migration head reads the chain from here; a test keeps ``MIGRATION_REVISIONS`` equal to the
chain in ``alembic/versions``.
"""

from __future__ import annotations

from typing import Final

# PRAGMA user_version of a database at head (redesign plan 5.1). Bumped when a consumer column is
# removed, renamed, or changes meaning, or a consumer view's frozen SQL changes (layout alone
# included), never for an added table column, table, or view; the newest migration sets it.
SCHEMA_VERSION: Final = 2

# Every migration revision id, oldest first.
MIGRATION_REVISIONS: Final = (
    "01421d8dfaeb",
    "a2b3c4d5e6f7",
    "b3c4d5e6f7g8",
    "33f8570f6540",
    "94a52e4f1d57",
    "2d2ba76b177c",
)
HEAD_REVISION: Final = MIGRATION_REVISIONS[-1]
