"""Rows for schema and view tests, written through the models' tables with foreign keys on."""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, create_engine, event, insert, pool

from github_activity_db.db.engine import _apply_sqlite_pragmas
from github_activity_db.db.models import (
    AccountProfile,
    AutomationAccount,
    AutomationEmail,
    Base,
    GitHubUser,
    PullRequest,
    PullRequestAssignee,
    PullRequestComment,
    PullRequestCommit,
    PullRequestReview,
    PullRequestReviewRequest,
    Repository,
    SyncRun,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)
# GitHub ids unique across every Seed in the process, so separate transactions never collide.
_IDS = itertools.count(1000)


@contextmanager
def connect(database: Path) -> Iterator[Connection]:
    """A transaction on ``database`` with the application's connection settings."""
    engine = create_engine(f"sqlite:///{database}", poolclass=pool.NullPool)
    event.listen(engine, "connect", _apply_sqlite_pragmas)
    try:
        with engine.begin() as connection:
            yield connection
    finally:
        engine.dispose()


SYNC_RUN: dict[str, Any] = {
    "started_at": T0,
    "status": "running",
    "trigger": "manual",
    "code_version": "test",
    "config_source": "packaged",
    "config_sha256": "0" * 64,
    "graphql_points": 0,
    "rest_calls": 0,
    "items_planned": 0,
    "items_hydrated": 0,
    "items_deleted": 0,
    "failures": 0,
}


class Seed:
    """Inserts rows with valid defaults for every column a test does not care about."""

    def __init__(self, connection: Connection) -> None:
        self.connection = connection
        self._run_id: int | None = None

    @staticmethod
    def next_id() -> int:
        return next(_IDS)

    def _insert(self, model: type[Base], values: dict[str, Any]) -> int:
        """Insert one row; return its first primary-key column."""
        result = self.connection.execute(insert(model).values(**values))
        assert result.inserted_primary_key is not None
        return int(result.inserted_primary_key[0])

    def sync_run(self, **values: Any) -> int:
        return self._insert(SyncRun, {**SYNC_RUN, **values})

    def run_id(self) -> int:
        """One run for the rows that name the run that wrote them."""
        if self._run_id is None:
            self._run_id = self.sync_run()
        return self._run_id

    def repository(self, full_name: str = "prebid/Prebid.js", **values: Any) -> int:
        owner, name = full_name.split("/")
        github_id = values.pop("github_id", self.next_id())
        row = {
            "github_id": github_id,
            "node_id": f"R_{github_id}",
            "owner": owner,
            "name": name,
            "full_name": full_name,
            "repo_group": "Client",
            "is_private": False,
            "is_archived": False,
            "default_branch": "master",
            "labels_total": 0,
            "is_tracked": True,
            "resolved_at": T0,
            "is_security_policy_enabled": None,
            "visibility": "PUBLIC",
            "created_at": T0,
            "updated_at": T0,
            "stargazer_count": 0,
            "fork_count": 0,
            "forking_allowed": True,
            "allow_update_branch": False,
            "web_commit_signoff_required": False,
            "has_wiki_enabled": False,
            "has_discussions_enabled": False,
            "has_projects_enabled": True,
            "has_vulnerability_alerts_enabled": True,
            "uses_custom_open_graph_image": False,
            "merge_commit_allowed": True,
            "squash_merge_allowed": True,
            "rebase_merge_allowed": True,
            "delete_branch_on_merge": False,
            "merge_commit_title": "MERGE_MESSAGE",
            "merge_commit_message": "PR_TITLE",
            "squash_merge_commit_title": "COMMIT_OR_PR_TITLE",
            "squash_merge_commit_message": "COMMIT_MESSAGES",
            "plan_features_codeowners": True,
            "plan_features_team_review_requests": True,
            "plan_features_maximum_manual_review_requests": 15,
        }
        return self._insert(Repository, {**row, **values})

    def user(self, login: str, type: str = "User", **values: Any) -> int:
        github_id = values.pop(
            "github_id", None if type == "EnterpriseUserAccount" else self.next_id()
        )
        row = {
            "github_id": github_id,
            "node_id": values.pop("node_id", f"U_{github_id if github_id else self.next_id()}"),
            "login": login,
            "type": type,
            "refreshed_at": T0,
        }
        return self._insert(GitHubUser, {**row, **values})

    def profile(self, account_id: int, **values: Any) -> None:
        self._insert(AccountProfile, {"account_id": account_id, "refreshed_at": T0, **values})

    def pull_request(
        self, repository_id: int, number: int, author_id: int | None = None, **values: Any
    ) -> int:
        github_id = values.pop("github_id", self.next_id())
        row = {
            "repository_id": repository_id,
            "number": number,
            "github_id": github_id,
            "node_id": f"PR_{github_id}",
            "state": "OPEN",
            "is_draft": False,
            "title": f"PR {number}",
            "body": "",
            "author_id": author_id,
            "author_reported": author_id is not None,
            "author_association": "MEMBER",
            "created_at": T0,
            "merged_at": None,
            "includes_created_edit": False,
            "maintainer_can_modify": False,
            "merged_by_id": None,
            "merged_by_reported": False,
            "base_ref": "master",
            "base_ref_oid": "b" * 40,
            "head_ref_name": f"feature-{number}",
            "head_ref_oid": "h" * 40,
            "targets_default_branch": True,
            "is_cross_repository": False,
            "mergeable": "MERGEABLE",
            "merge_state_status": "CLEAN",
            "can_be_rebased": True,
            "additions": 0,
            "deletions": 0,
            "changed_files": 0,
            "commits_total": 0,
            "files_total": 0,
            "reviews_total": 0,
            "comments_total": 0,
            "labels_total": 0,
            "closing_issues_total": 0,
            "user_linked_closing_issues_total": 0,
            "review_requests_total": 0,
            "assignees_total": 0,
            "github_updated_at": T0,
            "hydrated_at": T0,
            "hydration_version": 2,
        }
        return self._insert(PullRequest, {**row, **values})

    def review(
        self, pr_id: int, github_id: int, reviewer_id: int | None, state: str, **values: Any
    ) -> int:
        row = {
            "github_id": github_id,
            "node_id": f"PRR_{github_id}",
            "pr_id": pr_id,
            "reviewer_id": reviewer_id,
            "reviewer_reported": reviewer_id is not None,
            "state": state,
            "created_at": T0,
            "submitted_at": T0,
            "updated_at": T0,
            "includes_created_edit": False,
            "author_association": "MEMBER",
            "author_can_push_to_repository": True,
            "inline_comments_total": 0,
        }
        return self._insert(PullRequestReview, {**row, **values})

    def comment(
        self, pr_id: int, author_id: int | None, github_id: int | None = None, **values: Any
    ) -> int:
        github_id = github_id or self.next_id()
        row = {
            "github_id": github_id,
            "node_id": f"IC_{github_id}",
            "pr_id": pr_id,
            "author_id": author_id,
            "author_reported": author_id is not None,
            "created_at": T0,
            "updated_at": T0,
            "created_via_email": False,
            "includes_created_edit": False,
            "author_association": "MEMBER",
        }
        return self._insert(PullRequestComment, {**row, **values})

    def commit(self, pr_id: int, sha: str, author_id: int | None = None, **values: Any) -> None:
        row = {
            "pr_id": pr_id,
            "sha": sha,
            "author_id": author_id,
            "linked_github_id": None,
            "author_name": "A Developer",
            "author_email": "dev@example.com",
            "authored_at": T0,
        }
        self._insert(PullRequestCommit, {**row, **values})

    def review_request(self, pr_id: int, ordinal: int, **values: Any) -> None:
        row = {
            "pr_id": pr_id,
            "ordinal": ordinal,
            "request_github_id": self.next_id(),
            "request_node_id": f"RR_{pr_id}_{ordinal}",
            "as_code_owner": False,
        }
        self._insert(PullRequestReviewRequest, {**row, **values})

    def assignee(self, pr_id: int, account_id: int) -> None:
        self._insert(PullRequestAssignee, {"pr_id": pr_id, "account_id": account_id})

    def automation_account(self, github_id: int, login: str, reason: str = "test") -> None:
        self._insert(
            AutomationAccount,
            {"github_id": github_id, "login": login, "reason": reason, "run_id": self.run_id()},
        )

    def automation_email(self, email: str, reason: str = "test") -> None:
        self._insert(AutomationEmail, {"email": email, "reason": reason, "run_id": self.run_id()})
