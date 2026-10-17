"""Database module for GitHub Activity DB."""

from github_activity_db.db.engine import (
    create_tables,
    dispose_engine,
    drop_tables,
    get_engine,
    get_session,
    get_session_factory,
)
from github_activity_db.db.models import (
    Base,
    PRState,
    PullRequest,
    Repository,
    SyncFailure,
    SyncFailureStatus,
    UserTag,
    pr_user_tags,
)

__all__ = [
    # Models
    "Base",
    "PRState",
    "PullRequest",
    "Repository",
    "SyncFailure",
    "SyncFailureStatus",
    "UserTag",
    "pr_user_tags",
    # Engine
    "create_tables",
    "dispose_engine",
    "drop_tables",
    "get_engine",
    "get_session",
    "get_session_factory",
]
