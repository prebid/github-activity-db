"""The sync's database connections (redesign plan 5.3 steps 6-8).

The writer holds one connection for the run (5.3 step 6). Every statement on it, reads included,
runs inside ``transaction()``: one ``BEGIN IMMEDIATE ... COMMIT``. A statement outside it would
start SQLAlchemy's autobegin, whose ``BEGIN IMMEDIATE`` keeps the write lock until something
commits, so ``transaction()`` refuses to start inside an open transaction. Readers that must take
no lock (``--dry-run``, the head check, step 7's re-read) use ``open_reader``: through the driver's
own transaction handling a ``SELECT`` starts no transaction, and a fully fetched statement leaves no
lock behind (gap 77).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final

from sqlalchemy import event, select
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from github_activity_db.db.engine import build_engine
from github_activity_db.db.models import SyncRun


class LocalDatabaseError(Exception):
    """The database raised an error while the writer wrote (plan 5.3 step 4): fatal for the run.

    The writer connection is invalidated first: a ``COMMIT`` that failed with "database is
    locked" leaves SQLite's transaction open, which a close on the same DBAPI connection could not
    get past. The next statement on the connection opens a fresh one.
    """


class RunAborted(Exception):
    """The run's ``sync_runs`` row is no longer ``running``: another process aborted it
    (``--break-lock`` or a stale reclaim). The transaction committed nothing."""

    def __init__(self, run_id: int, status: str | None) -> None:
        super().__init__(f"run {run_id} is {status or 'not recorded'}, not running")
        self.run_id = run_id
        self.status = status


# Marks a writer connection in its record's info: only there does an open transaction hold the
# write lock.
_WRITER: Final = "sync_writer"


def _writer_connect(dbapi_connection: Any, record: Any, busy_timeout_ms: int | None) -> None:
    record.info[_WRITER] = True
    if busy_timeout_ms is not None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        finally:
            cursor.close()
    # The driver issues no BEGIN of its own; transaction() begins each one explicitly.
    dbapi_connection.isolation_level = None


def _begin_immediate(connection: Connection) -> None:
    connection.exec_driver_sql("BEGIN IMMEDIATE")


def writer_engine(database_url: str, *, busy_timeout_ms: int | None = None) -> AsyncEngine:
    """The application engine with each transaction a ``BEGIN IMMEDIATE``.

    ``busy_timeout_ms`` overrides the application's 30 s for tests.
    """
    engine = build_engine(database_url)
    event.listen(
        engine.sync_engine,
        "connect",
        lambda dbapi_connection, record: _writer_connect(dbapi_connection, record, busy_timeout_ms),
    )
    event.listen(engine.sync_engine, "begin", _begin_immediate)
    return engine


@asynccontextmanager
async def open_writer(
    database_url: str, *, busy_timeout_ms: int | None = None
) -> AsyncIterator[AsyncConnection]:
    """The writer's one connection for the run."""
    engine = writer_engine(database_url, busy_timeout_ms=busy_timeout_ms)
    try:
        async with engine.connect() as connection:
            yield connection
    finally:
        await engine.dispose()


@asynccontextmanager
async def open_reader(database_url: str) -> AsyncIterator[AsyncConnection]:
    """A connection that takes no lock between statements, for reads only."""
    engine = build_engine(database_url)
    try:
        async with engine.connect() as connection:
            yield connection
    finally:
        await engine.dispose()


@asynccontextmanager
async def transaction(
    connection: AsyncConnection, run_id: int | None = None
) -> AsyncIterator[AsyncConnection]:
    """One ``BEGIN IMMEDIATE ... COMMIT`` on the writer connection.

    With ``run_id``, the transaction first checks that the run's row is still ``running``, so a
    run another process aborted writes nothing more (``RunAborted``). An exception rolls the
    transaction back; a database error, a failed ``COMMIT`` included, invalidates the connection
    and becomes ``LocalDatabaseError``.

    On a reader connection a transaction the driver never began (a bare read's autobegin)
    holds nothing and is ended first.

    Raises:
        RuntimeError: the writer connection is already inside a transaction.
    """
    if connection.in_transaction():
        if connection.info.get(_WRITER):
            raise RuntimeError("the writer connection is already inside a transaction")
        await connection.commit()
    try:
        async with connection.begin():
            if run_id is not None:
                status = (
                    await connection.execute(select(SyncRun.status).where(SyncRun.id == run_id))
                ).scalar_one_or_none()
                if status != "running":
                    raise RunAborted(run_id, status)
            yield connection
    except DBAPIError as e:
        await connection.invalidate()
        raise LocalDatabaseError(f"{type(e.orig).__name__}: {e.orig}") from e
