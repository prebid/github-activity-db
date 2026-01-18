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
│  │  Every API method:                                         │  │
│  │    1. _apply_pacing() → delay if needed                    │  │
│  │    2. Make GitHub API request                              │  │
│  │    3. _update_rate_limit_from_response() → notify pacer    │  │
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
│  │         RequestPacer │ RateLimitMonitor                    │  │
│  │    (Calculates delays, tracks state from headers)          │  │
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
| `GitHubClient` | Apply pacing before each request, update monitor/pacer after | Calculate delays (delegates to pacer) |
| `RateLimitMonitor` | Track rate limit state from headers | Make pacing decisions |
| `RequestPacer` | Calculate optimal delays | Queue or execute requests |
| `RequestScheduler` | Queue and prioritize requests | Calculate delays |
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
│    delay = pacer.get_recommended_delay(RateLimitPool.CORE)       │
│    if delay > 0: await asyncio.sleep(delay)                      │
│    pacer.on_request_start()                                       │
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

The `RequestPacer` implements a token bucket algorithm with adaptive throttling:

### Algorithm

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
| `PACING__MIN_REQUEST_INTERVAL_MS` | `50` | Minimum delay between requests |
| `PACING__MAX_REQUEST_INTERVAL_MS` | `1000` | Maximum delay between requests |
| `PACING__RESERVE_BUFFER_PCT` | `0.1` | Reserve 10% of rate limit |
| `PACING__BURST_ALLOWANCE` | `10` | Allow burst of requests |
| `PACING__MAX_CONCURRENT` | `5` | Max concurrent requests |

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
