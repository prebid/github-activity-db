"""Synthetic source records for the writer's direct tests, and reads of what it wrote."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from github_activity_db.source.records import (
    Account,
    AccountType,
    Comment,
    CommitRecord,
    ConnectionTotals,
    FileChange,
    HeadRepository,
    HeadRepositoryOwner,
    Label,
    LinkedAccount,
    LinkedClosingIssue,
    LookedUpAccount,
    OrganizationProfile,
    PullRequestRecord,
    RepositoryLabel,
    RepositoryRecord,
    RequestedTeam,
    Review,
    ReviewRequest,
    UserProfile,
)

T0 = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
HYDRATED = T0 + timedelta(hours=1)


def ts(value: datetime | None) -> str | None:
    """A timestamp as UtcDateTime stores it."""
    return None if value is None else value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")


def fetch(database: Path, sql: str, *params: Any) -> list[dict[str, Any]]:
    """Rows of a query on a separate sqlite3 connection, as dicts."""
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(sql, params)]
    finally:
        connection.close()


def account(
    login: str,
    github_id: int | None,
    type: AccountType = "User",
    node_id: str | None = None,
    linked_user_github_id: int | None = None,
) -> Account:
    return Account(
        github_id=github_id,
        node_id=node_id or f"N_{login}",
        login=login,
        type=type,
        linked_user_github_id=linked_user_github_id,
    )


def link(user: Account) -> LinkedAccount:
    assert user.github_id is not None
    return LinkedAccount(github_id=user.github_id, node_id=user.node_id, login=user.login)


def repository(
    github_id: int = 11,
    full_name: str = "prebid/Prebid.js",
    *,
    default_branch: str = "master",
    labels: Iterable[tuple[str, str]] = (),
    resolved_at: datetime = T0,
    **values: Any,
) -> RepositoryRecord:
    owner, name = full_name.split("/")
    label_records = tuple(
        RepositoryLabel(
            node_id=node_id,
            name=label_name,
            color="ededed",
            description=None,
            is_default=False,
            created_at=T0,
            updated_at=None,
        )
        for node_id, label_name in labels
    )
    fields: dict[str, Any] = {
        "github_id": github_id,
        "node_id": f"R_{github_id}",
        "owner": owner,
        "name": name,
        "full_name": full_name,
        "is_private": False,
        "is_archived": False,
        "default_branch": default_branch,
        "labels": label_records,
        "labels_total": len(label_records),
        "is_security_policy_enabled": None,
        "visibility": "PUBLIC",
        "description": "a repository",
        "homepage_url": None,
        "created_at": T0 - timedelta(days=1000),
        "updated_at": T0,
        "pushed_at": T0,
        "archived_at": None,
        "stargazer_count": 5,
        "fork_count": 2,
        "disk_usage": 100,
        "forking_allowed": True,
        "allow_update_branch": False,
        "web_commit_signoff_required": False,
        "has_wiki_enabled": True,
        "has_discussions_enabled": False,
        "has_projects_enabled": True,
        "has_vulnerability_alerts_enabled": True,
        "uses_custom_open_graph_image": False,
        "merge_commit_allowed": True,
        "squash_merge_allowed": True,
        "rebase_merge_allowed": False,
        "delete_branch_on_merge": True,
        "merge_commit_title": "MERGE_MESSAGE",
        "merge_commit_message": "PR_TITLE",
        "squash_merge_commit_title": "COMMIT_OR_PR_TITLE",
        "squash_merge_commit_message": "COMMIT_MESSAGES",
        "primary_language": "JavaScript",
        "license_spdx_id": "Apache-2.0",
        "plan_features_codeowners": True,
        "plan_features_team_review_requests": True,
        "plan_features_maximum_manual_review_requests": 15,
        "resolved_at": resolved_at,
    }
    fields.update(values)
    return RepositoryRecord(**fields)


def review(github_id: int, reviewer: Account | None, **values: Any) -> Review:
    fields: dict[str, Any] = {
        "github_id": github_id,
        "node_id": f"PRR_{github_id}",
        "reviewer": reviewer,
        "state": "APPROVED",
        "created_at": T0,
        "submitted_at": T0,
        "published_at": T0,
        "updated_at": T0,
        "last_edited_at": None,
        "editor": None,
        "includes_created_edit": False,
        "commit_oid": "c" * 40,
        "author_association": "MEMBER",
        "author_can_push_to_repository": True,
        "inline_comments_total": 0,
    }
    fields.update(values)
    return Review(**fields)


def comment(github_id: int, author: Account | None, **values: Any) -> Comment:
    fields: dict[str, Any] = {
        "github_id": github_id,
        "node_id": f"IC_{github_id}",
        "author": author,
        "created_at": T0,
        "published_at": T0,
        "updated_at": T0,
        "last_edited_at": None,
        "editor": None,
        "created_via_email": False,
        "includes_created_edit": False,
        "author_association": "MEMBER",
    }
    fields.update(values)
    return Comment(**fields)


def commit(sha: str, author: LinkedAccount | None, **values: Any) -> CommitRecord:
    fields: dict[str, Any] = {
        "sha": sha,
        "author": author,
        "author_name": "A Developer",
        "author_email": "dev@example.com",
        "authored_at": T0,
    }
    fields.update(values)
    return CommitRecord(**fields)


def closing_issue(
    number: int, *, user_linked: bool = False, repo_github_id: int = 777
) -> LinkedClosingIssue:
    return LinkedClosingIssue(
        repo_github_id=repo_github_id,
        repo_full_name="owner/repo",
        number=number,
        github_id=90000 + number,
        user_linked=user_linked,
    )


def request_for(
    user: Account, *, github_id: int = 501, as_code_owner: bool = False
) -> ReviewRequest:
    return ReviewRequest(
        github_id=github_id,
        node_id=f"RR_{github_id}",
        as_code_owner=as_code_owner,
        requested_type=user.type,
        account=user,
        team=None,
    )


def request_for_team(
    slug: str, *, github_id: int = 502, team_github_id: int | None = 4001
) -> ReviewRequest:
    return ReviewRequest(
        github_id=github_id,
        node_id=f"RR_{github_id}",
        as_code_owner=True,
        requested_type="Team",
        account=None,
        team=RequestedTeam(github_id=team_github_id, node_id=f"T_{slug}", slug=slug),
    )


def request_without_reviewer(*, github_id: int = 503) -> ReviewRequest:
    return ReviewRequest(
        github_id=github_id,
        node_id=f"RR_{github_id}",
        as_code_owner=False,
        requested_type=None,
        account=None,
        team=None,
    )


def pull_request(
    number: int,
    github_id: int,
    *,
    repository: str = "prebid/Prebid.js",
    author: Account | None = None,
    merged_by: Account | None = None,
    editor: Account | None = None,
    reviews: Iterable[Review] = (),
    comments: Iterable[Comment] = (),
    commits: Iterable[CommitRecord] = (),
    files: Iterable[FileChange] = (),
    labels: Iterable[tuple[str, str]] = (),
    closing_issues: Iterable[LinkedClosingIssue] = (),
    review_requests: Iterable[ReviewRequest] = (),
    assignees: Iterable[Account] = (),
    hydrated_at: datetime = HYDRATED,
    github_updated_at: datetime = T0,
    **values: Any,
) -> PullRequestRecord:
    """A hydrated pull request; each total is its list's length, as the validation makes it."""
    review_records = tuple(reviews)
    comment_records = tuple(comments)
    commit_records = tuple(commits)
    file_records = tuple(files)
    label_records = tuple(Label(name=name, node_id=node_id) for node_id, name in labels)
    link_records = tuple(closing_issues)
    request_records = tuple(review_requests)
    assignee_records = tuple(assignees)
    fields: dict[str, Any] = {
        "repository": repository,
        "github_id": github_id,
        "node_id": f"PR_{github_id}",
        "number": number,
        "state": "OPEN",
        "is_draft": False,
        "title": f"PR {number}",
        "body": "",
        "author": author,
        "author_association": "MEMBER",
        "created_at": T0 - timedelta(days=3),
        "merged_at": None,
        "closed_at": None,
        "published_at": T0 - timedelta(days=3),
        "last_edited_at": None,
        "editor": editor,
        "includes_created_edit": False,
        "maintainer_can_modify": True,
        "merged_by": merged_by,
        "base_ref": "master",
        "base_ref_oid": "b" * 40,
        "head_ref_name": f"feature-{number}",
        "head_ref_oid": "h" * 40,
        "head_repository": HeadRepository(github_id=55, full_name="fork/Prebid.js"),
        "head_repository_owner": HeadRepositoryOwner(node_id="O_fork", login="fork"),
        "merge_commit_oid": None,
        "is_cross_repository": True,
        "milestone": None,
        "review_decision": None,
        "mergeable": "MERGEABLE",
        "merge_state_status": "CLEAN",
        "can_be_rebased": True,
        "potential_merge_commit_oid": "p" * 40,
        "stack": None,
        "stack_entry": None,
        "additions": 10,
        "deletions": 2,
        "changed_files": len(file_records),
        "total_comments_count": len(comment_records),
        "labels": label_records,
        "closing_issues": link_records,
        "reviews": review_records,
        "comments": comment_records,
        "commits": commit_records,
        "commits_source": "graphql",
        "files": file_records,
        "review_requests": request_records,
        "assignees": assignee_records,
        "totals": ConnectionTotals(
            labels=len(label_records),
            closing_issues=len(link_records),
            user_linked_closing_issues=sum(
                1 for link_record in link_records if link_record.user_linked
            ),
            reviews=len(review_records),
            comments=len(comment_records),
            commits=len(commit_records),
            files=len(file_records),
            review_requests=len(request_records),
            assignees=len(assignee_records),
        ),
        "github_updated_at": github_updated_at,
        "hydrated_at": hydrated_at,
        "hydration_version": 2,
    }
    fields.update(values)
    return PullRequestRecord(**fields)


def looked_up(
    user: Account,
    *,
    name: str | None = "A Name",
    profile: UserProfile | OrganizationProfile | None = None,
    claimant_github_id: int | None = None,
) -> LookedUpAccount:
    return LookedUpAccount(
        account=user,
        name=name,
        created_at=T0 - timedelta(days=2000),
        updated_at=T0 - timedelta(days=10),
        claimant_github_id=claimant_github_id,
        profile=profile,
    )
