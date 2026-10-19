"""The snapshot reader: what it reads, that it writes nothing, that it holds no lock (gap 77)."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from github_activity_db.db.engine import build_engine
from github_activity_db.db.models import PullRequest
from github_activity_db.sync.snapshot import (
    Snapshot,
    StoredPullRequest,
    StoredRepository,
    read_snapshot,
)
from tests.db.seed import Seed, connect
from tests.sync.support import (
    HYDRATED,
    LINKED_REPO,
    UPDATED,
    commit_does_not_wait,
    snapshot_of,
    stored_pr,
)


def _seed(database: Path) -> dict[str, int]:
    """Two repositories, one with rows in every table the reader reads, one with none."""
    with connect(database) as connection:
        seed = Seed(connection)
        seed.sync_run()
        js = seed.repository("prebid/Prebid.js", github_id=11)
        empty = seed.repository("prebid/go-gdpr", github_id=12, is_tracked=False)
        first = stored_pr(
            seed,
            js,
            7,
            github_id=70,
            labels=[("LA_1", "bug"), ("LA_2", "feature")],
            links=[3, 4],
            state="MERGED",
            merged_at=UPDATED,
            closed_at=UPDATED,
            is_draft=True,
            author_association="CONTRIBUTOR",
            hydration_version=1,
        )
        second = stored_pr(seed, js, 2, github_id=20)
        seed.closing_issue(second, 999, 5)
        seed.repo_label(js, "LA_9", "stale")
        seed.failure(js, 7)
        seed.failure(js, 8, kind="issue")
        seed.pending(js, 2)
        seed.pending(js, 9, kind="issue")
    return {"js": js, "empty": empty, "first": first, "second": second}


def _pr(row_id: int, github_id: int, number: int, **values: Any) -> StoredPullRequest:
    row: dict[str, Any] = {
        "row_id": row_id,
        "github_id": github_id,
        "number": number,
        "state": "OPEN",
        "is_draft": False,
        "author_association": "MEMBER",
        "github_updated_at": UPDATED,
        "hydrated_at": HYDRATED,
        "hydration_version": 2,
        "closing_issues_total": 0,
        "labels": frozenset(),
        "closing_links": frozenset(),
        **values,
    }
    return StoredPullRequest(**row)


async def test_reads_every_stored_value_the_planner_compares(migrated_database: Path) -> None:
    ids = _seed(migrated_database)
    snapshot = await snapshot_of(migrated_database)
    assert snapshot == Snapshot(
        repositories=(
            StoredRepository(
                row_id=ids["js"],
                github_id=11,
                full_name="prebid/Prebid.js",
                is_tracked=True,
                pull_requests=(
                    _pr(ids["second"], 20, 2, closing_links=frozenset({(999, 5)})),
                    _pr(
                        ids["first"],
                        70,
                        7,
                        state="MERGED",
                        is_draft=True,
                        author_association="CONTRIBUTOR",
                        hydration_version=1,
                        closing_issues_total=2,
                        labels=frozenset({("LA_1", "bug"), ("LA_2", "feature")}),
                        closing_links=frozenset({(LINKED_REPO, 3), (LINKED_REPO, 4)}),
                    ),
                ),
                failures=frozenset({("pull_request", 7), ("issue", 8)}),
                pending=frozenset({("pull_request", 2), ("issue", 9)}),
            ),
            StoredRepository(
                row_id=ids["empty"],
                github_id=12,
                full_name="prebid/go-gdpr",
                is_tracked=False,
                pull_requests=(),
                failures=frozenset(),
                pending=frozenset(),
            ),
        )
    )
    assert snapshot.repository(11) is snapshot.repositories[0]
    assert snapshot.repository(13) is None
    assert snapshot.repositories[0].item_rows == 2
    assert all(
        value.tzinfo is not None
        for pr in snapshot.repositories[0].pull_requests
        for value in (pr.github_updated_at, pr.hydrated_at)
    )


async def test_an_empty_database(migrated_database: Path) -> None:
    assert await snapshot_of(migrated_database) == Snapshot(repositories=())


async def test_writes_nothing(migrated_database: Path) -> None:
    _seed(migrated_database)
    before = hashlib.sha256(migrated_database.read_bytes()).hexdigest()
    await snapshot_of(migrated_database)
    assert hashlib.sha256(migrated_database.read_bytes()).hexdigest() == before


class _CheckedConnection:
    """The reader's connection, checking after each statement returns, before its rows are
    read, that another connection can commit without waiting."""

    def __init__(self, connection: AsyncConnection, database: Path) -> None:
        self._connection = connection
        self._database = database
        self.reads = 0

    async def execute(self, statement: Any) -> Any:
        result = await self._connection.execute(statement)
        self.reads += 1
        commit_does_not_wait(self._database)
        return result


async def test_no_read_is_left_half_consumed(migrated_database: Path) -> None:
    _seed(migrated_database)
    engine = build_engine(f"sqlite+aiosqlite:///{migrated_database}")
    try:
        async with engine.connect() as connection:
            checked = _CheckedConnection(connection, migrated_database)
            await read_snapshot(cast("AsyncConnection", checked))
            commit_does_not_wait(migrated_database)
    finally:
        await engine.dispose()
    # labels, closing links, pull requests, failures, pending, repositories
    assert checked.reads == 6


async def test_the_lock_probe_sees_a_half_consumed_read(migrated_database: Path) -> None:
    """Positive control: a server-side cursor left half-read blocks the probe's commit."""
    _seed(migrated_database)
    engine = build_engine(f"sqlite+aiosqlite:///{migrated_database}")
    try:
        async with engine.connect() as connection:
            stream = await connection.stream(select(PullRequest.id))
            assert await stream.fetchone() is not None
            with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                commit_does_not_wait(migrated_database)
            await stream.close()
            commit_does_not_wait(migrated_database)
    finally:
        await engine.dispose()
