"""Factory functions for creating test data.

This module provides factory functions for:
- SQLAlchemy ORM models (Repository, PullRequest, UserTag)
- Pydantic schemas (GitHub API responses)

Design principles:
- Factories provide sensible defaults that can be overridden
- Model factories add to session but don't flush (tests control flush timing)
- Schema factories return dicts suitable for Pydantic model instantiation
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from github_activity_db.db.models import (
    PRState,
    PullRequest,
    Repository,
    SyncFailure,
    SyncFailureStatus,
    UserTag,
)

# Import test timeline constants
from tests.conftest import JAN_15_ISO, JAN_16_ISO, JAN_20_ISO


# -----------------------------------------------------------------------------
# Model Factories
# -----------------------------------------------------------------------------
def make_repository(
    session: AsyncSession,
    *,
    owner: str = "prebid",
    name: str = "prebid-server",
    full_name: str | None = None,
    is_active: bool = True,
    last_synced_at: datetime | None = None,
    **overrides: Any,
) -> Repository:
    """Create a Repository model instance.

    Args:
        session: Async database session (model will be added but not flushed)
        owner: GitHub org/user
        name: Repository name
        full_name: Full path (defaults to "{owner}/{name}")
        is_active: Whether repo is active for syncing
        last_synced_at: Last sync timestamp
        **overrides: Additional field overrides

    Returns:
        Repository instance (added to session, not flushed)
    """
    repo = Repository(
        owner=owner,
        name=name,
        full_name=full_name or f"{owner}/{name}",
        is_active=is_active,
        last_synced_at=last_synced_at,
        **overrides,
    )
    session.add(repo)
    return repo


def make_pull_request(
    session: AsyncSession,
    repository: Repository,
    *,
    number: int = 1234,
    title: str | None = None,
    description: str | None = "Test PR description",
    submitter: str = "testuser",
    state: PRState = PRState.OPEN,
    open_date: datetime | None = None,
    last_update_date: datetime | None = None,
    # Merge fields
    close_date: datetime | None = None,
    merged_by: str | None = None,
    ai_summary: str | None = None,
    # Stats
    files_changed: int = 1,
    lines_added: int = 10,
    lines_deleted: int = 2,
    commits_count: int = 1,
    # Lists
    github_labels: list[str] | None = None,
    filenames: list[str] | None = None,
    reviewers: list[str] | None = None,
    assignees: list[str] | None = None,
    commits_breakdown: list[dict[str, str]] | None = None,
    participants: dict[str, list[str]] | None = None,
    classify_tags: str | None = None,
    **overrides: Any,
) -> PullRequest:
    """Create a PullRequest model instance.

    Args:
        session: Async database session (model will be added but not flushed)
        repository: Parent repository (must be flushed to have an ID)
        number: PR number
        title: PR title (defaults to "Test PR #{number}")
        description: PR body
        submitter: PR author username
        state: PR state (OPEN, MERGED, CLOSED)
        open_date: When PR was opened (defaults to now)
        last_update_date: Last update time (defaults to now)
        close_date: When PR was closed/merged
        merged_by: Who merged the PR
        ai_summary: AI-generated summary
        files_changed: Number of files changed
        lines_added: Lines added
        lines_deleted: Lines deleted
        commits_count: Number of commits
        github_labels: List of label names
        filenames: List of changed files
        reviewers: List of reviewer usernames
        assignees: List of assignee usernames
        commits_breakdown: List of {date, author} dicts
        participants: Dict of username -> actions
        classify_tags: AI classification tags
        **overrides: Additional field overrides

    Returns:
        PullRequest instance (added to session, not flushed)
    """
    now = datetime.now(UTC)

    pr = PullRequest(
        repository_id=repository.id,
        number=number,
        link=f"https://github.com/{repository.full_name}/pull/{number}",
        title=title or f"Test PR #{number}",
        description=description,
        submitter=submitter,
        state=state,
        open_date=open_date or now,
        last_update_date=last_update_date or now,
        close_date=close_date,
        merged_by=merged_by,
        ai_summary=ai_summary,
        files_changed=files_changed,
        lines_added=lines_added,
        lines_deleted=lines_deleted,
        commits_count=commits_count,
        github_labels=github_labels or [],
        filenames=filenames or [],
        reviewers=reviewers or [],
        assignees=assignees or [],
        commits_breakdown=commits_breakdown or [],
        participants=participants or {},
        classify_tags=classify_tags,
        **overrides,
    )
    session.add(pr)
    return pr


def make_merged_pr(
    session: AsyncSession,
    repository: Repository,
    *,
    merged_by: str = "maintainer",
    **overrides: Any,
) -> PullRequest:
    """Create a merged PullRequest.

    Convenience wrapper around make_pull_request with merge defaults.
    """
    now = datetime.now(UTC)
    defaults = {
        "state": PRState.MERGED,
        "close_date": now,
        "merged_by": merged_by,
    }
    return make_pull_request(session, repository, **(defaults | overrides))


def make_user_tag(
    session: AsyncSession,
    *,
    name: str = "test-tag",
    description: str | None = "Test tag description",
    color: str | None = "#ff9900",
    **overrides: Any,
) -> UserTag:
    """Create a UserTag model instance.

    Args:
        session: Async database session (model will be added but not flushed)
        name: Tag name (must be unique)
        description: Tag description
        color: Hex color code
        **overrides: Additional field overrides

    Returns:
        UserTag instance (added to session, not flushed)
    """
    tag = UserTag(
        name=name,
        description=description,
        color=color,
        **overrides,
    )
    session.add(tag)
    return tag


def make_sync_failure(
    session: AsyncSession,
    repository: Repository,
    *,
    pr_number: int = 123,
    error_message: str = "Test error message",
    error_type: str = "TestError",
    retry_count: int = 0,
    status: SyncFailureStatus = SyncFailureStatus.PENDING,
    failed_at: datetime | None = None,
    resolved_at: datetime | None = None,
    **overrides: Any,
) -> SyncFailure:
    """Create a SyncFailure model instance.

    Args:
        session: Async database session (model will be added but not flushed)
        repository: Repository the failure belongs to
        pr_number: PR number that failed
        error_message: Error message text
        error_type: Error class name
        retry_count: Number of retry attempts
        status: Failure status (PENDING, RESOLVED, PERMANENT)
        failed_at: When the failure occurred
        resolved_at: When the failure was resolved (if applicable)
        **overrides: Additional field overrides

    Returns:
        SyncFailure instance (added to session, not flushed)
    """
    failure = SyncFailure(
        repository_id=repository.id,
        pr_number=pr_number,
        error_message=error_message,
        error_type=error_type,
        retry_count=retry_count,
        status=status,
        failed_at=failed_at or datetime.now(UTC),
        resolved_at=resolved_at,
        **overrides,
    )
    session.add(failure)
    return failure


# -----------------------------------------------------------------------------
# GitHub API Schema Factories
# -----------------------------------------------------------------------------
def make_github_user(
    *,
    login: str = "testuser",
    user_id: int = 12345,
    user_type: str = "User",
) -> dict[str, Any]:
    """Create a GitHub user API response dict."""
    return {
        "login": login,
        "id": user_id,
        "type": user_type,
    }


def make_github_label(
    *,
    label_id: int = 1,
    name: str = "bug",
    color: str = "d73a4a",
    description: str | None = "Something isn't working",
) -> dict[str, Any]:
    """Create a GitHub label API response dict."""
    return {
        "id": label_id,
        "name": name,
        "color": color,
        "description": description,
    }


def make_github_pr(
    *,
    number: int = 1234,
    owner: str = "prebid",
    repo: str = "prebid-server",
    state: str = "open",
    title: str = "Test PR",
    body: str | None = "Test PR description",
    user: dict[str, Any] | None = None,
    merged: bool = False,
    merged_by: dict[str, Any] | None = None,
    created_at: str = JAN_15_ISO,
    updated_at: str = JAN_16_ISO,
    closed_at: str | None = None,
    merged_at: str | None = None,
    commits: int = 1,
    additions: int = 10,
    deletions: int = 2,
    changed_files: int = 1,
    labels: list[dict[str, Any]] | None = None,
    requested_reviewers: list[dict[str, Any]] | None = None,
    assignees: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create a GitHub PR API response dict.

    This matches the structure returned by:
    GET /repos/{owner}/{repo}/pulls/{number}
    """
    return {
        "number": number,
        "html_url": f"https://github.com/{owner}/{repo}/pull/{number}",
        "state": state,
        "title": title,
        "body": body,
        "user": user or make_github_user(),
        "merged_by": merged_by,
        "created_at": created_at,
        "updated_at": updated_at,
        "closed_at": closed_at,
        "merged_at": merged_at,
        "merged": merged,
        "commits": commits,
        "additions": additions,
        "deletions": deletions,
        "changed_files": changed_files,
        "labels": labels or [],
        "requested_reviewers": requested_reviewers or [],
        "assignees": assignees or [],
    }


def make_github_merged_pr(
    *,
    merged_by: str = "maintainer",
    merged_at: str = JAN_20_ISO,
    **overrides: Any,
) -> dict[str, Any]:
    """Create a merged GitHub PR API response dict."""
    defaults = {
        "state": "closed",
        "merged": True,
        "merged_by": make_github_user(login=merged_by),
        "merged_at": merged_at,
        "closed_at": merged_at,
    }
    return make_github_pr(**(defaults | overrides))


def make_github_file(
    *,
    sha: str = "abc123",
    filename: str = "src/example.py",
    status: str = "modified",
    additions: int = 10,
    deletions: int = 2,
    changes: int = 12,
) -> dict[str, Any]:
    """Create a GitHub file API response dict.

    This matches items in the response from:
    GET /repos/{owner}/{repo}/pulls/{number}/files
    """
    return {
        "sha": sha,
        "filename": filename,
        "status": status,
        "additions": additions,
        "deletions": deletions,
        "changes": changes,
    }


def make_github_commit(
    *,
    sha: str = "commit123",
    author_name: str = "Test User",
    author_email: str = "test@example.com",
    date: str = JAN_15_ISO,
    message: str = "Test commit",
) -> dict[str, Any]:
    """Create a GitHub commit API response dict.

    This matches items in the response from:
    GET /repos/{owner}/{repo}/pulls/{number}/commits
    """
    return {
        "sha": sha,
        "commit": {
            "author": {
                "name": author_name,
                "email": author_email,
                "date": date,
            },
            "message": message,
        },
    }


def make_github_review(
    *,
    review_id: int = 1001,
    user: dict[str, Any] | None = None,
    state: str = "APPROVED",
    submitted_at: str = JAN_16_ISO,
) -> dict[str, Any]:
    """Create a GitHub review API response dict.

    This matches items in the response from:
    GET /repos/{owner}/{repo}/pulls/{number}/reviews

    States: APPROVED, CHANGES_REQUESTED, COMMENTED, PENDING, DISMISSED
    """
    return {
        "id": review_id,
        "user": user or make_github_user(login="reviewer"),
        "state": state,
        "submitted_at": submitted_at,
    }
