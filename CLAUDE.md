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

# Development
uv run mypy src/           # Type check
uv run ruff check src/     # Lint
uv run pytest              # Test
```

## Documentation

| Document | Description |
|----------|-------------|
| [Architecture](docs/architecture.md) | Project structure, tech stack, module overview |
| [Database](docs/database.md) | Schema, tables, migrations, usage examples |
| [Data Model](docs/data-model.md) | PR fields, sync behavior, tagging systems |
| [Development](docs/development.md) | Setup, commands, configuration, troubleshooting |
| [Roadmap](docs/roadmap.md) | Implementation status and future plans |

## Current Scope (Phase 1)

**In Scope:**
- Pull Request data only
- 8 Prebid repositories
- Custom user tags via CLI
- Agent-generated classifications (`classify_tags`) and summaries (`ai_summary`)

**Out of Scope:**
- GitHub Issues (Phase 2)
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

## Status

**Phase 1 in progress.** Core database and CLI scaffold complete. See [Roadmap](docs/roadmap.md) for details.

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GITHUB_TOKEN` | Yes | - | GitHub personal access token |
| `DATABASE_URL` | No | `sqlite+aiosqlite:///./github_activity.db` | Database connection |
| `LOG_LEVEL` | No | `INFO` | Logging level |
| `ENVIRONMENT` | No | `development` | App environment |
