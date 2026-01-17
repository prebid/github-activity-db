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

# Development
uv run mypy src/           # Type check
uv run ruff check src/     # Lint
uv run pytest              # Test (403+ tests)
```

## Documentation

| Document | Description |
|----------|-------------|
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
- CLI commands: `ghactivity sync pr` and `ghactivity sync repo`

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
Async GitHub API client with integrated rate limit tracking. Provides both eager (`list_pull_requests`) and lazy (`iter_pull_requests`) iteration for efficient pagination.

### Rate Limiting (`github/rate_limit/`)
- `RateLimitMonitor`: Tracks rate limit state from response headers (zero API cost)
- `RateLimitStatus`: State machine (HEALTHY → WARNING → CRITICAL → EXHAUSTED)

### Request Pacing (`github/pacing/`)
- `RequestPacer`: Token bucket algorithm with adaptive throttling
- `RequestScheduler`: Priority queue with semaphore-controlled concurrency
- `BatchExecutor`: Coordinates batch operations with progress tracking

### PR Ingestion (`github/sync/`)
- `PRIngestionService`: Single PR fetch → transform → store pipeline
- `BulkPRIngestionService`: Multi-PR import using lazy iteration for efficient date filtering
- `PRIngestionResult` / `BulkIngestionResult`: Structured results (created/updated/skipped/failed)

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
