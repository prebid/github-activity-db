"""Run records: the head check, the claim and its lock, and the close (plan 5.3, 5.1)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError

from github_activity_db.config import Settings
from github_activity_db.db.schema import HEAD_REVISION, MIGRATION_REVISIONS
from github_activity_db.sync.connection import LocalDatabaseError, open_writer, transaction
from github_activity_db.sync.runs import (
    STALE_AFTER,
    ClaimRefused,
    CloseResult,
    HeadCheckError,
    IndexState,
    LastOutcome,
    RepositoryOutcome,
    RunCounters,
    RunStatus,
    check_head,
    claim_run,
    close_run,
    code_version,
    database_path,
    trigger,
)
from github_activity_db.tracking import read_tracking_config
from tests.data_dictionary import SCHEMA_LOCK
from tests.db.alembic_support import alembic_config, upgrade
from tests.db.seed import Seed, connect
from tests.sync.records import T0, fetch, ts

PACKAGED = read_tracking_config()
COUNTERS = RunCounters(
    graphql_points=12, rest_calls=3, items_planned=7, items_hydrated=6, failures=1
)


def _url(database: Path) -> str:
    return f"sqlite+aiosqlite:///{database}"


async def _claim(database: Path, **options: object) -> int:
    values: dict[str, object] = {
        "started_at": T0,
        "deadline_at": None,
        "break_lock": False,
        "config": PACKAGED,
        "trigger": "manual",
        "code_version": "abc123",
    }
    values.update(options)
    async with open_writer(_url(database)) as writer:
        claim = await claim_run(writer, **values)  # type: ignore[arg-type]
    return claim.run_id


# --------------------------------------------------------------------------------------------
# Head check
# --------------------------------------------------------------------------------------------
async def test_the_head_check_passes_at_head(migrated_database: Path) -> None:
    """The 2 values the check compares, read on a connection of the test's own, are the chain's
    head in ``alembic/versions`` and the newest schema version ``tests/schema_versions.json``
    records."""
    head = ScriptDirectory.from_config(alembic_config(migrated_database)).get_current_head()
    newest = max(int(version) for version in json.loads(SCHEMA_LOCK.read_text()))
    connection = sqlite3.connect(migrated_database)
    try:
        revisions = connection.execute("SELECT version_num FROM alembic_version").fetchall()
        (user_version,) = connection.execute("PRAGMA user_version").fetchone()
    finally:
        connection.close()
    assert (revisions, user_version) == ([(head,)], newest)
    await check_head(_url(migrated_database))


async def test_a_missing_path_is_refused_without_creating_it(tmp_path: Path) -> None:
    missing = tmp_path / "absent.db"
    with pytest.raises(HeadCheckError, match="database file not found"):
        await check_head(_url(missing))
    assert not missing.exists()


async def test_an_empty_file_is_refused(tmp_path: Path) -> None:
    empty = tmp_path / "empty.db"
    empty.touch()
    with pytest.raises(HeadCheckError, match="no migration revision"):
        await check_head(_url(empty))
    assert empty.stat().st_size == 0


async def test_an_older_revision_is_refused(tmp_path: Path) -> None:
    older = tmp_path / "older.db"
    upgrade(older, "33f8570f6540")
    with pytest.raises(
        HeadCheckError, match=f"revision 33f8570f6540, not the code's head {HEAD_REVISION}"
    ):
        await check_head(_url(older))


@pytest.mark.parametrize(
    "url", ["sqlite+aiosqlite://", "sqlite+aiosqlite:///:memory:", "postgresql://host/db"]
)
def test_a_url_without_a_file_is_refused(url: str) -> None:
    with pytest.raises(HeadCheckError):
        database_path(url)


async def test_the_claim_refuses_a_database_not_at_head(tmp_path: Path) -> None:
    """A file the migrations built to the revision before head, as the code before it did."""
    older = tmp_path / "older.db"
    upgrade(older, MIGRATION_REVISIONS[-2])
    with pytest.raises(
        HeadCheckError,
        match=f"revision {MIGRATION_REVISIONS[-2]}, not the code's head {HEAD_REVISION}",
    ):
        await _claim(older)
    assert fetch(older, "SELECT count(*) AS n FROM sync_runs") == [{"n": 0}]


async def test_the_claim_checks_the_head_inside_its_transaction(migrated_database: Path) -> None:
    """The statements the claim sends: the head reads come after its BEGIN IMMEDIATE and
    before its insert, so no migration can land between the check and the claim."""
    sent: list[str] = []

    def record(*args: Any) -> None:
        sent.append(" ".join(str(args[2]).split()))

    async with open_writer(_url(migrated_database)) as writer:
        event.listen(writer.sync_engine, "before_cursor_execute", record)
        await claim_run(
            writer,
            started_at=T0,
            deadline_at=None,
            break_lock=False,
            config=PACKAGED,
            trigger="manual",
            code_version="x",
        )
    begin = sent.index("BEGIN IMMEDIATE")
    revision = next(i for i, sql in enumerate(sent) if "FROM alembic_version" in sql)
    user_version = sent.index("PRAGMA user_version")
    insert = next(i for i, sql in enumerate(sent) if sql.startswith("INSERT INTO sync_runs"))
    assert begin < revision < insert
    assert begin < user_version < insert
    assert sent.count("BEGIN IMMEDIATE") == 1


# --------------------------------------------------------------------------------------------
# The claim
# --------------------------------------------------------------------------------------------
async def test_a_claim_inserts_the_run_and_copies_the_automation_lists(
    migrated_database: Path,
) -> None:
    deadline = T0 + timedelta(minutes=30)
    run_id = await _claim(migrated_database, deadline_at=deadline, trigger="schedule")
    (row,) = fetch(migrated_database, "SELECT * FROM sync_runs")
    assert row == {
        "id": run_id,
        "started_at": ts(T0),
        "deadline_at": ts(deadline),
        "finished_at": None,
        "status": "running",
        "exit_code": None,
        "trigger": "schedule",
        "code_version": "abc123",
        "config_source": "packaged",
        "config_sha256": PACKAGED.sha256,
        "graphql_points": 0,
        "rest_calls": 0,
        "items_planned": 0,
        "items_hydrated": 0,
        "items_deleted": 0,
        "failures": 0,
        "deletions_approved": None,
    }
    assert fetch(
        migrated_database,
        "SELECT github_id, login, reason, run_id FROM automation_accounts ORDER BY github_id",
    ) == [
        {
            "github_id": 81847,
            "login": "claude",
            "reason": "receives commits authored as noreply@anthropic.com",
            "run_id": run_id,
        },
        {
            "github_id": 99054218,
            "login": "prebidjs-release",
            "reason": "release automation",
            "run_id": run_id,
        },
    ]
    assert fetch(migrated_database, "SELECT email, reason, run_id FROM automation_emails") == [
        {
            "email": "noreply@anthropic.com",
            "reason": "Claude commits without a linked account",
            "run_id": run_id,
        }
    ]


async def test_the_config_digest_with_and_without_tracking_config_path(
    migrated_database: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file Settings reads, as a run reads it: the packaged one without the variable, the
    named one with it (gap 88)."""
    packaged = Path(__file__).resolve().parents[2] / "src" / "github_activity_db" / "tracking.toml"
    monkeypatch.delenv("TRACKING_CONFIG_PATH", raising=False)
    without = Settings(_env_file=None).tracking_config()
    assert (without.source, without.sha256) == (
        "packaged",
        hashlib.sha256(packaged.read_bytes()).hexdigest(),
    )
    other = tmp_path / "other.toml"
    other.write_bytes(packaged.read_bytes() + b"\n# a comment changes the digest\n")
    monkeypatch.setenv("TRACKING_CONFIG_PATH", str(other))
    custom = Settings(_env_file=None).tracking_config()
    assert custom.sha256 == hashlib.sha256(other.read_bytes()).hexdigest() != without.sha256
    assert custom.source == str(other)
    first = await _claim(migrated_database, config=without)
    with connect(migrated_database) as connection:
        connection.execute(text("UPDATE sync_runs SET status = 'complete'"))
    second = await _claim(migrated_database, config=custom, started_at=T0 + timedelta(hours=1))
    assert fetch(
        migrated_database, "SELECT id, config_source, config_sha256 FROM sync_runs ORDER BY id"
    ) == [
        {"id": first, "config_source": "packaged", "config_sha256": without.sha256},
        {"id": second, "config_source": str(other), "config_sha256": custom.sha256},
    ]


def _running(database: Path, deadline_at: object = None) -> int:
    with connect(database) as connection:
        return Seed(connection).sync_run(deadline_at=deadline_at)


@pytest.mark.parametrize(
    ("deadline", "blocks"),
    [
        (None, True),
        (T0 - STALE_AFTER + timedelta(microseconds=1), True),
        (T0 - STALE_AFTER, False),
        (T0 - STALE_AFTER - timedelta(hours=1), False),
        (T0 + timedelta(hours=1), True),
    ],
    ids=["no-deadline", "just-under-10-minutes-past", "10-minutes-past", "an-hour-later", "future"],
)
async def test_a_running_row_blocks_until_10_minutes_past_its_deadline(
    migrated_database: Path, deadline: object, blocks: bool
) -> None:
    other = _running(migrated_database, deadline)
    if blocks:
        with pytest.raises(ClaimRefused) as refused:
            await _claim(migrated_database)
        assert refused.value.blocking == (other,)
        assert fetch(migrated_database, "SELECT id, status FROM sync_runs") == [
            {"id": other, "status": "running"}
        ]
        return
    async with open_writer(_url(migrated_database)) as writer:
        claim = await claim_run(
            writer,
            started_at=T0,
            deadline_at=None,
            break_lock=False,
            config=PACKAGED,
            trigger="manual",
            code_version="x",
        )
    assert (claim.reclaimed, claim.broken) == ((other,), ())
    assert fetch(
        migrated_database, "SELECT id, status, exit_code, finished_at FROM sync_runs ORDER BY id"
    ) == [
        {"id": other, "status": "aborted", "exit_code": None, "finished_at": None},
        {"id": claim.run_id, "status": "running", "exit_code": None, "finished_at": None},
    ]


async def test_a_refused_claim_rolls_back_the_stale_reclaim(migrated_database: Path) -> None:
    stale = _running(migrated_database, T0 - timedelta(hours=1))
    blocking = _running(migrated_database, None)
    with pytest.raises(ClaimRefused) as refused:
        await _claim(migrated_database)
    assert refused.value.blocking == (blocking,)
    assert fetch(migrated_database, "SELECT id, status FROM sync_runs ORDER BY id") == [
        {"id": stale, "status": "running"},
        {"id": blocking, "status": "running"},
    ]
    assert fetch(migrated_database, "SELECT count(*) AS n FROM automation_accounts") == [{"n": 0}]


async def test_break_lock_aborts_the_blocking_row(migrated_database: Path) -> None:
    blocking = _running(migrated_database, None)
    async with open_writer(_url(migrated_database)) as writer:
        claim = await claim_run(
            writer,
            started_at=T0,
            deadline_at=None,
            break_lock=True,
            config=PACKAGED,
            trigger="manual",
            code_version="x",
        )
    assert (claim.reclaimed, claim.broken) == ((), (blocking,))
    assert fetch(migrated_database, "SELECT id, status, exit_code FROM sync_runs ORDER BY id") == [
        {"id": blocking, "status": "aborted", "exit_code": None},
        {"id": claim.run_id, "status": "running", "exit_code": None},
    ]


async def test_break_lock_without_a_blocking_row_proceeds(migrated_database: Path) -> None:
    async with open_writer(_url(migrated_database)) as writer:
        claim = await claim_run(
            writer,
            started_at=T0,
            deadline_at=None,
            break_lock=True,
            config=PACKAGED,
            trigger="manual",
            code_version="x",
        )
    assert (claim.reclaimed, claim.broken) == ((), ())


async def _busy_timeout(writer: Any) -> int:
    """The writer connection's ``PRAGMA busy_timeout``, in milliseconds."""
    async with transaction(writer):
        value: int = (await writer.exec_driver_sql("PRAGMA busy_timeout")).scalar_one()
    return value


async def test_a_lock_held_by_a_separate_connection(migrated_database: Path) -> None:
    """An uncommitted BEGIN IMMEDIATE elsewhere, held through the claim's busy timeout: the
    claim fails with SQLite's SQLITE_BUSY, inserting nothing (plan 5.3, section 7 "Run claim")."""
    holder = sqlite3.connect(migrated_database, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        async with open_writer(_url(migrated_database), busy_timeout_ms=300) as writer:
            with pytest.raises(LocalDatabaseError) as raised:
                await claim_run(
                    writer,
                    started_at=T0,
                    deadline_at=None,
                    break_lock=False,
                    config=PACKAGED,
                    trigger="manual",
                    code_version="x",
                )
            holder.execute("ROLLBACK")
            assert await _busy_timeout(writer) == 300
    finally:
        holder.close()
    cause = raised.value.__cause__
    assert isinstance(cause, DBAPIError)
    assert isinstance(cause.orig, sqlite3.Error)
    assert cause.orig.sqlite_errorname == "SQLITE_BUSY"
    assert fetch(migrated_database, "SELECT count(*) AS n FROM sync_runs") == [{"n": 0}]


async def test_a_claim_waits_for_a_claim_in_progress_then_refuses(migrated_database: Path) -> None:
    """Another claimant inserts its running row and commits once this claim sends its BEGIN
    IMMEDIATE, within the claim's 5 s busy timeout: the claim gets the lock after that commit,
    reads the row, and refuses (section 7, "Run claim"). Whether the commit lands before
    SQLite runs the BEGIN or while it waits, the claim reads the same committed row."""
    holding = threading.Event()
    begun = threading.Event()
    other: list[int] = []
    order: list[str] = []

    def claimant() -> None:
        connection = sqlite3.connect(migrated_database, isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "INSERT INTO sync_runs (started_at, status, trigger, code_version, config_source, "
                "config_sha256, graphql_points, rest_calls, items_planned, items_hydrated, "
                "items_deleted, failures) VALUES "
                "('2026-05-01 11:59:00.000000', 'running', 'manual', 'other', 'packaged', ?, "
                "0, 0, 0, 0, 0, 0)",
                ("0" * 64,),
            )
            assert cursor.lastrowid is not None
            other.append(cursor.lastrowid)
            holding.set()
            if begun.wait(5):
                order.append("commit")
                connection.execute("COMMIT")
        finally:
            connection.close()

    def beginning(*args: Any) -> None:
        if args[2] == "BEGIN IMMEDIATE" and not begun.is_set():
            order.append("begin")
            begun.set()

    thread = threading.Thread(target=claimant)
    thread.start()
    try:
        assert holding.wait(5)
        async with open_writer(_url(migrated_database), busy_timeout_ms=5000) as writer:
            event.listen(writer.sync_engine, "before_cursor_execute", beginning)
            with pytest.raises(ClaimRefused) as refused:
                await claim_run(
                    writer,
                    started_at=T0,
                    deadline_at=None,
                    break_lock=False,
                    config=PACKAGED,
                    trigger="manual",
                    code_version="x",
                )
            assert await _busy_timeout(writer) == 5000
    finally:
        begun.set()
        thread.join(5)
    assert not thread.is_alive()
    assert order == ["begin", "commit"]
    assert refused.value.blocking == tuple(other)
    assert fetch(migrated_database, "SELECT id, code_version FROM sync_runs") == [
        {"id": other[0], "code_version": "other"}
    ]


async def test_a_second_claim_replaces_the_automation_lists(
    migrated_database: Path, tmp_path: Path
) -> None:
    """An automation-list change shows in the views at the next claim, with no hydration."""
    with connect(migrated_database) as connection:
        seed = Seed(connection)
        seed.user("prebidjs-release", github_id=99054218, node_id="U_release")
        claude = seed.user("claude", github_id=81847, node_id="U_claude")
        repo = seed.repository()
        pr = seed.pull_request(repo, 1)
        seed.commit(pr, "s1", claude, author_email="NoReply@Anthropic.com")
    views = (
        "SELECT login, automation_reason FROM v_github_users ORDER BY login",
        "SELECT sha, account_automation_reason, email_automation_reason FROM v_commit_authors",
    )
    first = await _claim(migrated_database)
    assert fetch(migrated_database, views[0]) == [
        {
            "login": "claude",
            "automation_reason": "receives commits authored as noreply@anthropic.com",
        },
        {"login": "prebidjs-release", "automation_reason": "release automation"},
    ]
    assert fetch(migrated_database, views[1]) == [
        {
            "sha": "s1",
            "account_automation_reason": "receives commits authored as noreply@anthropic.com",
            "email_automation_reason": "Claude commits without a linked account",
        }
    ]
    async with open_writer(_url(migrated_database)) as writer:
        await close_run(
            writer,
            first,
            status="complete",
            exit_code=0,
            finished_at=T0,
            counters=COUNTERS,
            outcomes=(),
        )
    changed = tmp_path / "tracking.toml"
    body = (
        Path(__file__).resolve().parents[2] / "src" / "github_activity_db" / "tracking.toml"
    ).read_text()
    changed.write_text(
        body.replace('reason = "release automation"', 'reason = "publishes releases"').replace(
            '  { github_id = 81847, login = "claude", reason = "receives commits authored as '
            'noreply@anthropic.com" },\n',
            "",
        )
    )
    second = await _claim(
        migrated_database, config=read_tracking_config(changed), started_at=T0 + timedelta(hours=1)
    )
    assert fetch(
        migrated_database, "SELECT github_id, reason, run_id FROM automation_accounts"
    ) == [{"github_id": 99054218, "reason": "publishes releases", "run_id": second}]
    assert fetch(migrated_database, views[0]) == [
        {"login": "claude", "automation_reason": None},
        {"login": "prebidjs-release", "automation_reason": "publishes releases"},
    ]
    assert fetch(migrated_database, views[1]) == [
        {
            "sha": "s1",
            "account_automation_reason": None,
            "email_automation_reason": "Claude commits without a linked account",
        }
    ]


# --------------------------------------------------------------------------------------------
# The close
# --------------------------------------------------------------------------------------------
def _repositories(database: Path, *github_ids: int) -> dict[int, int]:
    with connect(database) as connection:
        seed = Seed(connection)
        return {
            github_id: seed.repository(f"prebid/r{github_id}", github_id=github_id)
            for github_id in github_ids
        }


async def _close(
    database: Path,
    run_id: int,
    *outcomes: RepositoryOutcome,
    status: RunStatus = "complete",
    exit_code: int = 0,
) -> CloseResult:
    async with open_writer(_url(database)) as writer:
        return await close_run(
            writer,
            run_id,
            status=status,
            exit_code=exit_code,
            finished_at=T0 + timedelta(hours=2),
            counters=COUNTERS,
            outcomes=outcomes,
        )


async def test_the_close_writes_the_run(migrated_database: Path) -> None:
    run_id = await _claim(migrated_database)
    result = await _close(migrated_database, run_id, status="time_limited", exit_code=2)
    assert result == CloseResult(closed=True, status="time_limited", exit_code=2)
    (row,) = fetch(
        migrated_database,
        "SELECT status, exit_code, finished_at, graphql_points, rest_calls, items_planned, "
        "items_hydrated, failures, items_deleted FROM sync_runs",
    )
    assert row == {
        "status": "time_limited",
        "exit_code": 2,
        "finished_at": ts(T0 + timedelta(hours=2)),
        "graphql_points": 12,
        "rest_calls": 3,
        "items_planned": 7,
        "items_hydrated": 6,
        "failures": 1,
        "items_deleted": 0,
    }


async def test_a_close_writes_nothing_when_another_process_aborted_the_row(
    migrated_database: Path,
) -> None:
    ids = _repositories(migrated_database, 11)
    run_id = await _claim(migrated_database)
    with connect(migrated_database) as connection:
        connection.execute(text("UPDATE sync_runs SET status = 'aborted'"))
    result = await _close(migrated_database, run_id, RepositoryOutcome(11, "complete"))
    assert result == CloseResult(closed=False, status="aborted", exit_code=None)
    assert fetch(migrated_database, "SELECT status, exit_code, finished_at FROM sync_runs") == [
        {"status": "aborted", "exit_code": None, "finished_at": None}
    ]
    assert fetch(migrated_database, "SELECT count(*) AS n FROM repo_sync_state") == [{"n": 0}]
    assert ids


async def test_a_second_close_reads_the_first(migrated_database: Path) -> None:
    """An interrupt arriving after this run's own close committed finds that close."""
    run_id = await _claim(migrated_database)
    await _close(migrated_database, run_id, status="complete", exit_code=0)
    result = await _close(migrated_database, run_id, status="aborted", exit_code=130)
    assert result == CloseResult(closed=False, status="complete", exit_code=0)


async def test_outcomes_write_what_they_carry(migrated_database: Path) -> None:
    ids = _repositories(migrated_database, 11, 12, 13)
    previous = await _claim(migrated_database)
    with connect(migrated_database) as connection:
        for github_id in (11, 12, 13):
            connection.execute(
                text(
                    "INSERT INTO repo_sync_state (repository_id, last_index_started_at, "
                    "index_pr_total, last_success_at, last_run_id, last_outcome, halt_reason) "
                    "VALUES "
                    "(:r, '2026-01-01 00:00:00.000000', 5, '2026-01-02 00:00:00.000000', :run, "
                    "'halted', 'old halt')"
                ),
                {"r": ids[github_id], "run": previous},
            )
            connection.execute(
                text(
                    "INSERT INTO sync_pending (repository_id, item_kind, number, run_id) VALUES "
                    "(:r, 'pull_request', 1, :run), (:r, 'issue', 1, :run)"
                ),
                {"r": ids[github_id], "run": previous},
            )
        connection.execute(text("UPDATE sync_runs SET status = 'complete'"))
    run_id = await _claim(migrated_database, started_at=T0 + timedelta(hours=1))
    index = IndexState(started_at=T0 + timedelta(minutes=59), pr_total=40)
    result = await _close(
        migrated_database,
        run_id,
        RepositoryOutcome(
            11,
            "complete",
            index=index,
            step_7_ran=True,
            halt_reason=None,
            pending=(),
            succeeded=True,
        ),
        RepositoryOutcome(
            12, "halted", index=index, step_7_ran=True, halt_reason="30 candidates", pending=(7, 3)
        ),
        RepositoryOutcome(13, "failed"),
        status="time_limited",
        exit_code=2,
    )
    assert result.closed
    finished = ts(T0 + timedelta(hours=2))
    rows = {
        row.pop("repository_id"): row
        for row in fetch(
            migrated_database,
            "SELECT repository_id, last_index_started_at, index_pr_total, last_success_at, "
            "last_run_id, last_outcome, halt_reason FROM repo_sync_state",
        )
    }
    assert rows[ids[11]] == {
        "last_index_started_at": ts(index.started_at),
        "index_pr_total": 40,
        "last_success_at": finished,
        "last_run_id": run_id,
        "last_outcome": "complete",
        "halt_reason": None,
    }
    assert rows[ids[12]] == {
        "last_index_started_at": ts(index.started_at),
        "index_pr_total": 40,
        "last_success_at": "2026-01-02 00:00:00.000000",
        "last_run_id": run_id,
        "last_outcome": "halted",
        "halt_reason": "30 candidates",
    }
    assert rows[ids[13]] == {
        "last_index_started_at": "2026-01-01 00:00:00.000000",
        "index_pr_total": 5,
        "last_success_at": "2026-01-02 00:00:00.000000",
        "last_run_id": run_id,
        "last_outcome": "failed",
        "halt_reason": "old halt",
    }
    pending = fetch(
        migrated_database,
        "SELECT repository_id, item_kind, number, run_id FROM sync_pending "
        "ORDER BY repository_id, item_kind, number",
    )
    assert pending == [
        {"repository_id": ids[11], "item_kind": "issue", "number": 1, "run_id": previous},
        {"repository_id": ids[12], "item_kind": "issue", "number": 1, "run_id": previous},
        {"repository_id": ids[12], "item_kind": "pull_request", "number": 3, "run_id": run_id},
        {"repository_id": ids[12], "item_kind": "pull_request", "number": 7, "run_id": run_id},
        {"repository_id": ids[13], "item_kind": "issue", "number": 1, "run_id": previous},
        {"repository_id": ids[13], "item_kind": "pull_request", "number": 1, "run_id": previous},
    ]


async def test_an_outcome_for_a_repository_no_longer_stored_is_skipped(
    migrated_database: Path,
) -> None:
    _repositories(migrated_database, 11)
    run_id = await _claim(migrated_database)
    result = await _close(
        migrated_database,
        run_id,
        RepositoryOutcome(11, "untracked"),
        RepositoryOutcome(99, "untracked"),
    )
    assert result == CloseResult(closed=True, status="complete", exit_code=0, skipped=(99,))
    assert fetch(migrated_database, "SELECT last_outcome FROM repo_sync_state") == [
        {"last_outcome": "untracked"}
    ]


async def test_a_fresh_repository_row_has_only_what_the_outcome_carries(
    migrated_database: Path,
) -> None:
    ids = _repositories(migrated_database, 11)
    run_id = await _claim(migrated_database)
    await _close(
        migrated_database, run_id, RepositoryOutcome(11, "failed"), status="failed", exit_code=1
    )
    assert fetch(migrated_database, "SELECT * FROM repo_sync_state") == [
        {
            "repository_id": ids[11],
            "last_index_started_at": None,
            "index_pr_total": None,
            "index_issue_total": None,
            "last_success_at": None,
            "last_run_id": run_id,
            "last_outcome": "failed",
            "halt_reason": None,
        }
    ]


async def test_a_failed_close_keeps_step_7_s_result(migrated_database: Path) -> None:
    """A run fatal after step 7: its halt, or a clearing, is written beside last_outcome failed,
    and the index, pending, and success columns stay (5.1 halt_reason, 5.3 step 8)."""
    ids = _repositories(migrated_database, 11, 12)
    previous = await _claim(migrated_database)
    with connect(migrated_database) as connection:
        for github_id in (11, 12):
            connection.execute(
                text(
                    "INSERT INTO repo_sync_state (repository_id, last_index_started_at, "
                    "index_pr_total, last_success_at, last_run_id, last_outcome, halt_reason) "
                    "VALUES (:r, '2026-01-01 00:00:00.000000', 5, '2026-01-02 00:00:00.000000', "
                    ":run, 'halted', 'old halt')"
                ),
                {"r": ids[github_id], "run": previous},
            )
            connection.execute(
                text(
                    "INSERT INTO sync_pending (repository_id, item_kind, number, run_id) VALUES "
                    "(:r, 'pull_request', 4, :run), (:r, 'issue', 4, :run)"
                ),
                {"r": ids[github_id], "run": previous},
            )
        connection.execute(text("UPDATE sync_runs SET status = 'complete'"))
    pending = "SELECT * FROM sync_pending ORDER BY repository_id, item_kind"
    before = fetch(migrated_database, pending)
    assert len(before) == 4
    run_id = await _claim(migrated_database, started_at=T0 + timedelta(hours=1))
    result = await _close(
        migrated_database,
        run_id,
        RepositoryOutcome(11, "failed", step_7_ran=True, halt_reason="40 candidates"),
        RepositoryOutcome(12, "failed", step_7_ran=True, halt_reason=None),
        status="failed",
        exit_code=1,
    )
    assert result == CloseResult(closed=True, status="failed", exit_code=1)
    assert fetch(
        migrated_database,
        "SELECT repository_id, last_index_started_at, index_pr_total, last_success_at, "
        "last_run_id, last_outcome, halt_reason FROM repo_sync_state ORDER BY repository_id",
    ) == [
        {
            "repository_id": ids[github_id],
            "last_index_started_at": "2026-01-01 00:00:00.000000",
            "index_pr_total": 5,
            "last_success_at": "2026-01-02 00:00:00.000000",
            "last_run_id": run_id,
            "last_outcome": "failed",
            "halt_reason": halt_reason,
        }
        for github_id, halt_reason in ((11, "40 candidates"), (12, None))
    ]
    assert fetch(migrated_database, pending) == before


@pytest.mark.parametrize(
    ("status", "outcome", "pending"),
    [
        ("complete", "complete", ()),
        ("complete", "halted", ()),
        ("time_limited", "time_limited", (6,)),
        ("time_limited", "halted", (6,)),
    ],
    ids=["complete", "halted", "time-limited", "time-limited-run-halted"],
)
async def test_a_completely_indexed_repository_writes_its_index_and_pending_items(
    migrated_database: Path, status: RunStatus, outcome: LastOutcome, pending: tuple[int, ...]
) -> None:
    """Each outcome 5.3 step 8 gives a completely indexed repository, with the pending items a
    run of that status leaves, replacing an earlier run's: none when it reached them all."""
    ids = _repositories(migrated_database, 11)
    with connect(migrated_database) as connection:
        earlier = Seed(connection).sync_run(status="complete")
        connection.execute(
            text(
                "INSERT INTO sync_pending (repository_id, item_kind, number, run_id) "
                "VALUES (:r, 'pull_request', 2, :run)"
            ),
            {"r": ids[11], "run": earlier},
        )
    run_id = await _claim(migrated_database)
    index = IndexState(started_at=T0 + timedelta(minutes=5), pr_total=9)
    await _close(
        migrated_database,
        run_id,
        RepositoryOutcome(11, outcome, index=index, pending=pending),
        status=status,
        exit_code=2,
    )
    assert fetch(
        migrated_database,
        "SELECT last_index_started_at, index_pr_total, last_outcome FROM repo_sync_state",
    ) == [
        {
            "last_index_started_at": ts(index.started_at),
            "index_pr_total": 9,
            "last_outcome": outcome,
        }
    ]
    assert fetch(migrated_database, "SELECT repository_id, number, run_id FROM sync_pending") == [
        {"repository_id": ids[11], "number": number, "run_id": run_id} for number in pending
    ]


@pytest.mark.parametrize(
    ("status", "outcomes"),
    [
        ("complete", (RepositoryOutcome(11, "halted", succeeded=True),)),
        ("complete", (RepositoryOutcome(11, "complete"), RepositoryOutcome(11, "failed"))),
        ("failed", (RepositoryOutcome(11, "complete"),)),
        ("aborted", (RepositoryOutcome(11, "time_limited"),)),
        ("failed", (RepositoryOutcome(11, "failed", index=IndexState(T0, 5)),)),
        ("aborted", (RepositoryOutcome(11, "failed", pending=(4,)),)),
        ("aborted", (RepositoryOutcome(11, "failed", pending=()),)),
        ("failed", (RepositoryOutcome(11, "complete", succeeded=True),)),
        ("complete", (RepositoryOutcome(11, "failed", index=IndexState(T0, 5)),)),
        ("time_limited", (RepositoryOutcome(11, "incomplete_index", pending=(4,)),)),
        ("complete", (RepositoryOutcome(11, "untracked", pending=()),)),
    ],
    ids=[
        "success-without-complete",
        "a-repository-twice",
        "failed-close-complete",
        "aborted-close-time-limited",
        "failed-close-index",
        "aborted-close-pending",
        "aborted-close-no-pending",
        "failed-close-success",
        "failed-with-an-index",
        "incomplete-index-with-pending",
        "untracked-with-no-pending",
    ],
)
async def test_invalid_outcomes_are_refused_before_writing(
    migrated_database: Path, status: RunStatus, outcomes: tuple[RepositoryOutcome, ...]
) -> None:
    _repositories(migrated_database, 11)
    run_id = await _claim(migrated_database)
    with pytest.raises(ValueError):
        await _close(migrated_database, run_id, *outcomes, status=status, exit_code=1)
    assert fetch(migrated_database, "SELECT status FROM sync_runs") == [{"status": "running"}]
    assert fetch(migrated_database, "SELECT count(*) AS n FROM repo_sync_state") == [{"n": 0}]


# --------------------------------------------------------------------------------------------
# trigger and code_version
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ("schedule", "schedule"),
        ("workflow_dispatch", "workflow_dispatch"),
        ("push", "manual"),
        (None, "manual"),
    ],
)
def test_trigger(event: str | None, expected: str) -> None:
    assert trigger({} if event is None else {"GITHUB_EVENT_NAME": event}) == expected


GIT = shutil.which("git")


@pytest.fixture
def git_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """git reads no configuration and finds no repository outside ``tmp_path``, and commits at a
    fixed date. ``code_version`` runs git with this process's environment, so the variables are
    set here too, and every other ``GIT_*`` variable is removed; the test's own git calls get
    the returned environment, built from these values and ``PATH`` alone."""
    isolated = {
        "HOME": str(tmp_path),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CEILING_DIRECTORIES": str(tmp_path),
    }
    for name in [name for name in os.environ if name.startswith("GIT_")]:
        monkeypatch.delenv(name)
    for name, value in isolated.items():
        monkeypatch.setenv(name, value)
    fixed = "2026-01-01T00:00:00+00:00"
    return {
        "PATH": os.environ["PATH"],
        **isolated,
        "GIT_AUTHOR_DATE": fixed,
        "GIT_COMMITTER_DATE": fixed,
    }


def _git(directory: Path, env: dict[str, str], *args: str) -> str:
    assert GIT is not None, "git is not installed"
    result = subprocess.run(
        [GIT, "-C", str(directory), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def checkout(tmp_path: Path, git_env: dict[str, str]) -> Path:
    repository = tmp_path / "checkout"
    package = repository / "github_activity_db"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (repository / ".gitignore").write_text("venv/\n")
    _git(repository, git_env, "init", "-q")
    _git(repository, git_env, "add", ".")
    _git(
        repository,
        git_env,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "commit",
        "-q",
        "-m",
        "init",
    )
    return repository


def test_code_version_prefers_github_sha(checkout: Path) -> None:
    assert code_version(checkout / "github_activity_db", {"GITHUB_SHA": "f00"}) == "f00"


def test_code_version_names_the_checkout_commit(checkout: Path, git_env: dict[str, str]) -> None:
    head = _git(checkout, git_env, "rev-parse", "HEAD")
    package = checkout / "github_activity_db"
    assert code_version(package, {}) == head
    (checkout / "untracked.txt").write_text("x")
    assert code_version(package, {}) == head
    (package / "__init__.py").write_text("# changed\n")
    assert code_version(package, {}) == f"{head}-dirty"


def test_code_version_without_git_is_the_package_version(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from importlib import metadata

    monkeypatch.setenv("PATH", "")
    assert shutil.which("git") is None
    assert code_version(checkout / "github_activity_db", {}) == metadata.version(
        "github-activity-db"
    )


def test_code_version_outside_a_checkout_is_the_package_version(
    tmp_path: Path, checkout: Path
) -> None:
    from importlib import metadata

    alone = tmp_path / "alone" / "github_activity_db"
    alone.mkdir(parents=True)
    (alone / "__init__.py").write_text("")
    assert code_version(alone, {}) == metadata.version("github-activity-db")
    ignored = checkout / "venv" / "github_activity_db"
    shutil.copytree(alone, ignored)
    assert code_version(ignored, {}) == metadata.version("github-activity-db")


async def test_another_user_version_is_refused(migrated_database: Path) -> None:
    connection = sqlite3.connect(migrated_database)
    connection.execute("PRAGMA user_version = 1")
    connection.close()
    with pytest.raises(HeadCheckError, match="user_version is 1, not schema_version 2"):
        await check_head(_url(migrated_database))
