"""ORM models: the head schema of redesign plan 5.1, except ``issues`` and ``issue_labels``.

Every table, column, and constraint here must match the newest migration; the views are
created by migrations only (``alembic/versions``), never declared here, and the data dictionary
(``docs/data-dictionary.md``) says which columns consumers may rely on.

Names: indexes, CHECK constraints, and foreign keys are named by ``NAMING_CONVENTION``, which
alembic also applies to every migration's operations, so it must not change. Unique constraints
carry explicit ``uq_`` names and stay out of the convention, which would otherwise report the
unnamed ``UNIQUE`` of the kept ``user_tags`` table to ``alembic check``.

References to ``repositories`` and ``pull_requests`` cascade on delete; references to
``github_users`` are set null, so removing an account row never removes what it authored, except
those in a primary key (``pr_assignees.account_id``, ``account_profiles.account_id``), which
cascade. References to ``sync_runs`` take SQLite's default (no action).

Columns holding what GitHub reports keep GitHub's names in snake case and its nullability; none
of them is computed here (D29).
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from github_activity_db.db.types import UtcDateTime, utc_now

NAMING_CONVENTION: Final = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
}

ITEM_KINDS: Final = ("pull_request", "issue")
ACCOUNT_TYPES: Final = ("User", "Bot", "Organization", "Mannequin", "EnterpriseUserAccount")


def _one_of(column: str, values: tuple[str, ...]) -> str:
    """CHECK text restricting ``column`` to ``values``."""
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Repository(Base):
    """A resolved repository, with every field the resolve reads (redesign plan 5.8)."""

    __tablename__ = "repositories"
    __table_args__ = (
        UniqueConstraint("github_id", name="uq_repositories_github_id"),
        UniqueConstraint("node_id", name="uq_repositories_node_id"),
        Index(None, "full_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    github_id: Mapped[int] = mapped_column(BigInteger)
    node_id: Mapped[str] = mapped_column(Text)
    owner: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    full_name: Mapped[str] = mapped_column(Text)
    repo_group: Mapped[str] = mapped_column(Text)
    is_private: Mapped[bool]
    is_archived: Mapped[bool]
    default_branch: Mapped[str] = mapped_column(Text)
    labels_total: Mapped[int]
    is_tracked: Mapped[bool]
    resolved_at: Mapped[datetime] = mapped_column(UtcDateTime)
    is_security_policy_enabled: Mapped[bool | None]
    visibility: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    homepage_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime)
    pushed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    archived_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    stargazer_count: Mapped[int]
    fork_count: Mapped[int]
    disk_usage: Mapped[int | None]
    forking_allowed: Mapped[bool]
    allow_update_branch: Mapped[bool]
    web_commit_signoff_required: Mapped[bool]
    has_wiki_enabled: Mapped[bool]
    has_discussions_enabled: Mapped[bool]
    has_projects_enabled: Mapped[bool]
    has_vulnerability_alerts_enabled: Mapped[bool]
    uses_custom_open_graph_image: Mapped[bool]
    merge_commit_allowed: Mapped[bool]
    squash_merge_allowed: Mapped[bool]
    rebase_merge_allowed: Mapped[bool]
    delete_branch_on_merge: Mapped[bool]
    merge_commit_title: Mapped[str] = mapped_column(Text)
    merge_commit_message: Mapped[str] = mapped_column(Text)
    squash_merge_commit_title: Mapped[str] = mapped_column(Text)
    squash_merge_commit_message: Mapped[str] = mapped_column(Text)
    primary_language: Mapped[str | None] = mapped_column(Text)
    license_spdx_id: Mapped[str | None] = mapped_column(Text)
    plan_features_codeowners: Mapped[bool]
    plan_features_team_review_requests: Mapped[bool]
    plan_features_maximum_manual_review_requests: Mapped[int]


class RepositoryLabel(Base):
    """One label of a repository's label set, as the latest complete resolve read it (D8)."""

    __tablename__ = "repo_labels"

    repository_id: Mapped[int] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), primary_key=True
    )
    node_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    color: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    is_default: Mapped[bool]
    created_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    updated_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class GitHubUser(Base):
    """An account; an ``EnterpriseUserAccount``, which has no ``databaseId``, has a null
    ``github_id`` and is identified by its node id (gap 85)."""

    __tablename__ = "github_users"
    __table_args__ = (
        UniqueConstraint("github_id", name="uq_github_users_github_id"),
        UniqueConstraint("node_id", name="uq_github_users_node_id"),
        CheckConstraint(_one_of("type", ACCOUNT_TYPES), name="type"),
        CheckConstraint(
            "(github_id IS NULL) = (type = 'EnterpriseUserAccount')",
            name="github_id_null_iff_enterprise_account",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    github_id: Mapped[int | None] = mapped_column(BigInteger)
    node_id: Mapped[str] = mapped_column(Text)
    login: Mapped[str] = mapped_column(Text)
    type: Mapped[str] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    updated_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    linked_user_github_id: Mapped[int | None] = mapped_column(BigInteger)
    claimant_github_id: Mapped[int | None] = mapped_column(BigInteger)
    refreshed_at: Mapped[datetime] = mapped_column(UtcDateTime)
    not_found_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class AccountProfile(Base):
    """A ``User``'s or an ``Organization``'s public profile as the latest lookup returned it
    (D31); a lookup that returns ``NOT_FOUND`` leaves the row unchanged."""

    __tablename__ = "account_profiles"

    account_id: Mapped[int] = mapped_column(
        ForeignKey("github_users.id", ondelete="CASCADE"), primary_key=True
    )
    company: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    website_url: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    twitter_username: Mapped[str | None] = mapped_column(Text)
    is_verified: Mapped[bool | None]
    archived_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    has_sponsors_listing: Mapped[bool | None]
    pinned_items_remaining: Mapped[int | None]
    refreshed_at: Mapped[datetime] = mapped_column(UtcDateTime)


def _account_reference(ondelete: str = "SET NULL") -> ForeignKey:
    return ForeignKey("github_users.id", ondelete=ondelete)


class PullRequest(Base):
    __tablename__ = "pull_requests"
    __table_args__ = (
        UniqueConstraint("repository_id", "number", name="uq_pull_requests_repository_id_number"),
        UniqueConstraint("github_id", name="uq_pull_requests_github_id"),
        CheckConstraint(_one_of("state", ("OPEN", "CLOSED", "MERGED")), name="state"),
        CheckConstraint(
            "(state = 'MERGED') = (merged_at IS NOT NULL)", name="merged_at_iff_merged"
        ),
        Index(None, "repository_id", "state"),
        Index(None, "repository_id", "merged_at"),
        Index(None, "author_id"),
        Index(None, "merged_by_id"),
        Index(None, "editor_id"),
        # D30 joins a default-branch merge event to its pull request by the merge commit.
        Index(None, "merge_commit_oid"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    repository_id: Mapped[int] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    number: Mapped[int]
    github_id: Mapped[int] = mapped_column(BigInteger)
    node_id: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text)
    is_draft: Mapped[bool]
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    author_id: Mapped[int | None] = mapped_column(_account_reference())
    author_reported: Mapped[bool]
    author_association: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    merged_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    closed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    published_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    last_edited_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    editor_id: Mapped[int | None] = mapped_column(_account_reference())
    includes_created_edit: Mapped[bool]
    maintainer_can_modify: Mapped[bool]
    merged_by_id: Mapped[int | None] = mapped_column(_account_reference())
    merged_by_reported: Mapped[bool]
    base_ref: Mapped[str] = mapped_column(Text)
    base_ref_oid: Mapped[str] = mapped_column(Text)
    head_ref_name: Mapped[str] = mapped_column(Text)
    head_ref_oid: Mapped[str] = mapped_column(Text)
    head_repo_github_id: Mapped[int | None] = mapped_column(BigInteger)
    head_repo_full_name: Mapped[str | None] = mapped_column(Text)
    head_repo_owner_node_id: Mapped[str | None] = mapped_column(Text)
    head_repo_owner_login: Mapped[str | None] = mapped_column(Text)
    merge_commit_oid: Mapped[str | None] = mapped_column(Text)
    targets_default_branch: Mapped[bool]
    is_cross_repository: Mapped[bool]
    milestone_number: Mapped[int | None]
    milestone_title: Mapped[str | None] = mapped_column(Text)
    review_decision: Mapped[str | None] = mapped_column(Text)
    mergeable: Mapped[str] = mapped_column(Text)
    merge_state_status: Mapped[str] = mapped_column(Text)
    can_be_rebased: Mapped[bool]
    potential_merge_commit_oid: Mapped[str | None] = mapped_column(Text)
    stack_node_id: Mapped[str | None] = mapped_column(Text)
    stack_number: Mapped[int | None]
    stack_size: Mapped[int | None]
    stack_base_ref_name: Mapped[str | None] = mapped_column(Text)
    stack_entry_node_id: Mapped[str | None] = mapped_column(Text)
    stack_position: Mapped[int | None]
    additions: Mapped[int]
    deletions: Mapped[int]
    changed_files: Mapped[int]
    total_comments_count: Mapped[int | None]
    commits_total: Mapped[int]
    files_total: Mapped[int]
    reviews_total: Mapped[int]
    comments_total: Mapped[int]
    labels_total: Mapped[int]
    closing_issues_total: Mapped[int]
    user_linked_closing_issues_total: Mapped[int]
    review_requests_total: Mapped[int]
    assignees_total: Mapped[int]
    github_updated_at: Mapped[datetime] = mapped_column(UtcDateTime)
    hydrated_at: Mapped[datetime] = mapped_column(UtcDateTime)
    hydration_version: Mapped[int]


class PullRequestLabel(Base):
    __tablename__ = "pr_labels"

    pr_id: Mapped[int] = mapped_column(
        ForeignKey("pull_requests.id", ondelete="CASCADE"), primary_key=True
    )
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    label_node_id: Mapped[str] = mapped_column(Text)


class PullRequestReview(Base):
    """A submitted review, never its body (D24)."""

    __tablename__ = "pr_reviews"
    __table_args__ = (
        UniqueConstraint("github_id", name="uq_pr_reviews_github_id"),
        CheckConstraint(
            _one_of("state", ("APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED")),
            name="state",
        ),
        # v_pr_comments looks up one account's reviews of one pull request.
        Index(None, "pr_id", "reviewer_id", "github_id"),
        Index(None, "reviewer_id"),
        Index(None, "editor_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    github_id: Mapped[int] = mapped_column(BigInteger)
    node_id: Mapped[str] = mapped_column(Text)
    pr_id: Mapped[int] = mapped_column(ForeignKey("pull_requests.id", ondelete="CASCADE"))
    reviewer_id: Mapped[int | None] = mapped_column(_account_reference())
    reviewer_reported: Mapped[bool]
    state: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    submitted_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    published_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime)
    last_edited_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    editor_id: Mapped[int | None] = mapped_column(_account_reference())
    includes_created_edit: Mapped[bool]
    commit_oid: Mapped[str | None] = mapped_column(Text)
    author_association: Mapped[str] = mapped_column(Text)
    author_can_push_to_repository: Mapped[bool]
    inline_comments_total: Mapped[int]


class PullRequestComment(Base):
    """A conversation comment: its ids, author, and times, never its text (D24)."""

    __tablename__ = "pr_comments"
    __table_args__ = (
        UniqueConstraint("github_id", name="uq_pr_comments_github_id"),
        Index(None, "pr_id", "author_id"),
        Index(None, "author_id"),
        Index(None, "editor_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    github_id: Mapped[int] = mapped_column(BigInteger)
    node_id: Mapped[str] = mapped_column(Text)
    pr_id: Mapped[int] = mapped_column(ForeignKey("pull_requests.id", ondelete="CASCADE"))
    author_id: Mapped[int | None] = mapped_column(_account_reference())
    author_reported: Mapped[bool]
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    published_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime)
    last_edited_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    editor_id: Mapped[int | None] = mapped_column(_account_reference())
    created_via_email: Mapped[bool]
    includes_created_edit: Mapped[bool]
    author_association: Mapped[str] = mapped_column(Text)


class PullRequestCommit(Base):
    """A commit like a comment: its author link, never its message (D19)."""

    __tablename__ = "pr_commits"
    __table_args__ = (Index(None, "author_id"),)

    pr_id: Mapped[int] = mapped_column(
        ForeignKey("pull_requests.id", ondelete="CASCADE"), primary_key=True
    )
    sha: Mapped[str] = mapped_column(Text, primary_key=True)
    author_id: Mapped[int | None] = mapped_column(_account_reference())
    linked_github_id: Mapped[int | None] = mapped_column(BigInteger)
    author_name: Mapped[str | None] = mapped_column(Text)
    author_email: Mapped[str | None] = mapped_column(Text)
    authored_at: Mapped[datetime] = mapped_column(UtcDateTime)


class PullRequestFile(Base):
    """One entry of the file list, keyed by position: a path can appear twice."""

    __tablename__ = "pr_files"

    pr_id: Mapped[int] = mapped_column(
        ForeignKey("pull_requests.id", ondelete="CASCADE"), primary_key=True
    )
    ordinal: Mapped[int] = mapped_column(primary_key=True)
    path: Mapped[str] = mapped_column(Text)
    change_type: Mapped[str] = mapped_column(Text)
    additions: Mapped[int]
    deletions: Mapped[int]


class PullRequestClosingIssue(Base):
    """An issue in the pull request's ``closingIssuesReferences``, which GitHub describes as
    "List of issues that may be closed by this pull request"; possibly in a repository the
    database does not track."""

    __tablename__ = "pr_closing_issues"
    __table_args__ = (Index(None, "issue_repo_github_id", "issue_number"),)

    pr_id: Mapped[int] = mapped_column(
        ForeignKey("pull_requests.id", ondelete="CASCADE"), primary_key=True
    )
    issue_repo_github_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    issue_repo_full_name: Mapped[str] = mapped_column(Text)
    issue_number: Mapped[int] = mapped_column(primary_key=True)
    issue_github_id: Mapped[int] = mapped_column(BigInteger)
    user_linked: Mapped[bool]


class PullRequestReviewRequest(Base):
    """One entry of GitHub's ``reviewRequests``, by position (D4)."""

    __tablename__ = "pr_review_requests"
    __table_args__ = (Index(None, "account_id"),)

    pr_id: Mapped[int] = mapped_column(
        ForeignKey("pull_requests.id", ondelete="CASCADE"), primary_key=True
    )
    ordinal: Mapped[int] = mapped_column(primary_key=True)
    request_github_id: Mapped[int | None] = mapped_column(BigInteger)
    request_node_id: Mapped[str] = mapped_column(Text)
    requested_type: Mapped[str | None] = mapped_column(Text)
    requested_github_id: Mapped[int | None] = mapped_column(BigInteger)
    requested_node_id: Mapped[str | None] = mapped_column(Text)
    account_id: Mapped[int | None] = mapped_column(_account_reference())
    team_slug: Mapped[str | None] = mapped_column(Text)
    as_code_owner: Mapped[bool]


class PullRequestAssignee(Base):
    """One actor of GitHub's ``assignedActors`` (D14)."""

    __tablename__ = "pr_assignees"
    __table_args__ = (Index(None, "account_id"),)

    pr_id: Mapped[int] = mapped_column(
        ForeignKey("pull_requests.id", ondelete="CASCADE"), primary_key=True
    )
    account_id: Mapped[int] = mapped_column(_account_reference("CASCADE"), primary_key=True)


class AutomationAccount(Base):
    """A ``[automation].accounts`` entry of tracking.toml, replaced at the start of every run."""

    __tablename__ = "automation_accounts"
    __table_args__ = (
        UniqueConstraint("github_id", name="uq_automation_accounts_github_id"),
        Index(None, "run_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    github_id: Mapped[int] = mapped_column(BigInteger)
    login: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    run_id: Mapped[int] = mapped_column(ForeignKey("sync_runs.id"))


class AutomationEmail(Base):
    """A ``[automation].emails`` entry of tracking.toml, stored lowercase.

    ASCII only: the views compare it with ``lower(author_email)``, and SQLite's ``lower()``
    folds only ASCII letters.
    """

    __tablename__ = "automation_emails"
    __table_args__ = (
        UniqueConstraint("email", name="uq_automation_emails_email"),
        CheckConstraint("email = lower(email)", name="email_lowercase"),
        CheckConstraint("email NOT GLOB '*[^ -~]*'", name="email_ascii"),
        Index(None, "run_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    run_id: Mapped[int] = mapped_column(ForeignKey("sync_runs.id"))


class SyncRun(Base):
    __tablename__ = "sync_runs"
    __table_args__ = (
        CheckConstraint(
            _one_of("status", ("running", "complete", "time_limited", "failed", "aborted")),
            name="status",
        ),
        CheckConstraint(
            _one_of('"trigger"', ("schedule", "workflow_dispatch", "manual")), name="trigger"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime)
    deadline_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    status: Mapped[str] = mapped_column(Text)
    exit_code: Mapped[int | None]
    trigger: Mapped[str] = mapped_column(Text)
    code_version: Mapped[str] = mapped_column(Text)
    config_source: Mapped[str] = mapped_column(Text)
    config_sha256: Mapped[str] = mapped_column(Text)
    graphql_points: Mapped[int]
    rest_calls: Mapped[int]
    items_planned: Mapped[int]
    items_hydrated: Mapped[int]
    items_deleted: Mapped[int]
    failures: Mapped[int]
    # JSON text: the approved deletions, per guarded set, and the pruned repositories.
    deletions_approved: Mapped[str | None] = mapped_column(Text)


class RepoSyncState(Base):
    __tablename__ = "repo_sync_state"
    __table_args__ = (
        CheckConstraint(
            _one_of(
                "last_outcome",
                ("complete", "time_limited", "failed", "incomplete_index", "halted", "untracked"),
            ),
            name="last_outcome",
        ),
        Index(None, "last_run_id"),
    )

    repository_id: Mapped[int] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), primary_key=True
    )
    last_index_started_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    index_pr_total: Mapped[int | None]
    index_issue_total: Mapped[int | None]
    last_success_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    last_run_id: Mapped[int] = mapped_column(ForeignKey("sync_runs.id"))
    last_outcome: Mapped[str] = mapped_column(Text)
    halt_reason: Mapped[str | None] = mapped_column(Text)


class SyncPending(Base):
    """An item a time-limited run planned but did not reach."""

    __tablename__ = "sync_pending"
    __table_args__ = (
        CheckConstraint(_one_of("item_kind", ITEM_KINDS), name="item_kind"),
        Index(None, "run_id"),
    )

    repository_id: Mapped[int] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), primary_key=True
    )
    item_kind: Mapped[str] = mapped_column(Text, primary_key=True)
    number: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("sync_runs.id"))


class SyncFailure(Base):
    __tablename__ = "sync_failures"
    __table_args__ = (
        UniqueConstraint(
            "repository_id",
            "item_kind",
            "number",
            name="uq_sync_failures_repository_id_item_kind_number",
        ),
        CheckConstraint(_one_of("item_kind", ITEM_KINDS), name="item_kind"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    repository_id: Mapped[int] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    item_kind: Mapped[str] = mapped_column(Text)
    number: Mapped[int]
    attempts: Mapped[int]
    last_error: Mapped[str] = mapped_column(Text)
    first_failed_at: Mapped[datetime] = mapped_column(UtcDateTime)
    last_failed_at: Mapped[datetime] = mapped_column(UtcDateTime)


class UserTag(Base):
    """Kept from the first schema with its nullability (D17); no producer writes it yet."""

    __tablename__ = "user_tags"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str | None] = mapped_column(String(500))
    color: Mapped[str | None] = mapped_column(String(7))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)


class PullRequestUserTag(Base):
    """Recreated empty by the Step 3 migration, pointing at the new ``pull_requests`` (D17)."""

    __tablename__ = "pr_user_tags"

    pr_id: Mapped[int] = mapped_column(
        ForeignKey("pull_requests.id", ondelete="CASCADE"), primary_key=True
    )
    user_tag_id: Mapped[int] = mapped_column(
        ForeignKey("user_tags.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=utc_now)
