"""GitHub API client module.

This module provides:
- GitHubClient: Async GitHub API client with rate limit tracking
- Rate limit monitoring: RateLimitMonitor, RateLimitStatus, etc.
- Request pacing: RequestPacer, RequestScheduler, RequestPriority
- PR Sync: PRIngestionService, PRIngestionResult
- Bulk PR Sync: BulkPRIngestionService, BulkIngestionConfig, BulkIngestionResult
"""

from .client import GitHubClient
from .exceptions import (
    GitHubAuthenticationError,
    GitHubClientError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubRetryableError,
)
from .pacing import (
    AsyncTokenBucket,
    RequestPacer,
    RequestPriority,
    RequestScheduler,
    RequestState,
)
from .rate_limit import (
    PoolRateLimit,
    RateLimitMonitor,
    RateLimitPool,
    RateLimitSnapshot,
    RateLimitStatus,
)
from .sync import (
    BulkIngestionConfig,
    BulkIngestionResult,
    BulkPRIngestionService,
    OutputFormat,
    PRIngestionResult,
    PRIngestionService,
    SyncStrategy,
)

__all__ = [
    # Client
    "GitHubClient",
    # Exceptions
    "GitHubAuthenticationError",
    "GitHubClientError",
    "GitHubNotFoundError",
    "GitHubRateLimitError",
    "GitHubRetryableError",
    # Rate limit monitoring
    "PoolRateLimit",
    "RateLimitMonitor",
    "RateLimitPool",
    "RateLimitSnapshot",
    "RateLimitStatus",
    # Request pacing
    "AsyncTokenBucket",
    "RequestPacer",
    "RequestPriority",
    "RequestScheduler",
    "RequestState",
    # PR Sync (single)
    "OutputFormat",
    "PRIngestionResult",
    "PRIngestionService",
    "SyncStrategy",
    # PR Sync (bulk)
    "BulkIngestionConfig",
    "BulkIngestionResult",
    "BulkPRIngestionService",
]
