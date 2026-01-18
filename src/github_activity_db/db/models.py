"""SQLAlchemy ORM models for GitHub Activity DB."""

from datetime import datetime
from enum import Enum

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)
from sqlalchemy.types import JSON


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy models."""

    pass


class PRState(str, Enum):
    """Pull request state enum."""

    OPEN = "open"
    MERGED = "merged"
    CLOSED = "closed"  # closed without merge


class SyncFailureStatus(str, Enum):
    """Status of a sync failure for retry tracking."""

    PENDING = "pending"  # Waiting for retry
    RESOLVED = "resolved"  # Successfully retried
    PERMANENT = "permanent"  # Max retries exceeded or non-retryable error


# ------------------------------------------------------------------------------
# Junction table for many-to-many: PullRequest <-> UserTag
# ------------------------------------------------------------------------------
pr_user_tags = Table(
    "pr_user_tags",
    Base.metadata,
    Column("pr_id", ForeignKey("pull_requests.id", ondelete="CASCADE"), primary_key=True),
    Column("user_tag_id", ForeignKey("user_tags.id", ondelete="CASCADE"), primary_key=True),
    Column("created_at", DateTime, default=func.now()),
)


# ------------------------------------------------------------------------------
# Repository model
# ------------------------------------------------------------------------------
class Repository(Base):
    """Tracked GitHub repository."""

    __tablename__ = "repositories"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner: Mapped[str] = mapped_column(String(100))  # e.g., "prebid"
    name: Mapped[str] = mapped_column(String(100))  # e.g., "prebid-server"
    full_name: Mapped[str] = mapped_column(String(200), unique=True)  # "prebid/prebid-server"
    is_active: Mapped[bool] = mapped_column(default=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    # Relationships
    pull_requests: Mapped[list["PullRequest"]] = relationship(
        back_populates="repository",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Repository(id={self.id}, full_name='{self.full_name}')>"


# ------------------------------------------------------------------------------
# PullRequest model
# ------------------------------------------------------------------------------
class PullRequest(Base):
    """GitHub Pull Request with all tracked fields."""

    __tablename__ = "pull_requests"

    # Primary key
    id: Mapped[int] = mapped_column(primary_key=True)

    # Foreign key to repository
    repository_id: Mapped[int] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))

    # --------------------------------------------------------------------------
    # Immutable fields (set once on creation)
    # --------------------------------------------------------------------------
    number: Mapped[int] = mapped_column()
    link: Mapped[str] = mapped_column(String(500))
    open_date: Mapped[datetime] = mapped_column(DateTime)
    submitter: Mapped[str] = mapped_column(String(100))

    # --------------------------------------------------------------------------
    # Synced fields (updated until merged)
    # --------------------------------------------------------------------------
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_update_date: Mapped[datetime] = mapped_column(DateTime)
    state: Mapped[PRState] = mapped_column(default=PRState.OPEN)

    # Numeric stats
    files_changed: Mapped[int] = mapped_column(default=0)
    lines_added: Mapped[int] = mapped_column(default=0)
    lines_deleted: Mapped[int] = mapped_column(default=0)
    commits_count: Mapped[int] = mapped_column(default=0)

    # JSON columns for complex data
    github_labels: Mapped[list[str]] = mapped_column(JSON, default=list)
    filenames: Mapped[list[str]] = mapped_column(JSON, default=list)
    commits_breakdown: Mapped[list[dict[str, str]]] = mapped_column(
        JSON, default=list
    )  # [{date, author}]
    reviewers: Mapped[list[str]] = mapped_column(JSON, default=list)
    assignees: Mapped[list[str]] = mapped_column(JSON, default=list)
    participants: Mapped[dict[str, list[str]]] = mapped_column(
        JSON, default=dict
    )  # {user: [actions]}

    # --------------------------------------------------------------------------
    # Agent-generated fields
    # --------------------------------------------------------------------------
    classify_tags: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # --------------------------------------------------------------------------
    # Merge-only fields (null until merged)
    # --------------------------------------------------------------------------
    close_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    merged_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    ai_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --------------------------------------------------------------------------
    # Metadata
    # --------------------------------------------------------------------------
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())

    # --------------------------------------------------------------------------
    # Relationships
    # --------------------------------------------------------------------------
    repository: Mapped["Repository"] = relationship(back_populates="pull_requests")
    user_tags: Mapped[list["UserTag"]] = relationship(
        secondary=pr_user_tags,
        back_populates="pull_requests",
    )

    # Unique constraint: one PR number per repo
    __table_args__ = (UniqueConstraint("repository_id", "number", name="uq_repo_pr_number"),)

    def __repr__(self) -> str:
        return f"<PullRequest(id={self.id}, repo='{self.repository_id}', number={self.number})>"

    @property
    def is_open(self) -> bool:
        """Check if PR is still open."""
        return self.state == PRState.OPEN

    @property
    def is_merged(self) -> bool:
        """Check if PR was merged."""
        return self.state == PRState.MERGED


# ------------------------------------------------------------------------------
# UserTag model
# ------------------------------------------------------------------------------
class UserTag(Base):
    """User-created tags for PRs (applied via CLI)."""

    __tablename__ = "user_tags"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    color: Mapped[str | None] = mapped_column(String(7), nullable=True)  # hex color e.g., "#ff0000"
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    # Relationships
    pull_requests: Mapped[list["PullRequest"]] = relationship(
        secondary=pr_user_tags,
        back_populates="user_tags",
    )

    def __repr__(self) -> str:
        return f"<UserTag(id={self.id}, name='{self.name}')>"


# ------------------------------------------------------------------------------
# SyncFailure model
# ------------------------------------------------------------------------------
class SyncFailure(Base):
    """Track failed PR ingestion attempts for retry.

    Records failures during sync operations, enabling:
    - Manual retry via `ghactivity sync retry`
    - Automatic retry on subsequent syncs
    - Failure analysis and metrics
    """

    __tablename__ = "sync_failures"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Foreign key to repository
    repository_id: Mapped[int] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))

    # PR that failed (set for PR-level failures)
    pr_number: Mapped[int] = mapped_column()

    # Error details
    error_message: Mapped[str] = mapped_column(Text)
    error_type: Mapped[str] = mapped_column(String(100))  # e.g., "GitHubAPIError", "ValueError"

    # Retry tracking
    retry_count: Mapped[int] = mapped_column(default=0)
    status: Mapped[SyncFailureStatus] = mapped_column(default=SyncFailureStatus.PENDING)

    # Timestamps
    failed_at: Mapped[datetime] = mapped_column(DateTime)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    # Relationships
    repository: Mapped["Repository"] = relationship()

    # Unique constraint: only one pending failure per repo+PR
    # This allows multiple resolved/permanent records for history
    __table_args__ = (
        UniqueConstraint("repository_id", "pr_number", "status", name="uq_repo_pr_pending_status"),
    )

    def __repr__(self) -> str:
        return (
            f"<SyncFailure(id={self.id}, repo_id={self.repository_id}, "
            f"pr={self.pr_number}, status={self.status.value})>"
        )
