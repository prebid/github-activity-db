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

### 2.1 GitHub User Identity (Normalized)

**Problem Statement:**

User identities are currently scattered and inconsistent:
- `submitter`, `merged_by` store GitHub login (e.g., "optidigital-prebid")
- `commits_breakdown.author` stores git display name (e.g., "Victor Gonzalez")
- `participants` keys are GitHub logins
- No way to query "all PRs involving user X"
- If a user changes their GitHub username, old data becomes orphaned
- Can't distinguish bots from humans
- Can't store user metadata (avatar, company, etc.)

**Solution: Normalized `github_users` Table**

#### Schema Design

```
┌─────────────────────────────────────────────────────────────┐
│                      github_users                            │
├─────────────────────────────────────────────────────────────┤
│ id           INTEGER PRIMARY KEY   -- GitHub's user ID       │
│ login        VARCHAR(100) NOT NULL -- Current username       │
│ name         VARCHAR(200)          -- Display name           │
│ email        VARCHAR(200)          -- Public email           │
│ avatar_url   VARCHAR(500)          -- Profile picture URL    │
│ type         VARCHAR(20) NOT NULL  -- User, Bot, Organization│
│ company      VARCHAR(200)          -- Company field          │
│ first_seen_at DATETIME NOT NULL    -- When we first saw them │
│ last_seen_at  DATETIME NOT NULL    -- Last activity date     │
│ created_at    DATETIME NOT NULL    -- Record creation        │
│ updated_at    DATETIME NOT NULL    -- Record update          │
├─────────────────────────────────────────────────────────────┤
│ UNIQUE INDEX ON (login)                                      │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│                    pr_participants                           │
│              (replaces JSON participants field)              │
├─────────────────────────────────────────────────────────────┤
│ pr_id        INTEGER FK(pull_requests.id) ON DELETE CASCADE  │
│ user_id      INTEGER FK(github_users.id)                     │
│ actions      JSON NOT NULL         -- ["approval", "comment"]│
│ first_action_at DATETIME           -- When first participated│
│ last_action_at  DATETIME           -- When last participated │
├─────────────────────────────────────────────────────────────┤
│ PRIMARY KEY (pr_id, user_id)                                 │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│                    pr_commits                                │
│            (replaces JSON commits_breakdown field)           │
├─────────────────────────────────────────────────────────────┤
│ id           INTEGER PRIMARY KEY                             │
│ pr_id        INTEGER FK(pull_requests.id) ON DELETE CASCADE  │
│ sha          VARCHAR(40) NOT NULL  -- Commit SHA             │
│ author_id    INTEGER FK(github_users.id) NULLABLE            │
│ author_name  VARCHAR(200) NOT NULL -- Git author name        │
│ author_email VARCHAR(200) NOT NULL -- Git author email       │
│ message      TEXT                  -- Commit message         │
│ committed_at DATETIME NOT NULL     -- Commit timestamp       │
├─────────────────────────────────────────────────────────────┤
│ UNIQUE INDEX ON (pr_id, sha)                                 │
│ INDEX ON (author_id)                                         │
│ INDEX ON (author_email)  -- For matching non-linked commits  │
└─────────────────────────────────────────────────────────────┘
```

#### Entity Relationship Diagram

```
┌──────────────┐       ┌─────────────────┐       ┌──────────────┐
│ github_users │       │  pull_requests  │       │ repositories │
├──────────────┤       ├─────────────────┤       ├──────────────┤
│ id (PK)      │◄──┐   │ id (PK)         │──────►│ id (PK)      │
│ login        │   │   │ repository_id   │       │ owner        │
│ name         │   │   │ submitter_id ───┼───────┤ name         │
│ email        │   │   │ merged_by_id ───┼───┐   └──────────────┘
│ type         │   │   │ number          │   │
└──────────────┘   │   │ title           │   │
       ▲           │   │ state           │   │
       │           │   │ ...             │   │
       │           │   └────────┬────────┘   │
       │           │            │            │
       │           └────────────┼────────────┘
       │                        │
       │              ┌─────────┴─────────┐
       │              │                   │
       │              ▼                   ▼
       │   ┌─────────────────┐   ┌──────────────┐
       │   │ pr_participants │   │  pr_commits  │
       │   ├─────────────────┤   ├──────────────┤
       └───┤ user_id (FK)    │   │ author_id(FK)│───┐
           │ pr_id (FK)      │   │ pr_id (FK)   │   │
           │ actions (JSON)  │   │ sha          │   │
           └─────────────────┘   │ author_name  │   │
                                 │ author_email │◄──┘ (nullable link)
                                 │ message      │
                                 └──────────────┘
```

#### Migration Strategy

**Step 1: Create new tables (non-breaking)**
```sql
CREATE TABLE github_users (...);
CREATE TABLE pr_participants (...);
CREATE TABLE pr_commits (...);
```

**Step 2: Add nullable FK columns to pull_requests**
```sql
ALTER TABLE pull_requests ADD COLUMN submitter_id INTEGER REFERENCES github_users(id);
ALTER TABLE pull_requests ADD COLUMN merged_by_id INTEGER REFERENCES github_users(id);
```

**Step 3: Backfill migration script**
```python
# For each PR:
#   1. Look up or create github_users from submitter/merged_by logins
#   2. Populate submitter_id, merged_by_id
#   3. Migrate participants JSON → pr_participants rows
#   4. Migrate commits_breakdown JSON → pr_commits rows
#   5. Attempt to link pr_commits.author_id by matching email
```

**Step 4: Mark old columns as deprecated**
```python
# Keep for backwards compatibility during transition:
# - submitter (string) - deprecated, use submitter_id
# - merged_by (string) - deprecated, use merged_by_id
# - participants (JSON) - deprecated, use pr_participants
# - commits_breakdown (JSON) - deprecated, use pr_commits
```

**Step 5: Remove deprecated columns (future)**
```sql
ALTER TABLE pull_requests DROP COLUMN submitter;
ALTER TABLE pull_requests DROP COLUMN merged_by;
ALTER TABLE pull_requests DROP COLUMN participants;
ALTER TABLE pull_requests DROP COLUMN commits_breakdown;
```

#### User Resolution Logic

```python
class GitHubUserRepository:
    """Repository for GitHub user identity management."""

    async def get_or_create_from_api(
        self,
        user_data: GitHubUser
    ) -> tuple[User, bool]:
        """
        Get existing user or create from API response.

        GitHub API provides: id, login, type, avatar_url
        Additional fields fetched lazily or via user endpoint.
        """

    async def link_commit_author(
        self,
        author_name: str,
        author_email: str,
    ) -> int | None:
        """
        Attempt to link a git commit author to a GitHub user.

        Strategy:
        1. Exact email match in github_users.email
        2. Email match in previously seen pr_commits
        3. Return None if no match (store unlinked)
        """

    async def merge_identities(
        self,
        primary_id: int,
        duplicate_id: int,
    ) -> None:
        """
        Merge two user records (e.g., discovered same person).
        Updates all FKs to point to primary_id.
        """
```

#### Sync Changes

```python
# Current (Phase 1.6):
pr_sync = gh_pr.to_pr_sync(files, commits, reviews)
# participants is JSON dict

# Phase 2:
async def sync_pr_with_users(
    gh_pr: GitHubPullRequest,
    files: list[GitHubFile],
    commits: list[GitHubCommit],
    reviews: list[GitHubReview],
    user_repo: GitHubUserRepository,
) -> tuple[PRSync, list[PRParticipant], list[PRCommit]]:
    """
    Transform GitHub data with proper user resolution.

    Returns:
        - PRSync (without participants/commits_breakdown)
        - List of PRParticipant records to insert
        - List of PRCommit records to insert
    """
    # 1. Resolve submitter → submitter_id
    submitter, _ = await user_repo.get_or_create_from_api(gh_pr.user)

    # 2. Resolve merged_by → merged_by_id (if merged)
    merged_by_id = None
    if gh_pr.merged_by:
        merged_by, _ = await user_repo.get_or_create_from_api(gh_pr.merged_by)
        merged_by_id = merged_by.id

    # 3. Build participant records from reviews
    participants = []
    for review in reviews:
        user, _ = await user_repo.get_or_create_from_api(review.user)
        participants.append(PRParticipant(
            user_id=user.id,
            actions=[map_review_state(review.state)],
            first_action_at=review.submitted_at,
        ))

    # 4. Build commit records with attempted user linking
    pr_commits = []
    for commit in commits:
        author_id = await user_repo.link_commit_author(
            commit.commit.author.name,
            commit.commit.author.email,
        )
        pr_commits.append(PRCommit(
            sha=commit.sha,
            author_id=author_id,  # May be None
            author_name=commit.commit.author.name,
            author_email=commit.commit.author.email,
            message=commit.commit.message,
            committed_at=commit.commit.author.date,
        ))

    return pr_sync, participants, pr_commits
```

#### Query Examples

```sql
-- All PRs where user participated (as submitter, reviewer, or committer)
SELECT DISTINCT p.* FROM pull_requests p
LEFT JOIN pr_participants pp ON p.id = pp.pr_id
LEFT JOIN pr_commits pc ON p.id = pc.pr_id
WHERE p.submitter_id = ?
   OR p.merged_by_id = ?
   OR pp.user_id = ?
   OR pc.author_id = ?;

-- Top reviewers by approval count
SELECT u.login, COUNT(*) as approvals
FROM github_users u
JOIN pr_participants pp ON u.id = pp.user_id
WHERE pp.actions @> '["approval"]'
GROUP BY u.id
ORDER BY approvals DESC;

-- Contributors who commit but don't have GitHub accounts linked
SELECT DISTINCT author_name, author_email
FROM pr_commits
WHERE author_id IS NULL;

-- Bots vs humans activity
SELECT u.type, COUNT(DISTINCT p.id) as pr_count
FROM github_users u
JOIN pull_requests p ON u.id = p.submitter_id
GROUP BY u.type;
```

#### CLI Enhancements

```bash
# List known users
ghactivity users list
ghactivity users list --type bot
ghactivity users list --sort-by activity

# Show user activity
ghactivity users show octocat
ghactivity users show octocat --prs
ghactivity users show octocat --reviews

# Link commit author to GitHub user (manual override)
ghactivity users link-email "victor@example.com" --to octocat

# Merge duplicate user records
ghactivity users merge duplicate-login --into primary-login
```

#### Testing Strategy

| Test Category | Purpose |
|---------------|---------|
| Schema | Verify FK constraints, indexes, cascades |
| Migration | Test backfill from existing JSON data |
| User Resolution | Test get_or_create, email matching |
| Identity Linking | Test commit author → user matching |
| Queries | Verify user-centric query performance |

#### Goals Checklist

- [ ] Create `github_users` table with GitHub user ID as PK
- [ ] Create `pr_participants` junction table
- [ ] Create `pr_commits` table with full commit data
- [ ] Add `submitter_id`, `merged_by_id` FK columns to `pull_requests`
- [ ] Implement `GitHubUserRepository` with resolution logic
- [ ] Update sync pipeline to populate user tables
- [ ] Write backfill migration for existing data
- [ ] Add commit author → user email matching
- [ ] CLI commands for user management
- [ ] Deprecate old string/JSON columns
- [ ] Update all queries to use normalized tables

---

### 2.2 GitHub Issues Support

- [ ] Issue data model (similar to PR)
- [ ] Issue sync from GitHub API
- [ ] Issue tagging and search

### 2.3 Agent Integration

- [ ] `classify_tags` generation pipeline
- [ ] `ai_summary` generation on PR merge
- [ ] Configurable prompts/models

### 2.4 Search Enhancements

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
