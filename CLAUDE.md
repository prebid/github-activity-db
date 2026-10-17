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
uv run alembic upgrade head

# CLI
uv run ghactivity --help
uv run ghactivity github rate-limit --all         # Check rate limits
uv run ghactivity sync pr owner/repo 1234         # Sync single PR
uv run ghactivity sync repo owner/repo --since 2024-10-01  # Bulk sync
uv run ghactivity sync all --since 2024-10-01     # Multi-repo sync

# Checks (CI runs the same: .github/workflows/check.yml)
uv run pre-commit run --all-files  # ruff, mypy, file checks, quality gates
uv run pytest                      # CI also runs Python 3.12, `alembic check`, and a built wheel
```

## Documentation

| Document | Description |
|----------|-------------|
| [Redesign Plan](docs/redesign-plan.md) | Plan of record: decisions, target design, execution steps, evidence |
| [Contributing](CONTRIBUTING.md) | Quality standards, code style, PR process |
| [Architecture](docs/architecture.md) | Project structure, tech stack, module overview |
| [Database](docs/database.md) | Schema, tables, migrations, usage examples |
| [Data Model](docs/data-model.md) | PR fields, sync behavior, tagging systems |
| [Development](docs/development.md) | Setup, commands, configuration, troubleshooting |
| [Testing](docs/testing.md) | Testing strategy, infrastructure, patterns, coverage |
| [Roadmap](docs/roadmap.md) | History of earlier phases; superseded by the redesign plan |

## Current Scope

**Implemented:**
- Pull request data from the repositories listed in `src/github_activity_db/tracking.toml`
- Rate limit monitoring with proactive tracking
- Request pacing with token bucket algorithm and priority queue scheduler
- Single and bulk PR ingestion pipelines with 2-week grace period for merged PRs
- Multi-repository sync orchestration for all tracked repositories
- CLI commands: `ghactivity sync pr`, `ghactivity sync repo`, and `ghactivity sync all`

**Not implemented despite existing code paths:** `classify_tags` and `ai_summary` have no
producer, the `tags` and `search` commands are stubs, and bulk sync commits only after a
repository finishes. The redesign plan replaces these.

**Planned in the redesign plan:** GitHub Issues and closing-issue links (Step 4); scheduled sync
and release publication (Step 6).

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

### GitHub Client (`github/client.py`)
Async GitHub API client with integrated pacing and rate limit tracking. Every API method automatically applies pacing delays before requests and feeds response headers back to the pacer for adaptive throttling. Provides both eager (`list_pull_requests`) and lazy (`iter_pull_requests`) iteration for efficient pagination.

### Rate Limiting (`github/rate_limit/`)
- `RateLimitMonitor`: Tracks rate limit state from response headers (zero API cost)
- `RateLimitStatus`: State machine (HEALTHY → WARNING → CRITICAL → EXHAUSTED)

### Request Pacing (`github/pacing/`)
- `AsyncTokenBucket`: Concurrency-safe shared admission gate (the primitive that holds the rate)
- `RequestPacer`: Façade over the token bucket; exposes `acquire()`, `force_wait*`, `get_stats()`
- `RequestScheduler`: Priority queue with semaphore-bounded dispatch (acquires the slot before popping so retried HIGH-priority requests jump the queue)
- `BatchExecutor`: Coordinates batch operations with progress tracking

### PR Ingestion (`github/sync/`)
- `PRIngestionService`: Single PR fetch → transform → store pipeline
- `BulkPRIngestionService`: Multi-PR import using lazy iteration for efficient date filtering
- `MultiRepoOrchestrator`: Coordinates syncing all tracked Prebid repositories
- `CommitManager`: Batch commit boundaries for database resilience
- `PRIngestionResult` / `BulkIngestionResult` / `MultiRepoSyncResult`: Structured results

### Repository Layer (`db/repositories/`)
- `RepositoryRepository`: CRUD for Repository table with get_or_create
- `PullRequestRepository`: CRUD for PullRequest table with frozen state handling

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GITHUB_TOKEN` | Yes | - | GitHub personal access token |
| `DATABASE_URL` | No | `sqlite+aiosqlite:///./github_activity.db` | Database connection |
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
- more than 3 `noqa` comments in src/ (file-level `# ruff: noqa` and `# flake8: noqa`, and
  the isort skip comments, count too)
- a blanket `# noqa` without rule codes (ruff `PGH004`)
- new module-level type aliases to `Any` (`SomeName = Any`, `type SomeName = Any`, unions and
  `Optional`/`Annotated` forms; `scripts/check_any_aliases.py`); use TYPE_CHECKING instead
- files over 1000 KB anywhere in the repository (`github_activity.db` exempt while tracked)
- `github_activity.db` in WAL mode (`scripts/check_db_journal.py`; redesign plan D20)

`scripts/count_suppressions.py` counts comment tokens and decorators in `.py` and `.pyi` files,
in the spellings its docstring lists; text inside strings is not counted.

ruff and mypy run from `uv.lock` through `uv run --no-sync`, so local and CI versions match.
