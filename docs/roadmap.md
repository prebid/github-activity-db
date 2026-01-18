# Roadmap

This document describes the implementation phases and architecture of GitHub Activity DB.

---

## Unified Architecture

This diagram shows how all components fit together into a cohesive system:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        Application Layer                                 │
│                    CLI Commands (cli/*.py)                               │
│    ghactivity sync pr | ghactivity sync repo | ghactivity sync all       │
└────────────────────────────────┬────────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                         Service Layer                                    │
│   PRIngestionService | BulkPRIngestionService | MultiRepoOrchestrator    │
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

## Phase 1: Core Implementation ✅ COMPLETE

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

## Phase 1.5: Rate Limiting & Request Pacing ✅ COMPLETE

Internal layers within the GitHub module for request control using a token bucket algorithm with adaptive throttling. These components are internal to the GitHub layer - higher layers don't interact with them directly.

**Components:** `RateLimitMonitor` (track state from headers), `RequestPacer` (calculate delays), `RequestScheduler` (priority queue), `BatchExecutor` (coordinate batches), `ProgressTracker` (observe progress).

**CLI Commands:**
```bash
ghactivity github rate-limit           # Check core rate limit
ghactivity github rate-limit --all     # Show all pools
```

See [Rate Limit Handling](rate-limit-handling.md) for algorithm details, retry logic, and testing patterns.

---

## Phase 1.6: Single PR Ingestion Pipeline ✅ COMPLETE

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

## Phase 1.7: Bulk PR Ingestion ✅ COMPLETE

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

## Phase 1.7.5: Test Coverage & Documentation ✅ COMPLETE

Expanded test coverage and created comprehensive testing documentation before adding multi-repo orchestration.

### Documentation

#### New: `docs/testing.md`

Comprehensive testing guide covering:

| Section | Content |
|---------|---------|
| Philosophy | Test pyramid, mocking strategy, coverage goals |
| Test Categories | Unit vs Integration vs E2E with examples |
| Current Coverage | 515+ tests, breakdown by module, known gaps |
| Infrastructure | Fixtures, factories, async patterns |
| Mocking Patterns | AsyncMock, async_iter helper, response fixtures |
| Running Tests | Commands, filtering, coverage reports |
| Writing Tests | Naming conventions, AAA pattern |
| Troubleshooting | Common async and database issues |

### Test Expansion

#### Current State (515 tests)

| Module | Tests | Status |
|--------|-------|--------|
| `github/pacing/` | 113 | ✅ Comprehensive (incl. integration) |
| `github/sync/` | 43 | ✅ Comprehensive (incl. MultiRepoOrchestrator) |
| `github/rate_limit/` | 50+ | ✅ Comprehensive (incl. state transitions) |
| `db/repositories/` | 42 | ✅ Good |
| `schemas/` | 150+ | ✅ Comprehensive |
| `cli/` | 31 | ✅ Integration tests added |
| E2E | 11 | ✅ Core paths |

#### Tests Added

**1. GitHubClient Tests** ✅

File: `tests/github/test_client.py`

| Test | Purpose |
|------|---------|
| `test_iter_pull_requests_pagination` | Verify lazy iteration works |
| `test_iter_pull_requests_early_termination` | Verify pagination stops on break |
| `test_get_full_pull_request_success` | Verify full PR fetch |
| `test_get_full_pull_request_not_found` | 404 handling |
| `test_rate_limit_header_extraction` | Verify monitor updates from headers |

**2. CLI Integration Tests** ✅

File: `tests/cli/test_sync_integration.py`

| Test | Purpose |
|------|---------|
| `test_sync_pr_creates_database_record` | Real DB, mocked API |
| `test_sync_repo_with_small_batch` | End-to-end with --max 3 |
| `test_sync_repo_dry_run_no_writes` | Verify dry-run doesn't persist |
| `test_sync_repo_json_output_structure` | Verify JSON schema |

**3. Pacer + Scheduler Integration** ✅

File: `tests/github/pacing/test_integration.py`

| Test | Purpose |
|------|---------|
| `test_scheduler_uses_pacer_delays` | Verify pacing applied |
| `test_batch_executor_respects_rate_limits` | Verify throttling |
| `test_scheduler_priority_with_pacing` | HIGH tasks get less delay |

**4. Rate Limit State Transitions** ✅

File: `tests/github/rate_limit/test_monitor.py`

| Test | Purpose |
|------|---------|
| `test_state_healthy_to_warning_transition` | State machine correctness |
| `test_state_warning_to_critical_transition` | State machine correctness |
| `test_state_recovery_after_reset` | Recovery behavior |

**5. GitHubClient Pacer Integration** ✅

File: `tests/github/test_client.py` (TestGitHubClientPacerIntegration class)

| Test | Purpose |
|------|---------|
| `test_init_with_pacer` | Client accepts pacer parameter |
| `test_apply_pacing_calls_get_recommended_delay` | Verify pacer delay hook called |
| `test_apply_pacing_calls_on_request_start` | Verify request start hook called |
| `test_apply_pacing_sleeps_when_delay_positive` | Verify asyncio.sleep called |
| `test_update_calls_pacer_on_request_complete` | Verify completion hook called |
| `test_update_passes_headers_to_pacer` | Verify headers passed correctly |
| `test_get_pull_request_calls_apply_pacing` | Verify pacing before API calls |

---

## Phase 1.8: Multi-Repository Sync Orchestration ✅ COMPLETE

Extends bulk PR ingestion to support syncing all 8 Prebid repositories in a single command, verifying the implementation scales and is composable.

### Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         CLI: ghactivity sync all                         │
└────────────────────────────────────┬────────────────────────────────────┘
                                     │
                                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                     MultiRepoOrchestrator                                │
│                                                                          │
│  async def sync_all(config):                                             │
│      repos = await initialize_repositories()                             │
│      for repo in repos:                                                  │
│          result = await bulk_service.ingest_repository(repo, config)     │
│          await update_last_synced_at(repo)                               │
│      return MultiRepoSyncResult.aggregate(results)                       │
└───────────────────────────────────┬─────────────────────────────────────┘
                                    │
              ┌─────────────────────┼─────────────────────┐
              │                     │                     │
              ▼                     ▼                     ▼
    RepositoryRepository   BulkPRIngestionService   Settings.tracked_repos
    (get_or_create)        (per-repo ingestion)     (8 Prebid repos)
```

### Tracked Repositories

```python
tracked_repos = [
    "prebid/prebid-server",
    "prebid/prebid-server-java",
    "prebid/Prebid.js",
    "prebid/prebid.github.io",
    "prebid/prebid-mobile-android",
    "prebid/prebid-mobile-ios",
    "prebid/prebid-universal-creative",
    "prebid/professor-prebid",
]
```

### Result Objects

```python
@dataclass
class RepoSyncResult:
    repository: str
    result: BulkIngestionResult
    started_at: datetime
    completed_at: datetime

@dataclass
class MultiRepoSyncResult:
    repo_results: list[RepoSyncResult]
    total_discovered: int
    total_created: int
    total_updated: int
    total_skipped: int
    total_failed: int
    duration_seconds: float
```

### CLI Commands

```bash
ghactivity sync all                                    # Sync all 8 repos
ghactivity sync all --since 2024-10-01                 # With date filter
ghactivity sync all --state merged                     # Only merged PRs
ghactivity sync all --repos prebid/Prebid.js,prebid/prebid-server  # Specific repos
ghactivity sync all --max-per-repo 50                  # Limit per repo
ghactivity sync all --dry-run                          # Preview mode
ghactivity sync all --format json                      # JSON output
```

### Progress Output

```
Syncing 8 repositories...

[1/8] prebid/prebid-server
      Discovered: 150 | Created: 45 | Updated: 80 | Skipped: 20 | Failed: 5

[2/8] prebid/Prebid.js
      Discovered: 230 | Created: 100 | Updated: 110 | Skipped: 15 | Failed: 5
...

Summary:
  Repositories synced: 8
  Total PRs processed: 1,240
  Created: 450 | Updated: 620 | Skipped: 150 | Failed: 20
  Duration: 45m 30s
```

### File Structure

```
src/github_activity_db/github/sync/
├── __init__.py                    # Add exports
├── multi_repo_orchestrator.py     # NEW: MultiRepoOrchestrator + result classes
├── bulk_ingestion.py              # Existing
└── ingestion.py                   # Existing
```

---

## Phase 1.9: Logging Infrastructure ✅ COMPLETE

Replaces stdlib logging with loguru for improved developer experience, structured context binding, and proper log level control.

### Why Loguru?

| Feature | stdlib logging | loguru |
|---------|---------------|--------|
| Setup complexity | Handler/Formatter boilerplate | Zero-config |
| Context binding | Manual, verbose | `logger.bind(repo="...", pr=123)` |
| Exception tracebacks | Basic | Rich, colorized |
| File rotation | Requires RotatingFileHandler | Built-in |
| Type hints | Limited | Full support |

### Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         CLI Entry Point                                  │
│                     cli/app.py callback()                                │
│         setup_logging(level, verbose, quiet, log_file)                   │
└────────────────────────────────┬────────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                      logging.py Module                                   │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │  setup_logging()     Configure handlers, levels, interception    │   │
│  │  get_logger(name)    Get logger with name context bound          │   │
│  │  bind_repo()         Bind repository context                     │   │
│  │  bind_pr()           Bind PR context                             │   │
│  │  InterceptHandler    Route stdlib → loguru                       │   │
│  └─────────────────────────────────────────────────────────────────┘   │
└────────────────────────────────┬────────────────────────────────────────┘
                                 │
              ┌──────────────────┼──────────────────┐
              │                  │                  │
              ▼                  ▼                  ▼
         Console            File (opt)        SQLAlchemy
         (stderr)           (rotation)        (intercepted)
```

### Configuration

**Environment Variables:**

| Variable | Default | Description |
|----------|---------|-------------|
| `LOG_LEVEL` | `INFO` | Base log level |
| `LOGGING__LOG_FILE` | None | Enable file logging with rotation |
| `LOGGING__ROTATION` | `10 MB` | When to rotate log file |
| `LOGGING__RETENTION` | `7 days` | How long to keep rotated logs |

**CLI Flags (Global):**

| Flag | Short | Effect |
|------|-------|--------|
| `--verbose` | `-v` | DEBUG level (overrides LOG_LEVEL) |
| `--quiet` | `-q` | WARNING level (overrides LOG_LEVEL) |

### Usage Examples

```bash
# Standard logging (INFO level)
ghactivity sync all --since 2024-12-17

# Debug logging (shows SQLAlchemy queries, detailed traces)
ghactivity -v sync pr prebid/prebid-server 1234

# Quiet mode (warnings and errors only)
ghactivity -q sync all

# With file logging
LOGGING__LOG_FILE=./sync.log ghactivity sync all --since 2024-12-17
```

### Context Binding

Loguru's `bind()` enables structured logging with contextual data:

```python
from github_activity_db.logging import bind_pr, get_logger

logger = get_logger(__name__)

# Bind PR context for all subsequent logs
pr_logger = bind_pr("prebid", "prebid-server", 1234)
pr_logger.info("Processing")
# Output: 10:15:30 | INFO | repo=prebid/prebid-server pr=1234 | Processing
```

### File Structure

```
src/github_activity_db/
├── logging.py              # NEW: Core logging module
├── config.py               # Updated: LoggingConfig
└── cli/
    └── app.py              # Updated: Global -v/-q flags, logging setup
```

---

## Phase 1.10: Bugfix - GitHub List API Missing Merged Status ✅ COMPLETE

Fixed critical bug where all merged PRs were incorrectly excluded during discovery. The GitHub list API always returns `merged=False`; the actual merge status is only available from the full PR endpoint.

**Fix:** Discovery phase now includes ALL closed PRs. The ingestion phase (which fetches full PR data) determines if closed PRs are merged or abandoned. Added `skipped_abandoned` field to result types.

**Trade-off:** Slightly increased API calls (4 per closed PR) but ensures correctness.

See [GitHub API Quirks](github-api-quirks.md) for detailed documentation of list vs full API differences.

---

## Phase 1.11: Testing Strategy Improvements ✅ COMPLETE

Improved test infrastructure following the GitHub list API bug (Phase 1.10). Added API contract tests to verify assumptions about GitHub API responses, sleep mocking patterns for fast test execution, and improved mock accuracy validation.

**Key improvements:**
- Real API response fixtures (`tests/fixtures/real_pr_*.py`)
- Sleep mocking patterns (targeted mock, complete mock, disable retries)
- Test suite optimized from 281s to ~18s (94% reduction)
- 502 tests with comprehensive coverage

See [Testing Guide](testing.md) for sleep mocking patterns, API contract testing, and mock accuracy guidelines.

---

## Phase 1.12: Rate Limit Retry Handling Fix ✅ COMPLETE

Fixed rate limit errors being swallowed by the ingestion pipeline instead of propagating to the scheduler for proper retry handling.

**Fix:** Added `GitHubRetryableError` base class for errors that should be retried. Modified `ingest_pr()` to re-raise retryable errors. Added explicit retry loop in discovery phase with exponential backoff.

**Result:** Rate limit errors now trigger proper wait-and-retry behavior with scheduler priority boosting.

See [Rate Limit Handling](rate-limit-handling.md) for retry flow details and exponential backoff tables.

---

## Phase 1.13: Sync Failure Management System ✅ COMPLETE

Persistent tracking of failed PR ingestion attempts with automatic and manual retry capabilities. Failed PRs are now recorded in the database with error details and retry state, enabling recovery from transient failures.

### Components

**Database Layer:**
- `SyncFailure` model with status tracking (PENDING, RESOLVED, PERMANENT)
- `SyncFailureRepository` for CRUD operations and status transitions
- Alembic migration for `sync_failures` table

**Service Layer:**
- `FailureRetryService` orchestrates retry operations with max retry limits
- `BulkPRIngestionService` integration for automatic failure persistence

**CLI Commands:**
```bash
ghactivity sync retry                      # Retry all pending failures
ghactivity sync retry -r owner/repo        # Retry for specific repository
ghactivity sync retry --max 10             # Limit retries
ghactivity sync retry --dry-run            # Preview mode

ghactivity sync repo owner/repo --auto-retry  # Auto-retry before main sync
ghactivity sync all --auto-retry              # Auto-retry across all repos
```

**Result Objects:**
- `RetryResult` with succeeded/failed/marked_permanent counts
- Integration with existing `BulkIngestionResult` for unified reporting

---

## Phase 1.14: Pacing Integration at GitHubClient Layer ✅ COMPLETE

Fixed critical architecture gap where pacing infrastructure existed but was disconnected from actual API calls.

### The Problem

The pacing infrastructure (Phase 1.5) only controlled PR-level concurrency via the scheduler, but **individual API calls were uncontrolled**:

```
BEFORE (Broken):
┌─────────────────────────────────────────────────────────────────────────┐
│  Scheduler controls: "Start next PR ingestion" (max 5 concurrent)       │
│                                                                          │
│  BUT each PR makes 4+ API calls with ZERO pacing:                       │
│    get_pull_request() → get_files() → get_commits() → get_reviews()    │
│                                                                          │
│  5 concurrent PRs × 4 calls = 20 rapid requests                         │
│  With 50ms fallback delay = ~20 requests/second                         │
│  GitHub limit = 5000/hour = 1.39 requests/second                        │
│  Result: Rate limit exhausted in ~4 minutes                             │
└─────────────────────────────────────────────────────────────────────────┘
```

### The Fix

Integrated pacing directly into `GitHubClient` so **every API call** is automatically paced:

```
AFTER (Fixed):
┌─────────────────────────────────────────────────────────────────────────┐
│                         GitHubClient                                     │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │  Every API method now:                                             │  │
│  │    1. await self._apply_pacing()  ← Calculate and apply delay     │  │
│  │    2. Make API request                                             │  │
│  │    3. _update_rate_limit_from_response() → Notify pacer           │  │
│  └───────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────┘
```

### Implementation Details

**File: `src/github_activity_db/github/client.py`**

1. **Constructor accepts optional `RequestPacer`**:
   ```python
   def __init__(
       self,
       token: str | None = None,
       rate_monitor: RateLimitMonitor | None = None,
       pacer: RequestPacer | None = None,  # NEW
   ) -> None:
   ```

2. **New `_apply_pacing()` method**:
   ```python
   async def _apply_pacing(self, pool: RateLimitPool = RateLimitPool.CORE) -> None:
       if self._pacer is None:
           return
       delay = self._pacer.get_recommended_delay(pool)
       if delay > 0:
           await asyncio.sleep(delay)
       self._pacer.on_request_start()
   ```

3. **Response headers feed back to pacer**:
   ```python
   def _update_rate_limit_from_response(self, response: Any) -> None:
       # ... extract headers ...
       if self._pacer is not None:
           self._pacer.on_request_complete(header_dict)
   ```

4. **All API methods call `_apply_pacing()` before requests**:
   - `get_rate_limit()`
   - `get_pull_request()`
   - `list_pull_requests()` / `iter_pull_requests()`
   - `get_pull_request_files()`
   - `get_pull_request_commits()`
   - `get_pull_request_reviews()`

**File: `src/github_activity_db/cli/sync.py`**

CLI commands now properly initialize the pacing infrastructure:

```python
async with GitHubClient() as base_client:
    # Initialize monitor and fetch current rate limit state
    monitor = RateLimitMonitor(base_client._github)
    await monitor.initialize()  # Fetches current rate limit from API

    # Create pacer with monitor state
    pacer = RequestPacer(monitor)

    # Create paced client for actual API calls
    async with GitHubClient(rate_monitor=monitor, pacer=pacer) as client:
        # All API calls through this client are now paced
```

### Expected Behavior After Fix

| Aspect | Before | After |
|--------|--------|-------|
| API call pacing | None (scheduler only) | Every call paced |
| Header feedback | Monitor updated, pacer ignored | Both updated |
| Monitor initialization | Never called | Called at CLI startup |
| Rate limit exhaustion | ~4 minutes | Adaptive (never exhausted) |

### Delay Calculation Example

With 5,000 requests/hour and 60 minutes until reset:
- `base_delay = 3600 seconds / 5000 requests = 0.72 seconds per request`
- With 10% reserve buffer: `effective = 4500 requests`
- `base_delay = 3600 / 4500 = 0.8 seconds per request`

As quota depletes to WARNING (20% remaining = 1000 requests):
- `base_delay = remaining_time / 1000`
- Multiplied by 1.5x throttle = **~1.2+ seconds per request**

---

## Phase 1.15: Database Write Resilience ✅ COMPLETE

Implements batch commit boundaries to prevent data loss during sync failures. Previously, all writes happened in a single transaction that only committed on session exit—any failure caused complete rollback of all work.

### The Problem

```
Before (all-or-nothing):
PR #1 → flush → PR #2 → flush → ... → PR #200 → flush → [context exit] → COMMIT ALL
[Any failure at PR #150] → ROLLBACK ALL → 0 PRs saved
```

**Impact by operation type:**
- Single PR sync: Loss of 1 PR (acceptable)
- Bulk repo sync: Loss of 100+ PRs if failure near end
- Multi-repo sync: Loss of 1000+ PRs across all repos if any failure

### Solution: CommitManager

The `CommitManager` class commits in configurable batches (default: 25 PRs), limiting data loss to the last uncommitted batch.

```
After (batched with batch_size=25):
PR #1-25 → flush each → COMMIT BATCH → 25 PRs saved
PR #26-50 → flush each → COMMIT BATCH → 50 PRs saved
...
PR #126-150 → flush each → [FAILURE at #150]
→ ROLLBACK current batch only → 125 PRs saved (5 batches)
```

### Architecture Integration

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         CLI: ghactivity sync                             │
│                                                                          │
│  async with get_session(auto_commit=False) as session:                   │
│      write_lock = asyncio.Lock()                                         │
│      commit_manager = CommitManager(session, write_lock, batch_size=25)  │
│      ...                                                                 │
│      await commit_manager.finalize()                                     │
└───────────────────────────────────┬─────────────────────────────────────┘
                                    │
              ┌─────────────────────┼─────────────────────┐
              │                     │                     │
              ▼                     ▼                     ▼
    BulkPRIngestionService   MultiRepoOrchestrator   PullRequestRepository
    (calls record_success)   (passes commit_manager)  (uses write_lock)
```

### CommitManager API

```python
class CommitManager:
    def __init__(
        self,
        session: AsyncSession,
        write_lock: asyncio.Lock | None = None,
        batch_size: int = 25,
    ) -> None: ...

    async def record_success(self) -> int:
        """Record successful operation, auto-commit at batch_size."""

    async def commit(self) -> int:
        """Force commit of pending changes."""

    async def finalize(self) -> int:
        """Commit any remaining uncommitted changes."""

    @property
    def uncommitted_count(self) -> int: ...
    @property
    def total_committed(self) -> int: ...
    @property
    def batch_size(self) -> int: ...
```

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `SYNC__COMMIT_BATCH_SIZE` | `25` | PRs to commit per batch (1-100) |

### CLI Integration

All sync commands automatically use CommitManager:

```bash
# Default batch size (25)
ghactivity sync repo prebid/prebid-server --since 2024-10-01

# Custom batch size via environment
SYNC__COMMIT_BATCH_SIZE=50 ghactivity sync all --since 2024-10-01
```

### File Structure

```
src/github_activity_db/
├── config.py                          # + commit_batch_size in SyncConfig
├── db/
│   └── engine.py                      # + auto_commit param to get_session()
└── github/
    └── sync/
        ├── __init__.py                # + export CommitManager
        ├── commit_manager.py          # NEW: CommitManager class
        ├── bulk_ingestion.py          # + CommitManager integration
        └── multi_repo_orchestrator.py # + CommitManager integration

tests/github/sync/
├── test_commit_manager.py             # Unit tests (13 tests)
└── test_commit_manager_integration.py # Integration tests (5 tests)
```

---

## Phase 1.16: Code Quality & Technical Debt Reduction ✅ COMPLETE

Systematic elimination of lazy engineering patterns that bypass proper solutions, improving type safety, reducing code duplication, and establishing better patterns for future development.

### Problem Statement

A code quality audit identified patterns that trade proper engineering for quick fixes:

| Pattern | Count | Impact |
|---------|-------|--------|
| Duplicate `split("/", 1)` parsing | 7 | DRY violation, inconsistent error handling |
| `type: ignore` comments | 10 | Bypasses type safety |
| `noqa` lint suppressions | 4 | Hides code smells |
| `Any` type aliases | 2 | Weakens type checking |
| `except: pass` in tests | 3 | Silent test failures |

### Architectural Improvements

#### 1. Centralized Repository String Parsing

**Problem:** `owner, name = repo.split("/", 1)` duplicated 7 times across CLI and service layers.

**Solution:** Add `parse_repo_string()` function to `schemas/repository.py`:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                     schemas/repository.py                                │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │  def parse_repo_string(full_name: str) -> tuple[str, str]:        │  │
│  │      """Parse 'owner/repo' into (owner, name) tuple."""           │  │
│  │      if "/" not in full_name:                                     │  │
│  │          raise ValueError(f"Invalid: {full_name}")                │  │
│  │      return tuple(full_name.split("/", 1))                        │  │
│  └───────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
              ┌─────────────────────┼─────────────────────┐
              │                     │                     │
              ▼                     ▼                     ▼
        cli/sync.py           cli/github.py       multi_repo_orchestrator.py
```

**Benefits:**
- Single validation logic with consistent error messages
- Easier to enhance (e.g., add organization validation)
- Type-safe return value
- Exported from `schemas/__init__.py` for easy access

#### 2. CLI Option Factory Pattern

**Problem:** Typer requires mutable defaults, triggering B008 lint rule. Same `noqa: B008` repeated 4 times.

**Solution:** Create `cli/common.py` with reusable option factories:

```python
# cli/common.py (NEW FILE)
def output_format_option() -> OutputFormat:  # noqa: B008
    """Standard output format option."""
    return typer.Option(OutputFormat.TEXT, "--format", "-f", help="Output format")

# cli/sync.py - usage (no noqa needed at call site)
def sync_pr(output_format: OutputFormat = output_format_option()):
```

**Benefits:**
- Single `noqa` comment in factory, not scattered
- Consistent option naming and help text across commands
- Extensible for future common options

#### 3. TYPE_CHECKING for Complex Types

**Problem:** `GitHub = Any` alias used to avoid complex generic typing.

**Solution:** Use `TYPE_CHECKING` block for proper typing:

```python
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from githubkit import GitHub

class RateLimitMonitor:
    def __init__(self, github: GitHub | None = None) -> None:
        self._github: GitHub | None = github
```

**Benefits:**
- Full type checking in IDE and mypy
- No runtime overhead from complex imports
- Aligns with project principle: "Type Safety - Strict mypy configuration"

### Files to Modify

| File | Changes |
|------|---------|
| `schemas/repository.py` | Add `parse_repo_string()` helper function |
| `schemas/__init__.py` | Export `parse_repo_string` |
| `cli/common.py` | **NEW**: Option factory functions |
| `cli/sync.py` | Use helpers, fix type ignores, use option factories |
| `cli/github.py` | Use helper, modernize async runner |
| `github/sync/multi_repo_orchestrator.py` | Use `parse_repo_string()` helper |
| `github/rate_limit/monitor.py` | Fix GitHub type alias with TYPE_CHECKING |
| `github/sync/ingestion.py` | Fix type narrowing with assertions |
| `github/pacing/batch.py` | Fix type narrowing with cast() |
| `github/client.py` | Fix lazy init typing |
| 2 test files | Replace `except: pass` with `pytest.raises()` |

### Expected Outcome

| Metric | Before | After |
|--------|--------|-------|
| `type: ignore` comments | 10 | 2-3 (unavoidable Pydantic) |
| `noqa` comments in sync.py | 4 | 0 |
| Duplicate parsing logic | 7 locations | 1 location |
| `except: pass` in tests | 3 | 0 |

### Configuration Enforcement

Beyond code changes, this phase adds tooling to prevent regression:

#### Ruff Rules (pyproject.toml)

```toml
[tool.ruff.lint]
select = [
    # ... existing rules ...
    "ANN001", "ANN201", "ANN204",  # Type annotation enforcement
    "TC001", "TC002", "TC003",     # TYPE_CHECKING best practices
    "FA100",                        # Future annotations
]
ignore = ["ANN101", "ANN102", "ANN401"]  # self/cls/Any in dict patterns

[tool.ruff.lint.flake8-type-checking]
strict = true
runtime-evaluated-base-classes = ["pydantic.BaseModel"]
```

#### Mypy Enhancement (pyproject.toml)

```toml
[tool.mypy]
enable_error_code = ["ignore-without-code"]  # Require codes on type: ignore
```

#### Pre-commit Quality Gates (.pre-commit-config.yaml)

```yaml
- repo: local
  hooks:
    - id: audit-type-ignores
      name: Audit type:ignore comments
      entry: bash -c 'count=$(grep -r "type: ignore" src/ | wc -l); echo "type:ignore: $count (target ≤5)"'
    - id: audit-noqa
      name: Audit noqa comments
      entry: bash -c 'count=$(grep -r "noqa:" src/ | wc -l); echo "noqa: $count (target ≤3)"'
    - id: prevent-any-aliases
      name: Prevent Any type aliases
      entry: bash -c 'grep -rn "^[A-Z].* = Any" src/ && exit 1 || exit 0'
```

#### Type Safety Policy (CLAUDE.md)

Document `Any` usage guidelines:
- ✅ Allowed: `dict[str, Any]`, Pydantic validators, ORM factories, log context
- ❌ Forbidden: Lazy initialization aliases, avoiding generics
- Policy: `type: ignore` must include error codes, `noqa` should be centralized

### Verification

```bash
# Verify metrics after implementation
grep -r "type: ignore" src/ | wc -l  # Target: 2-3
grep -r "noqa:" src/ | wc -l         # Target: 2 (in common.py only)
grep -rn "split(\"/\", 1)" src/      # Target: 1 (in repository.py)

# Standard verification
uv run mypy src/
uv run ruff check src/ tests/
uv run pytest
```

### Phase 1.16.2: CLI Refactoring ✅ COMPLETE

Building on the completed code quality work, this sub-phase focuses on:
1. ✅ Consolidating CLI async execution patterns into a robust `run_async_command` helper
2. ✅ Creating reusable repository argument/option factories

#### Results

| Metric | Before | After Phase 1.16.2 |
|--------|--------|-------------------|
| CLI async boilerplate | ~90 lines (6 commands) | ~6 lines |
| Repo argument definitions | 4 duplicated | 3 centralized |
| Async pattern consistency | 2 patterns | 1 pattern (`asyncio.run()`) |
| PEP 561 compliance | No | Yes (`py.typed` marker) |

#### Current State (Post Phase 1.16.1)

| Metric | Before | After Phase 1.16.1 |
|--------|--------|-------------------|
| `type: ignore` comments | 10 | 2 (Pydantic-only) |
| `noqa` comments | 4 | 0 |
| Duplicate `split("/", 1)` | 7 | 1 |
| `except: pass` in tests | 3 | 0 |

#### CLI Async Execution Consolidation

**Problem:** Two inconsistent async patterns exist:

| File | Pattern | Issues |
|------|---------|--------|
| `cli/sync.py` | `asyncio.get_event_loop().run_until_complete()` | Legacy pattern, reuses event loop |
| `cli/github.py` | `asyncio.run()` via `_run_async()` helper | Modern pattern, isolated event loop |

**Solution:** Unified `run_async_command()` helper in `cli/common.py`:

```python
def run_async_command(
    coro: Coroutine[object, object, T],
    *,
    error_prefix: str = "Error",
) -> T:
    """Execute async code from synchronous CLI command with unified error handling."""
    try:
        return asyncio.run(coro)
    except typer.Exit:
        raise  # Re-raise deliberate exits
    except Exception as e:
        console.print(f"[red]{error_prefix}:[/red] {e}")
        raise typer.Exit(1) from None
```

**Impact:** Reduces ~90 lines of boilerplate across 6 CLI commands.

#### Repository Argument Factories

**Problem:** Three repo argument patterns exist with duplicated definitions:

| Pattern | Usage | Definition |
|---------|-------|------------|
| Positional `Argument` | `sync pr`, `sync repo` | Required, `owner/name` format |
| Optional `--repo` Option | `sync retry` | Filtering, `owner/name` format |
| Comma-separated `--repos` | `sync all` | Override list |

**Solution:** Annotated type aliases in `cli/common.py`:

```python
RepoArgument = Annotated[
    str,
    typer.Argument(help="Repository in owner/name format"),
]

RepoFilterOption = Annotated[
    str | None,
    typer.Option("--repo", "-r", help="Filter by repository"),
]

ReposListOption = Annotated[
    str | None,
    typer.Option("--repos", "-r", help="Comma-separated list of repos"),
]
```

Plus validation helpers: `validate_repo()` and `validate_repo_list()`.

#### Files Modified

| File | Changes |
|------|---------|
| `cli/common.py` | Add `run_async_command`, repo types, validation helpers |
| `cli/sync.py` | Use new helpers (4 async blocks, 4 repo definitions) |
| `cli/github.py` | Remove `_run_async`, use `run_async_command` |
| `src/github_activity_db/py.typed` | PEP 561 marker file |

### Phase 1.16.3: Test Type Safety Enforcement ✅ COMPLETE

Properly enabled mypy for tests by fixing all 230 type errors. Pre-commit now runs mypy on both `src/` and `tests/` directories.

#### Results

| Metric | Before | After |
|--------|--------|-------|
| Mypy errors in tests | 230 | 0 |
| Pre-commit runs mypy on tests | No | Yes |
| CI runs mypy on tests | No | Yes |
| Factory `**overrides` usage | 2 functions | 0 |
| `type: ignore` in src/ | - | 2 (target: ≤5) ✅ |
| `noqa` comments in src/ | - | 0 (target: ≤3) ✅ |

#### Fix Strategies Applied

**1. Untyped Dict Unpacking (170+ errors)**

Replaced `Model(**dict)` with Pydantic's `model_validate()`:

```python
# Before (causes arg-type errors)
pr = GitHubPullRequest(**GITHUB_PR_RESPONSE)

# After (uses Pydantic's built-in validation)
pr = GitHubPullRequest.model_validate(GITHUB_PR_RESPONSE)
```

**2. Factory Dict Merging (16 errors)**

Removed `**overrides` pattern, using explicit parameters instead.

**3. Optional Access (37 errors)**

Added null assertions before attribute access:

```python
result = await repo.get_by_number(...)
assert result is not None  # Narrows type
assert result.state == PRState.MERGED  # Works
```

#### Pre-commit Configuration

```yaml
- id: mypy
  additional_dependencies:
    - pydantic>=2.0
    - pydantic-settings>=2.0
    - sqlalchemy>=2.0
    - pytest-asyncio>=0.24
    - typer>=0.9.0
    - rich>=13.0
    - loguru>=0.7.0
    - githubkit>=0.11.0
  args: [--config-file=pyproject.toml, src, tests]
  pass_filenames: false
  files: ^(src|tests)/
```

#### Quality Gates

Pre-commit now includes automated quality audits:

- **Audit type:ignore comments** - Reports count, target ≤5
- **Audit noqa comments** - Reports count, target ≤3
- **Prevent Any type aliases** - Blocks `TypeName = Any` patterns

---

## Phase 2: Enhanced Features (Future)

### 2.1 GitHub User Identity (Normalized)

Normalize user identities into a dedicated `github_users` table to enable queries like "all PRs involving user X" and handle username changes gracefully.

**Key Design Consideration: Verified vs Unverified Identities**

GitHub's API returns two types of author information for commits:
- **GitHub User** (`commit.author.login`): Verified GitHub account linked to the git email
- **Git Author** (`commit.commit.author.name`): Name from `git config user.name` (fallback when email not linked)

The `github_users` table should track this distinction:

```python
class GitHubUser(Base):
    id: int                      # Primary key
    username: str                # GitHub login OR git author name
    github_id: int | None        # GitHub user ID (null if unverified)
    is_verified: bool            # True = linked GitHub account, False = git-only
    email: str | None            # Git email (for potential future linking)
    display_name: str | None     # Full name if available
    first_seen_at: datetime      # When we first encountered this user

    # Relationships
    prs_submitted: list[PullRequest]
    prs_merged: list[PullRequest]
    commits: list[CommitAuthor]
    reviews: list[Review]
```

**Benefits:**
- Query all contributions by a user across verified and unverified commits
- Identify when a git author name gets linked to a GitHub account
- Track contributors who haven't linked their git email to GitHub
- Enable future deduplication (e.g., "Victor Gonzalez" → "optidigital-prebid")

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
