"""Step 3b-1 collection schema: the redesign plan 5.1 columns and tables Step 3b-1 adds.

No sync has written the Step 3 schema, so this revision refuses a database holding a row in any
table it adds a column to, and recreates those tables empty rather than altering them; the
tables it leaves alone reference them by name. It adds ``repo_labels``, ``pr_review_requests``,
``pr_assignees``, and ``account_profiles``; admits ``CLOSED`` pull requests and
``EnterpriseUserAccount`` accounts with a null ``github_id``; renames ``deleted_at`` to
``not_found_at``; drops ``sync_runs.halt_reason``; and replaces every view with the row-level
views of plan 5.1. Sets ``PRAGMA user_version`` to schema version 2.

The view SQL is a frozen copy: a later migration that replaces a view embeds its own.

Revision ID: 94a52e4f1d57
Revises: 33f8570f6540
Create Date: 2026-10-18
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "94a52e4f1d57"
down_revision: str | Sequence[str] | None = "33f8570f6540"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# SCHEMA_VERSION (src/github_activity_db/db/schema.py) at this revision.
USER_VERSION: Final = 2

# Every table this revision adds a column to; it refuses to run while one holds a row.
CHANGED_TABLES: Final = (
    "repositories",
    "github_users",
    "pull_requests",
    "pr_labels",
    "pr_reviews",
    "pr_comments",
    "pr_commits",
    "pr_closing_issues",
    "automation_accounts",
    "automation_emails",
    "sync_runs",
)

# Views this revision removes (gaps 81 and 82); the views it replaces are recreated from VIEWS.
DROPPED_VIEWS: Final = ("v_pr_reviewers", "v_pr_commenters")
REPLACED_VIEWS: Final = ("v_github_users", "v_commit_authors")

# One row per stored row; an account's type and its [automation] entry's reason stand side by
# side, never merged (D29). Views identify rows by consumer keys only, never by row ids.
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
    u.name AS name,
    u.created_at AS created_at,
    u.updated_at AS updated_at,
    u.linked_user_github_id AS linked_user_github_id,
    u.claimant_github_id AS claimant_github_id,
    u.refreshed_at AS refreshed_at,
    u.not_found_at AS not_found_at,
    (SELECT a.reason FROM automation_accounts AS a WHERE a.github_id = u.github_id)
        AS automation_reason
FROM github_users AS u
""",
    ),
    (
        "v_account_profiles",
        """
CREATE VIEW v_account_profiles AS
SELECT
    u.github_id AS github_id,
    u.node_id AS node_id,
    u.login AS login,
    u.type AS type,
    (SELECT a.reason FROM automation_accounts AS a WHERE a.github_id = u.github_id)
        AS automation_reason,
    p.company AS company,
    p.location AS location,
    p.website_url AS website_url,
    p.description AS description,
    p.email AS email,
    p.twitter_username AS twitter_username,
    p.is_verified AS is_verified,
    p.archived_at AS archived_at,
    p.has_sponsors_listing AS has_sponsors_listing,
    p.pinned_items_remaining AS pinned_items_remaining,
    p.refreshed_at AS refreshed_at
FROM account_profiles AS p
JOIN github_users AS u ON u.id = p.account_id
""",
    ),
    (
        "v_pr_reviews",
        """
CREATE VIEW v_pr_reviews AS
SELECT
    repo.github_id AS repository_github_id,
    repo.full_name AS repository_full_name,
    repo.is_tracked AS repository_is_tracked,
    p.number AS pr_number,
    p.github_id AS pr_github_id,
    r.github_id AS review_github_id,
    r.node_id AS review_node_id,
    r.state AS state,
    r.created_at AS created_at,
    r.submitted_at AS submitted_at,
    r.published_at AS published_at,
    r.updated_at AS updated_at,
    r.last_edited_at AS last_edited_at,
    r.includes_created_edit AS includes_created_edit,
    r.commit_oid AS commit_oid,
    r.author_association AS author_association,
    r.author_can_push_to_repository AS author_can_push_to_repository,
    r.inline_comments_total AS inline_comments_total,
    r.reviewer_reported AS reviewer_reported,
    reviewer.github_id AS reviewer_github_id,
    reviewer.node_id AS reviewer_node_id,
    reviewer.login AS reviewer_login,
    reviewer.type AS reviewer_type,
    (SELECT a.reason FROM automation_accounts AS a WHERE a.github_id = reviewer.github_id)
        AS reviewer_automation_reason,
    editor.github_id AS editor_github_id,
    editor.node_id AS editor_node_id,
    editor.login AS editor_login,
    editor.type AS editor_type,
    (SELECT a.reason FROM automation_accounts AS a WHERE a.github_id = editor.github_id)
        AS editor_automation_reason,
    CASE
        WHEN r.reviewer_id IS NULL OR p.author_id IS NULL THEN NULL
        WHEN r.reviewer_id = p.author_id THEN 1
        ELSE 0
    END AS is_pr_author
FROM pr_reviews AS r
JOIN pull_requests AS p ON p.id = r.pr_id
JOIN repositories AS repo ON repo.id = p.repository_id
LEFT JOIN github_users AS reviewer ON reviewer.id = r.reviewer_id
LEFT JOIN github_users AS editor ON editor.id = r.editor_id
""",
    ),
    (
        "v_pr_comments",
        """
CREATE VIEW v_pr_comments AS
SELECT
    repo.github_id AS repository_github_id,
    repo.full_name AS repository_full_name,
    repo.is_tracked AS repository_is_tracked,
    p.number AS pr_number,
    p.github_id AS pr_github_id,
    c.github_id AS comment_github_id,
    c.node_id AS comment_node_id,
    c.created_at AS created_at,
    c.published_at AS published_at,
    c.updated_at AS updated_at,
    c.last_edited_at AS last_edited_at,
    c.created_via_email AS created_via_email,
    c.includes_created_edit AS includes_created_edit,
    c.author_association AS author_association,
    c.author_reported AS author_reported,
    author.github_id AS author_github_id,
    author.node_id AS author_node_id,
    author.login AS author_login,
    author.type AS author_type,
    (SELECT a.reason FROM automation_accounts AS a WHERE a.github_id = author.github_id)
        AS author_automation_reason,
    editor.github_id AS editor_github_id,
    editor.node_id AS editor_node_id,
    editor.login AS editor_login,
    editor.type AS editor_type,
    (SELECT a.reason FROM automation_accounts AS a WHERE a.github_id = editor.github_id)
        AS editor_automation_reason,
    CASE
        WHEN c.author_id IS NULL OR p.author_id IS NULL THEN NULL
        WHEN c.author_id = p.author_id THEN 1
        ELSE 0
    END AS is_pr_author,
    CASE
        WHEN c.author_id IS NULL THEN NULL
        WHEN EXISTS (
            SELECT 1 FROM pr_reviews AS r WHERE r.pr_id = c.pr_id AND r.reviewer_id = c.author_id
        ) THEN 1
        ELSE 0
    END AS author_also_reviewed
FROM pr_comments AS c
JOIN pull_requests AS p ON p.id = c.pr_id
JOIN repositories AS repo ON repo.id = p.repository_id
LEFT JOIN github_users AS author ON author.id = c.author_id
LEFT JOIN github_users AS editor ON editor.id = c.editor_id
""",
    ),
    (
        "v_commit_authors",
        """
CREATE VIEW v_commit_authors AS
SELECT
    repo.github_id AS repository_github_id,
    repo.full_name AS repository_full_name,
    repo.is_tracked AS repository_is_tracked,
    p.number AS pr_number,
    p.github_id AS pr_github_id,
    c.sha AS sha,
    c.linked_github_id AS linked_github_id,
    c.author_name AS author_name,
    c.author_email AS author_email,
    c.authored_at AS authored_at,
    account.github_id AS account_github_id,
    account.node_id AS account_node_id,
    account.login AS account_login,
    account.type AS account_type,
    (SELECT a.reason FROM automation_accounts AS a WHERE a.github_id = account.github_id)
        AS account_automation_reason,
    (SELECT e.reason FROM automation_emails AS e WHERE e.email = lower(c.author_email))
        AS email_automation_reason
FROM pr_commits AS c
JOIN pull_requests AS p ON p.id = c.pr_id
JOIN repositories AS repo ON repo.id = p.repository_id
LEFT JOIN github_users AS account ON account.id = c.author_id
""",
    ),
    (
        "v_pr_relationships",
        """
CREATE VIEW v_pr_relationships AS
SELECT
    repo.github_id AS repository_github_id,
    repo.full_name AS repository_full_name,
    repo.is_tracked AS repository_is_tracked,
    p.number AS pr_number,
    p.github_id AS pr_github_id,
    rel.relationship AS relationship,
    rel.review_github_id AS review_github_id,
    rel.comment_github_id AS comment_github_id,
    rel.request_node_id AS request_node_id,
    rel.request_github_id AS request_github_id,
    rel.request_ordinal AS request_ordinal,
    rel.commit_sha AS commit_sha,
    rel.reported AS reported,
    account.github_id AS account_github_id,
    account.node_id AS account_node_id,
    account.login AS account_login,
    account.type AS account_type,
    (SELECT a.reason FROM automation_accounts AS a WHERE a.github_id = account.github_id)
        AS account_automation_reason,
    account.not_found_at AS account_not_found_at,
    rel.review_state AS review_state,
    rel.review_submitted_at AS review_submitted_at,
    rel.requested_type AS requested_type,
    rel.requested_github_id AS requested_github_id,
    rel.team_slug AS team_slug,
    rel.as_code_owner AS as_code_owner,
    rel.linked_github_id AS linked_github_id,
    rel.author_name AS author_name,
    rel.author_email AS author_email,
    (SELECT e.reason FROM automation_emails AS e WHERE e.email = lower(rel.author_email))
        AS email_automation_reason,
    p.state AS pr_state,
    p.created_at AS pr_created_at,
    p.merged_at AS pr_merged_at,
    p.closed_at AS pr_closed_at,
    p.base_ref AS base_ref,
    p.targets_default_branch AS targets_default_branch,
    p.merge_commit_oid AS merge_commit_oid,
    p.additions AS additions,
    p.deletions AS deletions,
    p.changed_files AS changed_files
FROM (
    SELECT
        pr.id AS pr_id,
        'author' AS relationship,
        NULL AS review_github_id,
        NULL AS comment_github_id,
        NULL AS request_node_id,
        NULL AS request_github_id,
        NULL AS request_ordinal,
        NULL AS commit_sha,
        pr.author_reported AS reported,
        pr.author_id AS account_id,
        NULL AS review_state,
        NULL AS review_submitted_at,
        NULL AS requested_type,
        NULL AS requested_github_id,
        NULL AS team_slug,
        NULL AS as_code_owner,
        NULL AS linked_github_id,
        NULL AS author_name,
        NULL AS author_email
    FROM pull_requests AS pr
    UNION ALL
    SELECT
        pr.id, 'merged_by', NULL, NULL, NULL, NULL, NULL, NULL, pr.merged_by_reported,
        pr.merged_by_id, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL
    FROM pull_requests AS pr
    WHERE pr.state = 'MERGED'
    UNION ALL
    SELECT
        r.pr_id, 'review', r.github_id, NULL, NULL, NULL, NULL, NULL, r.reviewer_reported,
        r.reviewer_id, r.state, r.submitted_at, NULL, NULL, NULL, NULL, NULL, NULL, NULL
    FROM pr_reviews AS r
    UNION ALL
    SELECT
        c.pr_id, 'comment', NULL, c.github_id, NULL, NULL, NULL, NULL, c.author_reported,
        c.author_id, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL
    FROM pr_comments AS c
    UNION ALL
    SELECT
        q.pr_id, 'review_request', NULL, NULL, q.request_node_id, q.request_github_id,
        q.ordinal, NULL, NULL, q.account_id, NULL, NULL, q.requested_type,
        q.requested_github_id, q.team_slug, q.as_code_owner, NULL, NULL, NULL
    FROM pr_review_requests AS q
    UNION ALL
    SELECT
        s.pr_id, 'assignee', NULL, NULL, NULL, NULL, NULL, NULL, NULL, s.account_id, NULL,
        NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL
    FROM pr_assignees AS s
    UNION ALL
    SELECT
        m.pr_id, 'commit_author', NULL, NULL, NULL, NULL, NULL, m.sha, NULL, m.author_id, NULL,
        NULL, NULL, NULL, NULL, NULL, m.linked_github_id, m.author_name, m.author_email
    FROM pr_commits AS m
    UNION ALL
    SELECT
        pr.id, 'pr_editor', NULL, NULL, NULL, NULL, NULL, NULL, NULL, pr.editor_id, NULL, NULL,
        NULL, NULL, NULL, NULL, NULL, NULL, NULL
    FROM pull_requests AS pr
    WHERE pr.editor_id IS NOT NULL OR pr.last_edited_at IS NOT NULL
    UNION ALL
    SELECT
        r.pr_id, 'review_editor', r.github_id, NULL, NULL, NULL, NULL, NULL, NULL, r.editor_id,
        NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL
    FROM pr_reviews AS r
    WHERE r.editor_id IS NOT NULL OR r.last_edited_at IS NOT NULL
    UNION ALL
    SELECT
        c.pr_id, 'comment_editor', NULL, c.github_id, NULL, NULL, NULL, NULL, NULL, c.editor_id,
        NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL
    FROM pr_comments AS c
    WHERE c.editor_id IS NOT NULL OR c.last_edited_at IS NOT NULL
) AS rel
JOIN pull_requests AS p ON p.id = rel.pr_id
JOIN repositories AS repo ON repo.id = p.repository_id
LEFT JOIN github_users AS account ON account.id = rel.account_id
""",
    ),
)


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


def _index(table: str, *columns: str) -> None:
    op.create_index(op.f(f"ix_{table}_{'_'.join(columns)}"), table, list(columns))


def upgrade() -> None:
    _refuse_rows()

    for name in (*DROPPED_VIEWS, *REPLACED_VIEWS):
        op.execute(f"DROP VIEW {name}")
    for table in CHANGED_TABLES:
        op.drop_table(table)

    op.create_table(
        "repositories",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("github_id", sa.BigInteger(), nullable=False),
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("full_name", sa.Text(), nullable=False),
        sa.Column("repo_group", sa.Text(), nullable=False),
        sa.Column("is_private", sa.Boolean(), nullable=False),
        sa.Column("is_archived", sa.Boolean(), nullable=False),
        sa.Column("default_branch", sa.Text(), nullable=False),
        sa.Column("labels_total", sa.Integer(), nullable=False),
        sa.Column("is_tracked", sa.Boolean(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=False),
        sa.Column("is_security_policy_enabled", sa.Boolean(), nullable=True),
        sa.Column("visibility", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("homepage_url", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("pushed_at", sa.DateTime(), nullable=True),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.Column("stargazer_count", sa.Integer(), nullable=False),
        sa.Column("fork_count", sa.Integer(), nullable=False),
        sa.Column("disk_usage", sa.Integer(), nullable=True),
        sa.Column("forking_allowed", sa.Boolean(), nullable=False),
        sa.Column("allow_update_branch", sa.Boolean(), nullable=False),
        sa.Column("web_commit_signoff_required", sa.Boolean(), nullable=False),
        sa.Column("has_wiki_enabled", sa.Boolean(), nullable=False),
        sa.Column("has_discussions_enabled", sa.Boolean(), nullable=False),
        sa.Column("has_projects_enabled", sa.Boolean(), nullable=False),
        sa.Column("has_vulnerability_alerts_enabled", sa.Boolean(), nullable=False),
        sa.Column("uses_custom_open_graph_image", sa.Boolean(), nullable=False),
        sa.Column("merge_commit_allowed", sa.Boolean(), nullable=False),
        sa.Column("squash_merge_allowed", sa.Boolean(), nullable=False),
        sa.Column("rebase_merge_allowed", sa.Boolean(), nullable=False),
        sa.Column("delete_branch_on_merge", sa.Boolean(), nullable=False),
        sa.Column("merge_commit_title", sa.Text(), nullable=False),
        sa.Column("merge_commit_message", sa.Text(), nullable=False),
        sa.Column("squash_merge_commit_title", sa.Text(), nullable=False),
        sa.Column("squash_merge_commit_message", sa.Text(), nullable=False),
        sa.Column("primary_language", sa.Text(), nullable=True),
        sa.Column("license_spdx_id", sa.Text(), nullable=True),
        sa.Column("plan_features_codeowners", sa.Boolean(), nullable=False),
        sa.Column("plan_features_team_review_requests", sa.Boolean(), nullable=False),
        sa.Column("plan_features_maximum_manual_review_requests", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("github_id", name="uq_repositories_github_id"),
        sa.UniqueConstraint("node_id", name="uq_repositories_node_id"),
    )
    _index("repositories", "full_name")

    op.create_table(
        "repo_labels",
        sa.Column("repository_id", sa.Integer(), nullable=False),
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("color", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("repository_id", "node_id"),
        _references("repository_id", "repo_labels", "repositories", "CASCADE"),
    )

    op.create_table(
        "github_users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("github_id", sa.BigInteger(), nullable=True),
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column("login", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("linked_user_github_id", sa.BigInteger(), nullable=True),
        sa.Column("claimant_github_id", sa.BigInteger(), nullable=True),
        sa.Column("refreshed_at", sa.DateTime(), nullable=False),
        sa.Column("not_found_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("github_id", name="uq_github_users_github_id"),
        sa.UniqueConstraint("node_id", name="uq_github_users_node_id"),
        sa.CheckConstraint(
            "type IN ('User', 'Bot', 'Organization', 'Mannequin', 'EnterpriseUserAccount')",
            name=op.f("ck_github_users_type"),
        ),
        sa.CheckConstraint(
            "(github_id IS NULL) = (type = 'EnterpriseUserAccount')",
            name=op.f("ck_github_users_github_id_null_iff_enterprise_account"),
        ),
    )

    op.create_table(
        "account_profiles",
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("company", sa.Text(), nullable=True),
        sa.Column("location", sa.Text(), nullable=True),
        sa.Column("website_url", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("twitter_username", sa.Text(), nullable=True),
        sa.Column("is_verified", sa.Boolean(), nullable=True),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.Column("has_sponsors_listing", sa.Boolean(), nullable=True),
        sa.Column("pinned_items_remaining", sa.Integer(), nullable=True),
        sa.Column("refreshed_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("account_id"),
        _references("account_id", "account_profiles", "github_users", "CASCADE"),
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
        sa.Column("author_reported", sa.Boolean(), nullable=False),
        sa.Column("author_association", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("merged_at", sa.DateTime(), nullable=True),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("last_edited_at", sa.DateTime(), nullable=True),
        sa.Column("editor_id", sa.Integer(), nullable=True),
        sa.Column("includes_created_edit", sa.Boolean(), nullable=False),
        sa.Column("maintainer_can_modify", sa.Boolean(), nullable=False),
        sa.Column("merged_by_id", sa.Integer(), nullable=True),
        sa.Column("merged_by_reported", sa.Boolean(), nullable=False),
        sa.Column("base_ref", sa.Text(), nullable=False),
        sa.Column("base_ref_oid", sa.Text(), nullable=False),
        sa.Column("head_ref_name", sa.Text(), nullable=False),
        sa.Column("head_ref_oid", sa.Text(), nullable=False),
        sa.Column("head_repo_github_id", sa.BigInteger(), nullable=True),
        sa.Column("head_repo_full_name", sa.Text(), nullable=True),
        sa.Column("head_repo_owner_node_id", sa.Text(), nullable=True),
        sa.Column("head_repo_owner_login", sa.Text(), nullable=True),
        sa.Column("merge_commit_oid", sa.Text(), nullable=True),
        sa.Column("targets_default_branch", sa.Boolean(), nullable=False),
        sa.Column("is_cross_repository", sa.Boolean(), nullable=False),
        sa.Column("milestone_number", sa.Integer(), nullable=True),
        sa.Column("milestone_title", sa.Text(), nullable=True),
        sa.Column("review_decision", sa.Text(), nullable=True),
        sa.Column("mergeable", sa.Text(), nullable=False),
        sa.Column("merge_state_status", sa.Text(), nullable=False),
        sa.Column("can_be_rebased", sa.Boolean(), nullable=False),
        sa.Column("potential_merge_commit_oid", sa.Text(), nullable=True),
        sa.Column("stack_node_id", sa.Text(), nullable=True),
        sa.Column("stack_number", sa.Integer(), nullable=True),
        sa.Column("stack_size", sa.Integer(), nullable=True),
        sa.Column("stack_base_ref_name", sa.Text(), nullable=True),
        sa.Column("stack_entry_node_id", sa.Text(), nullable=True),
        sa.Column("stack_position", sa.Integer(), nullable=True),
        sa.Column("additions", sa.Integer(), nullable=False),
        sa.Column("deletions", sa.Integer(), nullable=False),
        sa.Column("changed_files", sa.Integer(), nullable=False),
        sa.Column("total_comments_count", sa.Integer(), nullable=True),
        sa.Column("commits_total", sa.Integer(), nullable=False),
        sa.Column("files_total", sa.Integer(), nullable=False),
        sa.Column("reviews_total", sa.Integer(), nullable=False),
        sa.Column("comments_total", sa.Integer(), nullable=False),
        sa.Column("labels_total", sa.Integer(), nullable=False),
        sa.Column("closing_issues_total", sa.Integer(), nullable=False),
        sa.Column("user_linked_closing_issues_total", sa.Integer(), nullable=False),
        sa.Column("review_requests_total", sa.Integer(), nullable=False),
        sa.Column("assignees_total", sa.Integer(), nullable=False),
        sa.Column("github_updated_at", sa.DateTime(), nullable=False),
        sa.Column("hydrated_at", sa.DateTime(), nullable=False),
        sa.Column("hydration_version", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        _references("repository_id", "pull_requests", "repositories", "CASCADE"),
        _references("author_id", "pull_requests", "github_users", "SET NULL"),
        _references("editor_id", "pull_requests", "github_users", "SET NULL"),
        _references("merged_by_id", "pull_requests", "github_users", "SET NULL"),
        sa.UniqueConstraint(
            "repository_id", "number", name="uq_pull_requests_repository_id_number"
        ),
        sa.UniqueConstraint("github_id", name="uq_pull_requests_github_id"),
        sa.CheckConstraint(
            "state IN ('OPEN', 'CLOSED', 'MERGED')", name=op.f("ck_pull_requests_state")
        ),
        sa.CheckConstraint(
            "(state = 'MERGED') = (merged_at IS NOT NULL)",
            name=op.f("ck_pull_requests_merged_at_iff_merged"),
        ),
    )
    _index("pull_requests", "repository_id", "state")
    _index("pull_requests", "repository_id", "merged_at")
    _index("pull_requests", "author_id")
    _index("pull_requests", "merged_by_id")
    _index("pull_requests", "editor_id")
    _index("pull_requests", "merge_commit_oid")

    op.create_table(
        "pr_labels",
        sa.Column("pr_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("label_node_id", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("pr_id", "name"),
        _references("pr_id", "pr_labels", "pull_requests", "CASCADE"),
    )

    op.create_table(
        "pr_reviews",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("github_id", sa.BigInteger(), nullable=False),
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column("pr_id", sa.Integer(), nullable=False),
        sa.Column("reviewer_id", sa.Integer(), nullable=True),
        sa.Column("reviewer_reported", sa.Boolean(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("last_edited_at", sa.DateTime(), nullable=True),
        sa.Column("editor_id", sa.Integer(), nullable=True),
        sa.Column("includes_created_edit", sa.Boolean(), nullable=False),
        sa.Column("commit_oid", sa.Text(), nullable=True),
        sa.Column("author_association", sa.Text(), nullable=False),
        sa.Column("author_can_push_to_repository", sa.Boolean(), nullable=False),
        sa.Column("inline_comments_total", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        _references("pr_id", "pr_reviews", "pull_requests", "CASCADE"),
        _references("reviewer_id", "pr_reviews", "github_users", "SET NULL"),
        _references("editor_id", "pr_reviews", "github_users", "SET NULL"),
        sa.UniqueConstraint("github_id", name="uq_pr_reviews_github_id"),
        sa.CheckConstraint(
            "state IN ('APPROVED', 'CHANGES_REQUESTED', 'COMMENTED', 'DISMISSED')",
            name=op.f("ck_pr_reviews_state"),
        ),
    )
    _index("pr_reviews", "pr_id", "reviewer_id", "github_id")
    _index("pr_reviews", "reviewer_id")
    _index("pr_reviews", "editor_id")

    op.create_table(
        "pr_comments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("github_id", sa.BigInteger(), nullable=False),
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column("pr_id", sa.Integer(), nullable=False),
        sa.Column("author_id", sa.Integer(), nullable=True),
        sa.Column("author_reported", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("last_edited_at", sa.DateTime(), nullable=True),
        sa.Column("editor_id", sa.Integer(), nullable=True),
        sa.Column("created_via_email", sa.Boolean(), nullable=False),
        sa.Column("includes_created_edit", sa.Boolean(), nullable=False),
        sa.Column("author_association", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        _references("pr_id", "pr_comments", "pull_requests", "CASCADE"),
        _references("author_id", "pr_comments", "github_users", "SET NULL"),
        _references("editor_id", "pr_comments", "github_users", "SET NULL"),
        sa.UniqueConstraint("github_id", name="uq_pr_comments_github_id"),
    )
    _index("pr_comments", "pr_id", "author_id")
    _index("pr_comments", "author_id")
    _index("pr_comments", "editor_id")

    op.create_table(
        "pr_commits",
        sa.Column("pr_id", sa.Integer(), nullable=False),
        sa.Column("sha", sa.Text(), nullable=False),
        sa.Column("author_id", sa.Integer(), nullable=True),
        sa.Column("linked_github_id", sa.BigInteger(), nullable=True),
        sa.Column("author_name", sa.Text(), nullable=True),
        sa.Column("author_email", sa.Text(), nullable=True),
        sa.Column("authored_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("pr_id", "sha"),
        _references("pr_id", "pr_commits", "pull_requests", "CASCADE"),
        _references("author_id", "pr_commits", "github_users", "SET NULL"),
    )
    _index("pr_commits", "author_id")

    op.create_table(
        "pr_closing_issues",
        sa.Column("pr_id", sa.Integer(), nullable=False),
        sa.Column("issue_repo_github_id", sa.BigInteger(), nullable=False),
        sa.Column("issue_repo_full_name", sa.Text(), nullable=False),
        sa.Column("issue_number", sa.Integer(), nullable=False),
        sa.Column("issue_github_id", sa.BigInteger(), nullable=False),
        sa.Column("user_linked", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("pr_id", "issue_repo_github_id", "issue_number"),
        _references("pr_id", "pr_closing_issues", "pull_requests", "CASCADE"),
    )
    _index("pr_closing_issues", "issue_repo_github_id", "issue_number")

    op.create_table(
        "pr_review_requests",
        sa.Column("pr_id", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("request_github_id", sa.BigInteger(), nullable=True),
        sa.Column("request_node_id", sa.Text(), nullable=False),
        sa.Column("requested_type", sa.Text(), nullable=True),
        sa.Column("requested_github_id", sa.BigInteger(), nullable=True),
        sa.Column("requested_node_id", sa.Text(), nullable=True),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("team_slug", sa.Text(), nullable=True),
        sa.Column("as_code_owner", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("pr_id", "ordinal"),
        _references("pr_id", "pr_review_requests", "pull_requests", "CASCADE"),
        _references("account_id", "pr_review_requests", "github_users", "SET NULL"),
    )
    _index("pr_review_requests", "account_id")

    op.create_table(
        "pr_assignees",
        sa.Column("pr_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("pr_id", "account_id"),
        _references("pr_id", "pr_assignees", "pull_requests", "CASCADE"),
        _references("account_id", "pr_assignees", "github_users", "CASCADE"),
    )
    _index("pr_assignees", "account_id")

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
        sa.Column("config_source", sa.Text(), nullable=False),
        sa.Column("config_sha256", sa.Text(), nullable=False),
        sa.Column("graphql_points", sa.Integer(), nullable=False),
        sa.Column("rest_calls", sa.Integer(), nullable=False),
        sa.Column("items_planned", sa.Integer(), nullable=False),
        sa.Column("items_hydrated", sa.Integer(), nullable=False),
        sa.Column("items_deleted", sa.Integer(), nullable=False),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("deletions_approved", sa.Text(), nullable=True),
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
        "automation_accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("github_id", sa.BigInteger(), nullable=False),
        sa.Column("login", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        _references("run_id", "automation_accounts", "sync_runs"),
        sa.UniqueConstraint("github_id", name="uq_automation_accounts_github_id"),
    )
    _index("automation_accounts", "run_id")

    op.create_table(
        "automation_emails",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        _references("run_id", "automation_emails", "sync_runs"),
        sa.UniqueConstraint("email", name="uq_automation_emails_email"),
        sa.CheckConstraint(
            "email = lower(email)", name=op.f("ck_automation_emails_email_lowercase")
        ),
        # The views compare with lower(author_email), which folds only ASCII letters.
        sa.CheckConstraint(
            "email NOT GLOB '*[^ -~]*'", name=op.f("ck_automation_emails_email_ascii")
        ),
    )
    _index("automation_emails", "run_id")

    for _, create in VIEWS:
        op.execute(create)

    op.execute(f"PRAGMA user_version = {USER_VERSION}")


def downgrade() -> None:
    raise RuntimeError(
        "migrations are never downgraded; recover from a database the code cannot upgrade with "
        "a bootstrap (redesign plan 5.4)"
    )
