"""The writer's connection: one BEGIN IMMEDIATE per transaction, no lock between them, a run
guard, and a database error that leaves the connection usable (plan 5.3 steps 4 and 6)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import insert, select, text, update

from github_activity_db.db.models import SyncRun
from github_activity_db.sync import connection as connection_module
from github_activity_db.sync.connection import (
    LocalDatabaseError,
    RunAborted,
    open_reader,
    open_writer,
    transaction,
)
from github_activity_db.sync.snapshot import read_snapshot
from tests.db.seed import Seed, connect
from tests.sync.records import fetch
from tests.sync.support import commit_does_not_wait


def _url(database: Path) -> str:
    return f"sqlite+aiosqlite:///{database}"


def _run(database: Path, status: str = "running") -> int:
    with connect(database) as connection:
        return Seed(connection).sync_run(status=status)


def _write_is_refused(database: Path) -> bool:
    """Whether another connection's BEGIN IMMEDIATE is refused at once (a lock is held)."""
    other = sqlite3.connect(database, timeout=0, isolation_level=None)
    try:
        other.execute("BEGIN IMMEDIATE")
        other.execute("ROLLBACK")
        return False
    except sqlite3.OperationalError as e:
        assert "locked" in str(e)
        return True
    finally:
        other.close()


async def test_a_transaction_holds_the_write_lock_and_releases_it(migrated_database: Path) -> None:
    run_id = _run(migrated_database)
    async with open_writer(_url(migrated_database)) as writer:
        assert not _write_is_refused(migrated_database)
        async with transaction(writer, run_id):
            assert _write_is_refused(migrated_database)
        assert not writer.in_transaction()
        assert not _write_is_refused(migrated_database)
        commit_does_not_wait(migrated_database)


async def test_a_read_in_a_transaction_leaves_no_lock(migrated_database: Path) -> None:
    _run(migrated_database)
    async with open_writer(_url(migrated_database)) as writer:
        async with transaction(writer):
            await writer.execute(select(SyncRun.id))
        assert not _write_is_refused(migrated_database)
        async with transaction(writer):
            await writer.execute(update(SyncRun).values(rest_calls=1))
    assert fetch(migrated_database, "SELECT rest_calls FROM sync_runs") == [{"rest_calls": 1}]


async def test_a_statement_outside_a_transaction_is_refused_by_the_next(
    migrated_database: Path,
) -> None:
    """A bare read autobegins a BEGIN IMMEDIATE and keeps the lock: transaction() refuses."""
    _run(migrated_database)
    async with open_writer(_url(migrated_database)) as writer:
        await writer.execute(select(SyncRun.id))
        assert _write_is_refused(migrated_database)
        with pytest.raises(RuntimeError, match="already inside a transaction"):
            async with transaction(writer):
                pass
        await writer.rollback()
        assert not _write_is_refused(migrated_database)


@pytest.mark.parametrize("status", ["aborted", "complete", None])
async def test_a_run_that_is_not_running_writes_nothing(
    migrated_database: Path, status: str | None
) -> None:
    run_id = _run(migrated_database, status or "running")
    if status is None:
        with connect(migrated_database) as connection:
            connection.execute(text("DELETE FROM sync_runs"))
    async with open_writer(_url(migrated_database)) as writer:
        with pytest.raises(RunAborted) as raised:
            async with transaction(writer, run_id):
                await writer.execute(update(SyncRun).values(rest_calls=99))
        assert raised.value.status == status
        assert not writer.in_transaction()
    assert fetch(
        migrated_database, "SELECT count(*) AS n FROM sync_runs WHERE rest_calls = 99"
    ) == [{"n": 0}]


async def test_the_body_s_exception_rolls_back(migrated_database: Path) -> None:
    run_id = _run(migrated_database)
    async with open_writer(_url(migrated_database)) as writer:
        with pytest.raises(KeyError):
            async with transaction(writer, run_id):
                await writer.execute(update(SyncRun).values(rest_calls=7))
                raise KeyError("planted")
    assert fetch(migrated_database, "SELECT rest_calls FROM sync_runs") == [{"rest_calls": 0}]


async def test_a_constraint_failure_is_a_local_database_error(migrated_database: Path) -> None:
    run_id = _run(migrated_database)
    async with open_writer(_url(migrated_database)) as writer:
        with pytest.raises(LocalDatabaseError, match="IntegrityError"):
            async with transaction(writer, run_id):
                await writer.execute(update(SyncRun).values(rest_calls=5))
                await writer.execute(insert(SyncRun).values(status="bogus"))
        async with transaction(writer, run_id):
            rest_calls = (await writer.execute(select(SyncRun.rest_calls))).scalar_one()
    assert rest_calls == 0


async def test_a_commit_that_fails_on_a_lock_leaves_the_connection_usable(
    migrated_database: Path,
) -> None:
    """A reader holding SHARED past the busy timeout fails the COMMIT; SQLite then keeps the
    transaction open, which the invalidated connection leaves behind: the next transaction runs
    on a fresh one once the reader has gone, and nothing of the failed one was written."""
    run_id = _run(migrated_database)
    async with open_writer(_url(migrated_database), busy_timeout_ms=200) as writer:
        reader = sqlite3.connect(migrated_database, isolation_level=None)
        reader.execute("BEGIN")
        assert reader.execute("SELECT count(*) FROM sync_runs").fetchone() == (1,)
        try:
            with pytest.raises(LocalDatabaseError, match="database is locked"):
                async with transaction(writer, run_id):
                    await writer.execute(update(SyncRun).values(rest_calls=3))
        finally:
            reader.execute("ROLLBACK")
            reader.close()
        async with transaction(writer, run_id):
            await writer.execute(update(SyncRun).values(items_planned=4))
    assert fetch(migrated_database, "SELECT rest_calls, items_planned FROM sync_runs") == [
        {"rest_calls": 0, "items_planned": 4}
    ]


async def test_the_writer_opens_one_connection_for_many_transactions(
    migrated_database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _run(migrated_database)
    opened: list[object] = []
    original = connection_module._writer_connect

    def counting(dbapi_connection: object, record: object, busy_timeout_ms: int | None) -> None:
        opened.append(dbapi_connection)
        original(dbapi_connection, record, busy_timeout_ms)

    monkeypatch.setattr(connection_module, "_writer_connect", counting)
    async with open_writer(_url(migrated_database)) as writer:
        for value in range(5):
            async with transaction(writer, run_id):
                await writer.execute(update(SyncRun).values(rest_calls=value))
    assert len(opened) == 1


async def test_a_reader_takes_no_lock(migrated_database: Path) -> None:
    _run(migrated_database)
    async with open_reader(_url(migrated_database)) as reader:
        await read_snapshot(reader)
        assert not _write_is_refused(migrated_database)
        async with transaction(reader):
            await reader.execute(select(SyncRun.id))
        assert not _write_is_refused(migrated_database)
