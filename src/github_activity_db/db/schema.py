"""Schema identity the installed package can read without ``alembic/`` (gap 74).

The wheel ships ``src/github_activity_db`` only, so code that must refuse a database not at the
migration head reads the chain from here; a test keeps ``MIGRATION_REVISIONS`` equal to the
chain in ``alembic/versions``.
"""

from __future__ import annotations

from typing import Final

# PRAGMA user_version of a database at head (redesign plan 5.1). Bumped when a consumer column is
# removed, renamed, or changes meaning, never for an additive change; the newest migration sets it.
SCHEMA_VERSION: Final = 1

# Every migration revision id, oldest first.
MIGRATION_REVISIONS: Final = ("01421d8dfaeb", "a2b3c4d5e6f7", "b3c4d5e6f7g8", "33f8570f6540")
HEAD_REVISION: Final = MIGRATION_REVISIONS[-1]
