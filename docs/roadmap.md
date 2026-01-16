# Roadmap

## Phase 1: Core Implementation ✅

### Completed

- [x] Project scaffolding (uv, pyproject.toml, ruff, mypy)
- [x] Database models (Repository, PullRequest, UserTag)
- [x] Async SQLAlchemy engine and session management
- [x] Configuration with pydantic-settings
- [x] Alembic migrations (initial schema)
- [x] CLI scaffold with typer
- [x] Pydantic schemas (validation, GitHub API parsing, factory pattern)
- [x] Test infrastructure (comprehensive pytest suite)
- [x] GitHub client with error handling

---

## Unified Architecture

This diagram shows how all phases fit together into a cohesive system:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        Application Layer                                 │
│                    CLI Commands (cli/*.py)                               │
│              ghactivity sync pr | ghactivity github rate-limit           │
└────────────────────────────────┬────────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                         Service Layer (Phase 1.6)                        │
│                        PRIngestionService                                │
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
│  └──────────────┬──────────────┘  │   │  PullRequestRepository          │
│                 │                 │   └────────────────┬────────────────┘
│  ┌──────────────▼──────────────┐  │                    │
│  │   Orchestration (Phase 1.5) │  │                    │
│  │   BatchExecutor             │  │                    │
│  │   RequestScheduler          │  │                    │
│  │   ProgressTracker           │  │                    │
│  └──────────────┬──────────────┘  │                    │
│                 │                 │                    │
│  ┌──────────────▼──────────────┐  │                    │
│  │   Control (Phase 1.5)       │  │                    │
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

**Key Insight:** Phase 1.5 (rate limiting) is **internal** to the GitHub layer. The Service Layer doesn't know about pacing - it just calls `client.get_full_pull_request()` and rate limiting happens automatically.

---

## Phase 1.5: Rate Limiting & Request Pacing ✅

### Completed

- [x] Rate limit monitoring with proactive tracking
- [x] Request pacing with token bucket algorithm
- [x] Priority queue scheduler with concurrency control
- [x] Batch execution with progress tracking
- [x] CLI command: `ghactivity github rate-limit`

### Architecture Overview

**Pattern:** Internal layers within GitHub module for request control

Phase 1.5 components are **internal to the GitHub layer** - they control *how* API requests are made (timing, pacing, rate limits). Higher layers don't interact with them directly.

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

**Component Responsibilities (Single Responsibility Principle):**

| Component | Responsibility | Does NOT |
|-----------|----------------|----------|
| `RateLimitMonitor` | Track rate limit state from headers | Make pacing decisions |
| `RequestPacer` | Calculate optimal delays | Queue or execute requests |
| `RequestScheduler` | Queue and prioritize requests | Calculate delays |
| `BatchExecutor` | Coordinate batch operations | Implement queueing |
| `ProgressTracker` | Observe and report progress | Affect execution |

**Core Algorithm: Token Bucket with Adaptive Throttling**

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

**State Machine: Rate Limit Status**

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

### Testing Strategy

**Test Categories:**

| Category | Purpose | Example |
|----------|---------|---------|
| Contract | Verify Pydantic schema parsing | Parse GitHub `/rate_limit` response |
| Unit | Verify isolated component behavior | State transitions in monitor |
| Behavioral | Verify mathematical correctness | Token bucket delay calculations |
| Integration | Verify component interactions | Full sync with mocked API |

**Test-Driven Implementation Order:**

1. **Schema Tests First** - Parse real GitHub responses, verify all fields extracted
2. **Monitor Tests** - State transitions, threshold callbacks, PAT verification
3. **Pacer Tests** - Delay formula correctness, bounds clamping, property tests
4. **Scheduler Tests** - Priority ordering, concurrency limits, retry logic
5. **Integration Tests** - End-to-end flow with mocked GitHub client

### Test Coverage

Phase 1.5 tests:
- `tests/github/rate_limit/` - Schema parsing, monitor state machine
- `tests/github/pacing/` - Pacer math, scheduler ordering, batch execution

### CLI Commands

```bash
ghactivity github rate-limit           # Check core rate limit
ghactivity github rate-limit --all     # Show all pools
ghactivity github rate-limit --all -v  # Verbose with reset times
```

---

## Phase 1.6: Single PR Ingestion Pipeline ✅

### Completed

- [x] Repository Layer: CRUD operations for Repository and PullRequest models
- [x] PR Ingestion Service: Fetch → Transform → Store pipeline with structured results
- [x] Type Verification: Validate Pydantic schemas against real GitHub API responses
- [x] End-to-End Testing: Integration tests with open and merged PR fixtures
- [x] CLI command: `ghactivity sync pr` with --dry-run, --format, --quiet, --verbose
- [x] 2-week grace period for merged PRs before freezing
- [x] Diff detection (skip unchanged PRs)

### Overview

End-to-end pipeline to fetch a single PR from GitHub API, transform it through our schema hierarchy, and persist it to the database. This phase added the **Service Layer** and **Repository Layer** shown in the Unified Architecture above.

**Design Goal:** Build a foundation that extends naturally to multi-PR sync and state management in future phases.

### PR State Machine

We only care about two states: **OPEN** and **MERGED**.

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

### Configuration

```python
class SyncConfig(BaseModel):
    merge_grace_period_days: int = Field(default=14, ge=0)
```

Environment variable: `SYNC__MERGE_GRACE_PERIOD_DAYS=14`

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

### Architecture Overview

**Pattern:** Service Layer with Repository Pattern

Phase 1.6 adds two new layers that sit **above** the GitHub layer (see Unified Architecture):

```
┌─────────────────────────────────────────────────────────────────────────┐
│                      Service Layer (NEW in Phase 1.6)                    │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │                    PRIngestionService                              │  │
│  │                                                                    │  │
│  │  async def ingest_pr(owner, repo, pr_number):                      │  │
│  │      repository = await repo_repository.get_or_create(owner, repo) │  │
│  │      gh_pr, files, commits, reviews = await client.get_full_pr()   │  │
│  │      pr_create = gh_pr.to_pr_create(repository.id)                 │  │
│  │      pr_sync = gh_pr.to_pr_sync(files, commits, reviews)           │  │
│  │      return await pr_repository.create_or_update(...)              │  │
│  └───────────────────────────────────────────────────────────────────┘  │
└───────────────┬─────────────────────────────────┬───────────────────────┘
                │                                 │
                │ uses (rate limiting             │ uses
                │ handled internally)             │
                ▼                                 ▼
┌───────────────────────────┐       ┌─────────────────────────────────────┐
│      GitHub Layer         │       │   Repository Layer (NEW in 1.6)     │
│  (includes Phase 1.5)     │       │  ┌───────────────────────────────┐  │
│                           │       │  │    RepositoryRepository       │  │
│  GitHubClient             │       │  │    - get_or_create()          │  │
│    └── rate_limit/        │       │  │    - get_by_owner_name()      │  │
│    └── pacing/            │       │  └───────────────────────────────┘  │
│                           │       │  ┌───────────────────────────────┐  │
└───────────────────────────┘       │  │    PullRequestRepository      │  │
                                    │  │    - create_or_update()       │  │
                                    │  │    - get_by_number()          │  │
                                    │  └───────────────────────────────┘  │
                                    └──────────────────┬──────────────────┘
                                                       │
                                                       ▼
                                              SQLite Database
```

**Component Responsibilities:**

| Component | Responsibility | Does NOT |
|-----------|----------------|----------|
| `PRIngestionService` | Orchestrate fetch → transform → store | Know about rate limits |
| `GitHubClient` | Fetch raw API data (with internal pacing) | Transform or store data |
| `RepositoryRepository` | CRUD for Repository table | Business logic |
| `PullRequestRepository` | CRUD for PullRequest table | Fetching from GitHub |
| Schema factories | Transform API response to domain | Database operations |

**Schema Transformation Flow:**

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
       │
       ▼
  CLI / API Response
```

### File Structure

```
src/github_activity_db/
├── config.py                     # UPDATE: Add SyncConfig
├── db/
│   ├── repositories/
│   │   ├── __init__.py           # Public exports
│   │   ├── base.py               # BaseRepository ABC
│   │   ├── repository.py         # RepositoryRepository
│   │   └── pull_request.py       # PullRequestRepository
├── github/
│   └── sync/
│       ├── __init__.py           # Public exports
│       ├── ingestion.py          # PRIngestionService
│       ├── results.py            # PRIngestionResult dataclass
│       └── enums.py              # SyncStrategy, OutputFormat
├── cli/
│   └── sync.py                   # Sync CLI commands

tests/
├── fixtures/
│   ├── real_pr_open.py           # Open PR fixture
│   └── real_pr_merged.py         # Merged PR fixture
├── db/
│   └── repositories/
│       ├── test_repository_repo.py   # RepositoryRepository tests
│       └── test_pull_request_repo.py # PullRequestRepository tests
├── github/
│   └── sync/
│       ├── test_ingestion.py     # PRIngestionService tests
│       └── test_results.py       # Result object tests
├── test_pr_ingestion_e2e.py      # End-to-end integration test
└── test_cli_sync.py              # CLI command tests
```

### Type Verification Mapping

| GitHub API Field | Internal Field | Transformation |
|-----------------|----------------|----------------|
| `user.login` | `submitter` | Direct string copy |
| `additions` | `additions` | Direct int copy |
| `deletions` | `deletions` | Direct int copy |
| `changed_files` | `changed_files_count` | Direct int copy |
| `commits` (list) | `commits_breakdown` | Aggregate by author into JSON |
| `reviews` (list) | `participants` | Aggregate by user/action into JSON |
| `merged_by.login` | `merged_by` | String if merged, None otherwise |
| `merged_at` | `close_date` | Datetime if merged |

### Testing Strategy

**Test Categories:**

| Category | Purpose | Location |
|----------|---------|----------|
| Contract | Verify schema parsing of real API responses | `test_schemas_github_api.py` |
| Unit | Repository CRUD in isolation | `tests/db/repositories/` |
| Integration | Ingestion service with mocked client | `tests/github/sync/` |
| Idempotency | Same input produces same output | `test_pr_ingestion_e2e.py` |
| State Machine | State transitions with grace period | `test_pull_request_repo.py` |
| E2E | Full pipeline with real PR fixtures | `test_pr_ingestion_e2e.py` |
| CLI | Command flags and output formats | `test_cli_sync.py` |

**Test-Driven Implementation Order:**

1. **Real PR Response Fixtures** - Capture open and merged PR responses
2. **Contract Tests** - Parse real responses through GitHubPullRequest schema
3. **Repository Unit Tests** - CRUD, frozen state, grace period
4. **Ingestion Service Tests** - Mock client, diff detection, result objects
5. **Idempotency Tests** - Skip unchanged, update changed, skip frozen
6. **CLI Tests** - All flags (--dry-run, --format, --quiet, --verbose)
7. **E2E Integration Test** - Full database round-trip

### Test Coverage

**304 total tests passing** across all phases:

| Test Location | Count | Purpose |
|---------------|-------|---------|
| `tests/fixtures/` | - | Real PR fixtures (open, merged) |
| `tests/test_schemas_contract.py` | 23 | Validate schemas against real GitHub responses |
| `tests/db/repositories/` | 42 | Repository CRUD, frozen state, grace period |
| `tests/github/sync/` | 11 | Ingestion service with mocked client |
| `tests/test_pr_ingestion_e2e.py` | 11 | Full pipeline E2E tests |
| `tests/test_cli_sync.py` | 13 | CLI command flags and output formats |

### CLI Commands

```bash
# Basic usage
ghactivity sync pr prebid/prebid-server 1234

# With options
ghactivity sync pr prebid/prebid-server 1234 --verbose      # Detailed output
ghactivity sync pr prebid/prebid-server 1234 --quiet        # Silent (errors only)
ghactivity sync pr prebid/prebid-server 1234 --dry-run      # Preview without writing
ghactivity sync pr prebid/prebid-server 1234 --format json  # JSON output for scripting
```

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
