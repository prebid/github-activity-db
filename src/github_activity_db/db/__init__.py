"""Database module for GitHub Activity DB."""

from github_activity_db.db.engine import (
    dispose_engine,
    get_engine,
    get_session,
    get_session_factory,
)
from github_activity_db.db.models import (
    AutomationAccount,
    AutomationEmail,
    Base,
    GitHubUser,
    PullRequest,
    PullRequestClosingIssue,
    PullRequestComment,
    PullRequestCommit,
    PullRequestFile,
    PullRequestLabel,
    PullRequestReview,
    PullRequestUserTag,
    Repository,
    RepoSyncState,
    SyncFailure,
    SyncPending,
    SyncRun,
    UserTag,
)
from github_activity_db.db.schema import HEAD_REVISION, MIGRATION_REVISIONS, SCHEMA_VERSION
from github_activity_db.db.types import UtcDateTime

__all__ = [
    # Models
    "AutomationAccount",
    "AutomationEmail",
    "Base",
    "GitHubUser",
    "PullRequest",
    "PullRequestClosingIssue",
    "PullRequestComment",
    "PullRequestCommit",
    "PullRequestFile",
    "PullRequestLabel",
    "PullRequestReview",
    "PullRequestUserTag",
    "RepoSyncState",
    "Repository",
    "SyncFailure",
    "SyncPending",
    "SyncRun",
    "UserTag",
    # Schema identity and types
    "HEAD_REVISION",
    "MIGRATION_REVISIONS",
    "SCHEMA_VERSION",
    "UtcDateTime",
    # Engine
    "dispose_engine",
    "get_engine",
    "get_session",
    "get_session_factory",
]
