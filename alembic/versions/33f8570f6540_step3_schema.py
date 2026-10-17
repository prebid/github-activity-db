"""Step 3 schema: the redesign plan 5.1 tables and views, except issues.

Drops the first schema's ``pull_requests``, ``pr_user_tags``, ``repositories``, and
``sync_failures`` (D18: the database is rebuilt from GitHub, not migrated row by row), keeps
``user_tags``, and recreates ``pr_user_tags`` empty, pointing at the new ``pull_requests``
(D17). It refuses to run while either tag table holds a row, the only data a rebuild could not
recover. Sets ``PRAGMA user_version`` to schema version 1.

The view SQL is a frozen copy: a later migration that replaces a view embeds its own.

Revision ID: 33f8570f6540
Revises: b3c4d5e6f7g8
Create Date: 2026-10-17
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "33f8570f6540"
down_revision: str | Sequence[str] | None = "b3c4d5e6f7g8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# SCHEMA_VERSION (src/github_activity_db/db/schema.py) at this revision.
USER_VERSION: Final = 1

# An account is automation when its type is Bot or its github_id is listed in
# automation_accounts; a commit is, when its linked account is or its author email is listed in
# automation_emails (plan 5.2). Views identify rows by consumer keys only, never by row ids.
VIEWS: Final = (
    (
        "v_github_users",
        """
CREATE VIEW v_github_users AS
SELECT
    u.github_id AS github_id,
    u.node_id AS node_id,
    u.login AS login,
    u.type AS type,
    u.deleted_at AS deleted_at,
    CASE
        WHEN u.type = 'Bot' THEN 1
        WHEN u.github_id IN (SELECT a.github_id FROM automation_accounts AS a) THEN 1
        ELSE 0
    END AS is_automation
FROM github_users AS u
""",
    ),
    (
        "v_pr_reviewers",
        """
CREATE VIEW v_pr_reviewers AS
SELECT
    repo.full_name AS repository_full_name,
    p.number AS pr_number,
    p.github_id AS pr_github_id,
    u.github_id AS reviewer_github_id,
    u.login AS reviewer_login,
    COALESCE(
        (
            SELECT v.state
            FROM pr_reviews AS v
            WHERE v.pr_id = pair.pr_id
                AND v.reviewer_id = pair.reviewer_id
                AND v.state IN ('APPROVED', 'CHANGES_REQUESTED', 'DISMISSED')
            ORDER BY v.github_id DESC
            LIMIT 1
        ),
        'COMMENTED'
    ) AS final_verdict,
    (
        SELECT COUNT(*)
        FROM pr_comments AS c
        WHERE c.pr_id = pair.pr_id AND c.author_id = pair.reviewer_id
    ) AS comment_count
FROM (
    SELECT DISTINCT r.pr_id AS pr_id, r.reviewer_id AS reviewer_id
    FROM pr_reviews AS r
    WHERE r.reviewer_id IS NOT NULL
) AS pair
JOIN pull_requests AS p ON p.id = pair.pr_id
JOIN repositories AS repo ON repo.id = p.repository_id
JOIN github_users AS u ON u.id = pair.reviewer_id
WHERE pair.reviewer_id IS NOT p.author_id
    AND u.type <> 'Bot'
    AND u.github_id NOT IN (SELECT a.github_id FROM automation_accounts AS a)
""",
    ),
    (
        "v_pr_commenters",
        """
CREATE VIEW v_pr_commenters AS
SELECT
    repo.full_name AS repository_full_name,
    p.number AS pr_number,
    p.github_id AS pr_github_id,
    u.github_id AS commenter_github_id,
    u.login AS commenter_login,
    COUNT(*) AS comment_count
FROM pr_comments AS c
JOIN pull_requests AS p ON p.id = c.pr_id
JOIN repositories AS repo ON repo.id = p.repository_id
JOIN github_users AS u ON u.id = c.author_id
WHERE c.author_id IS NOT p.author_id
    AND u.type <> 'Bot'
    AND u.github_id NOT IN (SELECT a.github_id FROM automation_accounts AS a)
    AND NOT EXISTS (
        SELECT 1 FROM pr_reviews AS r WHERE r.pr_id = c.pr_id AND r.reviewer_id = c.author_id
    )
GROUP BY p.id, u.id
""",
    ),
    (
        "v_commit_authors",
        """
CREATE VIEW v_commit_authors AS
SELECT
    repo.full_name AS repository_full_name,
    p.number AS pr_number,
    p.github_id AS pr_github_id,
    c.sha AS sha,
    u.github_id AS author_github_id,
    c.author_name AS author_name,
    c.author_email AS author_email,
    c.authored_at AS authored_at,
    CASE
        WHEN u.type = 'Bot' THEN 1
        WHEN u.github_id IN (SELECT a.github_id FROM automation_accounts AS a) THEN 1
        WHEN lower(c.author_email) IN (SELECT e.email FROM automation_emails AS e) THEN 1
        ELSE 0
    END AS is_automation
FROM pr_commits AS c
JOIN pull_requests AS p ON p.id = c.pr_id
JOIN repositories AS repo ON repo.id = p.repository_id
LEFT JOIN github_users AS u ON u.id = c.author_id
""",
    ),
)


def _refuse_tag_rows() -> None:
    bind = op.get_bind()
    counts = {
        "user_tags": bind.exec_driver_sql("SELECT COUNT(*) FROM user_tags").scalar_one(),
        "pr_user_tags": bind.exec_driver_sql("SELECT COUNT(*) FROM pr_user_tags").scalar_one(),
    }
    if any(counts.values()):
        held = ", ".join(f"{table} {count}" for table, count in counts.items())
        raise RuntimeError(
            f"tag rows would be lost ({held}): this migration recreates pr_user_tags empty and "
            "rebuilds the pull requests the tags point at; nothing was applied"
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
    _refuse_tag_rows()

    op.drop_table("pr_user_tags")
    op.drop_table("pull_requests")
    op.drop_table("sync_failures")
    op.drop_table("repositories")

    op.create_table(
        "repositories",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("github_id", sa.BigInteger(), nullable=False),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("full_name", sa.Text(), nullable=False),
        sa.Column("repo_group", sa.Text(), nullable=False),
        sa.Column("is_private", sa.Boolean(), nullable=False),
        sa.Column("is_archived", sa.Boolean(), nullable=False),
        sa.Column("default_branch", sa.Text(), nullable=False),
        sa.Column("is_tracked", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("github_id", name="uq_repositories_github_id"),
    )
    op.create_index(op.f("ix_repositories_full_name"), "repositories", ["full_name"])

    op.create_table(
        "github_users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("github_id", sa.BigInteger(), nullable=False),
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column("login", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("refreshed_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("github_id", name="uq_github_users_github_id"),
        sa.UniqueConstraint("node_id", name="uq_github_users_node_id"),
        sa.CheckConstraint(
            "type IN ('User', 'Bot', 'Organization', 'Mannequin')",
            name=op.f("ck_github_users_type"),
        ),
    )

    op.create_table(
        "pull_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("repository_id", sa.Integer(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("github_id", sa.BigInteger(), nullable=False),
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("is_draft", sa.Boolean(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("author_id", sa.Integer(), nullable=True),
        sa.Column("author_association", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("merged_at", sa.DateTime(), nullable=True),
        sa.Column("merged_by_id", sa.Integer(), nullable=True),
        sa.Column("base_ref", sa.Text(), nullable=False),
        sa.Column("targets_default_branch", sa.Boolean(), nullable=False),
        sa.Column("is_cross_repository", sa.Boolean(), nullable=False),
        sa.Column("additions", sa.Integer(), nullable=False),
        sa.Column("deletions", sa.Integer(), nullable=False),
        sa.Column("changed_files", sa.Integer(), nullable=False),
        sa.Column("commits_total", sa.Integer(), nullable=False),
        sa.Column("files_total", sa.Integer(), nullable=False),
        sa.Column("reviews_total", sa.Integer(), nullable=False),
        sa.Column("comments_total", sa.Integer(), nullable=False),
        sa.Column("labels_total", sa.Integer(), nullable=False),
        sa.Column("closing_issues_total", sa.Integer(), nullable=False),
        sa.Column("github_updated_at", sa.DateTime(), nullable=False),
        sa.Column("hydrated_at", sa.DateTime(), nullable=False),
        sa.Column("hydration_version", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        _references("repository_id", "pull_requests", "repositories", "CASCADE"),
        _references("author_id", "pull_requests", "github_users", "SET NULL"),
        _references("merged_by_id", "pull_requests", "github_users", "SET NULL"),
        sa.UniqueConstraint(
            "repository_id", "number", name="uq_pull_requests_repository_id_number"
        ),
        sa.UniqueConstraint("github_id", name="uq_pull_requests_github_id"),
        sa.CheckConstraint("state IN ('OPEN', 'MERGED')", name=op.f("ck_pull_requests_state")),
        sa.CheckConstraint(
            "(state = 'MERGED') = (merged_at IS NOT NULL)",
            name=op.f("ck_pull_requests_merged_at_iff_merged"),
        ),
    )
    op.create_index(
        op.f("ix_pull_requests_repository_id_state"), "pull_requests", ["repository_id", "state"]
    )
    op.create_index(
        op.f("ix_pull_requests_repository_id_merged_at"),
        "pull_requests",
        ["repository_id", "merged_at"],
    )
    op.create_index(op.f("ix_pull_requests_author_id"), "pull_requests", ["author_id"])
    op.create_index(op.f("ix_pull_requests_merged_by_id"), "pull_requests", ["merged_by_id"])

    op.create_table(
        "pr_labels",
        sa.Column("pr_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("pr_id", "name"),
        _references("pr_id", "pr_labels", "pull_requests", "CASCADE"),
    )

    op.create_table(
        "pr_reviews",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("github_id", sa.BigInteger(), nullable=False),
        sa.Column("pr_id", sa.Integer(), nullable=False),
        sa.Column("reviewer_id", sa.Integer(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        _references("pr_id", "pr_reviews", "pull_requests", "CASCADE"),
        _references("reviewer_id", "pr_reviews", "github_users", "SET NULL"),
        sa.UniqueConstraint("github_id", name="uq_pr_reviews_github_id"),
        sa.CheckConstraint(
            "state IN ('APPROVED', 'CHANGES_REQUESTED', 'COMMENTED', 'DISMISSED')",
            name=op.f("ck_pr_reviews_state"),
        ),
    )
    op.create_index(op.f("ix_pr_reviews_pr_id"), "pr_reviews", ["pr_id"])
    op.create_index(op.f("ix_pr_reviews_reviewer_id"), "pr_reviews", ["reviewer_id"])

    op.create_table(
        "pr_comments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("github_id", sa.BigInteger(), nullable=False),
        sa.Column("pr_id", sa.Integer(), nullable=False),
        sa.Column("author_id", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        _references("pr_id", "pr_comments", "pull_requests", "CASCADE"),
        _references("author_id", "pr_comments", "github_users", "SET NULL"),
        sa.UniqueConstraint("github_id", name="uq_pr_comments_github_id"),
    )
    op.create_index(op.f("ix_pr_comments_pr_id"), "pr_comments", ["pr_id"])
    op.create_index(op.f("ix_pr_comments_author_id"), "pr_comments", ["author_id"])

    op.create_table(
        "pr_commits",
        sa.Column("pr_id", sa.Integer(), nullable=False),
        sa.Column("sha", sa.Text(), nullable=False),
        sa.Column("author_id", sa.Integer(), nullable=True),
        sa.Column("author_name", sa.Text(), nullable=True),
        sa.Column("author_email", sa.Text(), nullable=True),
        sa.Column("authored_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("pr_id", "sha"),
        _references("pr_id", "pr_commits", "pull_requests", "CASCADE"),
        _references("author_id", "pr_commits", "github_users", "SET NULL"),
    )
    op.create_index(op.f("ix_pr_commits_author_id"), "pr_commits", ["author_id"])

    op.create_table(
        "pr_files",
        sa.Column("pr_id", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("change_type", sa.Text(), nullable=False),
        sa.Column("additions", sa.Integer(), nullable=False),
        sa.Column("deletions", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("pr_id", "ordinal"),
        _references("pr_id", "pr_files", "pull_requests", "CASCADE"),
    )

    op.create_table(
        "pr_closing_issues",
        sa.Column("pr_id", sa.Integer(), nullable=False),
        sa.Column("issue_repo_github_id", sa.BigInteger(), nullable=False),
        sa.Column("issue_repo_full_name", sa.Text(), nullable=False),
        sa.Column("issue_number", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("pr_id", "issue_repo_github_id", "issue_number"),
        _references("pr_id", "pr_closing_issues", "pull_requests", "CASCADE"),
    )
    op.create_index(
        op.f("ix_pr_closing_issues_issue_repo_github_id_issue_number"),
        "pr_closing_issues",
        ["issue_repo_github_id", "issue_number"],
    )

    op.create_table(
        "automation_accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("github_id", sa.BigInteger(), nullable=False),
        sa.Column("login", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("github_id", name="uq_automation_accounts_github_id"),
    )

    op.create_table(
        "automation_emails",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email", name="uq_automation_emails_email"),
        sa.CheckConstraint(
            "email = lower(email)", name=op.f("ck_automation_emails_email_lowercase")
        ),
    )

    op.create_table(
        "sync_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("deadline_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("code_version", sa.Text(), nullable=False),
        sa.Column("graphql_points", sa.Integer(), nullable=False),
        sa.Column("rest_calls", sa.Integer(), nullable=False),
        sa.Column("items_planned", sa.Integer(), nullable=False),
        sa.Column("items_hydrated", sa.Integer(), nullable=False),
        sa.Column("items_deleted", sa.Integer(), nullable=False),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("deletions_approved", sa.Text(), nullable=True),
        sa.Column("halt_reason", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('running', 'complete', 'time_limited', 'failed', 'aborted')",
            name=op.f("ck_sync_runs_status"),
        ),
        sa.CheckConstraint(
            "\"trigger\" IN ('schedule', 'workflow_dispatch', 'manual')",
            name=op.f("ck_sync_runs_trigger"),
        ),
    )

    op.create_table(
        "repo_sync_state",
        sa.Column("repository_id", sa.Integer(), nullable=False),
        sa.Column("last_index_started_at", sa.DateTime(), nullable=True),
        sa.Column("index_pr_total", sa.Integer(), nullable=True),
        sa.Column("index_issue_total", sa.Integer(), nullable=True),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
        sa.Column("last_run_id", sa.Integer(), nullable=False),
        sa.Column("last_outcome", sa.Text(), nullable=False),
        sa.Column("halt_reason", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("repository_id"),
        _references("repository_id", "repo_sync_state", "repositories", "CASCADE"),
        _references("last_run_id", "repo_sync_state", "sync_runs"),
        sa.CheckConstraint(
            "last_outcome IN ('complete', 'time_limited', 'failed', 'incomplete_index', "
            "'halted', 'untracked')",
            name=op.f("ck_repo_sync_state_last_outcome"),
        ),
    )
    op.create_index(op.f("ix_repo_sync_state_last_run_id"), "repo_sync_state", ["last_run_id"])

    op.create_table(
        "sync_pending",
        sa.Column("repository_id", sa.Integer(), nullable=False),
        sa.Column("item_kind", sa.Text(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("repository_id", "item_kind", "number"),
        _references("repository_id", "sync_pending", "repositories", "CASCADE"),
        _references("run_id", "sync_pending", "sync_runs"),
        sa.CheckConstraint(
            "item_kind IN ('pull_request', 'issue')", name=op.f("ck_sync_pending_item_kind")
        ),
    )
    op.create_index(op.f("ix_sync_pending_run_id"), "sync_pending", ["run_id"])

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
        sa.PrimaryKeyConstraint("id"),
        _references("repository_id", "sync_failures", "repositories", "CASCADE"),
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

    op.create_table(
        "pr_user_tags",
        sa.Column("pr_id", sa.Integer(), nullable=False),
        sa.Column("user_tag_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("pr_id", "user_tag_id"),
        _references("pr_id", "pr_user_tags", "pull_requests", "CASCADE"),
        _references("user_tag_id", "pr_user_tags", "user_tags", "CASCADE"),
    )

    for _, create in VIEWS:
        op.execute(create)

    op.execute(f"PRAGMA user_version = {USER_VERSION}")


def downgrade() -> None:
    raise RuntimeError(
        "migrations are never downgraded; recover from a database the code cannot upgrade with "
        "a bootstrap (redesign plan 5.4)"
    )
