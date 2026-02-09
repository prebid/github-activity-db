"""Pydantic schemas for GitHub Activity DB.

This module provides input validation and output serialization models.
"""

from .base import SchemaBase
from .enums import FileChangeStatus, ParticipantActionType
from .github_api import (
    GitHubCommit,
    GitHubCommitAuthor,
    GitHubCommitDetail,
    GitHubFile,
    GitHubLabel,
    GitHubPullRequest,
    GitHubReview,
    GitHubUser,
)
from .nested import (
    CommitBreakdown,
    FileChange,
    ParticipantEntry,
    file_changes_from_list,
    file_changes_to_list,
    participants_from_dict,
    participants_to_dict,
)
from .pr import PRCreate, PRMerge, PRRead, PRSync
from .repository import RepositoryCreate, RepositoryRead, parse_repo_string
from .tag import UserTagCreate, UserTagRead

__all__ = [
    # Nested models
    "CommitBreakdown",
    "FileChange",
    "FileChangeStatus",
    "file_changes_from_list",
    "file_changes_to_list",
    "GitHubCommit",
    "GitHubCommitAuthor",
    "GitHubCommitDetail",
    "GitHubFile",
    "GitHubLabel",
    "GitHubPullRequest",
    "GitHubReview",
    # GitHub API
    "GitHubUser",
    # PR
    "PRCreate",
    "PRMerge",
    "PRRead",
    "PRSync",
    # Enums
    "ParticipantActionType",
    "ParticipantEntry",
    # Repository
    "RepositoryCreate",
    "RepositoryRead",
    "parse_repo_string",
    # Base
    "SchemaBase",
    # Tags
    "UserTagCreate",
    "UserTagRead",
    "participants_from_dict",
    "participants_to_dict",
]
