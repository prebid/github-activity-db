"""GitHub client exceptions."""

from datetime import datetime


class GitHubClientError(Exception):
    """Base exception for GitHub client errors."""

    pass


class GitHubAuthenticationError(GitHubClientError):
    """Raised when authentication fails (401)."""

    pass


class GitHubRateLimitError(GitHubClientError):
    """Raised when rate limit is exceeded (403 with rate limit headers)."""

    def __init__(self, message: str, reset_at: datetime | None = None) -> None:
        super().__init__(message)
        self.reset_at = reset_at


class GitHubNotFoundError(GitHubClientError):
    """Raised when a resource is not found (404)."""

    pass
