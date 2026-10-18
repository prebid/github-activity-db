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
    AutomationAccount,
    AutomationEmail,
    Base,
    GitHubUser,
    PullRequest,
    PullRequestComment,
    PullRequestCommit,
    PullRequestReview,
    Repository,
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


class Seed:
    """Inserts rows with valid defaults for every column a test does not care about."""

    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    @staticmethod
    def next_id() -> int:
        return next(_IDS)

    def _insert(self, model: type[Base], values: dict[str, Any]) -> int:
        """Insert one row; return its first primary-key column."""
        result = self.connection.execute(insert(model).values(**values))
        assert result.inserted_primary_key is not None
        return int(result.inserted_primary_key[0])

    def repository(self, full_name: str = "prebid/Prebid.js", **values: Any) -> int:
        owner, name = full_name.split("/")
        row = {
            "github_id": self.next_id(),
            "owner": owner,
            "name": name,
            "full_name": full_name,
            "repo_group": "Client",
            "is_private": False,
            "is_archived": False,
            "default_branch": "master",
            "is_tracked": True,
        }
        return self._insert(Repository, {**row, **values})

    def user(self, login: str, type: str = "User", **values: Any) -> int:
        github_id = values.pop("github_id", self.next_id())
        row = {
            "github_id": github_id,
            "node_id": f"U_{github_id}",
            "login": login,
            "type": type,
            "refreshed_at": T0,
        }
        return self._insert(GitHubUser, {**row, **values})

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
            "author_association": "MEMBER",
            "created_at": T0,
            "merged_at": None,
            "merged_by_id": None,
            "base_ref": "master",
            "targets_default_branch": True,
            "is_cross_repository": False,
            "additions": 0,
            "deletions": 0,
            "changed_files": 0,
            "commits_total": 0,
            "files_total": 0,
            "reviews_total": 0,
            "comments_total": 0,
            "labels_total": 0,
            "closing_issues_total": 0,
            "github_updated_at": T0,
            "hydrated_at": T0,
            "hydration_version": 1,
        }
        return self._insert(PullRequest, {**row, **values})

    def review(self, pr_id: int, github_id: int, reviewer_id: int | None, state: str) -> int:
        return self._insert(
            PullRequestReview,
            {"github_id": github_id, "pr_id": pr_id, "reviewer_id": reviewer_id, "state": state},
        )

    def comment(self, pr_id: int, author_id: int | None, github_id: int | None = None) -> int:
        return self._insert(
            PullRequestComment,
            {"github_id": github_id or self.next_id(), "pr_id": pr_id, "author_id": author_id},
        )

    def commit(self, pr_id: int, sha: str, author_id: int | None = None, **values: Any) -> None:
        row = {
            "pr_id": pr_id,
            "sha": sha,
            "author_id": author_id,
            "author_name": "A Developer",
            "author_email": "dev@example.com",
            "authored_at": T0,
        }
        self._insert(PullRequestCommit, {**row, **values})

    def automation_account(self, github_id: int, login: str) -> None:
        self._insert(
            AutomationAccount,
            {"github_id": github_id, "login": login, "reason": "test"},
        )

    def automation_email(self, email: str) -> None:
        self._insert(AutomationEmail, {"email": email, "reason": "test"})
