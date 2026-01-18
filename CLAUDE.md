# GitHub Activity DB

A searchable data store for GitHub PR data from Prebid repositories with custom tagging and AI-generated summaries.

## Quick Reference

```bash
# Install & setup
uv sync
cp .env.example .env  # Add GITHUB_TOKEN
uv run alembic upgrade head

# CLI
uv run ghactivity --help
uv run ghactivity github rate-limit --all         # Check rate limits
uv run ghactivity sync pr owner/repo 1234         # Sync single PR
uv run ghactivity sync repo owner/repo --since 2024-10-01  # Bulk sync
uv run ghactivity sync all --since 2024-10-01     # Multi-repo sync

# Development
uv run mypy src/ tests/    # Type check
uv run ruff check src/     # Lint
uv run pytest              # Test (573+ tests)
uv run pre-commit run --all-files  # All quality checks
```

## Documentation

| Document | Description |
|----------|-------------|
| [Contributing](CONTRIBUTING.md) | Quality standards, code style, PR process |
| [Architecture](docs/architecture.md) | Project structure, tech stack, module overview |
| [Database](docs/database.md) | Schema, tables, migrations, usage examples |
| [Data Model](docs/data-model.md) | PR fields, sync behavior, tagging systems |
| [Development](docs/development.md) | Setup, commands, configuration, troubleshooting |
| [Testing](docs/testing.md) | Testing strategy, infrastructure, patterns, coverage |
| [Roadmap](docs/roadmap.md) | Implementation phases and future plans |

## Current Scope

**Implemented:**
- Pull Request data from 8 Prebid repositories
- Custom user tags via CLI
- Agent-generated classifications (`classify_tags`) and summaries (`ai_summary`)
- Rate limit monitoring with proactive tracking
- Request pacing with token bucket algorithm and priority queue scheduler
- Single and bulk PR ingestion pipelines with 2-week grace period for merged PRs
- Multi-repository sync orchestration for all tracked Prebid repositories
- Batch commit resilience to prevent data loss during sync failures
- CLI commands: `ghactivity sync pr`, `ghactivity sync repo`, and `ghactivity sync all`

**Out of Scope (Future):**
- GitHub Issues
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
- `RequestPacer`: Token bucket algorithm with adaptive throttling (integrated into GitHubClient)
- `RequestScheduler`: Priority queue with semaphore-controlled concurrency for bulk operations
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
| `SYNC__MERGE_GRACE_PERIOD_DAYS` | No | `14` | Days after merge before PR is frozen |
| `SYNC__COMMIT_BATCH_SIZE` | No | `25` | PRs to commit per batch (limits data loss on failure) |

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
4. **Tracked:** Pre-commit reports count (target: <=5)

### `noqa` Policy

1. **Centralize** in shared utility functions (e.g., `cli/common.py`)
2. **Document** why the rule doesn't apply
3. **Tracked:** Pre-commit reports count (target: <=3)

### Quality Gates

Pre-commit hooks track:
- `type: ignore` count in src/ (target: <=5)
- `noqa` count in src/ (target: <=3)
- Blocks new `SomeName = Any` type aliases (use TYPE_CHECKING instead)
