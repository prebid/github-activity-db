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

# Development
uv run mypy src/           # Type check
uv run ruff check src/     # Lint
uv run pytest              # Test (304 tests)
```

## Documentation

| Document | Description |
|----------|-------------|
| [Architecture](docs/architecture.md) | Project structure, tech stack, module overview |
| [Database](docs/database.md) | Schema, tables, migrations, usage examples |
| [Data Model](docs/data-model.md) | PR fields, sync behavior, tagging systems |
| [Development](docs/development.md) | Setup, commands, configuration, troubleshooting |
| [Roadmap](docs/roadmap.md) | Implementation status and future plans |

## Current Scope

**Phase 1 (Complete):**
- Pull Request data only
- 8 Prebid repositories
- Custom user tags via CLI
- Agent-generated classifications (`classify_tags`) and summaries (`ai_summary`)

**Phase 1.5 (Complete):**
- Rate limit monitoring with proactive tracking
- Request pacing with token bucket algorithm
- Priority queue scheduler with concurrency control
- Batch execution with progress tracking

**Phase 1.6 (Complete):**
- Single PR ingestion pipeline (fetch → transform → store)
- Repository layer with CRUD operations
- 2-week grace period for merged PRs
- Diff detection (skip unchanged PRs)
- CLI: `ghactivity sync pr` with --dry-run, --format, --quiet, --verbose

**Out of Scope (Phase 2+):**
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
Async GitHub API client with integrated rate limit tracking.

### Rate Limiting (`github/rate_limit/`)
- `RateLimitMonitor`: Tracks rate limit state from response headers (zero API cost)
- `RateLimitStatus`: State machine (HEALTHY → WARNING → CRITICAL → EXHAUSTED)
- Passive header tracking on every API response

### Request Pacing (`github/pacing/`)
- `RequestPacer`: Token bucket algorithm with adaptive throttling
- `RequestScheduler`: Priority queue with semaphore-controlled concurrency
- `BatchExecutor`: Coordinates batch operations
- `ProgressTracker`: Observable progress reporting

### PR Ingestion (`github/sync/`)
- `PRIngestionService`: Orchestrates fetch → transform → store pipeline
- `PRIngestionResult`: Structured result with action tracking (created/updated/skipped)

### Repository Layer (`db/repositories/`)
- `RepositoryRepository`: CRUD for Repository table with get_or_create
- `PullRequestRepository`: CRUD for PullRequest table with frozen state handling

## Status

**Phase 1.6 complete.** Single PR ingestion pipeline implemented. See [Roadmap](docs/roadmap.md) for details.

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GITHUB_TOKEN` | Yes | - | GitHub personal access token |
| `DATABASE_URL` | No | `sqlite+aiosqlite:///./github_activity.db` | Database connection |
| `LOG_LEVEL` | No | `INFO` | Logging level |
| `ENVIRONMENT` | No | `development` | App environment |
| `SYNC__MERGE_GRACE_PERIOD_DAYS` | No | `14` | Days after merge before PR is frozen |
