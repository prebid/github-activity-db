"""Pydantic schemas for parsing GitHub API responses.

These schemas map directly to the GitHub REST API response structure.
See: https://docs.github.com/en/rest/pulls/pulls
"""

from datetime import datetime

from pydantic import BaseModel, Field

from .enums import ParticipantActionType
from .nested import CommitBreakdown, ParticipantEntry
from .pr import PRCreate, PRSync


class GitHubUser(BaseModel):
    """GitHub user object from API responses."""

    login: str = Field(description="GitHub username")
    id: int = Field(description="GitHub user ID")
    type: str = Field(default="User", description="User type")


class GitHubLabel(BaseModel):
    """GitHub label object from API responses."""

    id: int = Field(description="Label ID")
    name: str = Field(description="Label name")
    color: str = Field(description="Label color (hex without #)")
    description: str | None = Field(default=None, description="Label description")


class GitHubCommit(BaseModel):
    """GitHub commit object from commits endpoint."""

    sha: str = Field(description="Commit SHA")
    commit: "GitHubCommitDetail" = Field(description="Commit details")


class GitHubCommitDetail(BaseModel):
    """Nested commit detail object."""

    author: "GitHubCommitAuthor" = Field(description="Commit author info")
    message: str = Field(description="Commit message")


class GitHubCommitAuthor(BaseModel):
    """Commit author info (from git, not GitHub user)."""

    name: str = Field(description="Author name")
    email: str = Field(description="Author email")
    date: datetime = Field(description="Commit date (UTC)")


class GitHubFile(BaseModel):
    """GitHub file object from files endpoint."""

    sha: str = Field(description="File blob SHA")
    filename: str = Field(description="File path")
    status: str = Field(description="File status (added, modified, removed)")
    additions: int = Field(default=0, description="Lines added")
    deletions: int = Field(default=0, description="Lines deleted")
    changes: int = Field(default=0, description="Total line changes")


class GitHubReview(BaseModel):
    """GitHub review object from reviews endpoint."""

    id: int = Field(description="Review ID")
    user: GitHubUser = Field(description="Reviewer")
    state: str = Field(description="Review state (APPROVED, CHANGES_REQUESTED, COMMENTED, etc.)")
    submitted_at: datetime | None = Field(default=None, description="When review was submitted")


class GitHubPullRequest(BaseModel):
    """GitHub Pull Request object from API.

    Maps to: GET /repos/{owner}/{repo}/pulls/{number}
    """

    # Basic info
    number: int = Field(description="PR number")
    html_url: str = Field(description="GitHub PR URL")
    state: str = Field(description="PR state (open, closed)")
    title: str = Field(description="PR title")
    body: str | None = Field(default=None, description="PR description")

    # User info
    user: GitHubUser = Field(description="PR author")
    merged_by: GitHubUser | None = Field(default=None, description="Who merged the PR")

    # Dates
    created_at: datetime = Field(description="When PR was created")
    updated_at: datetime = Field(description="Last update timestamp")
    closed_at: datetime | None = Field(default=None, description="When PR was closed")
    merged_at: datetime | None = Field(default=None, description="When PR was merged")

    # Status
    merged: bool = Field(default=False, description="Whether PR was merged")

    # Stats
    commits: int = Field(default=0, description="Number of commits")
    additions: int = Field(default=0, description="Lines added")
    deletions: int = Field(default=0, description="Lines deleted")
    changed_files: int = Field(default=0, description="Number of files changed")

    # Collections
    labels: list[GitHubLabel] = Field(default_factory=list, description="PR labels")
    requested_reviewers: list[GitHubUser] = Field(
        default_factory=list, description="Requested reviewers"
    )
    assignees: list[GitHubUser] = Field(default_factory=list, description="Assignees")

    def to_pr_create(self, repository_id: int) -> PRCreate:
        """
        Factory method to convert to PRCreate schema.

        Args:
            repository_id: ID of the repository this PR belongs to

        Returns:
            PRCreate instance with immutable fields
        """
        return PRCreate(
            number=self.number,
            link=self.html_url,
            open_date=self.created_at,
            submitter=self.user.login,
            repository_id=repository_id,
        )

    def to_pr_sync(
        self,
        files: list[GitHubFile] | None = None,
        commits: list[GitHubCommit] | None = None,
        reviews: list[GitHubReview] | None = None,
    ) -> PRSync:
        """
        Factory method to convert to PRSync schema.

        Args:
            files: Files from the files endpoint (optional)
            commits: Commits from the commits endpoint (optional)
            reviews: Reviews from the reviews endpoint (optional)

        Returns:
            PRSync instance with synced fields
        """
        from github_activity_db.db.models import PRState

        # Determine state
        if self.merged:
            state = PRState.MERGED
        elif self.state == "closed":
            state = PRState.CLOSED
        else:
            state = PRState.OPEN

        # Extract filenames
        filenames = [f.filename for f in (files or [])]

        # Build commits breakdown
        commits_breakdown = []
        for commit in commits or []:
            commits_breakdown.append(
                CommitBreakdown(
                    date=commit.commit.author.date,
                    author=commit.commit.author.name,
                )
            )

        # Build participants from reviews
        participants: list[ParticipantEntry] = []
        participant_map: dict[str, list[ParticipantActionType]] = {}

        for review in reviews or []:
            username = review.user.login
            if username not in participant_map:
                participant_map[username] = []

            # Map review state to action type
            if review.state == "APPROVED":
                participant_map[username].append(ParticipantActionType.APPROVAL)
            elif review.state == "CHANGES_REQUESTED":
                participant_map[username].append(ParticipantActionType.CHANGES_REQUESTED)
            elif review.state == "DISMISSED":
                participant_map[username].append(ParticipantActionType.DISMISSED)
            elif review.state in ("COMMENTED", "PENDING"):
                participant_map[username].append(ParticipantActionType.REVIEW)

        for username, actions in participant_map.items():
            # Deduplicate actions
            unique_actions = list(set(actions))
            participants.append(ParticipantEntry(username=username, actions=unique_actions))

        return PRSync(
            title=self.title,
            description=self.body,
            last_update_date=self.updated_at,
            state=state,
            files_changed=self.changed_files,
            lines_added=self.additions,
            lines_deleted=self.deletions,
            commits_count=self.commits,
            github_labels=[label.name for label in self.labels],
            filenames=filenames,
            reviewers=[r.login for r in self.requested_reviewers],
            assignees=[a.login for a in self.assignees],
            commits_breakdown=commits_breakdown,
            participants=participants,
        )


# Update forward references
GitHubCommit.model_rebuild()
