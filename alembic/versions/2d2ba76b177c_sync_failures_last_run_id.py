"""sync_failures.last_run_id: the latest run in which the item failed.

``attempts`` counts the runs in which an item failed; the run that last counted is this
column, so a second failure in the same run adds nothing and a failure in another run adds one,
whatever the two runs' clocks read (redesign plan 5.1). No sync has written ``sync_failures``,
so this revision refuses a database holding a row in it and recreates it empty with the column.
No view reads the table. ``PRAGMA user_version`` stays schema version 2: an added column needs no
new schema version, and the schema-version lock lists it in version 2's consumer columns.

Revision ID: 2d2ba76b177c
Revises: 94a52e4f1d57
Create Date: 2026-10-19
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "2d2ba76b177c"
down_revision: str | Sequence[str] | None = "94a52e4f1d57"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# SCHEMA_VERSION (src/github_activity_db/db/schema.py) at this revision.
USER_VERSION: Final = 2

# Every table this revision adds a column to; it refuses to run while one holds a row.
CHANGED_TABLES: Final = ("sync_failures",)


def _refuse_rows() -> None:
    bind = op.get_bind()
    counts = {
        table: bind.execute(sa.select(sa.func.count()).select_from(sa.table(table))).scalar_one()
        for table in CHANGED_TABLES
    }
    held = {table: count for table, count in counts.items() if count}
    if held:
        listed = ", ".join(f"{table} {count}" for table, count in held.items())
        raise RuntimeError(
            f"rows would be lost ({listed}): this migration recreates the tables it adds "
            "columns to, which no sync has written yet; nothing was applied"
        )


def _references(
    column: str, table: str, target: str, ondelete: str | None = None
) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column],
        [f"{target}.id"],
        name=op.f(f"fk_{table}_{column}_{target}"),
        ondelete=ondelete,
    )


def upgrade() -> None:
    _refuse_rows()
    op.drop_table("sync_failures")
    op.create_table(
        "sync_failures",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("repository_id", sa.Integer(), nullable=False),
        sa.Column("item_kind", sa.Text(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column("first_failed_at", sa.DateTime(), nullable=False),
        sa.Column("last_failed_at", sa.DateTime(), nullable=False),
        sa.Column("last_run_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        _references("repository_id", "sync_failures", "repositories", "CASCADE"),
        _references("last_run_id", "sync_failures", "sync_runs"),
        sa.UniqueConstraint(
            "repository_id",
            "item_kind",
            "number",
            name="uq_sync_failures_repository_id_item_kind_number",
        ),
        sa.CheckConstraint(
            "item_kind IN ('pull_request', 'issue')", name=op.f("ck_sync_failures_item_kind")
        ),
    )
    op.create_index(op.f("ix_sync_failures_last_run_id"), "sync_failures", ["last_run_id"])

    op.execute(f"PRAGMA user_version = {USER_VERSION}")


def downgrade() -> None:
    raise RuntimeError(
        "migrations are never downgraded; recover from a database the code cannot upgrade with "
        "a bootstrap (redesign plan 5.4)"
    )
