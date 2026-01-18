# Architecture Overview

## Project Purpose

GitHub Activity DB is a searchable data store for GitHub Pull Request data from Prebid organization repositories. It supports:

- Fetching and storing PR metadata from GitHub API
- Custom user tagging via CLI
- Agent-generated classifications and summaries
- Search and filtering capabilities

## Tech Stack

| Layer | Technology | Purpose |
|-------|------------|---------|
| **Runtime** | Python 3.12+ | Core language |
| **Database** | SQLite | Local storage |
| **ORM** | SQLAlchemy 2.0 + aiosqlite | Async database access |
| **Validation** | Pydantic 2.0 | Data validation and serialization |
| **GitHub API** | githubkit | Async, typed GitHub client |
| **CLI** | typer + rich | Command-line interface |
| **Migrations** | alembic | Database schema migrations |
| **Package Manager** | uv | Fast dependency management |

## Project Structure

```
github-activity-db/
├── src/github_activity_db/     # Main package
│   ├── __init__.py             # Package version
│   ├── config.py               # Settings (pydantic-settings)
│   ├── cli/                    # CLI commands
│   │   ├── app.py              # Main typer app
│   │   ├── common.py           # Shared helpers: run_async_command, option factories
│   │   ├── github.py           # GitHub commands (rate-limit)
│   │   ├── sync.py             # Sync commands (sync pr)
│   │   ├── search.py           # Search commands [TODO]
│   │   └── tags.py             # Tag management [TODO]
│   ├── db/                     # Database layer
│   │   ├── models.py           # SQLAlchemy ORM models
│   │   ├── engine.py           # Async engine/session
│   │   └── repositories/       # Repository pattern
│   │       ├── base.py         # BaseRepository ABC
│   │       ├── repository.py   # RepositoryRepository
│   │       └── pull_request.py # PullRequestRepository
│   ├── github/                 # GitHub integration
│   │   ├── client.py           # githubkit wrapper with integrated pacing
│   │   ├── exceptions.py       # Custom GitHub exceptions
│   │   ├── rate_limit/         # Rate limit monitoring
│   │   │   ├── schemas.py      # RateLimitPool, PoolRateLimit, RateLimitSnapshot
│   │   │   └── monitor.py      # RateLimitMonitor (state machine)
│   │   ├── pacing/             # Request pacing
│   │   │   ├── pacer.py        # RequestPacer (token bucket algorithm)
│   │   │   ├── scheduler.py    # RequestScheduler (priority queue)
│   │   │   ├── batch.py        # BatchExecutor
│   │   │   └── progress.py     # ProgressTracker
│   │   └── sync/               # PR sync/ingestion
│   │       ├── ingestion.py    # PRIngestionService (single PR)
│   │       ├── bulk_ingestion.py # BulkPRIngestionService (multi-PR)
│   │       ├── results.py      # PRIngestionResult
│   │       └── enums.py        # SyncStrategy, OutputFormat
│   ├── schemas/                # Pydantic validation models
│   │   ├── __init__.py         # Re-exports all schemas
│   │   ├── base.py             # SchemaBase with factory pattern
│   │   ├── enums.py            # ParticipantActionType enum
│   │   ├── nested.py           # CommitBreakdown, ParticipantEntry
│   │   ├── repository.py       # RepositoryCreate, RepositoryRead, parse_repo_string()
│   │   ├── pr.py               # PRCreate, PRSync, PRMerge, PRRead
│   │   ├── tag.py              # UserTagCreate, UserTagRead
│   │   └── github_api.py       # GitHub API response schemas
│   └── search/                 # Search module [TODO]
│       └── query.py            # Query builder
├── alembic/                    # Database migrations
│   ├── env.py                  # Async alembic config
│   └── versions/               # Migration files
├── tests/                      # Test suite
│   ├── conftest.py             # Shared fixtures (db_session, sample data)
│   ├── factories.py            # Factory functions for test data
│   ├── fixtures/               # Mock data
│   │   └── github_responses.py # GitHub API mock responses
│   ├── test_config.py          # Settings tests
│   ├── test_db_engine.py       # Engine & session tests
│   ├── test_db_models.py       # ORM model tests
│   ├── test_schemas_*.py       # Schema validation tests
│   └── ...
├── docs/                       # Documentation
├── pyproject.toml              # Project configuration
└── uv.lock                     # Dependency lockfile
```

## Design Principles

### 1. Async-First
All database operations use async SQLAlchemy with aiosqlite. This allows efficient concurrent operations when syncing multiple repositories.

### 2. Type Safety
- Strict mypy configuration
- Pydantic for runtime validation
- SQLAlchemy 2.0 mapped columns with type hints

### 3. Separation of Concerns
- **Models** (`db/models.py`): Pure data structure definitions
- **Engine** (`db/engine.py`): Connection and session management
- **Schemas** (`schemas/`): Input/output validation
- **Repositories** (`db/repositories.py`): Data access patterns

## Schemas Module

The `schemas/` module provides Pydantic models for validation and serialization:

### Schema Categories

| Category | Schemas | Purpose |
|----------|---------|---------|
| **Base** | `SchemaBase` | Factory pattern with `from_orm()` method |
| **PR** | `PRCreate`, `PRSync`, `PRMerge`, `PRRead` | PR lifecycle stages |
| **Repository** | `RepositoryCreate`, `RepositoryRead` | Repository CRUD |
| **Tags** | `UserTagCreate`, `UserTagRead` | User tag management |
| **Nested** | `CommitBreakdown`, `ParticipantEntry` | Complex field types |
| **GitHub API** | `GitHubPullRequest`, `GitHubUser`, etc. | Parse API responses |

### PR Schema Lifecycle

```
GitHub API Response
       │
       ▼
 GitHubPullRequest.to_pr_create()  →  PRCreate (immutable fields)
 GitHubPullRequest.to_pr_sync()    →  PRSync (synced fields)
       │
       ▼
  SQLAlchemy Model
       │
       ▼
 PRRead.from_orm(model)  →  PRRead (output)
       │
       ▼
  CLI / API Response
```

### Factory Pattern

All schemas inherit from `SchemaBase` which provides:

```python
# Convert SQLAlchemy model to Pydantic schema
pr_read = PRRead.from_orm(pr_model)

# Convert list of models
pr_list = PRRead.from_orm_list(pr_models)

# Convert GitHub API response to internal schemas
pr_create = github_pr.to_pr_create(repository_id)
pr_sync = github_pr.to_pr_sync(files, commits, reviews)
```

### Validation Rules

| Field | Constraint |
|-------|------------|
| `title` | max 500 chars |
| `link` | max 500 chars, valid URL |
| `submitter`, `merged_by` | max 100 chars |
| `classify_tags` | max 500 chars |
| `color` | hex format `#rrggbb` |

### 4. Configuration Management
Environment-based configuration via pydantic-settings:
- `.env` file for local development
- Environment variables for production
- Type-safe with validation

## Data Flow

```
GitHub API → githubkit → Pydantic Schema → SQLAlchemy Model → SQLite
                              ↓
                      Agent Processing
                      (classify_tags, ai_summary)
```

### Sync Process
1. Fetch open PRs from GitHub API
2. Compare `last_update_date` with stored value
3. Update changed PRs (if still open)
4. For newly merged PRs:
   - Set `close_date`, `merged_by`
   - Trigger AI summary generation
5. Skip already-merged PRs in database

## Module Dependencies

```
config.py ←── db/engine.py ←── db/models.py
    ↑              ↑
    └──────────────┴─── cli/app.py
                        github/client.py
                        schemas/*.py
```

## Implementation Status

| Module | Status | Notes |
|--------|--------|-------|
| `config.py` | ✅ Complete | 8 repos, rate limit, pacing & sync configs |
| `db/models.py` | ✅ Complete | 4 tables, 26 columns |
| `db/engine.py` | ✅ Complete | Async session factory |
| `db/repositories/` | ✅ Complete | Repository, PullRequest repositories |
| `cli/app.py` | ✅ Complete | GitHub and sync commands |
| `cli/sync.py` | ✅ Complete | Single PR sync with --dry-run, --format, etc. |
| `alembic/` | ✅ Complete | Initial migration applied |
| `schemas/` | ✅ Complete | 8 files, factory pattern, GitHub API schemas |
| `github/client.py` | ✅ Complete | API wrapper with integrated pacing and rate limit tracking |
| `github/rate_limit/` | ✅ Complete | Monitor, schemas, state machine |
| `github/pacing/` | ✅ Complete | Pacer, scheduler, batch, progress |
| `github/sync/` | ✅ Complete | PRIngestionService, BulkPRIngestionService, CommitManager, results, enums |
| `tests/` | ✅ Complete | 533+ tests, factory pattern |
| `search/query.py` | 🔲 TODO | Search builder |

## Test Infrastructure

The `tests/` module provides comprehensive test coverage using pytest-asyncio. For detailed testing documentation including philosophy, patterns, and coverage goals, see **[Testing Guide](testing.md)**.

### Quick Reference

```bash
# Run all tests
uv run pytest

# Run with coverage
uv run pytest --cov=src/github_activity_db --cov-report=term-missing
```

### Test Organization

| Directory | Purpose |
|-----------|---------|
| `tests/conftest.py` | Shared fixtures (db_session, engine) |
| `tests/factories.py` | Factory functions for test data |
| `tests/fixtures/` | Mock data and real GitHub fixtures |
| `tests/db/` | Database layer tests |
| `tests/github/` | GitHub module tests (pacing, rate_limit, sync) |
| `tests/test_*.py` | Top-level module tests |
