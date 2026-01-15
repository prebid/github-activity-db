# Roadmap

## Phase 1: Core Implementation (Current)

### Completed

- [x] Project scaffolding (uv, pyproject.toml, ruff, mypy)
- [x] Database models (Repository, PullRequest, UserTag)
- [x] Async SQLAlchemy engine and session management
- [x] Configuration with pydantic-settings
- [x] Alembic migrations (initial schema)
- [x] CLI scaffold with typer
- [x] Pydantic schemas (validation, GitHub API parsing, factory pattern)
- [x] Test infrastructure (69 tests, 87% coverage)
- [x] Github client

---

## Phase 2: Enhanced Features (Future)

### GitHub Issues Support

- [ ] Issue data model (similar to PR)
- [ ] Issue sync from GitHub API
- [ ] Issue tagging and search

### Agent Integration

- [ ] `classify_tags` generation pipeline
- [ ] `ai_summary` generation on PR merge
- [ ] Configurable prompts/models

### Search Enhancements

- [ ] Full-text search on title/description
- [ ] Date range filtering
- [ ] Export to CSV/JSON

---

## Phase 3: Advanced Features (Future)

### Real-time Sync

- [ ] GitHub webhooks support
- [ ] Incremental updates
- [ ] Background sync daemon

### Web Interface

- [ ] REST API layer
- [ ] Simple web UI for browsing
- [ ] Dashboard with statistics

### Multi-org Support

- [ ] Support repos outside Prebid org
- [ ] Configurable repo list via CLI
- [ ] Per-repo sync settings
