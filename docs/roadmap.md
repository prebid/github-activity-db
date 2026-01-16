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
- [x] Test infrastructure (69 tests, 87% coverage)
- [x] GitHub client with error handling

---

## Phase 1.5: Rate Limiting & Request Pacing ✅

### Architecture Overview

**Pattern:** Layered Architecture with Dependency Injection

```
┌─────────────────────────────────────────────────────────────────┐
│                      Application Layer                          │
│                  (CLI commands, sync services)                  │
└──────────────────────────────┬──────────────────────────────────┘
                               │ uses
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                     Orchestration Layer                         │
│   BatchExecutor (coordinates) │ RequestScheduler (queues work)  │
│   ProgressTracker (observes)                                    │
└──────────────────────────────┬──────────────────────────────────┘
                               │ delegates to
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                       Control Layer                             │
│   RequestPacer (calculates delays via token bucket)             │
│   RateLimitMonitor (tracks state from headers)                  │
└──────────────────────────────┬──────────────────────────────────┘
                               │ updates from headers
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                       GitHub Layer                              │
│   GitHubClient (makes API calls, extracts headers)              │
└─────────────────────────────────────────────────────────────────┘
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

268 total tests passing:
- `tests/github/rate_limit/` - Schema parsing, monitor state machine
- `tests/github/pacing/` - Pacer math, scheduler ordering, batch execution

### CLI Commands

```bash
ghactivity github rate-limit           # Check core rate limit
ghactivity github rate-limit --all     # Show all pools
ghactivity github rate-limit --all -v  # Verbose with reset times
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
