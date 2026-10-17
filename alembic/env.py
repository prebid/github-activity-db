"""Alembic environment: SQLite migrations, each upgrade one transaction (redesign plan 5.1).

- Online only. Offline ``--sql`` mode is refused, because the checks below need a live
  connection.
- Only SQLite is migrated. The tracked ``github_activity.db`` is refused while D26 freezes it
  (gap 73): the connect listener asks SQLite which file it opened, so no spelling of the path
  or URL reaches it, and it also refuses any file named ``github_activity.db``, such as another
  checkout's copy; a path of that name is refused before SQLite would create it.
- Foreign keys are off while migrating: batch mode drops and recreates tables, and an enforced
  drop cascades into child rows. The connect listener, which runs before SQLAlchemy's own
  connect handling, sets the pragma, which has no effect inside a transaction, and refuses the
  connection unless it then reads 0 and no transaction is open. Only then does it turn off the
  driver's own transaction handling, because that commits a transaction already open on the
  connection.
- An upgrade is one transaction (gap 70): a ``begin`` listener emits ``BEGIN IMMEDIATE`` and
  ``transactional_ddl=True``, so a failed revision leaves the file at the revision the upgrade
  started from. ``PRAGMA foreign_key_check`` runs after the last revision, before the commit.
  An SQLite authorizer gives the transaction to env.py alone: ``BEGIN`` only to open it,
  ``COMMIT`` only after the check passed, ``ROLLBACK`` only once the upgrade failed or its
  commit did. While the upgrade is open it refuses every statement once no transaction is open,
  which covers SQLite ending one by itself (``INSERT OR ROLLBACK``, ``RAISE(ROLLBACK)``), and it
  refuses ``ATTACH``, so a revision writes no file the refusals did not see. Nothing runs on the
  connection before ``context.configure()``: alembic would treat the transaction as the
  caller's and never commit it.
- ``legacy_alter_table`` stays off, so batch-altering a table a view reads fails unless the
  migration drops the view first and recreates it from its own frozen SQL (gap 71).

A programmatic caller passes ``config.attributes["database_url"]`` (otherwise the application
settings decide) and ``config.attributes["configure_logger"] = False`` when it has configured
logging, because ``fileConfig`` disables existing loggers. No caller-supplied connection is
accepted: it would bypass the listener and the transaction setup.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, MutableMapping
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, event, pool
from sqlalchemy.engine import URL, Connection, make_url
from sqlalchemy.pool import ConnectionPoolEntry

from github_activity_db.config import get_settings
from github_activity_db.db.models import Base

config = context.config
TRACKED_DATABASE = Path(__file__).resolve().parents[1] / "github_activity.db"
# Key in the connection record's info: which transaction statement env.py allows next.
TRANSACTION_STATE = "migration_transaction"
ALLOWED_TRANSACTION_STATEMENTS = {
    ("BEGIN", "connected"),
    ("COMMIT", "checked"),
    ("ROLLBACK", "checked"),  # the commit failed
    ("ROLLBACK", "failed"),
}


class MigrationRefused(RuntimeError):
    """The migration cannot run on this database or connection; nothing was applied."""


def database_url() -> URL:
    """The database to migrate, as a URL for the synchronous ``sqlite3`` driver."""
    url = make_url(config.attributes.get("database_url") or get_settings().database_url)
    if url.get_backend_name() != "sqlite":
        raise MigrationRefused(f"only SQLite is supported, not {url.get_backend_name()!r}")
    database = url.database or ""
    # Before SQLite would create a missing file; refuse_tracked_file decides for the rest.
    if database and not database.startswith("file:") and database != ":memory:":
        if Path(database).name.lower() == TRACKED_DATABASE.name:
            raise MigrationRefused(refusal(database))
    return url.set(drivername="sqlite+pysqlite")


def refusal(path: str) -> str:
    return (
        f"refusing to migrate {path}: the tracked github_activity.db keeps its pre-Step-3 "
        "content (D26), and no other file of that name is migrated. Set DATABASE_URL to another "
        "file, for example sqlite+aiosqlite:///./github_activity.local.db"
    )


def refuse_tracked_file(dbapi_connection: sqlite3.Connection) -> None:
    """Refuse the file SQLite opened when it is the tracked database or carries its name.

    Asking SQLite covers every spelling of the path (relative, ``..``, symlinks, URIs), and
    ``samefile`` a hard link; the name covers another checkout's tracked file.
    """
    (opened,) = [
        row[2] for row in dbapi_connection.execute("PRAGMA database_list") if row[1] == "main"
    ]
    if not opened:  # an in-memory or temporary database
        return
    real = Path(os.path.realpath(opened))
    tracked = real.name.lower() == TRACKED_DATABASE.name or (
        TRACKED_DATABASE.exists() and real.samefile(TRACKED_DATABASE)
    )
    if tracked:
        raise MigrationRefused(refusal(opened))


def transaction_guard(
    dbapi_connection: sqlite3.Connection, info: MutableMapping[str, object]
) -> Callable[..., int]:
    """An SQLite authorizer: env.py's own transaction statements only, no statement outside the
    open upgrade's transaction, and no ATTACH."""

    def authorize(action: int, statement: str | None, *_: object) -> int:
        state = info.get(TRANSACTION_STATE)
        if action == sqlite3.SQLITE_ATTACH:
            return sqlite3.SQLITE_DENY
        if state == "open" and not dbapi_connection.in_transaction:
            return sqlite3.SQLITE_DENY  # the transaction ended without env.py
        if action != sqlite3.SQLITE_TRANSACTION:
            return sqlite3.SQLITE_OK
        allowed = (statement, state) in ALLOWED_TRANSACTION_STATEMENTS
        return sqlite3.SQLITE_OK if allowed else sqlite3.SQLITE_DENY

    return authorize


def configure_connection(dbapi_connection: sqlite3.Connection, record: ConnectionPoolEntry) -> None:
    """The tracked file refused, foreign keys off and checked, the transaction guard installed,
    then the driver's transaction handling off.

    SQLAlchemy does not close a connection whose connect listener raised, so this does.
    """
    try:
        refuse_tracked_file(dbapi_connection)
        dbapi_connection.execute("PRAGMA foreign_keys=OFF")
        (foreign_keys,) = dbapi_connection.execute("PRAGMA foreign_keys").fetchone()
        if foreign_keys != 0:
            raise MigrationRefused(
                f"PRAGMA foreign_keys reads {foreign_keys} after turning it off: the connection is "
                "inside a transaction"
            )
        if dbapi_connection.in_transaction:
            raise MigrationRefused("the migration connection is already inside a transaction")
        record.info[TRANSACTION_STATE] = "connected"
        dbapi_connection.set_authorizer(transaction_guard(dbapi_connection, record.info))
        # Last: this commits any transaction open on the connection.
        dbapi_connection.isolation_level = None
    except BaseException:
        dbapi_connection.close()
        raise


def begin_immediate(connection: Connection) -> None:
    """Open the upgrade's transaction, taking the write lock at once."""
    connection.exec_driver_sql("BEGIN IMMEDIATE")
    connection.info[TRANSACTION_STATE] = "open"


def check_foreign_keys(connection: Connection) -> None:
    violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    if violations:
        shown = ", ".join(str(tuple(row)) for row in violations[:5])
        raise MigrationRefused(
            f"{len(violations)} foreign-key violations after the upgrade (table, rowid, parent, "
            f"fkid): {shown}; nothing was applied"
        )
    connection.info[TRANSACTION_STATE] = "checked"


def run_migrations_online() -> None:
    engine = create_engine(database_url(), poolclass=pool.NullPool)
    # Ahead of SQLAlchemy's own first-connect handling, which rolls the connection back: placed
    # after it, the check would see the connection that rollback left, not the one connect made.
    event.listen(engine, "connect", configure_connection, insert=True)
    event.listen(engine, "begin", begin_immediate)
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=Base.metadata,
                transactional_ddl=True,
                render_as_batch=True,  # autogenerate: SQLite alters tables through batch mode
            )
            with context.begin_transaction():
                try:
                    context.run_migrations()
                    check_foreign_keys(connection)
                except BaseException:
                    connection.info[TRANSACTION_STATE] = "failed"
                    raise
    finally:
        engine.dispose()


if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

if context.is_offline_mode():
    raise MigrationRefused(
        "offline --sql mode is not supported: the upgrade checks foreign keys on a live connection"
    )
run_migrations_online()
