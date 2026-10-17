"""Alembic environment: SQLite migrations, each upgrade one transaction (redesign plan 5.1).

- Online only. Offline ``--sql`` mode is refused, because the checks below need a live
  connection.
- The repository-root ``github_activity.db`` is refused while D26 freezes it (gap 73). That
  check models a plain file path, so URL query parameters and ``file:`` URI filenames are
  refused, as is any database but SQLite.
- Foreign keys are off while migrating: batch mode drops and recreates tables, and an enforced
  drop cascades into child rows. The connect listener, which runs before SQLAlchemy's own
  connect handling, sets the pragma, which has no effect inside a transaction, and refuses the
  connection unless it then reads 0 and no transaction is open. Only then does it turn off the
  driver's own transaction handling, because that commits a transaction already open on the
  connection.
- An upgrade is one transaction (gap 70): a ``begin`` listener emits ``BEGIN IMMEDIATE`` and
  ``transactional_ddl=True``, so a failed revision leaves the file at the revision the upgrade
  started from. ``PRAGMA foreign_key_check`` runs after the last revision, before the commit.
  Nothing runs on the connection before ``context.configure()``: alembic would treat the
  transaction as the caller's and never commit it.
- ``legacy_alter_table`` stays off, so batch-altering a table a view reads fails unless the
  migration drops the view first and recreates it from its own frozen SQL (gap 71).

A programmatic caller passes ``config.attributes["database_url"]`` (otherwise the application
settings decide) and ``config.attributes["configure_logger"] = False`` when it has configured
logging, because ``fileConfig`` disables existing loggers. No caller-supplied connection is
accepted: it would bypass the listener and the transaction setup.
"""

from __future__ import annotations

import sqlite3
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


class MigrationRefused(RuntimeError):
    """The migration cannot run on this database or connection; nothing was applied."""


def database_url() -> URL:
    """The database to migrate, as a URL for the synchronous ``sqlite3`` driver."""
    url = make_url(config.attributes.get("database_url") or get_settings().database_url)
    if url.get_backend_name() != "sqlite":
        raise MigrationRefused(f"only SQLite is supported, not {url.get_backend_name()!r}")
    if url.query:
        raise MigrationRefused(f"database URL query parameters are not supported: {url}")
    if (url.database or "").startswith("file:"):
        # Whether SQLite reads this as a URI depends on how it was built.
        raise MigrationRefused(f"SQLite URI filenames are not supported: {url}")
    if url.database and url.database != ":memory:":
        refuse_tracked_file(Path(url.database))
    return url.set(drivername="sqlite+pysqlite")


def refuse_tracked_file(path: Path) -> None:
    """Refuse the tracked database by any spelling: relative, symlinked, or hard-linked.

    A relative path resolves against the working directory, as SQLite opens it.
    """
    resolved = path.resolve()
    same = resolved == TRACKED_DATABASE or (
        resolved.exists() and TRACKED_DATABASE.exists() and resolved.samefile(TRACKED_DATABASE)
    )
    if same:
        raise MigrationRefused(
            f"refusing to migrate {TRACKED_DATABASE}: the tracked database keeps its pre-Step-3 "
            "content (D26). Set DATABASE_URL to another file, for example "
            "sqlite+aiosqlite:///./github_activity.local.db"
        )


def configure_connection(
    dbapi_connection: sqlite3.Connection, _record: ConnectionPoolEntry
) -> None:
    """Foreign keys off and checked, then the driver's transaction handling off.

    SQLAlchemy does not close a connection whose connect listener raised, so this does.
    """
    try:
        dbapi_connection.execute("PRAGMA foreign_keys=OFF")
        (foreign_keys,) = dbapi_connection.execute("PRAGMA foreign_keys").fetchone()
        if foreign_keys != 0:
            raise MigrationRefused(
                f"PRAGMA foreign_keys reads {foreign_keys} after turning it off: the connection is "
                "inside a transaction"
            )
        if dbapi_connection.in_transaction:
            raise MigrationRefused("the migration connection is already inside a transaction")
        # Last: this commits any transaction open on the connection.
        dbapi_connection.isolation_level = None
    except BaseException:
        dbapi_connection.close()
        raise


def begin_immediate(connection: Connection) -> None:
    """Open the upgrade's transaction, taking the write lock at once."""
    connection.exec_driver_sql("BEGIN IMMEDIATE")


def check_foreign_keys(connection: Connection) -> None:
    violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    if violations:
        shown = ", ".join(str(tuple(row)) for row in violations[:5])
        raise MigrationRefused(
            f"{len(violations)} foreign-key violations after the upgrade (table, rowid, parent, "
            f"fkid): {shown}; nothing was applied"
        )


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
                context.run_migrations()
                check_foreign_keys(connection)
    finally:
        engine.dispose()


if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

if context.is_offline_mode():
    raise MigrationRefused(
        "offline --sql mode is not supported: the upgrade checks foreign keys on a live connection"
    )
run_migrations_online()
