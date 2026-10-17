# GitHub Activity DB

A searchable data store for GitHub PR data from Prebid repositories.

Work follows [docs/redesign-plan.md](docs/redesign-plan.md), the plan of record: read it before
changing the schema, sync, or CI, and update its step status table when a step lands.

## Quick Reference

```bash
# Install & setup
uv sync --frozen --extra dev  # dev tools are an optional extra; plain `uv sync` removes them
uv run pre-commit install
cp .env.example .env  # Add GITHUB_TOKEN
uv run alembic upgrade head  # builds github_activity.local.db; refuses the tracked github_activity.db

# CLI (no sync command until redesign plan Step 3b)
uv run ghactivity --help
uv run ghactivity github rate-limit --all                          # Rate-limit pools
uv run ghactivity github test --pr 13481 --repo prebid/Prebid.js   # Resolve and hydrate one PR

# Checks (CI runs the same: .github/workflows/check.yml)
uv run pre-commit run --all-files  # ruff, mypy, file checks, quality gates
uv run pytest                      # CI also runs Python 3.12, `alembic check`, and a built wheel
# On pull requests, CI also runs scripts/check_query_lock.py (hydration- and schema-version locks)
```

## Documentation

| Document | Description |
|----------|-------------|
| [Redesign Plan](docs/redesign-plan.md) | Plan of record: decisions, target design, execution steps, evidence |
| [Data Dictionary](docs/data-dictionary.md) | Every table, view, and column at head, marked consumer or internal |
| [Database](docs/database.md) | Database files, connection settings, migrations and the rules for writing one |
| [Development](docs/development.md) | Setup, commands, configuration, troubleshooting |
| [Testing](docs/testing.md) | Evidence rules, test databases, recorded GitHub responses, CI |
| [Contributing](CONTRIBUTING.md) | Quality standards, code style, PR process |
| [Architecture](docs/architecture.md), [Data Model](docs/data-model.md) | History: the pipeline and schema Step 3a removed, kept until Step 7 |
| [Roadmap](docs/roadmap.md) | History of earlier phases; superseded by the redesign plan |

## Current Scope

**Implemented:**
- The tracked repositories listed in `src/github_activity_db/tracking.toml`
- GitHub source layer (`src/github_activity_db/source/`, redesign plan Step 2): resolve, PR index,
  batch hydration, user lookup, and rate-limit readings, with typed records
- The Step 3 schema (plan 5.1, without issues): migration `33f8570f6540`, the `v_*` consumer
  views, the data dictionary, and the schema-version lock
- CLI commands: `ghactivity github rate-limit` and `ghactivity github test`

**Not implemented yet:** nothing writes the new tables until the `sync` and `verify` commands
(Step 3b). The tracked `github_activity.db` stays frozen at the first schema (D26).

**Planned in the redesign plan:** the sync engine and verify (Step 3b); GitHub Issues and
closing-issue links (Step 4); scheduled sync and release publication (Step 6).

**Out of Scope (Future):**
- Webhooks / real-time sync
- Web UI

## Tech Stack

| Component | Technology |
|-----------|------------|
| Runtime | Python 3.12+ |
| Database | SQLite + SQLAlchemy 2.0 + aiosqlite |
| Validation | Pydantic 2.0 |
| GitHub API | githubkit |
| CLI | typer + rich |
| Migrations | alembic |
| Package Manager | uv |

## Key Modules

### GitHub Source Layer (`source/`)
The only package that imports githubkit, and it imports neither SQLAlchemy nor `db/`
(`tests/test_import_boundaries.py`).
- `GitHubSource`: `async with` entry point for `resolve`, `index_pull_requests`,
  `hydrate_pull_requests`, `lookup_users`, and `rate_limits`
- `Transport`: the one place requests are sent: one in flight, budget per pool, failure classes
  (rate limited, timeout, server error, 401, 403), waits and retries; injectable `Clock`
- `PullRequestHydrator`: batches of 25, `node(id:)` overflow, REST compare above 250 commits,
  split on failure, one solo retry per failed item (none when its own request already failed
  alone); returns `Hydrated`, `Gone`, or `Failed` per distinct number
- `records.py` (public) and `models.py` (private, strict types); `errors.py` splits fatal,
  repository, and item errors
- Recorded fixtures: `scripts/record_fixtures.py` (see docs/testing.md)

### Database (`db/`, `alembic/`)
- Databases are built only by `alembic upgrade head`; tests copy one session template
  (`tests/conftest.py`), and `metadata.create_all` appears only in the schema comparison test
- `alembic/env.py` runs an upgrade as one transaction with foreign keys off and checked, and
  refuses offline mode and the tracked `github_activity.db`; `downgrade()` raises
- A migration changes `db/models.py`, `MIGRATION_REVISIONS` in `db/schema.py`,
  `docs/data-dictionary.md`, and `tests/schema_versions.json` together ("Writing a migration"
  in docs/database.md)
- Timestamps use `UtcDateTime` (`db/types.py`), which rejects naive datetimes

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GITHUB_TOKEN` | For `github` commands | - | GitHub token, for example `GITHUB_TOKEN="$(gh auth token)"` |
| `DATABASE_URL` | No | `sqlite+aiosqlite:///./github_activity.local.db` | Database connection; never the tracked `github_activity.db` (D26) |
| `LOG_LEVEL` | No | `INFO` | Logging level |
| `ENVIRONMENT` | No | `development` | App environment |
| `TRACKING_CONFIG_PATH` | No | packaged `tracking.toml` | Tracked repositories file to read instead |

## Type Safety Policy

This project enforces strict type safety through mypy strict mode and custom quality gates.

### Required Type Annotations

- All public functions must have type annotations
- All class attributes must have type annotations
- Use `from __future__ import annotations` for forward references

### `Any` Usage Guidelines

| Use Case | Allowed | Alternative |
|----------|---------|-------------|
| JSON/dict serialization | `dict[str, Any]` | N/A |
| Pydantic validators | `v: Any` | N/A |
| ORM factories | `from_orm(obj: Any)` | N/A |
| Log context | `**context: Any` | N/A |
| **Lazy initialization** | Use `T \| None` with TYPE_CHECKING |
| **Avoiding generics** | Use TYPE_CHECKING block |
| **Third-party without types** | Document reason, add stub or TYPE_CHECKING |

### `type: ignore` Policy

1. **Must include error code:** `# type: ignore[arg-type]`
2. **Must have justification comment** if not obvious
3. **Prefer fixing** the underlying type issue
4. **Enforced:** pre-commit fails above 5 in `src/`

### `noqa` Policy

1. **Centralize** in shared utility functions (e.g., `cli/common.py`)
2. **Document** why the rule doesn't apply
3. **Enforced:** pre-commit fails above 3 in `src/`

### Quality Gates

Pre-commit (run locally and by CI) fails on:
- more than 5 `type: ignore` comments in src/ (`# mypy:` inline configuration and
  `@no_type_check` count too)
- more than 3 `noqa` comments in src/ (file-level `# ruff: noqa` and `# flake8: noqa`,
  `# ruff: disable[...]` range suppressions, and the isort skip comments count too)
- a blanket `# noqa` without rule codes (ruff `PGH004`)
- new module-level type aliases to `Any` (`SomeName = Any`, `type SomeName = Any`, unions and
  `Optional`/`Annotated` forms; `scripts/check_any_aliases.py`); use TYPE_CHECKING instead
- files over 1000 KB anywhere in the repository (`github_activity.db` exempt while tracked)
- `github_activity.db` in WAL mode (`scripts/check_db_journal.py`; redesign plan D20)
- `github_activity.db` content other than its pinned SHA-256 (`scripts/check_frozen_db.py`; D26)
- loguru calls with `%`-style placeholders (`scripts/check_log_placeholders.py`)

`scripts/count_suppressions.py` counts comment tokens and decorators in `.py` and `.pyi` files,
in the spellings its docstring lists; text inside strings is not counted. It refuses (exit 1) a
file it may read differently from ruff, such as any file with a non-UTF-8 coding declaration.

ruff and mypy run from `uv.lock` through `uv run --no-sync`, so local and CI versions match.
