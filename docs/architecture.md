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
│   │   ├── sync.py             # Sync commands [TODO]
│   │   ├── search.py           # Search commands [TODO]
│   │   └── tags.py             # Tag management [TODO]
│   ├── db/                     # Database layer
│   │   ├── models.py           # SQLAlchemy ORM models
│   │   ├── engine.py           # Async engine/session
│   │   └── repositories.py     # Data access layer [TODO]
│   ├── github/                 # GitHub integration [TODO]
│   │   ├── client.py           # githubkit wrapper
│   │   └── sync.py             # Sync logic
│   ├── schemas/                # Pydantic models [TODO]
│   │   ├── pr.py               # PR schemas
│   │   └── tag.py              # Tag schemas
│   └── search/                 # Search module [TODO]
│       └── query.py            # Query builder
├── alembic/                    # Database migrations
│   ├── env.py                  # Async alembic config
│   └── versions/               # Migration files
├── tests/                      # Test suite
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
| `config.py` | ✅ Complete | 8 repos configured |
| `db/models.py` | ✅ Complete | 4 tables, 26 columns |
| `db/engine.py` | ✅ Complete | Async session factory |
| `cli/app.py` | ✅ Scaffold | Stub commands |
| `alembic/` | ✅ Complete | Initial migration applied |
| `schemas/` | 🔲 TODO | Pydantic models |
| `db/repositories.py` | 🔲 TODO | Data access layer |
| `github/client.py` | 🔲 TODO | API wrapper |
| `github/sync.py` | 🔲 TODO | Sync logic |
| `search/query.py` | 🔲 TODO | Search builder |
| `tests/conftest.py` | 🔲 TODO | Test fixtures |
