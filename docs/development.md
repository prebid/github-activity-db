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

# Subcommands (when implemented)
uv run ghactivity sync --help
uv run ghactivity search --help
uv run ghactivity user-tags --help
```

### Code Quality

```bash
# Type checking
uv run mypy src/

# Linting
uv run ruff check src/ tests/

# Auto-fix lint issues
uv run ruff check --fix src/ tests/

# Formatting
uv run ruff format src/ tests/

# Check formatting without changes
uv run ruff format --check src/ tests/
```

### Testing

```bash
# Run all tests
uv run pytest

# Run with coverage
uv run pytest --cov

# Run specific test file
uv run pytest tests/test_db/test_models.py

# Run with verbose output
uv run pytest -v
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
```

Hooks configured:
- `ruff` - Linting and formatting
- `mypy` - Type checking
- `trailing-whitespace` - Remove trailing whitespace
- `end-of-file-fixer` - Ensure newline at end of files
- `check-yaml` / `check-toml` - Validate config files

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
│   └── app.py           # Typer application
├── db/                  # Database layer
│   ├── __init__.py      # Public exports
│   ├── models.py        # SQLAlchemy models
│   └── engine.py        # Async engine/sessions
├── github/              # GitHub API [TODO]
├── schemas/             # Pydantic models [TODO]
└── search/              # Search logic [TODO]
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
