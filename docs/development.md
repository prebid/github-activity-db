# Development Guide

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager

## Quick Start

```bash
# Clone the repository
git clone https://github.com/prebid/github-activity-db.git
cd github-activity-db

# Install dependencies
uv sync

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
DATABASE_URL=sqlite+aiosqlite:///./github_activity.db
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

# Add a dev dependency
uv add --dev package-name

# Update all dependencies
uv lock --upgrade
uv sync
```

### CLI

```bash
# Show help
uv run ghactivity --help

# Show version
uv run ghactivity --version

# GitHub commands
uv run ghactivity github rate-limit           # Check core rate limit
uv run ghactivity github rate-limit --all     # Show all pools
uv run ghactivity github rate-limit --all -v  # Verbose with reset times

# Sync commands - single PR
uv run ghactivity sync pr owner/repo 1234              # Sync single PR
uv run ghactivity sync pr owner/repo 1234 --verbose    # Detailed output
uv run ghactivity sync pr owner/repo 1234 --quiet      # Silent (errors only)
uv run ghactivity sync pr owner/repo 1234 --dry-run    # Preview without writing
uv run ghactivity sync pr owner/repo 1234 --format json # JSON output

# Sync commands - bulk repository
uv run ghactivity sync repo owner/repo                    # Sync all PRs
uv run ghactivity sync repo owner/repo --since 2024-10-01 # Since date
uv run ghactivity sync repo owner/repo --state open       # Only open PRs
uv run ghactivity sync repo owner/repo --max 10           # Limit count
uv run ghactivity sync repo owner/repo --dry-run          # Preview mode
uv run ghactivity sync repo owner/repo --format json      # JSON output

# Not yet implemented
uv run ghactivity search --help
uv run ghactivity user-tags --help
```

### Code Quality

```bash
# Type checking (strict mode for src/, relaxed for tests/)
uv run mypy src/
uv run mypy tests/  # Runs with relaxed settings
uv run mypy src/ tests/  # Check everything

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
| `type: ignore` comments in src/ | ≤5 | Pre-commit audit |
| `noqa` comments in src/ | ≤3 | Pre-commit audit |
| `Any` type aliases | 0 | Pre-commit blocks |
| Mypy errors | 0 | Pre-commit blocks |

```bash
# Check current quality metrics
grep -r "type: ignore" src/ | wc -l  # type:ignore count
grep -r "noqa:" src/ | wc -l         # noqa count
```

### Testing

For comprehensive testing documentation (philosophy, patterns, coverage goals), see **[Testing Guide](testing.md)**.

```bash
# Run all tests
uv run pytest

# Run with coverage
uv run pytest --cov=src/github_activity_db --cov-report=term-missing

# Run specific test file
uv run pytest tests/test_db_models.py -v

# Run tests matching a pattern
uv run pytest -k "test_pr"
```

### Database

```bash
# Check current migration
uv run alembic current

# Apply all migrations
uv run alembic upgrade head

# Generate new migration
uv run alembic revision --autogenerate -m "description"

# Rollback one migration
uv run alembic downgrade -1

# View migration history
uv run alembic history
```

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
| `ruff` | Linting with auto-fix |
| `ruff-format` | Code formatting |
| `mypy` | Type checking (src/ and tests/) |
| `audit-type-ignores` | Report `type: ignore` count (target ≤5) |
| `audit-noqa` | Report `noqa` count (target ≤3) |
| `prevent-any-aliases` | Block `TypeName = Any` patterns |

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
├── cli/                 # CLI commands
│   ├── __init__.py
│   ├── app.py           # Typer application
│   ├── common.py        # Shared option factories
│   ├── github.py        # GitHub commands (rate-limit)
│   └── sync.py          # Sync commands (sync pr)
├── db/                  # Database layer
│   ├── __init__.py      # Public exports
│   ├── models.py        # SQLAlchemy models
│   ├── engine.py        # Async engine/sessions
│   └── repositories/    # Repository pattern
│       ├── base.py      # BaseRepository ABC
│       ├── repository.py # RepositoryRepository
│       └── pull_request.py # PullRequestRepository
├── schemas/             # Pydantic validation models
│   ├── __init__.py      # Re-exports all schemas
│   ├── base.py          # SchemaBase with factory pattern
│   ├── pr.py            # PRCreate, PRSync, PRMerge, PRRead
│   ├── repository.py    # RepositoryCreate, RepositoryRead, parse_repo_string()
│   ├── tag.py           # UserTagCreate, UserTagRead
│   ├── nested.py        # CommitBreakdown, ParticipantEntry
│   └── github_api.py    # GitHub API response schemas
├── github/              # GitHub integration
│   ├── client.py        # githubkit wrapper
│   ├── exceptions.py    # Custom exceptions
│   ├── rate_limit/      # Rate limit monitoring
│   ├── pacing/          # Request pacing
│   └── sync/            # PR ingestion service
└── search/              # Search logic [TODO]

tests/
├── conftest.py          # Shared fixtures (db_session, sample data)
├── factories.py         # Factory functions for test data
├── fixtures/            # Mock data and real GitHub fixtures
├── db/repositories/     # Repository tests
├── github/              # GitHub module tests
│   ├── rate_limit/      # Rate limit tests
│   ├── pacing/          # Pacing tests
│   └── sync/            # Ingestion tests
├── test_config.py       # Settings tests
├── test_db_*.py         # Database tests
├── test_schemas_*.py    # Schema validation tests
├── test_pr_ingestion_e2e.py # E2E integration tests
└── test_cli_sync.py     # CLI sync command tests
```

## Adding New Features

### 1. New Database Model

1. Add model class to `src/github_activity_db/db/models.py`
2. Export from `src/github_activity_db/db/__init__.py`
3. Generate migration: `uv run alembic revision --autogenerate -m "add model"`
4. Apply migration: `uv run alembic upgrade head`

### 2. New CLI Command

1. Create command file in `src/github_activity_db/cli/`
2. Register in `src/github_activity_db/cli/app.py`
3. Add tests in `tests/test_cli/`

**CLI Async Pattern:**

All CLI commands use `run_async_command()` from `cli/common.py` for unified async execution:

```python
from github_activity_db.cli.common import console, run_async_command

@app.command()
def my_command() -> None:
    """Command description."""
    async def _impl() -> dict[str, Any]:
        async with GitHubClient() as client:
            return await client.some_method()

    result = run_async_command(_impl())
    console.print(f"Result: {result}")
```

This pattern provides:
- Clean event loop management via `asyncio.run()`
- Unified error handling with user-friendly messages
- Automatic `typer.Exit(1)` on exceptions

### 3. New Pydantic Schema

1. Add schema to `src/github_activity_db/schemas/`
2. Export from `src/github_activity_db/schemas/__init__.py`
3. Add validation tests

## Troubleshooting

### "greenlet library is required"

```bash
uv add greenlet
```

### Alembic can't find models

Ensure `alembic/env.py` imports the models:
```python
from github_activity_db.db.models import Base
target_metadata = Base.metadata
```

### SQLite "database is locked"

This can happen with concurrent access. The async engine uses `NullPool` to avoid connection issues:
```python
create_async_engine(url, poolclass=pool.NullPool)
```

### Type errors with JSON columns

JSON columns return `Any` type. Cast explicitly:
```python
labels: list[str] = pr.github_labels  # type: ignore[assignment]
```
