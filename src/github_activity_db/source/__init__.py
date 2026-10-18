"""GitHub source layer: GraphQL and REST reads, typed records out (redesign plan Step 2).

The sync engine uses ``GitHubSource``, the records, and the errors exported here; the response
models stay private. Only this package imports githubkit (``tests/test_import_boundaries.py``).
"""

from .budget import Usage
from .client import GitHubSource
from .clock import Clock, SystemClock
from .documents import PULL_REQUEST_HYDRATION_VERSION, PULL_REQUEST_KIND
from .errors import (
    DeadlineReached,
    FatalSourceError,
    ItemError,
    RepositoryError,
    RequestFailedError,
    SourceError,
    UserLookupFailed,
)
from .records import (
    Account,
    ClosingIssue,
    Comment,
    CommitRecord,
    ConnectionTotals,
    Failed,
    FileChange,
    Gone,
    Hydrated,
    IndexedPullRequest,
    LinkedAccount,
    PullRequestIndex,
    PullRequestOutcome,
    PullRequestRecord,
    RateLimits,
    RepositoryRecord,
    RestRateLimit,
    Review,
    UserLookup,
)

__all__ = [
    "PULL_REQUEST_HYDRATION_VERSION",
    "PULL_REQUEST_KIND",
    "Account",
    "ClosingIssue",
    "Clock",
    "Comment",
    "CommitRecord",
    "ConnectionTotals",
    "DeadlineReached",
    "Failed",
    "FatalSourceError",
    "FileChange",
    "GitHubSource",
    "Gone",
    "Hydrated",
    "IndexedPullRequest",
    "ItemError",
    "LinkedAccount",
    "PullRequestIndex",
    "PullRequestOutcome",
    "PullRequestRecord",
    "RateLimits",
    "RepositoryError",
    "RepositoryRecord",
    "RequestFailedError",
    "RestRateLimit",
    "Review",
    "SourceError",
    "SystemClock",
    "Usage",
    "UserLookup",
    "UserLookupFailed",
]
