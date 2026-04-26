"""Rename filenames to file_changes with per-file statistics.

Replaces the simple list of filename strings with a richer structure
that includes per-file status (added/modified/removed), additions,
deletions, and total changes.

Revision ID: b3c4d5e6f7g8
Revises: a2b3c4d5e6f7
Create Date: 2026-02-09
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3c4d5e6f7g8"
down_revision: str | Sequence[str] | None = "a2b3c4d5e6f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Rename filenames column and migrate data to per-file format."""
    with op.batch_alter_table("pull_requests") as batch_op:
        batch_op.alter_column("filenames", new_column_name="file_changes")

    # Migrate existing data from list[str] to list[dict]
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, file_changes FROM pull_requests")).fetchall()

    for row in rows:
        pr_id = row[0]
        raw = row[1]
        if raw is None:
            continue
        data = json.loads(raw) if isinstance(raw, str) else raw
        if not data:
            continue
        # Skip if already in new format (list of dicts)
        if isinstance(data[0], dict):
            continue
        # Convert list[str] to list[dict]
        new_data = [
            {
                "filename": filename,
                "status": "unknown",
                "additions": 0,
                "deletions": 0,
                "changes": 0,
            }
            for filename in data
        ]
        conn.execute(
            sa.text("UPDATE pull_requests SET file_changes = :data WHERE id = :id"),
            {"data": json.dumps(new_data), "id": pr_id},
        )


def downgrade() -> None:
    """Revert to filenames column with list of strings."""
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, file_changes FROM pull_requests")).fetchall()

    for row in rows:
        pr_id = row[0]
        raw = row[1]
        if raw is None:
            continue
        data = json.loads(raw) if isinstance(raw, str) else raw
        if not data:
            continue
        if isinstance(data[0], str):
            continue
        filenames = [item.get("filename", "") for item in data if isinstance(item, dict)]
        conn.execute(
            sa.text("UPDATE pull_requests SET file_changes = :data WHERE id = :id"),
            {"data": json.dumps(filenames), "id": pr_id},
        )

    with op.batch_alter_table("pull_requests") as batch_op:
        batch_op.alter_column("file_changes", new_column_name="filenames")
