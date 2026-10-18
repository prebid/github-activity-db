"""Alembic environment: SQLite migrations, each upgrade one transaction (redesign plan 5.1).

- Online only. Offline ``--sql`` mode is refused, because the checks below need a live
  connection.
- Only SQLite is migrated, and only by a file path: SQLite URI filenames (``?uri=true``) are
  refused, because their parameters (``nolock``, ``immutable``, ``vfs``) can remove the locking
  an upgrade relies on, and only SQLite reads the path a URI names. The tracked
  ``github_activity.db`` is refused while D26 freezes it (gap 73): the connect listener asks
  SQLite which file it opened, so no spelling of the path reaches it, and it also refuses any
  file named ``github_activity.db``, such as another checkout's copy. A path of that name, or a
  missing file the driver's path resolves to under that name, is refused before SQLite would
  create it.
- Foreign keys are off while migrating: batch mode drops and recreates tables, and an enforced
  drop cascades into child rows. The connect listener, which runs before SQLAlchemy's own
  connect handling, sets the pragma, which has no effect inside a transaction, and refuses the
  connection unless it then reads 0 and no transaction is open. Only then does it turn off the
  driver's own transaction handling, because that commits a transaction already open on the
  connection.
- An upgrade is one transaction (gap 70): a ``begin`` listener emits ``BEGIN IMMEDIATE`` and
  ``transactional_ddl=True``, so a failed revision leaves the file at the revision the upgrade
  started from. ``PRAGMA foreign_key_check`` runs after the last revision, before the commit; a
  command that ran no revision and changed no row (``current``, ``check``, or ``stamp`` to the
  file's own revision) is not checked. Every command that connects opens that ``BEGIN
  IMMEDIATE`` transaction, so each one waits for the write lock, up to the driver's default 5
  seconds.
- An SQLite authorizer allows only the actions in ``ALLOWED_ACTIONS``: the main database's DDL
  and DML, and the reads alembic and SQLAlchemy's reflection make. It gives the transaction to
  env.py alone: ``BEGIN`` only to open it, ``COMMIT`` only after the check passed, ``ROLLBACK``
  only once the upgrade failed or its commit did. A pragma may set a value only for
  ``user_version`` and the pragmas SQLAlchemy's reflection reads with an argument
  (``PRAGMAS_WITH_A_VALUE``), so a revision cannot turn off the journal, the CHECK constraints,
  or the view check below. No ``ATTACH``, temporary object (``TEMP`` or a ``temp.`` name), or
  virtual table is allowed, so a revision writes no file the refusals did not see. While the
  upgrade is open the authorizer refuses every statement once no transaction is, which covers
  SQLite ending one by itself (``INSERT OR ROLLBACK``, ``RAISE(ROLLBACK)``). SQLite authorizes a
  statement when it prepares it, so the connection keeps no prepared statements: each execute is
  prepared, and checked, again.
- Nothing runs on the connection before ``context.configure()``: alembic would treat the
  transaction as the caller's and never commit it.
- ``legacy_alter_table`` stays off, so batch-altering a table a view reads fails unless the
  migration drops the view first and recreates it from its own frozen SQL (gap 71).

A programmatic caller passes ``config.attributes["database_url"]`` (otherwise the application
settings decide) and ``config.attributes["configure_logger"] = False`` when it has configured
logging, because ``fileConfig`` disables existing loggers. A caller-supplied connection
(``config.attributes["connection"]``) or ``sqlalchemy.url`` is refused: the connection would
bypass the listener and the transaction setup, and either would otherwise be ignored.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Collection, Mapping, MutableMapping
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from alembic.runtime.migration import MigrationContext, MigrationInfo
from sqlalchemy import create_engine, event, pool
from sqlalchemy.engine import URL, Connection, Engine, make_url
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
# Everything else is refused: ATTACH writes another file, a temporary object lives outside the
# database file, a virtual table runs its module's code, and SAVEPOINT, ANALYZE, and the rest no
# migration needs.
ALLOWED_ACTIONS = frozenset(
    {
        sqlite3.SQLITE_ALTER_TABLE,
        sqlite3.SQLITE_CREATE_INDEX,
        sqlite3.SQLITE_CREATE_TABLE,
        sqlite3.SQLITE_CREATE_TRIGGER,
        sqlite3.SQLITE_CREATE_VIEW,
        sqlite3.SQLITE_DELETE,
        sqlite3.SQLITE_DROP_INDEX,
        sqlite3.SQLITE_DROP_TABLE,
        sqlite3.SQLITE_DROP_TRIGGER,
        sqlite3.SQLITE_DROP_VIEW,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_INSERT,
        sqlite3.SQLITE_PRAGMA,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_RECURSIVE,
        sqlite3.SQLITE_REINDEX,  # SQLite asks for it while CREATE INDEX fills the new index
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_TRANSACTION,
        sqlite3.SQLITE_UPDATE,
    }
)
# The pragmas a statement may pass a value: reflection's readers and the version a migration sets.
PRAGMAS_WITH_A_VALUE = frozenset(
    {"foreign_key_list", "index_info", "index_list", "table_info", "table_xinfo", "user_version"}
)


class MigrationRefused(RuntimeError):
    """The migration cannot run on this database or connection; nothing was applied."""


def database_url() -> URL:
    """The database to migrate, as a URL for the synchronous ``sqlite3`` driver."""
    if "connection" in config.attributes:
        raise MigrationRefused(
            "a caller-supplied connection is not used: pass config.attributes['database_url']"
        )
    if config.get_main_option("sqlalchemy.url"):
        raise MigrationRefused(
            "sqlalchemy.url is not used: pass config.attributes['database_url'] or set DATABASE_URL"
        )
    url = make_url(config.attributes.get("database_url") or get_settings().database_url)
    if url.get_backend_name() != "sqlite":
        raise MigrationRefused(f"only SQLite is supported, not {url.get_backend_name()!r}")
    if "uri" in url.query:
        raise MigrationRefused(
            f"refusing to migrate {url.database}: SQLite URI filenames are not supported, "
            "because their parameters can remove the locking an upgrade relies on. Set "
            "DATABASE_URL to a file path"
        )
    return url.set(drivername="sqlite+pysqlite")


def refusal(path: str, checked: str) -> str:
    return (
        f"refusing to migrate {path} ({checked}): the tracked github_activity.db keeps its "
        "pre-Step-3 content (D26), and no other file of that name is migrated. Set DATABASE_URL "
        "to another file, for example sqlite+aiosqlite:///./github_activity.local.db"
    )


def refuse_creating_a_tracked_file(engine: Engine) -> None:
    """Refuse the file the driver will open when it is named ``github_activity.db``, or is
    missing and its path resolves to that name, before SQLite would create it.

    The dialect gives the file the driver opens (``os.path.abspath`` of the URL's path, which
    drops ``..`` as text), and opening a missing file creates it where that path's symlinks
    lead. refuse_tracked_file decides for a file that exists.
    """
    (filename, *_), _options = engine.dialect.create_connect_args(engine.url)
    if filename == ":memory:":
        return
    path = Path(filename)
    names = {path.name}
    if not path.exists():
        names.add(Path(os.path.realpath(path)).name)
    if TRACKED_DATABASE.name in {name.lower() for name in names}:
        raise MigrationRefused(refusal(filename, "checked before connecting"))


def refuse_tracked_file(dbapi_connection: sqlite3.Connection) -> None:
    """Refuse the file SQLite opened when it is the tracked database or carries its name.

    Asking SQLite covers every spelling of the path (relative, ``..``, symlinks), and
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
        raise MigrationRefused(refusal(opened, "the file SQLite opened"))


def transaction_guard(
    dbapi_connection: sqlite3.Connection, info: MutableMapping[str, object]
) -> Callable[..., int]:
    """An SQLite authorizer: the listed actions only, env.py's own transaction statements, a
    value only for the listed pragmas, and no statement outside the open upgrade's transaction."""

    def authorize(
        action: int, name: str | None, value: str | None, schema: str | None, _: object
    ) -> int:
        state = info.get(TRANSACTION_STATE)
        if action not in ALLOWED_ACTIONS:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_INSERT and schema == "temp":
            return sqlite3.SQLITE_DENY  # creating a temp. object inserts into the temp schema
        if state == "open" and not dbapi_connection.in_transaction:
            return sqlite3.SQLITE_DENY  # the transaction ended without env.py
        if action == sqlite3.SQLITE_TRANSACTION:
            allowed = (name, state) in ALLOWED_TRANSACTION_STATEMENTS
        elif action == sqlite3.SQLITE_PRAGMA:
            allowed = value is None or (name or "").lower() in PRAGMAS_WITH_A_VALUE
        else:
            allowed = True
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


def check_foreign_keys(connection: Connection, revisions_ran: bool) -> None:
    """After the last revision, before the commit, unless the command ran no revision and changed
    no row. alembic loads each revision module inside the transaction, so a module's top-level
    code can change rows when no ``upgrade()`` runs."""
    driver = connection.connection.driver_connection
    if revisions_ran or (driver is not None and driver.total_changes):
        violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        if violations:
            shown = ", ".join(str(tuple(row)) for row in violations[:5])
            raise MigrationRefused(
                f"{len(violations)} foreign-key violations after the migration (table, rowid, "
                f"parent, fkid): {shown}; nothing was applied"
            )
    connection.info[TRANSACTION_STATE] = "checked"


def run_migrations_online() -> None:
    # No prepared-statement cache: SQLite authorizes a statement when it prepares it, and the
    # driver would reuse one by its text after SQLite ended the transaction by itself.
    # connect_args overrides a cached_statements value in the URL.
    engine = create_engine(
        database_url(), poolclass=pool.NullPool, connect_args={"cached_statements": 0}
    )
    revisions: list[MigrationInfo] = []

    def record_revision(
        ctx: MigrationContext,
        step: MigrationInfo,
        heads: Collection[str],
        run_args: Mapping[str, object],
    ) -> None:
        if step.is_migration:
            revisions.append(step)

    try:
        refuse_creating_a_tracked_file(engine)
        # Ahead of SQLAlchemy's own first-connect handling, which rolls the connection back:
        # placed after it, the check would see the connection that rollback left, not the one
        # connect made.
        event.listen(engine, "connect", configure_connection, insert=True)
        event.listen(engine, "begin", begin_immediate)
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=Base.metadata,
                transactional_ddl=True,
                render_as_batch=True,  # autogenerate: SQLite alters tables through batch mode
                on_version_apply=record_revision,
            )
            with context.begin_transaction():
                try:
                    context.run_migrations()
                    check_foreign_keys(connection, revisions_ran=bool(revisions))
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
