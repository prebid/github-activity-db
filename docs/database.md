# Database

SQLite, read and written through SQLAlchemy 2.0 with aiosqlite, and migrated by alembic. The
schema is [redesign plan 5.1](redesign-plan.md#51-schema); every table, view, and column is
listed in the [data dictionary](data-dictionary.md), which also says what consumers may rely on.

## Files

| File | Use |
|------|-----|
| `github_activity.local.db` | The default `DATABASE_URL` (`sqlite+aiosqlite:///./github_activity.local.db`); git-ignored |
| `github_activity.db` | Tracked in git and frozen at its pre-Step-3 content until Step 6c (D26): `scripts/check_frozen_db.py` pins its SHA-256 as a pre-commit hook, and `alembic/env.py` refuses to migrate it. It holds the first schema (revision `b3c4d5e6f7g8`), which the current code does not read |

Build or upgrade a database with:

```bash
uv run alembic upgrade head      # the file DATABASE_URL names
uv run alembic current           # its revision
```

Databases are built only by migrations; there is no `create_all` path. Tests copy one template
database per session, built by `alembic upgrade head` (`tests/conftest.py`).

## Connection settings

`build_engine()` in `db/engine.py` sets these on every application connection:

| Pragma | Value | Why |
|--------|-------|-----|
| `foreign_keys` | `ON` | SQLite disables it per connection by default; declared `ON DELETE` actions only hold with it on |
| `busy_timeout` | `30000` ms | Wait for a lock before raising "database is locked". Python's `sqlite3` default is 5000 ms; in rollback-journal mode a reader's lock blocks a writer's commit, and the longer wait lets the sync outlast a concurrent read |

The tracked database stays in SQLite's default rollback-journal mode (D20). The listener does
not set the journal mode: that statement needs a lock, so every connection would fail while
another process has a WAL database open. `scripts/check_db_journal.py` checks the tracked file's
header as a pre-commit hook and in the test suite. In WAL mode committed transactions can sit in
a git-ignored `*-wal` file until a checkpoint, so a commit, checkout, or stash could separate the
database from its WAL. Step 6 of the redesign plan moves the database out of git.

Timestamps use `UtcDateTime` (`db/types.py`): it rejects a naive datetime, stores an aware one
converted to UTC, and returns aware UTC values. Defaults are `utc_now()`, never SQL's
`CURRENT_TIMESTAMP`, whose shorter text would not order with the stored values.

## Migrations

`alembic/env.py` runs each upgrade as one transaction (gap 70):

- The connection has foreign keys off, because batch mode drops and recreates tables and an
  enforced drop cascades into child rows. The connect listener refuses a connection on which
  the pragma does not take effect or a transaction is already open.
- `BEGIN IMMEDIATE` opens the transaction and `PRAGMA foreign_key_check` runs after the last
  revision, before the commit. A failed revision, or a violation, leaves the file at the
  revision the upgrade started from. The check runs before every command commits, one that
  runs no revision (`current`, `check`, `stamp`) included, because alembic loads each revision
  module inside the transaction and a module's top-level code can change rows or the schema;
  a file that already holds a violation therefore fails every command that connects until it
  is repaired or rebuilt. Each of those commands opens the same transaction, so like an upgrade
  it waits up to 5 seconds for the write lock and fails if another connection holds the file
  that long.
- An SQLite authorizer allows only the actions `ALLOWED_ACTIONS` in `env.py` lists: the main
  database's DDL and DML, and the reads alembic and SQLAlchemy's reflection make. No statement
  a revision runs commits part of the upgrade: the authorizer refuses any `BEGIN`, `COMMIT`, `END`, or
  `ROLLBACK` but the upgrade's own, `SAVEPOINT`, and every statement once SQLite has ended the
  transaction by itself (`INSERT OR ROLLBACK`, `RAISE(ROLLBACK)`); the connection keeps no
  prepared statements, so a repeated statement is checked again. It refuses `ATTACH`, temporary
  objects (`TEMP` or a `temp.` name), virtual tables, and a pragma value other than
  `user_version` and the pragmas reflection reads with an argument (`table_info`, `table_xinfo`, `index_list`, `index_info`, `foreign_key_list`),
  so `journal_mode`, `legacy_alter_table`, and
  `ignore_check_constraints` keep the values the connection opened with.
- Refused: offline `--sql` mode, databases other than SQLite, SQLite URI filenames
  (`?uri=true`), a caller-supplied connection or `sqlalchemy.url`, and the tracked
  `github_activity.db`. The connect listener asks SQLite which file it opened, so the tracked
  file is refused through any path, symlink, or hard link, and so is any file named
  `github_activity.db`, such as another checkout's copy; a missing path of that name, or a
  missing file the driver's path resolves to under that name, is refused before SQLite would
  create it.
- `downgrade()` raises: migrations are never downgraded, and a database the code cannot upgrade
  is rebuilt (redesign plan 5.4).

### Writing a migration

A migration changes the models (`db/models.py`), the chain, and the documents together:

- Raise in `downgrade()`, as `alembic/script.py.mako` generates it.
- Import only alembic, SQLAlchemy, and the standard library. Name every index, CHECK constraint, and foreign key
  through `op.f()` with the name `NAMING_CONVENTION` in `db/models.py` gives it, and every unique
  constraint explicitly (`uq_<table>_<columns>`): batch mode keeps named CHECKs and drops
  unnamed ones (gap 72).
- Before batch-altering a table a view reads, drop the view, and recreate it afterwards from
  the migration's own frozen `VIEWS` constant; a view left in place fails the migration
  (gap 71). A migration that creates or replaces a view embeds its SQL in `VIEWS`, and one that
  drops a view without replacing it names it in `DROPPED_VIEWS`, which the view comparison reads.
- Add the revision id to `MIGRATION_REVISIONS` in `db/schema.py`: code that must refuse a
  database not at head reads the chain from there (Step 3b's head check, gap 74).
- Update `docs/data-dictionary.md` and `tests/schema_versions.json`: the consumer columns, and
  the SHA-256 of each view's frozen SQL, exactly as the migration embeds it. A removed, renamed,
  or redefined consumer column, and any change to a view's frozen SQL (a new column on an
  existing view, and a change of layout alone, included), needs a new `SCHEMA_VERSION`, set by
  the migration as `PRAGMA user_version`.
- A migration that adds or redefines a GitHub-derived column of a table that may hold rows
  bumps that kind's hydration version (plan 5.1).

The tests that hold these: `tests/db/test_schema.py` compares a migrated database with the
models (CHECK names and text, index SQL, and triggers on tables included) and each view with its
frozen SQL, refuses any schema object neither comparison reads (a trigger on a view, an index on
`alembic_version`, an `ANALYZE` table), and holds both to plan 5.1's nullable columns, unique
column sets, and delete actions; `tests/db/test_migrations.py` covers the chain, `env.py`, and
each revision's imports and `VIEWS` constant; `tests/test_data_dictionary.py` covers the
dictionary and the schema-version lock. CI also runs `alembic check`.

| Revision | Description |
|----------|-------------|
| `01421d8dfaeb` | First schema: `repositories`, `pull_requests`, `user_tags`, `pr_user_tags` |
| `a2b3c4d5e6f7` | Adds `sync_failures` |
| `b3c4d5e6f7g8` | Renames `pull_requests.filenames` to `file_changes` |
| `33f8570f6540` | Step 3 schema: drops the first schema's tables except `user_tags`, creates the 5.1 tables and views, sets `user_version` 1 |
| `94a52e4f1d57` | Step 3b-1 collection schema: refuses a file holding rows in a table it adds columns to, recreates those tables with the 5.1 columns, adds `repo_labels`, `pr_review_requests`, `pr_assignees`, and `account_profiles`, replaces the views with the row-level views, sets `user_version` 2 |

## Querying

Consumers read the views and the columns the dictionary marks consumer. The views keep every
row and leave counting to the query. For example, for each account, the merged pull requests of
one tracked repository it approved, with the account's type and `[automation]` reason beside it,
and the lines those pull requests changed, each pull request counted once however many times the
account approved it. An approval GitHub returned without a reviewer, which the database had none
stored for, has null account columns; the query leaves those out:

```sql
SELECT account_login, account_type, account_automation_reason,
       COUNT(*) AS pull_requests_approved, SUM(additions + deletions) AS lines_changed
FROM (
    SELECT DISTINCT account_github_id, account_node_id, account_login, account_type,
           account_automation_reason, pr_github_id, additions, deletions
    FROM v_pr_relationships
    WHERE repository_full_name = 'prebid/Prebid.js'
      AND repository_is_tracked = 1
      AND relationship = 'review'
      AND review_state = 'APPROVED'
      AND pr_state = 'MERGED'
      AND account_node_id IS NOT NULL
)
GROUP BY account_github_id, account_node_id
ORDER BY pull_requests_approved DESC;
```

Counting approvals rather than pull requests, or lines once per approval, is the query's choice,
not the view's.

Application code uses `get_session()`, which commits on a clean exit and rolls back on an
exception:

```python
from sqlalchemy import select

from github_activity_db.db import PullRequest, get_session

async with get_session() as session:
    open_numbers = (
        await session.execute(select(PullRequest.number).where(PullRequest.state == "OPEN"))
    ).scalars().all()
```
