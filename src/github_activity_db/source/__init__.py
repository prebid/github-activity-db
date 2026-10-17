"""GitHub source layer: GraphQL and REST reads, typed records out (redesign plan Step 2).

Only this package imports githubkit. The sync engine uses ``GitHubSource`` and the records; the
response models stay private.
"""

from .budget import Usage
from .client import GitHubSource, build_github
from .clock import Clock, SystemClock
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
    "Account",
    "ClosingIssue",
    "Clock",
    "Commenter",
    "CommitRecord",
    "ConnectionTotals",
    "Failed",
    "FileChange",
    "GitHubSource",
    "Gone",
    "Hydrated",
    "IndexedPullRequest",
    "LinkedAccount",
    "PullRequestIndex",
    "PullRequestOutcome",
    "PullRequestRecord",
    "RepositoryRecord",
    "Review",
    "SystemClock",
    "Usage",
    "build_github",
]
