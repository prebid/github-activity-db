"""GitHub API client module."""

from .client import GitHubClient
from .exceptions import (
    GitHubAuthenticationError,
    GitHubClientError,
    GitHubNotFoundError,
    GitHubRateLimitError,
)

__all__ = [
    "GitHubAuthenticationError",
    "GitHubClient",
    "GitHubClientError",
    "GitHubNotFoundError",
    "GitHubRateLimitError",
]
