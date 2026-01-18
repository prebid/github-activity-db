# GitHub API Quirks and Behaviors

This document captures non-obvious behaviors of the GitHub REST API that affect our implementation.

---

## List API vs Full API Response Differences

GitHub's REST API has different response schemas for list endpoints vs single-resource endpoints.

### The Merged Status Issue

**Problem Discovered:** During production sync, only open PRs were being imported. All merged PRs were incorrectly filtered out.

**Root Cause:** The list API (`/repos/{owner}/{repo}/pulls`) does NOT include the actual `merged` status - it always returns `False`.

| Field | List API | Full API |
|-------|----------|----------|
| `merged` | **Always `False`** | Actual merge status |
| `merged_by` | Not included | Merge author |
| `merged_at` | Not included | Merge timestamp |
| `mergeable` | Not included | Merge conflict status |

**Evidence from live debugging:**
```python
# Same PR, different endpoints
PR #4549: list_api.merged=False, full_api.merged=True  # Actually merged!
PR #4615: list_api.merged=False, full_api.merged=True  # Actually merged!
```

### Fields Available from List API

The list endpoint provides these fields reliably:

```python
required_fields = [
    "number",       # PR number
    "state",        # "open" or "closed"
    "title",        # PR title
    "created_at",   # When PR was opened
    "updated_at",   # Last activity
    "user",         # Author info
    "labels",       # Applied labels
]
```

### Fields Only Available from Full API

These require fetching the individual PR:

```python
full_only_fields = [
    "merged",       # True/False merge status
    "merged_by",    # User who merged
    "merged_at",    # Merge timestamp
    "mergeable",    # Whether PR can be merged
    "additions",    # Lines added
    "deletions",    # Lines deleted
    "changed_files", # File count
    "commits",      # Commit count
]
```

---

## Implementation Implications

### Discovery Phase

The discovery phase (`iter_pull_requests`) cannot filter by merge status. It must include all closed PRs:

```python
# Cannot do this - merged is always False from list API
if config.state == "merged" and not pr.merged:  # BROKEN
    continue

# Correct approach - include all closed, filter in ingestion
if config.state == "merged" and pr.state == "open":
    continue  # Can skip open PRs for "merged" filter
# But must include all closed PRs - could be merged or abandoned
```

### Ingestion Phase

The ingestion phase fetches full PR data and can accurately filter:

```python
# After fetching full PR
if gh_pr.state == "closed" and not gh_pr.merged:
    # This is an abandoned PR (closed without merge)
    return PRIngestionResult.from_skipped_abandoned(existing)
```

### API Cost Trade-off

This design increases API calls for merged PR discovery:

| Before (broken) | After (correct) |
|-----------------|-----------------|
| 0 API calls for closed PRs | 4 API calls per closed PR |

The trade-off is acceptable: **correctness over efficiency**.

---

## Testing for API Behavior

### Contract Tests

We maintain contract tests to verify our assumptions about API structure:

```python
# tests/test_schemas_contract.py
def test_list_api_does_not_include_merged_status():
    """Document that list API merged field is always False."""
    from tests.fixtures.real_list_pr import REAL_LIST_PR_DATA
    assert REAL_LIST_PR_DATA.get("merged") is False
```

### Real API Fixtures

Test fixtures captured from actual API responses:

- `tests/fixtures/real_pr_open.py` - Full API response for open PR
- `tests/fixtures/real_pr_merged.py` - Full API response for merged PR

---

## PR State Handling

### State Values

| API State | Merged | Our State | Description |
|-----------|--------|-----------|-------------|
| `open` | - | `OPEN` | Work in progress |
| `closed` | `True` | `MERGED` | Successfully merged |
| `closed` | `False` | `CLOSED` | Abandoned (not imported) |

### State Detection

```python
def determine_pr_state(gh_pr: GitHubPullRequest) -> PRState:
    if gh_pr.state == "open":
        return PRState.OPEN
    elif gh_pr.merged:
        return PRState.MERGED
    else:
        return PRState.CLOSED  # Abandoned
```

---

## Related Documentation

- [Roadmap](roadmap.md) - Phase 1.10 for implementation timeline
- [Testing Guide](testing.md) - API contract testing patterns
- [Architecture](architecture.md) - Overall system design
