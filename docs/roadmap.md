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
| Current Coverage | 403+ tests, breakdown by module, known gaps |
| Infrastructure | Fixtures, factories, async patterns |
| Mocking Patterns | AsyncMock, async_iter helper, response fixtures |
| Running Tests | Commands, filtering, coverage reports |
| Writing Tests | Naming conventions, AAA pattern |
| Troubleshooting | Common async and database issues |

### Test Expansion

#### Current State (445 tests)

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

### Problem Discovery

During production sync of all 2025 PRs, only 204 PRs were imported across 8 repositories. Investigation revealed a critical bug: **the GitHub list API does NOT include the `merged` status** - it always returns `False`.

**Evidence from live debugging:**
```python
# Same PRs, different endpoints
PR #4549: list_api.merged=False, full_api.merged=True  ← Actually merged!
PR #4615: list_api.merged=False, full_api.merged=True  ← Actually merged!
PR #4532: list_api.merged=False, full_api.merged=True  ← Actually merged!
```

### Impact

The `discover_prs()` function in `bulk_ingestion.py` filters PRs using `pr.merged` from the list API. Since this is always `False`, **all merged PRs are incorrectly excluded as "abandoned"**.

```python
# Current broken code (lines 228-243)
is_merged = pr.merged  # ALWAYS FALSE FROM LIST API!

if config.state == "all":
    if not is_open and not is_merged:  # Excludes ALL closed PRs!
        continue  # WRONG - merged PRs filtered out here
```

### Root Cause

GitHub's REST API has different response schemas for list vs single endpoints:

| Field | List API (`/repos/{owner}/{repo}/pulls`) | Full API (`/repos/{owner}/{repo}/pulls/{number}`) |
|-------|------------------------------------------|---------------------------------------------------|
| `merged` | **Always `False`** (not included) | Actual merge status |
| `merged_by` | Not included | Merge author |
| `merged_at` | Not included | Merge timestamp |

This is documented GitHub API behavior, but our code incorrectly assumed the list API includes merge status.

### Fix Implementation

#### 1. Update Discovery Logic (`bulk_ingestion.py`)

**File:** `src/github_activity_db/github/sync/bulk_ingestion.py`

**Before:**
```python
is_open = pr.state == "open"
is_merged = pr.merged

if config.state == "open" and not is_open:
    continue
elif config.state == "merged" and not is_merged:
    continue
elif config.state == "all":
    if not is_open and not is_merged:
        continue
```

**After:**
```python
# NOTE: List API does NOT include merge status (pr.merged always False).
# We cannot filter out "abandoned" PRs here - must do it during ingestion
# when we fetch full PR details.
is_open = pr.state == "open"
is_closed = pr.state == "closed"

if config.state == "open" and not is_open:
    continue
elif config.state == "merged" and is_open:
    # Can only skip open PRs for "merged" filter; closed might be merged
    continue
# For state="all", include both open and closed PRs
# Ingestion step determines if closed PRs are merged or abandoned
```

#### 2. Add Abandoned PR Filtering in Ingestion (`ingestion.py`)

**File:** `src/github_activity_db/github/sync/ingestion.py`

After fetching full PR data, check if abandoned:

```python
async def ingest_pr(self, owner: str, repo: str, pr_number: int) -> PRIngestionResult:
    # ... fetch full PR ...

    # Skip abandoned PRs (closed but not merged)
    if gh_pr.state == "closed" and not gh_pr.merged:
        pr_logger.debug("PR is abandoned (closed without merge), skipping")
        return PRIngestionResult.from_skipped_abandoned(existing)

    # ... continue with ingestion ...
```

#### 3. Update Result Types (`results.py`)

**File:** `src/github_activity_db/github/sync/results.py`

```python
@dataclass
class PRIngestionResult:
    pr: PullRequest | None
    created: bool = False
    updated: bool = False
    skipped_frozen: bool = False
    skipped_unchanged: bool = False
    skipped_abandoned: bool = False  # NEW
    error: Exception | None = None

    @classmethod
    def from_skipped_abandoned(cls, pr: "PullRequest | None") -> "PRIngestionResult":
        return cls(pr=pr, skipped_abandoned=True)
```

#### 4. Update Bulk Result Tracking (`bulk_ingestion.py`)

```python
@dataclass
class BulkIngestionResult:
    # ... existing fields ...
    skipped_abandoned: int = 0  # NEW
```

### Architecture Notes

- Discovery phase now includes ALL closed PRs (not just merged)
- Filtering for abandoned PRs moves to ingestion phase (where we have full data)
- This increases API calls slightly (4 calls per closed PR instead of 0)
- Trade-off is acceptable: correctness over efficiency

### Verification

```bash
# After fix, re-sync and verify merged PRs are included
sqlite3 github_activity.db "DELETE FROM pull_requests WHERE repository_id = 1;"
uv run ghactivity sync repo prebid/prebid-server --since 2025-01-01

# Check state distribution - should have MERGED PRs now
sqlite3 github_activity.db "SELECT state, COUNT(*) FROM pull_requests GROUP BY state;"
```

---

## Phase 1.11: Testing Strategy Improvements ⚠️ PENDING

### Problem Analysis

The GitHub list API bug (Phase 1.10) was not caught by tests because:

1. **Mocks didn't reflect real API behavior** - Test mocks set `merged=True` on list API responses, which doesn't match reality
2. **No contract tests between list and full API** - We assumed fields work the same across endpoints
3. **No API behavior verification** - We didn't test what the actual API returns for shared fields

### Testing Gaps Identified

| Gap | Why It Matters | Priority |
|-----|----------------|----------|
| API response contract tests | Verify our assumptions about API structure | HIGH |
| List vs Full API field comparison | Catch discrepancies in shared fields | HIGH |
| Mock accuracy validation | Ensure mocks reflect real API behavior | MEDIUM |
| Production smoke tests | Verify real API calls work as expected | MEDIUM |

### New Test Categories

#### 1. API Contract Tests

Verify that our schema expectations match real GitHub API responses.

**File:** `tests/github/test_api_contracts.py`

```python
class TestGitHubAPIContracts:
    """Tests verifying our assumptions about GitHub API behavior.

    These tests document known API behaviors and catch if our
    assumptions become invalid due to API changes.
    """

    def test_list_api_does_not_include_merged_status(self):
        """Document that list API merged field is always False.

        The list endpoint (/repos/{owner}/{repo}/pulls) does NOT include
        the actual merge status. We must fetch the full PR to get it.
        """
        from tests.fixtures.real_list_pr import REAL_LIST_PR_DATA

        # List API always returns merged=False
        assert REAL_LIST_PR_DATA.get("merged") is False or "merged" not in REAL_LIST_PR_DATA

        # But the full API has the real value
        from tests.fixtures.real_pr_merged import REAL_MERGED_PR_DATA
        assert REAL_MERGED_PR_DATA["merged"] is True

    def test_list_api_fields_available(self):
        """Document which fields ARE available from list API."""
        from tests.fixtures.real_list_pr import REAL_LIST_PR_DATA

        required_fields = ["number", "state", "title", "created_at", "updated_at"]
        for field in required_fields:
            assert field in REAL_LIST_PR_DATA, f"List API should include {field}"

    def test_full_api_fields_available(self):
        """Document fields only available from full API."""
        from tests.fixtures.real_pr_merged import REAL_MERGED_PR_DATA

        full_only_fields = ["merged", "merged_by", "merged_at", "mergeable"]
        for field in full_only_fields:
            assert field in REAL_MERGED_PR_DATA, f"Full API should include {field}"
```

#### 2. Mock Accuracy Tests

Ensure test mocks accurately reflect real API behavior.

**File:** `tests/fixtures/test_fixture_accuracy.py`

```python
class TestFixtureAccuracy:
    """Verify test fixtures match real API response structure."""

    def test_mock_list_pr_matches_real_structure(self):
        """Ensure mock list PRs have same fields as real API."""
        from tests.factories import make_github_list_pr
        from tests.fixtures.real_list_pr import REAL_LIST_PR_DATA

        mock = make_github_list_pr(number=123)

        # Mock should NOT have merged=True if list API doesn't
        assert mock.get("merged", False) is False, \
            "Mock list PR should not have merged=True (matches real API)"

    def test_mock_full_pr_has_merge_fields(self):
        """Ensure mock full PRs include merge-related fields."""
        from tests.factories import make_github_pr

        mock = make_github_pr(number=123, merged=True)

        assert "merged" in mock
        assert "merged_by" in mock or mock["state"] == "open"
```

#### 3. Integration Tests with Real-like Mocks

Test the full discovery → ingestion flow with accurate mocks.

**File:** `tests/github/sync/test_discovery_ingestion_integration.py`

```python
class TestDiscoveryIngestionIntegration:
    """Test that discovery and ingestion work together correctly."""

    async def test_merged_pr_discovered_via_closed_state(self):
        """Merged PRs should be discovered (state=closed) then identified in ingestion."""
        # List API returns merged PR as state="closed", merged=False
        list_pr = make_github_list_pr(number=100, state="closed", merged=False)

        # Full API returns the truth
        full_pr = make_github_pr(number=100, state="closed", merged=True)

        mock_client.iter_pull_requests.return_value = async_iter([list_pr])
        mock_client.get_full_pull_request.return_value = full_pr

        result = await bulk_service.ingest_repository("prebid", "prebid-server", config)

        # PR should be created with MERGED state
        assert result.created == 1
        pr = await pr_repo.get_by_number(repo_id, 100)
        assert pr.state == PRState.MERGED

    async def test_abandoned_pr_skipped_in_ingestion(self):
        """Abandoned PRs (closed, not merged) should be skipped during ingestion."""
        # List API returns abandoned PR as state="closed", merged=False
        list_pr = make_github_list_pr(number=200, state="closed", merged=False)

        # Full API confirms it's NOT merged
        full_pr = make_github_pr(number=200, state="closed", merged=False)

        mock_client.iter_pull_requests.return_value = async_iter([list_pr])
        mock_client.get_full_pull_request.return_value = full_pr

        result = await bulk_service.ingest_repository("prebid", "prebid-server", config)

        # PR should be skipped
        assert result.skipped_abandoned == 1
        assert result.created == 0
```

### New Fixtures Required

#### Real List API Response

**File:** `tests/fixtures/real_list_pr.py`

Capture actual response from `/repos/{owner}/{repo}/pulls` endpoint:

```python
# Captured from: GET /repos/prebid/prebid-server/pulls?state=closed
REAL_LIST_PR_DATA = {
    "number": 4549,
    "state": "closed",
    "title": "Fix bid caching issue",
    "merged": False,  # NOTE: Always False from list API!
    # ... other fields ...
}
```

### Updated Factory Functions

**File:** `tests/factories.py`

Add factory for list API responses (distinct from full API):

```python
def make_github_list_pr(
    number: int = 1,
    state: str = "open",
    **kwargs
) -> dict:
    """Create a mock GitHub list API PR response.

    NOTE: List API does NOT include accurate merged status.
    Always returns merged=False to match real API behavior.
    """
    return {
        "number": number,
        "state": state,
        "merged": False,  # Always False from list API
        "title": kwargs.get("title", f"Test PR #{number}"),
        # ... minimal fields from list API ...
    }

def make_github_pr(
    number: int = 1,
    state: str = "open",
    merged: bool = False,
    **kwargs
) -> dict:
    """Create a mock GitHub full API PR response.

    Full API includes accurate merged status and additional fields.
    """
    return {
        "number": number,
        "state": state,
        "merged": merged,
        "merged_by": kwargs.get("merged_by") if merged else None,
        "merged_at": kwargs.get("merged_at") if merged else None,
        # ... all fields from full API ...
    }
```

### Documentation Updates

Update `docs/testing.md` to include:

1. **API Contract Testing** section explaining the pattern
2. **Mock Accuracy Guidelines** for creating realistic test data
3. **Known API Behaviors** documenting list vs full API differences

### Verification Checklist

- [ ] Capture real list API response fixture
- [ ] Update `make_github_list_pr` factory to return `merged=False`
- [ ] Add API contract tests
- [ ] Add mock accuracy tests
- [ ] Add discovery → ingestion integration tests
- [ ] Update testing.md documentation
- [ ] Run full test suite
- [ ] Re-sync 2025 PRs and verify counts

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
