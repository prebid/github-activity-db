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
