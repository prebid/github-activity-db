"""The snapshot reader: what it reads, that it writes nothing, that it holds no lock (gap 77)."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import Executable, event, select, update
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
from tests.sync.records import fetch
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
                repo_group="Client",
                is_archived=False,
                default_branch="master",
                labels=frozenset({("LA_9", "stale")}),
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
                repo_group="Client",
                is_archived=False,
                default_branch="master",
                labels=frozenset(),
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


async def _read_then_commit(database: Path, write: Executable | None = None) -> Snapshot:
    """Run ``read_snapshot`` on an application connection, then ``write`` when given, then
    commit, so a write either made would land in the file."""
    engine = build_engine(f"sqlite+aiosqlite:///{database}")
    try:
        async with engine.connect() as connection:
            snapshot = await read_snapshot(connection)
            if write is not None:
                await connection.execute(write)
            await connection.commit()
    finally:
        await engine.dispose()
    return snapshot


def _pages(database: Path) -> int:
    """The file's size in bytes as SQLite counts it: its pages times the page size."""
    connection = sqlite3.connect(database)
    try:
        (count,) = connection.execute("PRAGMA page_count").fetchone()
        (size,) = connection.execute("PRAGMA page_size").fetchone()
    finally:
        connection.close()
    return int(count) * int(size)


async def test_writes_nothing(migrated_database: Path) -> None:
    """Every byte of the file is unchanged after the reader's statements and a commit, over a
    file whose every page is compared and whose pull requests the reader read."""
    _seed(migrated_database)
    before = migrated_database.read_bytes()
    snapshot = await _read_then_commit(migrated_database)
    assert migrated_database.read_bytes() == before
    assert len(before) == _pages(migrated_database)
    assert (
        sum(repository.item_rows for repository in snapshot.repositories)
        == fetch(migrated_database, "SELECT count(*) AS n FROM pull_requests")[0]["n"]
    )


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
    """After each statement the reader sends, and after it returns, another connection commits
    at once; the statements, one per table, are counted as they reach the database."""
    _seed(migrated_database)
    sent: list[str] = []

    def record(connection: Any, cursor: Any, statement: str, *args: Any) -> None:
        sent.append(statement)

    engine = build_engine(f"sqlite+aiosqlite:///{migrated_database}")
    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        async with engine.connect() as connection:
            checked = _CheckedConnection(connection, migrated_database)
            await read_snapshot(cast("AsyncConnection", checked))
            commit_does_not_wait(migrated_database)
    finally:
        await engine.dispose()
    tables = [re.search(r"\bFROM (\w+)", statement) for statement in sent]
    assert checked.reads == len(sent) == 7
    assert all(statement.startswith("SELECT ") for statement in sent)
    assert sorted(table.group(1) for table in tables if table is not None) == [
        "pr_closing_issues",
        "pr_labels",
        "pull_requests",
        "repo_labels",
        "repositories",
        "sync_failures",
        "sync_pending",
    ]


async def test_the_lock_probe_refuses_while_another_connection_reads(
    migrated_database: Path,
) -> None:
    """The probe's commit is refused at once while another process holds a read open, and
    succeeds once it ends."""
    _seed(migrated_database)
    reader = sqlite3.connect(migrated_database, isolation_level=None)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM sync_runs").fetchone()
        with pytest.raises(sqlite3.OperationalError) as raised:
            commit_does_not_wait(migrated_database)
        assert raised.value.sqlite_errorname == "SQLITE_BUSY"
        reader.execute("ROLLBACK")
    finally:
        reader.close()
    commit_does_not_wait(migrated_database)


async def test_the_digest_sees_a_committed_write(migrated_database: Path) -> None:
    """Positive control for ``test_writes_nothing``: through the same helper, an UPDATE sent
    after the reader's statements is committed, and the file's bytes change."""
    _seed(migrated_database)
    query = "SELECT number, is_draft FROM pull_requests ORDER BY number"
    assert fetch(migrated_database, query) == [
        {"number": 2, "is_draft": 0},
        {"number": 7, "is_draft": 1},
    ]
    before = migrated_database.read_bytes()
    await _read_then_commit(migrated_database, update(PullRequest).values(is_draft=False))
    assert migrated_database.read_bytes() != before
    assert fetch(migrated_database, query) == [
        {"number": 2, "is_draft": 0},
        {"number": 7, "is_draft": 0},
    ]


async def test_the_lock_probe_sees_a_half_consumed_read(migrated_database: Path) -> None:
    """Positive control: a server-side cursor left half-read blocks the probe's commit."""
    _seed(migrated_database)
    engine = build_engine(f"sqlite+aiosqlite:///{migrated_database}")
    try:
        async with engine.connect() as connection:
            stream = await connection.stream(select(PullRequest.id))
            assert await stream.fetchone() is not None
            with pytest.raises(sqlite3.OperationalError) as raised:
                commit_does_not_wait(migrated_database)
            assert raised.value.sqlite_errorname == "SQLITE_BUSY"
            await stream.close()
            commit_does_not_wait(migrated_database)
    finally:
        await engine.dispose()
