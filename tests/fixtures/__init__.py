"""Test fixtures for GitHub Activity DB."""

from .github_responses import (
    GITHUB_COMMITS_RESPONSE,
    GITHUB_FILES_RESPONSE,
    GITHUB_LABEL_RESPONSE,
    GITHUB_PR_MERGED_RESPONSE,
    GITHUB_PR_RESPONSE,
    GITHUB_REVIEWS_RESPONSE,
    GITHUB_USER_RESPONSE,
)
from .real_pr_merged import MERGED_PR_METADATA, REAL_MERGED_PR
from .real_pr_open import OPEN_PR_METADATA, REAL_OPEN_PR

__all__ = [
    # Mock GitHub API responses
    "GITHUB_COMMITS_RESPONSE",
    "GITHUB_FILES_RESPONSE",
    "GITHUB_LABEL_RESPONSE",
    "GITHUB_PR_MERGED_RESPONSE",
    "GITHUB_PR_RESPONSE",
    "GITHUB_REVIEWS_RESPONSE",
    "GITHUB_USER_RESPONSE",
    # Real PR fixtures from prebid/prebid-server
    "REAL_OPEN_PR",
    "OPEN_PR_METADATA",
    "REAL_MERGED_PR",
    "MERGED_PR_METADATA",
]
