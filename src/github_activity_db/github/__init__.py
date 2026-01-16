"""GitHub API client module.

This module provides:
- GitHubClient: Async GitHub API client with rate limit tracking
- Rate limit monitoring: RateLimitMonitor, RateLimitStatus, etc.
- Request pacing: RequestPacer, RequestScheduler, RequestPriority
"""

from .client import GitHubClient
from .exceptions import (
    GitHubAuthenticationError,
    GitHubClientError,
    GitHubNotFoundError,
    GitHubRateLimitError,
)
from .pacing import (
    RequestPacer,
    RequestPriority,
    RequestScheduler,
    RequestState,
    wait_with_pacer,
)
from .rate_limit import (
    PoolRateLimit,
    RateLimitMonitor,
    RateLimitPool,
    RateLimitSnapshot,
    RateLimitStatus,
)

__all__ = [
    # Client
    "GitHubClient",
    # Exceptions
    "GitHubAuthenticationError",
    "GitHubClientError",
    "GitHubNotFoundError",
    "GitHubRateLimitError",
    # Rate limit monitoring
    "PoolRateLimit",
    "RateLimitMonitor",
    "RateLimitPool",
    "RateLimitSnapshot",
    "RateLimitStatus",
    # Request pacing
    "RequestPacer",
    "RequestPriority",
    "RequestScheduler",
    "RequestState",
    "wait_with_pacer",
]
