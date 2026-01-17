"""Result objects for sync operations.

Structured results provide consistent interfaces for monitoring,
error handling, and CLI output.
"""

from dataclasses import dataclass

from github_activity_db.db.models import PullRequest


@dataclass
class PRIngestionResult:
    """Result of a single PR ingestion operation.

    Captures the outcome of fetching and storing a PR, including
    what action was taken and any errors that occurred.
    """

    pr: PullRequest | None
    """The PR object (None if error prevented creation)."""

    created: bool = False
    """True if a new PR was created."""

    updated: bool = False
    """True if an existing PR was updated."""

    skipped_frozen: bool = False
    """True if PR was skipped because it's frozen (merged past grace period)."""

    skipped_unchanged: bool = False
    """True if PR was skipped because no changes detected."""

    skipped_abandoned: bool = False
    """True if PR was skipped because it's abandoned (closed but not merged)."""

    error: Exception | None = None
    """Exception if operation failed."""

    @property
    def success(self) -> bool:
        """Check if operation completed without errors."""
        return self.error is None

    @property
    def action(self) -> str:
        """Get human-readable description of action taken.

        Returns one of:
            - "error": Operation failed
            - "created": New PR was created
            - "updated": Existing PR was updated
            - "skipped (frozen)": PR is frozen, no changes made
            - "skipped (unchanged)": PR data unchanged, no update needed
            - "unknown": Unexpected state
        """
        if self.error:
            return "error"
        if self.created:
            return "created"
        if self.updated:
            return "updated"
        if self.skipped_frozen:
            return "skipped (frozen)"
        if self.skipped_unchanged:
            return "skipped (unchanged)"
        if self.skipped_abandoned:
            return "skipped (abandoned)"
        return "unknown"

    def to_dict(self) -> dict[str, object]:
        """Convert to dictionary for JSON serialization.

        Returns:
            Dict with all result data
        """
        result: dict[str, object] = {
            "success": self.success,
            "action": self.action,
            "created": self.created,
            "updated": self.updated,
            "skipped_frozen": self.skipped_frozen,
            "skipped_unchanged": self.skipped_unchanged,
            "skipped_abandoned": self.skipped_abandoned,
        }

        if self.pr:
            result["pr_id"] = self.pr.id
            result["pr_number"] = self.pr.number
            result["title"] = self.pr.title
            result["state"] = self.pr.state.value

        if self.error:
            result["error"] = str(self.error)
            result["error_type"] = type(self.error).__name__

        return result

    @classmethod
    def from_error(cls, error: Exception) -> "PRIngestionResult":
        """Create a result representing a failed operation.

        Args:
            error: The exception that caused the failure

        Returns:
            PRIngestionResult with error set
        """
        return cls(pr=None, error=error)

    @classmethod
    def from_created(cls, pr: PullRequest) -> "PRIngestionResult":
        """Create a result for a newly created PR.

        Args:
            pr: The created PR

        Returns:
            PRIngestionResult with created=True
        """
        return cls(pr=pr, created=True)

    @classmethod
    def from_updated(cls, pr: PullRequest) -> "PRIngestionResult":
        """Create a result for an updated PR.

        Args:
            pr: The updated PR

        Returns:
            PRIngestionResult with updated=True
        """
        return cls(pr=pr, updated=True)

    @classmethod
    def from_skipped_frozen(cls, pr: PullRequest) -> "PRIngestionResult":
        """Create a result for a skipped frozen PR.

        Args:
            pr: The frozen PR

        Returns:
            PRIngestionResult with skipped_frozen=True
        """
        return cls(pr=pr, skipped_frozen=True)

    @classmethod
    def from_skipped_unchanged(cls, pr: PullRequest) -> "PRIngestionResult":
        """Create a result for a skipped unchanged PR.

        Args:
            pr: The unchanged PR

        Returns:
            PRIngestionResult with skipped_unchanged=True
        """
        return cls(pr=pr, skipped_unchanged=True)

    @classmethod
    def from_skipped_abandoned(cls, pr: PullRequest | None = None) -> "PRIngestionResult":
        """Create a result for a skipped abandoned PR.

        Abandoned PRs are closed without being merged - we don't track them.

        Args:
            pr: The existing PR if any (may be None for newly discovered abandoned PRs)

        Returns:
            PRIngestionResult with skipped_abandoned=True
        """
        return cls(pr=pr, skipped_abandoned=True)
