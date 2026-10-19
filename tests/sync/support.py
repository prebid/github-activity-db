"""Builders for the planner tests: snapshots of migrated databases, synthetic index entries."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from github_activity_db.db.engine import build_engine
from github_activity_db.source.documents import PULL_REQUEST_HYDRATION_VERSION
from github_activity_db.source.records import (
    ClosingIssue,
    IndexedPullRequest,
    PullRequestIndex,
    PullRequestState,
)
from github_activity_db.sync.snapshot import Snapshot, read_snapshot
from github_activity_db.tracking import TrackingConfig
from tests.db.seed import Seed

FLOOR = datetime(2023, 1, 1, tzinfo=UTC)
CREATED = datetime(2024, 3, 1, tzinfo=UTC)
UPDATED = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
# A stored row hydrated an hour after its updatedAt: settled under the 10-minute window.
HYDRATED = UPDATED + timedelta(hours=1)
LINKED_REPO = 777


def config(*, floor: datetime = FLOOR, unsettled_minutes: int = 10, **sync: Any) -> TrackingConfig:
    return TrackingConfig.model_validate(
        {
            "coverage": {"floor": floor},
            "repositories": [{"owner": "prebid", "name": "Prebid.js", "group": "Client"}],
            "sync": {"unsettled_minutes": unsettled_minutes, **sync},
        }
    )


async def snapshot_of(database: Path) -> Snapshot:
    """The snapshot the application engine reads from ``database``."""
    engine = build_engine(f"sqlite+aiosqlite:///{database}")
    try:
        async with engine.connect() as connection:
            return await read_snapshot(connection)
    finally:
        await engine.dispose()


def link(number: int, repo_github_id: int = LINKED_REPO) -> ClosingIssue:
    return ClosingIssue(repo_github_id=repo_github_id, repo_full_name="owner/repo", number=number)


def entry(
    number: int,
    github_id: int,
    *,
    state: PullRequestState = "OPEN",
    is_draft: bool = False,
    author_association: str = "MEMBER",
    created_at: datetime = CREATED,
    updated_at: datetime = UPDATED,
    merged_at: datetime | None = None,
    closed_at: datetime | None = None,
    links: Iterable[ClosingIssue] = (),
    links_total: int | None = None,
) -> IndexedPullRequest:
    """An index entry; ``links_total`` defaults to the number of links listed."""
    listed = tuple(links)
    return IndexedPullRequest(
        number=number,
        github_id=github_id,
        state=state,
        is_draft=is_draft,
        author_association=author_association,
        created_at=created_at,
        updated_at=updated_at,
        merged_at=merged_at,
        closed_at=closed_at,
        closing_issues=listed,
        closing_issues_total=len(listed) if links_total is None else links_total,
    )


def merged(number: int, github_id: int, at: datetime, **values: Any) -> IndexedPullRequest:
    return entry(number, github_id, state="MERGED", merged_at=at, closed_at=at, **values)


def closed(number: int, github_id: int, at: datetime, **values: Any) -> IndexedPullRequest:
    return entry(number, github_id, state="CLOSED", closed_at=at, **values)


def index(full_name: str, *entries: IndexedPullRequest) -> PullRequestIndex:
    return PullRequestIndex(
        repository=full_name,
        started_at=UPDATED,
        total_count=len(entries),
        entries=entries,
        scans=1,
    )


def stored_pr(
    seed: Seed,
    repository_id: int,
    number: int,
    github_id: int,
    *,
    labels: Iterable[tuple[str, str]] = (),
    links: Iterable[int] = (),
    **values: Any,
) -> int:
    """A stored, settled pull request matching ``entry(number, github_id)`` field for field;
    ``labels`` are (node id, name) pairs and ``links`` issue numbers in ``LINKED_REPO``."""
    linked = tuple(links)
    row = {
        "github_id": github_id,
        "state": "OPEN",
        "is_draft": False,
        "author_association": "MEMBER",
        "created_at": CREATED,
        "github_updated_at": UPDATED,
        "hydrated_at": HYDRATED,
        "hydration_version": PULL_REQUEST_HYDRATION_VERSION,
        "closing_issues_total": len(linked),
        **values,
    }
    pr_id = seed.pull_request(repository_id, number, **row)
    for node_id, name in labels:
        seed.label(pr_id, name, node_id)
    for issue in linked:
        seed.closing_issue(pr_id, LINKED_REPO, issue)
    return pr_id


def commit_does_not_wait(database: Path) -> None:
    """A separate ``sqlite3`` connection writes and commits without waiting for any lock."""
    connection = sqlite3.connect(database, timeout=0, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        changed = connection.execute("UPDATE sync_runs SET rest_calls = rest_calls + 1").rowcount
        assert changed >= 1, "the probe needs a sync_runs row to change"
        connection.execute("COMMIT")
    finally:
        connection.close()
