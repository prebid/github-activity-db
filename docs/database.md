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
  revision the upgrade started from.
- Refused: offline `--sql` mode, the tracked `github_activity.db` (also through a relative
  path, a symlink, or a hard link), non-SQLite URLs, URL query parameters, and `file:` URI
  filenames.
- `downgrade()` raises: migrations are never downgraded, and a database the code cannot upgrade
  is rebuilt (redesign plan 5.4).

### Writing a migration

A migration changes the models (`db/models.py`), the chain, and the documents together:

- Import only alembic and SQLAlchemy. Name every index, CHECK constraint, and foreign key
  through `op.f()` with the name `NAMING_CONVENTION` in `db/models.py` gives it, and every unique
  constraint explicitly (`uq_<table>_<columns>`): batch mode keeps named CHECKs and drops
  unnamed ones (gap 72).
- Before batch-altering a table a view reads, drop the view, and recreate it afterwards from
  the migration's own frozen `VIEWS` constant; a view left in place fails the migration
  (gap 71). A migration that creates or replaces a view embeds its SQL in `VIEWS`.
- Add the revision id to `MIGRATION_REVISIONS` in `db/schema.py`; the installed package reads
  the chain from there.
- Update `docs/data-dictionary.md` and, for a consumer column, `tests/schema_versions.json`. A
  removed, renamed, or redefined consumer column needs a new `SCHEMA_VERSION`, set by the
  migration as `PRAGMA user_version`.
- A migration that adds or redefines a GitHub-derived column of a table that may hold rows
  bumps that kind's hydration version (plan 5.1).

The tests that hold these: `tests/db/test_schema.py` compares a migrated database with the
models (CHECK names and text included) and each view with its frozen SQL;
`tests/db/test_migrations.py` covers the chain and `env.py`; `tests/test_data_dictionary.py`
covers the dictionary and the schema-version lock. CI also runs `alembic check`.

| Revision | Description |
|----------|-------------|
| `01421d8dfaeb` | First schema: `repositories`, `pull_requests`, `user_tags`, `pr_user_tags` |
| `a2b3c4d5e6f7` | Adds `sync_failures` |
| `b3c4d5e6f7g8` | Renames `pull_requests.filenames` to `file_changes` |
| `33f8570f6540` | Step 3 schema: drops the first schema's tables except `user_tags`, creates the 5.1 tables and views, sets `user_version` 1 |

## Querying

Consumers read the views and the columns the dictionary marks consumer. For example, the
reviewers of one repository's pull requests, with their final verdicts:

```sql
SELECT pr_number, reviewer_login, final_verdict, comment_count
FROM v_pr_reviewers
WHERE repository_full_name = 'prebid/Prebid.js'
ORDER BY pr_number;
```

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
