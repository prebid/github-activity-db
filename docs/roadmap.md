# Roadmap

This document describes the implementation phases and architecture of GitHub Activity DB.

---

## Unified Architecture

This diagram shows how all components fit together into a cohesive system:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        Application Layer                                 │
│                    CLI Commands (cli/*.py)                               │
│         ghactivity sync pr | ghactivity sync repo | ghactivity github    │
└────────────────────────────────┬────────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                         Service Layer                                    │
│          PRIngestionService  |  BulkPRIngestionService                   │
│                                                                          │
│    Orchestrates "what to do": fetch PR → transform → store               │
│    Abstracted from rate limiting details                                 │
└───────────────┬─────────────────────────────────┬───────────────────────┘
                │                                 │
                │ fetches via                     │ stores via
                ▼                                 ▼
┌───────────────────────────────────┐   ┌─────────────────────────────────┐
│        GitHub Layer               │   │      Repository Layer           │
│  ┌─────────────────────────────┐  │   │     (db/repositories/)          │
│  │       GitHubClient          │  │   │                                 │
│  │   get_full_pull_request()   │  │   │  RepositoryRepository           │
│  │   iter_pull_requests()      │  │   │  PullRequestRepository          │
│  └──────────────┬──────────────┘  │   └────────────────┬────────────────┘
│                 │                 │                    │
│  ┌──────────────▼──────────────┐  │                    │
│  │   Orchestration Layer       │  │                    │
│  │   BatchExecutor             │  │                    │
│  │   RequestScheduler          │  │                    │
│  │   ProgressTracker           │  │                    │
│  └──────────────┬──────────────┘  │                    │
│                 │                 │                    │
│  ┌──────────────▼──────────────┐  │                    │
│  │   Control Layer             │  │                    │
│  │   RequestPacer              │  │                    │
│  │   RateLimitMonitor          │  │                    │
│  └──────────────┬──────────────┘  │                    │
│                 │                 │                    │
└─────────────────┼─────────────────┘                    │
                  │                                      │
                  ▼                                      ▼
           GitHub API                          ┌─────────────────┐
                                               │  Database Layer │
                                               │  (db/models.py) │
                                               │  SQLite + ORM   │
                                               └─────────────────┘
```

---

## Phase 1: Core Implementation

Foundation layer providing database models, GitHub client, and CLI scaffold.

**Components:**
- Project scaffolding (uv, pyproject.toml, ruff, mypy)
- Database models (Repository, PullRequest, UserTag)
- Async SQLAlchemy engine and session management
- Configuration with pydantic-settings
- Alembic migrations
- CLI scaffold with typer
- Pydantic schemas with factory pattern
- GitHub client with error handling

---

## Phase 1.5: Rate Limiting & Request Pacing

Internal layers within the GitHub module for request control. These components are internal to the GitHub layer - higher layers don't interact with them directly.

### Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     GitHub Layer (github/)                       │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │                      GitHubClient                          │  │
│  │              (public API for callers)                      │  │
│  └─────────────────────────┬─────────────────────────────────┘  │
│                            │ uses internally                     │
│  ┌─────────────────────────▼─────────────────────────────────┐  │
│  │                  Orchestration Layer                       │  │
│  │    BatchExecutor │ RequestScheduler │ ProgressTracker      │  │
│  └─────────────────────────┬─────────────────────────────────┘  │
│                            │ delegates to                        │
│  ┌─────────────────────────▼─────────────────────────────────┐  │
│  │                    Control Layer                           │  │
│  │         RequestPacer │ RateLimitMonitor                    │  │
│  └─────────────────────────┬─────────────────────────────────┘  │
│                            │                                     │
└────────────────────────────┼─────────────────────────────────────┘
                             ▼
                       GitHub API
```

### Component Responsibilities

| Component | Responsibility | Does NOT |
|-----------|----------------|----------|
| `RateLimitMonitor` | Track rate limit state from headers | Make pacing decisions |
| `RequestPacer` | Calculate optimal delays | Queue or execute requests |
| `RequestScheduler` | Queue and prioritize requests | Calculate delays |
| `BatchExecutor` | Coordinate batch operations | Implement queueing |
| `ProgressTracker` | Observe and report progress | Affect execution |

### Token Bucket Algorithm

```
INPUTS:
  remaining   = requests left in window
  reset_time  = when window resets (UTC)
  buffer_pct  = reserve percentage (default 10%)

CALCULATION:
  time_left   = reset_time - now()
  buffer      = limit * buffer_pct
  effective   = max(1, remaining - buffer)
  base_delay  = time_left / effective

ADAPTIVE THROTTLE (multiplier by health status):
  > 50% remaining:  1.0x (healthy)
  20-50% remaining: 1.5x (warning)
  5-20% remaining:  2.0x (critical)
  < 5% remaining:   4.0x (exhausted soon)

OUTPUT:
  delay = clamp(base_delay * multiplier, min=0.05s, max=60s)
```

### Rate Limit State Machine

```
HEALTHY (>50%) ─────┐
    ▲               │ remaining drops
    │               ▼
    │         WARNING (20-50%)
    │               │
    │               ▼
    │         CRITICAL (5-20%)
    │               │
    │               ▼
    └───────── EXHAUSTED (0) ─── wait for reset
```

### File Structure

```
src/github_activity_db/github/
├── rate_limit/
│   ├── __init__.py         # Public exports
│   ├── schemas.py          # RateLimitPool, PoolRateLimit, RateLimitSnapshot
│   └── monitor.py          # RateLimitMonitor
└── pacing/
    ├── __init__.py         # Public exports
    ├── pacer.py            # RequestPacer (token bucket)
    ├── scheduler.py        # RequestScheduler (priority queue)
    ├── batch.py            # BatchExecutor
    └── progress.py         # ProgressTracker
```

### CLI Commands

```bash
ghactivity github rate-limit           # Check core rate limit
ghactivity github rate-limit --all     # Show all pools
ghactivity github rate-limit --all -v  # Verbose with reset times
```

---

## Phase 1.6: Single PR Ingestion Pipeline

End-to-end pipeline to fetch a single PR from GitHub API, transform it through the schema hierarchy, and persist it to the database.

### PR State Machine

We track two states: **OPEN** and **MERGED**.

```
                          create
            [None] ───────────────────▶ [OPEN]
                                          │
                    ┌─────────────────────┤
                    │                     │
                    │ update              │ merge
                    │                     ▼
                    │                 [MERGED]
                    │           (frozen after grace period)
                    │
                    └──────▶ [OPEN]
```

**2-Week Grace Period:** Merged PRs can still be updated for 14 days post-merge to capture any late changes. After the grace period, they are frozen.

```python
is_frozen = (state == MERGED) and (now - close_date > grace_period)
```

### Schema Transformation Flow

```
GitHub API Response
       │
       ▼
 GitHubPullRequest (Pydantic)
       │
       ├──▶ .to_pr_create(repo_id)  →  PRCreate (immutable fields)
       │                                 - number, link, submitter
       │                                 - open_date, repository_id
       │
       └──▶ .to_pr_sync(files,      →  PRSync (synced fields)
            commits, reviews)            - title, state, additions
                                         - deletions, changed_files_count
                                         - commits_breakdown, participants
       │
       ▼
  SQLAlchemy Model
       │
       ▼
 PRRead.from_orm(model)  →  PRRead (output)
```

### Result Objects

```python
@dataclass
class PRIngestionResult:
    pr: PullRequest | None
    created: bool             # New PR created
    updated: bool             # Existing PR updated
    skipped_frozen: bool      # Skipped - frozen state
    skipped_unchanged: bool   # Skipped - no changes detected
    error: Exception | None   # Error if failed
```

### CLI Commands

```bash
ghactivity sync pr prebid/prebid-server 1234              # Basic usage
ghactivity sync pr prebid/prebid-server 1234 --verbose    # Detailed output
ghactivity sync pr prebid/prebid-server 1234 --quiet      # Silent (errors only)
ghactivity sync pr prebid/prebid-server 1234 --dry-run    # Preview without writing
ghactivity sync pr prebid/prebid-server 1234 --format json # JSON output
```

---

## Phase 1.7: Bulk PR Ingestion

Extends the single PR pipeline to support bulk historical imports using lazy pagination for efficient date filtering.

### Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                      Service Layer                                       │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │                BulkPRIngestionService                              │  │
│  │                                                                    │  │
│  │  async def ingest_repository(owner, repo, config):                 │  │
│  │      async for pr in client.iter_pull_requests(...):               │  │
│  │          if pr.created_at < config.since: break  # Stops pagination│  │
│  │          pr_numbers.append(pr.number)                              │  │
│  │      return await batch_executor.execute(pr_numbers, ingest_pr)    │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                    │                          │                          │
│                    │ discovers via            │ ingests via              │
│                    ▼                          ▼                          │
│            GitHubClient              PRIngestionService                  │
│       iter_pull_requests()                                               │
└─────────────────────────────────────────────────────────────────────────┘
```

### Lazy Pagination

The `iter_pull_requests()` method returns an `AsyncIterator` that fetches pages on-demand. When the caller breaks out of the loop (e.g., when hitting PRs older than `--since`), pagination stops immediately, saving API calls.

```python
# Lazy iteration - only fetches pages as needed
async for pr in client.iter_pull_requests(owner, repo, state="all"):
    if config.since and pr.created_at < config.since:
        break  # Stops network pagination immediately
    # ... process PR
```

### Cold vs Hot Path Handling

**Hot Path** (synced normally):
- Open PRs
- Merged PRs within 14-day grace period

**Cold Path** (skipped):
- Merged PRs past 14-day grace period (frozen)

**Filtered Out** (excluded from discovery):
- Closed but not merged PRs (abandoned)

### Result Objects

```python
@dataclass
class BulkIngestionResult:
    total_discovered: int     # PRs matching filters
    created: int              # New PRs created
    updated: int              # Existing PRs updated
    skipped_frozen: int       # Skipped - frozen state
    skipped_unchanged: int    # Skipped - no changes
    failed: int               # PRs that failed
    failed_prs: list[tuple[int, str]]  # (pr_number, error)
    duration_seconds: float
```

### CLI Commands

```bash
ghactivity sync repo prebid/prebid-server                    # Sync all PRs
ghactivity sync repo prebid/prebid-server --since 2024-10-01 # Since date
ghactivity sync repo prebid/prebid-server --state open       # Only open PRs
ghactivity sync repo prebid/prebid-server --state merged     # Only merged PRs
ghactivity sync repo prebid/prebid-server --max 10           # Limit count
ghactivity sync repo prebid/prebid-server --dry-run          # Preview mode
ghactivity sync repo prebid/prebid-server --format json      # JSON output
```

### API Cost Analysis

| PRs | Discovery Calls | Per-PR Calls | Total | Est. Time (5 concurrent) |
|-----|-----------------|--------------|-------|--------------------------|
| 100 | 1 | 400 | ~401 | ~5 min |
| 300 | 3 | 1200 | ~1203 | ~15 min |
| 500 | 5 | 2000 | ~2005 | ~25 min |

With `--since` filtering, discovery calls are reduced significantly due to lazy pagination stopping early.

---

## Phase 2: Enhanced Features (Future)

### 2.1 GitHub User Identity (Normalized)

Normalize user identities into a dedicated `github_users` table to enable queries like "all PRs involving user X" and handle username changes gracefully.

### 2.2 GitHub Issues Support

- Issue data model (similar to PR)
- Issue sync from GitHub API
- Issue tagging and search

### 2.3 Agent Integration

- `classify_tags` generation pipeline
- `ai_summary` generation on PR merge
- Configurable prompts/models

### 2.4 Search Enhancements

- Full-text search on title/description
- Date range filtering
- Export to CSV/JSON

---

## Phase 3: Advanced Features (Future)

### Real-time Sync

- GitHub webhooks support
- Incremental updates
- Background sync daemon

### Web Interface

- REST API layer
- Simple web UI for browsing
- Dashboard with statistics

### Multi-org Support

- Support repos outside Prebid org
- Configurable repo list via CLI
- Per-repo sync settings
