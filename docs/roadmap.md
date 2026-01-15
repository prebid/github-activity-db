# Roadmap

## Phase 1: Core Implementation (Current)

### Completed

- [x] Project scaffolding (uv, pyproject.toml, ruff, mypy)
- [x] Database models (Repository, PullRequest, UserTag)
- [x] Async SQLAlchemy engine and session management
- [x] Configuration with pydantic-settings
- [x] Alembic migrations (initial schema)
- [x] CLI scaffold with typer

### In Progress

#### Step 7: Pydantic Schemas

Create input/output validation models in `src/github_activity_db/schemas/`:

```
schemas/
├── __init__.py          # Re-exports all schemas
├── base.py              # Base schema class with factory pattern
├── enums.py             # ParticipantActionType enum
├── nested.py            # CommitBreakdown, ParticipantEntry models
├── repository.py        # RepositoryCreate, RepositoryRead
├── pr.py                # PRCreate, PRSync, PRMerge, PRRead
├── tag.py               # UserTagCreate, UserTagRead
└── github_api.py        # GitHub API response schemas
```

**Key schemas:**
- `PRCreate` - Immutable fields (number, link, open_date, submitter)
- `PRSync` - Synced fields for updates
- `PRMerge` - Merge-only fields (close_date, merged_by, ai_summary)
- `PRRead` - Full output schema with all fields

**Nested models:**
- `CommitBreakdown` - `{date: datetime, author: str}`
- `ParticipantEntry` - `{username: str, actions: list[ParticipantActionType]}`

**GitHub API schemas (for parsing API responses):**
- `GitHubUser` - Parse user objects
- `GitHubLabel` - Parse label objects
- `GitHubPullRequest` - Parse full PR response
- `GitHubCommit`, `GitHubFile`, `GitHubReview` - Parse endpoint responses

**Factory pattern:**
- `SchemaBase.from_orm(model)` - Convert SQLAlchemy → Pydantic
- `PRCreate.from_github(gh_pr)` - Convert GitHub API → Pydantic

**Validation rules:**
- `title`: max 500 chars
- `link`: max 500 chars, valid URL
- `submitter`, `merged_by`: max 100 chars
- `color`: regex `^#[0-9a-fA-F]{6}$`

#### Step 11: Test Fixtures

Create test infrastructure in `tests/conftest.py`:

- In-memory SQLite database for fast tests
- Factory functions for sample data
- Async fixtures for database sessions
- Sample Repository, PullRequest, UserTag objects
- PRs in all states (open, merged, closed)

#### Step 12: CLI Commands

Implement full CLI in `src/github_activity_db/cli/`:

```bash
# Sync commands
ghactivity sync <owner/repo>      # Sync single repo
ghactivity sync --all             # Sync all 8 repos

# Search commands
ghactivity search                 # List all PRs
ghactivity search --state open    # Filter by state
ghactivity search --repo <name>   # Filter by repo
ghactivity search --submitter <user>

# User tag commands
ghactivity user-tags list
ghactivity user-tags create <name> [--color #hex]
ghactivity user-tags add <pr-number> <tag-name>
ghactivity user-tags remove <pr-number> <tag-name>
```

#### Step 13: GitHub Client

Implement GitHub API integration in `src/github_activity_db/github/`:

**client.py:**
- githubkit wrapper with authentication
- Rate limiting handling
- Pagination support

**sync.py:**
- Fetch PRs from GitHub API
- Compare `last_update_date` for change detection
- Handle state transitions (open → merged/closed)
- Trigger agent processing on merge

**API endpoints needed:**
| Endpoint | Data |
|----------|------|
| `GET /repos/{owner}/{repo}/pulls` | List PRs |
| `GET /repos/{owner}/{repo}/pulls/{number}` | PR details |
| `GET /repos/{owner}/{repo}/pulls/{number}/files` | Changed files |
| `GET /repos/{owner}/{repo}/pulls/{number}/commits` | Commit history |
| `GET /repos/{owner}/{repo}/pulls/{number}/reviews` | Reviews |

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
