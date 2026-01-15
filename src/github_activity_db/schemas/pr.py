"""Pydantic schemas for PullRequest model."""

from datetime import datetime
from typing import Any

from pydantic import Field, HttpUrl, field_validator

from github_activity_db.db.models import PRState

from .base import SchemaBase
from .nested import CommitBreakdown, ParticipantEntry, participants_from_dict


class PRCreate(SchemaBase):
    """Schema for immutable fields set when PR is first created."""

    number: int = Field(gt=0, description="PR number")
    link: str = Field(max_length=500, description="GitHub PR URL")
    open_date: datetime = Field(description="When the PR was opened (UTC)")
    submitter: str = Field(max_length=100, description="PR author's GitHub username")
    repository_id: int = Field(description="Foreign key to repository")

    @field_validator("link")
    @classmethod
    def validate_github_url(cls, v: str) -> str:
        """Validate that link is a valid GitHub PR URL."""
        # Basic validation - must be a valid URL structure
        HttpUrl(v)
        return v


class PRSync(SchemaBase):
    """Schema for fields that are updated on each sync while PR is open."""

    title: str = Field(max_length=500, description="PR title")
    description: str | None = Field(default=None, description="PR body/description")
    last_update_date: datetime = Field(description="Last update timestamp from GitHub (UTC)")
    state: PRState = Field(default=PRState.OPEN, description="PR state")

    # Numeric stats
    files_changed: int = Field(default=0, ge=0, description="Number of files changed")
    lines_added: int = Field(default=0, ge=0, description="Lines added")
    lines_deleted: int = Field(default=0, ge=0, description="Lines deleted")
    commits_count: int = Field(default=0, ge=0, description="Number of commits")

    # List fields
    github_labels: list[str] = Field(default_factory=list, description="GitHub labels")
    filenames: list[str] = Field(default_factory=list, description="Changed file paths")
    reviewers: list[str] = Field(default_factory=list, description="Requested reviewers")
    assignees: list[str] = Field(default_factory=list, description="Assignees")

    # Complex fields
    commits_breakdown: list[CommitBreakdown] = Field(
        default_factory=list,
        description="Commit history with date and author",
    )
    participants: list[ParticipantEntry] = Field(
        default_factory=list,
        description="Participants and their actions",
    )

    # Agent-generated
    classify_tags: str | None = Field(
        default=None, max_length=500, description="AI-generated classification tags"
    )

    @field_validator("commits_breakdown", mode="before")
    @classmethod
    def parse_commits_breakdown(cls, v: Any) -> list[CommitBreakdown] | Any:
        """Convert raw dict format to CommitBreakdown objects."""
        if not v:
            return []
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return [CommitBreakdown(**item) for item in v]
        return v

    @field_validator("participants", mode="before")
    @classmethod
    def parse_participants(cls, v: Any) -> list[ParticipantEntry] | Any:
        """Convert raw dict format to ParticipantEntry objects."""
        if not v:
            return []
        if isinstance(v, dict):
            return participants_from_dict(v)
        return v


class PRMerge(SchemaBase):
    """Schema for fields set when PR is merged or closed."""

    close_date: datetime = Field(description="When the PR was merged/closed (UTC)")
    merged_by: str | None = Field(
        default=None, max_length=100, description="Who merged the PR (None if closed without merge)"
    )
    ai_summary: str | None = Field(default=None, description="AI-generated summary")


class PRRead(SchemaBase):
    """Full schema for reading PR data with all fields."""

    # Primary key and foreign key
    id: int
    repository_id: int

    # Immutable fields
    number: int
    link: str
    open_date: datetime
    submitter: str

    # Synced fields
    title: str
    description: str | None
    last_update_date: datetime
    state: PRState

    # Numeric stats
    files_changed: int
    lines_added: int
    lines_deleted: int
    commits_count: int

    # List fields (kept as raw types for simplicity in output)
    github_labels: list[str]
    filenames: list[str]
    reviewers: list[str]
    assignees: list[str]

    # Complex fields (raw format from DB)
    commits_breakdown: list[dict[str, str]]
    participants: dict[str, list[str]]

    # Agent-generated
    classify_tags: str | None

    # Merge-only fields
    close_date: datetime | None
    merged_by: str | None
    ai_summary: str | None

    # Metadata
    created_at: datetime
    updated_at: datetime

    @property
    def is_open(self) -> bool:
        """Check if PR is still open."""
        return self.state == PRState.OPEN

    @property
    def is_merged(self) -> bool:
        """Check if PR was merged."""
        return self.state == PRState.MERGED

    def get_commits_breakdown_typed(self) -> list[CommitBreakdown]:
        """Get commits_breakdown as typed CommitBreakdown objects."""
        result = []
        for item in self.commits_breakdown:
            date_str = item.get("date", "")
            # Parse ISO format datetime string
            date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            result.append(CommitBreakdown(date=date, author=item.get("author", "")))
        return result

    def get_participants_typed(self) -> list[ParticipantEntry]:
        """Get participants as typed ParticipantEntry objects."""
        return participants_from_dict(self.participants)
