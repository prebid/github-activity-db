"""Pydantic schemas for GitHub Activity DB.

This module provides input validation and output serialization models.
"""

from .base import SchemaBase
from .enums import ParticipantActionType
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
    ParticipantEntry,
    participants_from_dict,
    participants_to_dict,
)
from .pr import PRCreate, PRMerge, PRRead, PRSync
from .repository import RepositoryCreate, RepositoryRead, parse_repo_string
from .tag import UserTagCreate, UserTagRead

__all__ = [
    # Nested models
    "CommitBreakdown",
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
