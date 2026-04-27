# Rate Limit Handling

This document describes the rate limiting strategy, pacing algorithm, and retry handling used in GitHub Activity DB.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                     GitHub Layer (github/)                       │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │                      GitHubClient                          │  │
│  │     (public API for callers, with integrated pacing)       │  │
│  │                                                            │  │
│  │  Every API method (and every page of paginated calls):     │  │
│  │    1. _apply_pacing() → await pacer.acquire() (one token) │  │
│  │    2. Make GitHub API request                              │  │
│  │    3. _update_rate_limit_from_response() → bucket+monitor  │  │
│  └─────────────────────────┬─────────────────────────────────┘  │
│                            │ uses internally                     │
│  ┌─────────────────────────▼─────────────────────────────────┐  │
│  │                  Orchestration Layer                       │  │
│  │    BatchExecutor │ RequestScheduler │ ProgressTracker      │  │
│  │    (Controls concurrency and batch operations)             │  │
│  └─────────────────────────┬─────────────────────────────────┘  │
│                            │ delegates to                        │
│  ┌─────────────────────────▼─────────────────────────────────┐  │
│  │                    Control Layer                           │  │
│  │   RequestPacer (façade) → AsyncTokenBucket │ Monitor       │  │
│  │   (Single shared admission gate; tracks state)             │  │
│  └─────────────────────────┬─────────────────────────────────┘  │
│                            │                                     │
└────────────────────────────┼─────────────────────────────────────┘
                             ▼
                       GitHub API
```

---

## Component Responsibilities

| Component | Responsibility | Does NOT |
|-----------|----------------|----------|
| `GitHubClient` | Acquire from the pacer before each request (and each page); feed response headers back to the bucket | Decide concurrency (delegates to scheduler) |
| `RateLimitMonitor` | Track rate-limit state from response headers | Make pacing decisions (the bucket does that) |
| `AsyncTokenBucket` | Issue tokens at an adaptive rate; block on hard floor / forced wait | Manage retries or priority |
| `RequestPacer` | Façade over the bucket: lifecycle, stats, and forced-wait API | Compute per-call delays (the old model) |
| `RequestScheduler` | Queue, prioritize, and concurrency-cap requests; retry on rate-limit errors | Pace individual API calls |
| `BatchExecutor` | Coordinate batch operations | Implement queueing |
| `ProgressTracker` | Observe and report progress | Affect execution |

---

## Client-Level Pacing Integration

The `GitHubClient` integrates pacing at the lowest level, ensuring **every API call** is automatically paced. This is critical because:

1. **Scheduler only controls PR-level concurrency** - it manages when to start a new PR ingestion (max 5 concurrent)
2. **Each PR makes 4+ API calls** - `get_full_pull_request()` calls 4 methods sequentially
3. **Without client-level pacing**: 5 PRs × 4 calls = 20 rapid requests, exhausting rate limits quickly

### Client Initialization

CLI commands initialize the pacing infrastructure before making requests:

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

### Request Flow

```
┌──────────────────────────────────────────────────────────────────┐
│  client.get_pull_request(owner, repo, number)                     │
└──────────────────────────┬───────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│  _apply_pacing()                                                  │
│    await pacer.acquire()  ← single shared token bucket            │
│    (blocks until a token is available; concurrency-aware)         │
└──────────────────────────┬───────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│  Make GitHub API Request                                          │
│    response = await github.rest.pulls.get(...)                   │
└──────────────────────────┬───────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│  _update_rate_limit_from_response(response)                       │
│    Extract: x-ratelimit-remaining, x-ratelimit-reset, etc.       │
│    monitor.update_from_headers(headers)                          │
│    pacer.on_request_complete(headers)  ← Closes feedback loop    │
└──────────────────────────────────────────────────────────────────┘
```

### Header Feedback Loop

The feedback loop ensures the pacer always has current rate limit state:

1. **Request completes** → response headers contain `x-ratelimit-*` values
2. **Monitor updates** → tracks remaining quota and reset time
3. **Pacer notified** → adjusts next delay calculation
4. **Next request** → uses updated delay based on current state

This zero-cost tracking (no extra API calls) keeps the system responsive to quota changes.

---

## Token Bucket Algorithm

The `RequestPacer` wraps an `AsyncTokenBucket` — a concurrency-safe shared
admission gate. All workers acquire from the same bucket, so the realized
request rate matches the bucket rate regardless of how many workers run.
This is a deliberate change from the prior per-call delay model, which
under N workers each computing the same delay produced a realized rate of
``N x intended`` and exhausted the quota.

### Algorithm

```
STATE (per bucket):
  rate            = tokens issued per second (adaptive)
  capacity        = max accumulated tokens (burst limit, default 10)
  hard_floor      = quota threshold below which we block until reset
  wait_until      = forced block deadline (set on observed 403/429)

ACQUIRE (called before every request):
  1. If wait_until is in the future, sleep until it expires.
  2. Refill tokens by (now - last_refill) * rate, capped at capacity.
  3. If tokens >= 1: consume one, return.
  4. Else compute (1 - tokens) / rate, sleep that long, retry from 1.

UPDATE FROM HEADERS (called on every response):
  remaining   = x-ratelimit-remaining
  reset_at    = x-ratelimit-reset
  if remaining <= hard_floor:
      wait_until = reset_at        # hard admission gate
      rate = min_rate
  else:
      budget = remaining - hard_floor
      rate = clamp(budget / (reset_at - now), min_rate, max_rate)
```

### Why a shared bucket

A per-call delay layer cannot maintain a target rate across N concurrent
workers without coordination — each worker independently observes the same
``x-ratelimit-remaining`` and computes the same delay, so the realized
total rate is ``N x intended``. A single shared bucket serializes token
issuance through one async lock, so adding workers does not multiply the
issuance rate.

The bucket also provides a hard admission gate: when ``remaining`` falls
below ``hard_floor`` (or a 403/429 with ``Retry-After`` is observed), all
acquires block until the reset deadline. This prevents the in-flight
overshoot that any per-call delay scheme tolerates.

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

---

## Retry Handling

### Retryable Errors

The system distinguishes between retryable and non-retryable errors:

```python
class GitHubRetryableError(GitHubClientError):
    """Base class for errors that should be retried by the scheduler."""
    pass

class GitHubRateLimitError(GitHubRetryableError):
    """Raised when rate limit is exceeded."""
    reset_at: datetime | None  # When rate limit resets
```

### Retry Flow

```
Request fails with GitHubRateLimitError
    │
    ▼
Scheduler._handle_request_error()
    │
    ├─── Rate limit error?
    │         │
    │         ▼
    │    Pacer.force_wait(reset_at)
    │         │
    │         ▼
    │    Requeue with HIGH priority
    │
    └─── Other retryable error?
              │
              ▼
         Exponential backoff (2, 4, 8... seconds)
              │
              ▼
         Requeue for retry
```

### Exponential Backoff

For non-rate-limit retryable errors:

```python
backoff = min(2 ** retry_count, 60)  # Cap at 60 seconds
await asyncio.sleep(backoff)
```

| Retry | Backoff |
|-------|---------|
| 1 | 2 seconds |
| 2 | 4 seconds |
| 3 | 8 seconds |
| 4 | 16 seconds |
| 5 | 32 seconds |
| 6+ | 60 seconds (capped) |

### Discovery Phase Retry

Rate limit errors during PR discovery are handled with explicit retry logic:

```python
for attempt in range(1, max_retries + 1):
    try:
        async for pr in client.iter_pull_requests(...):
            # ... process PRs
        return pr_numbers
    except GitHubRateLimitError as e:
        if attempt == max_retries:
            raise
        wait_time = 60.0
        if e.reset_at:
            wait_time = max(5.0, (e.reset_at - now).total_seconds() + 5)
        await asyncio.sleep(wait_time)
```

---

## CLI Commands

### Check Rate Limits

```bash
ghactivity github rate-limit           # Check core rate limit
ghactivity github rate-limit --all     # Show all pools
ghactivity github rate-limit --all -v  # Verbose with reset times
```

### Example Output

```
Rate Limit Status: HEALTHY

Core API:
  Remaining: 4,850 / 5,000 (97%)
  Resets in: 42m 30s

Search API:
  Remaining: 28 / 30 (93%)
  Resets in: 1m 15s
```

---

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PACING__MIN_REQUEST_INTERVAL_MS` | `50` | Sets the bucket's max rate ceiling (1000 / value = req/s) |
| `PACING__MAX_REQUEST_INTERVAL_MS` | `60000` | Sets the bucket's min rate floor (1000 / value = req/s) |
| `PACING__RESERVE_BUFFER_PCT` | `10.0` | Hard floor: bucket blocks until reset when `remaining <= floor` (`max(50, pct% × 5000)`) |
| `PACING__BURST_ALLOWANCE` | `10` | Bucket capacity (tokens that can accumulate) |
| `PACING__MAX_CONCURRENT_REQUESTS` | `5` | Scheduler concurrency cap (in-flight requests) |

### File Structure

```
src/github_activity_db/github/
├── rate_limit/
│   ├── __init__.py         # Public exports
│   ├── schemas.py          # RateLimitPool, PoolRateLimit, RateLimitSnapshot
│   └── monitor.py          # RateLimitMonitor
└── pacing/
    ├── __init__.py         # Public exports
    ├── token_bucket.py     # AsyncTokenBucket (shared admission gate)
    ├── pacer.py            # RequestPacer (façade over the bucket)
    ├── scheduler.py        # RequestScheduler (priority queue)
    ├── batch.py            # BatchExecutor
    └── progress.py         # ProgressTracker
```

---

## Testing Considerations

When testing rate limit handling, mock `asyncio.sleep` to avoid real delays:

```python
@pytest.fixture
def mock_scheduler_sleep():
    """Mock asyncio.sleep to avoid real exponential backoff delays."""
    original_sleep = asyncio.sleep

    async def fast_sleep(delay: float) -> None:
        if delay >= 1.0:
            await original_sleep(0.001)  # Minimal yield
        else:
            await original_sleep(delay)  # Keep small delays

    with patch(
        "github_activity_db.github.pacing.scheduler.asyncio.sleep",
        side_effect=fast_sleep,
    ) as mock:
        yield mock
```

See [Testing Guide](testing.md) for more patterns.

---

## Related Documentation

- [Roadmap](roadmap.md) - Phase 1.5, 1.12 for implementation timeline
- [Testing Guide](testing.md) - Sleep mocking patterns
- [Architecture](architecture.md) - Overall system design
