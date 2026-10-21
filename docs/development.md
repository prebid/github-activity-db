# Development Guide

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager

## Quick Start

```bash
# Clone the repository
git clone https://github.com/prebid/github-activity-db.git
cd github-activity-db

# Install dependencies, including the dev extra (plain `uv sync` removes the dev tools)
uv sync --frozen --extra dev
uv run pre-commit install

# Copy environment template
cp .env.example .env
# Edit .env and add your GITHUB_TOKEN

# Run database migrations
uv run alembic upgrade head

# Verify installation
uv run ghactivity --version
```

## Environment Setup

### Environment Variables

Create a `.env` file (copy from `.env.example`):

```bash
# Required
GITHUB_TOKEN=ghp_your_token_here

# Optional (defaults shown)
DATABASE_URL=sqlite+aiosqlite:///./github_activity.local.db
LOG_LEVEL=INFO
ENVIRONMENT=development
```

### GitHub Token

Create a personal access token at https://github.com/settings/tokens with:
- `repo` scope (for private repos) OR
- `public_repo` scope (for public repos only)

## Common Commands

### Package Management

```bash
# Install all dependencies (including dev)
uv sync --all-extras

# Add a new dependency
uv add package-name

# Add a dev dependency (dev tools live in the `dev` optional extra)
uv add --optional dev package-name

# Update all dependencies
uv lock --upgrade
uv sync --extra dev

# Change the build backend: edit [build-system] requires in pyproject.toml, regenerate the
# hashed constraints the built-wheel CI job uses, copy the same pins into
# [tool.uv] build-constraint-dependencies, then run `uv lock`, which copies them into
# uv.lock for `uv sync --frozen` (tests/test_build_inputs.py checks that all four agree;
# CI runs `uv lock --locked`)
printf 'hatchling==<version>\neditables==<version>\n' | uv pip compile - --universal \
  --generate-hashes --python-version 3.12 -o build-constraints.txt
```

### CLI

```bash
# Show help
uv run ghactivity --help

# Show version
uv run ghactivity --version

# GitHub commands (they need GITHUB_TOKEN, for example GITHUB_TOKEN="$(gh auth token)")
uv run ghactivity github rate-limit           # GraphQL reading and the core and graphql pools
uv run ghactivity github rate-limit --all     # Every REST pool
uv run ghactivity github test --pr 13481 --repo prebid/Prebid.js  # Resolve and hydrate one PR
uv run ghactivity github test --pr 4343       # The first repository in tracking.toml

# Sync into DATABASE_URL (build it first with `uv run alembic upgrade head`)
uv run ghactivity sync --dry-run              # Resolve, index, and plan; write nothing
uv run ghactivity sync --repo prebid/go-gdpr  # Only this configured repository (repeatable)
uv run ghactivity sync --max-minutes 30       # Stop issuing requests after 30 minutes
uv run ghactivity sync --allow-deletions prebid/Prebid.js=40  # All candidates, if at most 40
uv run ghactivity sync --prune-untracked      # Delete repositories already untracked
uv run ghactivity sync --break-lock           # Mark a running sync aborted and claim the run
```

`ghactivity sync` follows [redesign plan](redesign-plan.md) 5.3: it exits 0 when every repository
indexed completely and nothing failed, 2 when something needs attention (an item or a repository
failed, an index was incomplete, deletions halted, a stored repository is untracked, or a prune
was refused), and 1 on a fatal error. A command line that never starts its work (a usage error,
an invalid `tracking.toml` or setting, a log file that cannot be opened, a missing token, a
missing or unmigrated database file, or another sync holding the lock) exits 1, before any
`sync_runs` row. A first Ctrl-C closes the run `aborted` and exits 130; a second one during that
close leaves the run `running`, for `--break-lock` or the stale-row rule (10 minutes past its
`--max-minutes` deadline) to recover.

### Code Quality

```bash
# Type checking (strict mode for src/, relaxed for tests/)
uv run mypy src/
uv run mypy tests/  # Runs with relaxed settings
uv run mypy src tests scripts  # Check everything (what the pre-commit hook runs)

# Linting
uv run ruff check src/ tests/

# Auto-fix lint issues
uv run ruff check --fix src/ tests/

# Formatting
uv run ruff format src/ tests/

# Check formatting without changes
uv run ruff format --check src/ tests/

# Run all pre-commit checks
uv run pre-commit run --all-files
```

### Quality Metrics

The project maintains strict quality standards with automated enforcement:

| Metric | Target | Enforcement |
|--------|--------|-------------|
| `type: ignore` comments in src/ | ≤5 | Pre-commit blocks |
| `noqa` comments in src/ | ≤3 | Pre-commit blocks |
| `Any` type aliases | 0 | Pre-commit blocks |
| Mypy errors | 0 | Pre-commit blocks |

```bash
# Check current quality metrics (the spellings it counts are listed in the script's docstring)
uv run python scripts/count_suppressions.py src --max-type-ignore 5 --max-noqa 3
```

### Testing

For comprehensive testing documentation (philosophy, patterns, coverage goals), see **[Testing Guide](testing.md)**.

```bash
# Run all tests
uv run pytest

# Run with coverage
uv run pytest --cov=src/github_activity_db --cov-report=term-missing

# Run specific test file
uv run pytest tests/db/test_schema.py -v

# Run tests matching a pattern
uv run pytest -k "test_pr"
```

### Database

```bash
# Check current migration
uv run alembic current

# Apply all migrations
uv run alembic upgrade head

# Generate a migration to start from; then follow "Writing a migration" in docs/database.md
uv run alembic revision --autogenerate -m "description"

# View migration history
uv run alembic history
```

Migrations are never downgraded (`downgrade()` raises), and `alembic upgrade` refuses the
tracked `github_activity.db` ([database.md](database.md)). Every `alembic` command that connects,
`current` and `check` included, takes the database's write lock, so it waits up to 5 seconds
while another connection, such as a running sync, holds the file, and then fails with "database
is locked". Each one also checks the file's foreign keys before it commits, so a file holding a
violation fails every command until it is repaired or rebuilt.

## Pre-commit Hooks

Install pre-commit hooks for automatic code quality checks:

```bash
# Install hooks
uv run pre-commit install

# Run on all files manually
uv run pre-commit run --all-files

# Run specific hook
uv run pre-commit run mypy --all-files
```

### Configured Hooks

| Hook | Purpose |
|------|---------|
| `trailing-whitespace` | Remove trailing whitespace |
| `end-of-file-fixer` | Ensure newline at end of files |
| `check-yaml` / `check-toml` | Validate config files |
| `check-added-large-files` | Block files >1MB |
| `check-merge-conflict` | Block merge conflict markers |
| `detect-private-key` | Block private keys |
| `ruff-check` | Linting with auto-fix (ruff from `uv.lock`) |
| `ruff-format` | Code formatting (ruff from `uv.lock`) |
| `mypy` | Type checking of src/, tests/, and scripts/ (mypy from `uv.lock`) |
| `suppression-caps` | Fail above 5 `type: ignore` or 3 `noqa` comments in src/ (`scripts/count_suppressions.py`) |
| `tracked-db-rollback-journal` | `github_activity.db` stays in rollback-journal mode (`scripts/check_db_journal.py`, D20) |
| `tracked-db-frozen` | `github_activity.db` keeps its pinned pre-Step-3 content (`scripts/check_frozen_db.py`, D26) |
| `tracked-db-no-sidecar` | No journal, WAL, or shared-memory file of `github_activity.db` is committed |
| `prevent-any-aliases` | Block `TypeName = Any` patterns |
| `log-placeholders` | loguru messages use `{}` placeholders, not `%` (`scripts/check_log_placeholders.py`) |

ruff and mypy run through `uv run --no-sync`, so hooks never change the environment; run
`uv sync --frozen --extra dev` first.

### Continuous Integration

`.github/workflows/check.yml` runs on pull requests to `main` and pushes to `main`:

- `uv lock --locked` (uv.lock matches pyproject.toml), then pre-commit over all files;
- on pull requests, the base commit's `scripts/check_query_lock.py` (the pull request's own copy
  where the base has none), which fails when an entry of `tests/query_versions.json` was edited
  or removed, or an entry of `tests/schema_versions.json`, a pair of one, or a view digest of
  one, was removed or the digest changed, or pairs or views were added to an entry that is not
  the newest; a lock it cannot read as its rule expects (a malformed entry or digest, or a key
  listed twice) also fails the step;
- pytest on Python 3.12 and 3.13, then `scripts/check_junit.py --max-skipped 0`, which fails
  unless at least one test ran and none failed, errored, or was skipped (pytest reports `xfail`
  as skipped);
- `alembic upgrade head` plus `alembic check` against a fresh database;
- a built wheel (build dependencies hash-checked against `build-constraints.txt`), installed
  into a new environment after the `uv.lock` dependencies (with hash checking, then
  `uv pip check`) and run from outside the checkout (`scripts/wheel_smoke.py`,
  `ghactivity --help`).

## Project Configuration

All configuration is in `pyproject.toml`:

### Ruff (Linting)

```toml
[tool.ruff]
target-version = "py312"
line-length = 100

[tool.ruff.lint]
select = ["E", "W", "F", "I", "B", "C4", "UP", "ASYNC", "S", "PTH", "RUF"]
```

### Mypy (Type Checking)

```toml
[tool.mypy]
python_version = "3.12"
strict = true
plugins = ["pydantic.mypy"]
```

### Pytest

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
```

## Directory Structure

```
src/github_activity_db/
├── __init__.py          # Package version
├── config.py            # Settings (pydantic-settings)
├── logging.py           # loguru setup
├── tracking.py          # tracking.toml loading and validation
├── tracking.toml        # Tracked repositories, coverage floor, sync settings; packaged (D21)
├── cli/
│   ├── app.py           # Typer application, global flags, exit codes
│   ├── common.py        # Shared helpers (consoles, token, repository argument)
│   └── github.py        # `github rate-limit` and `github test`
├── db/
│   ├── engine.py        # Async engine and sessions
│   ├── models.py        # SQLAlchemy models of the head schema
│   ├── schema.py        # SCHEMA_VERSION and the migration revision ids
│   └── types.py         # UtcDateTime
├── source/              # GitHub source layer: the only package that imports githubkit
└── sync/                # Snapshot reader, planner, deletions, writer, run records, and the engine

alembic/
├── env.py               # One transaction per upgrade; refusals (docs/database.md)
└── versions/            # The migration chain

tests/
├── conftest.py          # Session template database built by `alembic upgrade head`
├── db/                  # Migrations, schema comparison, views, timestamp type, tracked file
├── source/              # Source layer against recorded GitHub responses
├── sync/                # Planner, deletions, and writer over seeded databases; the engine over the scenario model
├── fixtures/source/     # Recorded and synthetic GitHub responses
├── data_dictionary.py   # Reader for docs/data-dictionary.md
├── query_versions.json  # Hydration-version lock
├── schema_versions.json # Schema-version lock
└── test_*.py            # CLI, settings, logging, scripts, and quality gates
```

## Adding New Features

### 1. New Database Model

1. Add the model to `src/github_activity_db/db/models.py` and export it from
   `src/github_activity_db/db/__init__.py`
2. Write the migration as "Writing a migration" in [database.md](database.md) describes, and
   add its revision id to `MIGRATION_REVISIONS` in `db/schema.py`
3. Add the table to [data-dictionary.md](data-dictionary.md), and its consumer columns (and the
   digest of any view the migration creates) to `tests/schema_versions.json`

### 2. New CLI Command

1. Create command file in `src/github_activity_db/cli/`
2. Register in `src/github_activity_db/cli/app.py`
3. Add tests in `tests/test_cli_<group>.py`; commands that call GitHub get their client from
   `common.open_source`, which tests replace with one over the recorded-response replay

**CLI Async Pattern:**

All CLI commands use `run_async_command()` from `cli/common.py` for unified async execution:

```python
from github_activity_db.cli import common
from github_activity_db.source import RateLimits

@app.command()
def my_command() -> None:
    """Command description."""
    token = common.github_token()

    async def _impl() -> RateLimits:
        async with common.open_source(token) as source:
            return await source.rate_limits()

    result = common.run_async_command(_impl())
    common.console.print(f"GraphQL remaining: {result.graphql_remaining}")
```

This pattern provides:
- Clean event loop management via `asyncio.run()`
- Unified error handling with user-friendly messages
- Automatic `typer.Exit(1)` on exceptions

## Troubleshooting

### "greenlet library is required"

```bash
uv add greenlet
```

### Alembic can't find models

`alembic/env.py` imports `Base` from `github_activity_db.db.models` and passes `Base.metadata`
to `context.configure()` as `target_metadata`, so `alembic check` and autogenerate compare the
tables `db/models.py` defines.

### SQLite "database is locked"

This can happen with concurrent access. Create engines with `build_engine` from
`github_activity_db.db.engine`, which uses `NullPool` and sets a 30 s busy timeout and foreign keys
on every connection. It does not set the journal mode (redesign plan D20); `scripts/check_db_journal.py`
checks the tracked file:
```python
engine = build_engine(url)
```
