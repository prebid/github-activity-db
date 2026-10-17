"""GitHub source layer: GraphQL and REST reads, typed records out (redesign plan Step 2).

The sync engine uses ``GitHubSource``, the records, and the errors exported here; the response
models stay private. Only this package will import githubkit once Step 3 removes ``github/``.
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
    SourceError,
    UserLookupFailed,
)
from .records import (
    Account,
    ClosingIssue,
    Commenter,
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
    RepositoryRecord,
    Review,
)

__all__ = [
    "PULL_REQUEST_HYDRATION_VERSION",
    "PULL_REQUEST_KIND",
    "Account",
    "ClosingIssue",
    "Clock",
    "Commenter",
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
    "RepositoryError",
    "RepositoryRecord",
    "Review",
    "SourceError",
    "SystemClock",
    "Usage",
    "UserLookupFailed",
]
