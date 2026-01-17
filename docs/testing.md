# Testing Guide

This document describes the testing strategy, infrastructure, and patterns used in GitHub Activity DB.

---

## Philosophy

### Why We Test

1. **Confidence** - Verify code works as intended before shipping
2. **Refactoring Safety** - Change implementation without breaking behavior
3. **Documentation** - Tests demonstrate how components should be used
4. **Regression Prevention** - Catch bugs before they reach production

### Test Pyramid

```
        ▲
       /E\        E2E Tests (Few, Slow)
      /2E \       - Full pipeline: GitHub API → Database
     /─────\
    /Integration\ Integration Tests (Some, Medium)
   /─────────────\ - Component interactions
  /     Unit      \ Unit Tests (Many, Fast)
 /─────────────────\ - Isolated functions, classes
```

**Principle:** More unit tests (fast, isolated), fewer E2E tests (slow, brittle).

### Mocking Strategy

- **Mock external dependencies** (GitHub API, network)
- **Test real internal logic** (business rules, transformations)
- **Use real database** (in-memory SQLite for speed + isolation)

---

## Test Categories

### Unit Tests (Fast, Isolated)

Test individual functions and classes in isolation.

| Area | Location | Examples |
|------|----------|----------|
| Schema validation | `tests/test_schemas_*.py` | Field constraints, serialization |
| Repository CRUD | `tests/db/repositories/` | Create, read, update operations |
| Pacing algorithms | `tests/github/pacing/test_pacer.py` | Delay calculations, throttling |
| Result dataclasses | `tests/github/sync/test_bulk_ingestion.py` | Aggregation logic |
| Rate limit schemas | `tests/github/rate_limit/test_schemas.py` | Status enums, snapshots |

**Characteristics:**
- No external dependencies
- Fast execution (< 1ms each)
- Deterministic (no timing-dependent behavior)

### Integration Tests (Component Interactions)

Test multiple components working together.

| Area | Location | Examples |
|------|----------|----------|
| Ingestion service | `tests/github/sync/test_ingestion.py` | Client + Repository + Service |
| Bulk ingestion | `tests/github/sync/test_bulk_ingestion.py` | Discovery + Batch execution |
| CLI commands | `tests/test_cli_sync.py` | CLI → Service → (mocked) DB |

**Characteristics:**
- Mock external APIs (GitHub)
- Use real database (in-memory SQLite)
- Test component contracts

### E2E Tests (Full Pipeline)

Test complete workflows from input to output.

| Area | Location | Examples |
|------|----------|----------|
| PR ingestion | `tests/test_pr_ingestion_e2e.py` | Mock API → Transform → Store → Read |

**Characteristics:**
- Full data flow
- Real database operations
- Verify final state

---

## Current Coverage

### Test Statistics (403+ tests)

| Module | Tests | Coverage |
|--------|-------|----------|
| `github/pacing/` | 104 | ✅ Comprehensive |
| `github/sync/` | 31 | ✅ Good |
| `github/rate_limit/` | 15 | ⚠️ Partial |
| `db/repositories/` | 42 | ✅ Good |
| `schemas/` | 150+ | ✅ Comprehensive |
| `cli/` | 27 | ⚠️ Mocked only |
| E2E | 11 | ✅ Core paths |

### Known Gaps

| Gap | Priority | Notes |
|-----|----------|-------|
| GitHubClient unit tests | HIGH | Core component lacks dedicated tests |
| CLI integration tests | HIGH | Only mocked, no real DB tests |
| Pacer + Scheduler integration | MEDIUM | Tested separately, not together |
| Rate limit state transitions | MEDIUM | Partial coverage |

---

## Test Infrastructure

### Directory Structure

```
tests/
├── conftest.py              # Shared fixtures (db_session, engine)
├── factories.py             # Factory functions for test data
├── fixtures/                # Mock data files
│   ├── github_responses.py  # GitHub API mock responses
│   ├── rate_limit_responses.py
│   ├── real_pr_open.py      # Real open PR fixture
│   └── real_pr_merged.py    # Real merged PR fixture
├── db/
│   └── repositories/        # Repository CRUD tests
├── github/
│   ├── pacing/              # Pacer, scheduler, batch tests
│   ├── rate_limit/          # Rate limit monitor tests
│   └── sync/                # Ingestion service tests
├── test_config.py           # Settings tests
├── test_db_*.py             # Database layer tests
├── test_schemas_*.py        # Schema validation tests
├── test_cli_sync.py         # CLI command tests
└── test_pr_ingestion_e2e.py # E2E integration tests
```

### Key Fixtures

#### Database Session (`conftest.py`)

```python
@pytest.fixture
async def db_session(test_engine):
    """Async session with auto-rollback for test isolation."""
    async with async_sessionmaker(test_engine)() as session:
        yield session
        await session.rollback()  # Isolation between tests
```

**Key properties:**
- In-memory SQLite (fast, no cleanup needed)
- Auto-rollback (each test starts fresh)
- Async support (matches production code)

#### Factory Functions (`factories.py`)

**Model Factories** (add to session):
```python
from tests.factories import make_repository, make_pull_request, make_merged_pr

# Create repository
repo = make_repository(db_session, owner="prebid", name="prebid-server")
await db_session.flush()

# Create PR linked to repository
pr = make_pull_request(db_session, repo, number=1234, title="Add feature")
await db_session.flush()

# Create merged PR with all fields
merged_pr = make_merged_pr(db_session, repo, number=5678, merged_by="reviewer")
await db_session.flush()
```

**Schema Factories** (return dicts for Pydantic):
```python
from tests.factories import make_github_pr, make_github_user, make_github_review

# Create GitHub API response dict
github_pr = make_github_pr(number=1234, state="open", title="Test PR")
github_user = make_github_user(login="testuser")
github_review = make_github_review(user=github_user, state="APPROVED")
```

### Mocking Patterns

#### AsyncMock for Async Methods

```python
from unittest.mock import AsyncMock, patch

@patch("github_activity_db.github.client.GitHubClient")
async def test_example(mock_client_class):
    mock_client = AsyncMock()
    mock_client_class.return_value.__aenter__.return_value = mock_client

    # Configure mock behavior
    mock_client.get_full_pull_request.return_value = make_github_pr(number=123)

    # Test code that uses the client
    ...
```

#### Async Iterator Helper

```python
async def async_iter(items):
    """Convert a list to an async iterator for mocking iter_pull_requests."""
    for item in items:
        yield item

# Usage in test
mock_client.iter_pull_requests.return_value = async_iter([pr1, pr2, pr3])
```

#### Response Fixtures

Real GitHub API responses stored in `tests/fixtures/`:

```python
from tests.fixtures.real_pr_open import REAL_OPEN_PR_DATA
from tests.fixtures.real_pr_merged import REAL_MERGED_PR_DATA

# Use in contract tests to verify schema parsing
def test_parse_real_open_pr():
    pr = GitHubPullRequest.model_validate(REAL_OPEN_PR_DATA)
    assert pr.state == "open"
```

---

## Running Tests

### Basic Commands

```bash
# Run all tests
uv run pytest

# Run with verbose output
uv run pytest -v

# Run specific test file
uv run pytest tests/test_db_models.py

# Run tests matching a pattern
uv run pytest -k "test_pr"

# Run specific test class
uv run pytest tests/github/sync/test_bulk_ingestion.py::TestBulkIngestionResult
```

### Coverage Reports

```bash
# Run with coverage
uv run pytest --cov=src/github_activity_db --cov-report=term-missing

# Generate HTML coverage report
uv run pytest --cov=src/github_activity_db --cov-report=html
# Open htmlcov/index.html in browser
```

### Filtering Tests

```bash
# Run only async tests
uv run pytest -m asyncio

# Run only fast tests (exclude slow E2E)
uv run pytest --ignore=tests/test_pr_ingestion_e2e.py

# Run tests in parallel (requires pytest-xdist)
uv run pytest -n auto
```

---

## Writing Tests

### Naming Conventions

```python
# Test file: test_<module>.py
# Test class: Test<ClassName>
# Test method: test_<behavior>_<condition>

class TestPullRequestRepository:
    def test_create_returns_model(self): ...
    def test_create_with_duplicate_raises_error(self): ...
    def test_get_by_number_returns_none_when_not_found(self): ...
```

### Test Structure (Arrange-Act-Assert)

```python
async def test_create_pull_request(db_session):
    # Arrange - Set up test data
    repo = make_repository(db_session)
    await db_session.flush()
    pr_data = PRCreate(number=123, title="Test", ...)

    # Act - Execute the code under test
    repository = PullRequestRepository(db_session)
    result = await repository.create(pr_data, repo.id)

    # Assert - Verify the outcome
    assert result.number == 123
    assert result.repository_id == repo.id
```

### Testing Async Code

```python
import pytest

# pytest-asyncio handles async test functions automatically
async def test_async_operation(db_session):
    result = await some_async_function()
    assert result is not None

# For async context managers
async def test_client_context_manager():
    async with GitHubClient() as client:
        result = await client.get_rate_limit()
        assert result is not None
```

### Testing Exceptions

```python
import pytest

def test_invalid_input_raises_validation_error():
    with pytest.raises(ValidationError) as exc_info:
        PRCreate(number=-1, title="")  # Invalid data

    assert "number" in str(exc_info.value)

async def test_not_found_raises_exception(db_session):
    repository = PullRequestRepository(db_session)

    with pytest.raises(PRNotFoundError):
        await repository.get_by_number(repo_id=1, number=99999)
```

---

## Coverage Goals

### Targets

| Category | Target | Rationale |
|----------|--------|-----------|
| Overall | 80%+ | Reasonable for active development |
| Critical paths | 100% | Ingestion, rate limiting, data integrity |
| Edge cases | High | Error handling, boundary conditions |
| Happy paths | 100% | Core functionality must work |

### Critical Paths Requiring 100% Coverage

1. **PR Ingestion Pipeline** - Data must be correctly transformed and stored
2. **Rate Limit Handling** - Must not exceed API limits
3. **Frozen State Logic** - Merged PRs must not be updated after grace period
4. **Schema Validation** - Invalid data must be rejected

---

## CI/CD Integration

### Pre-commit Hooks

```bash
# Install hooks
uv run pre-commit install

# Runs automatically on commit:
# - ruff (lint + format)
# - mypy (type check)
# - pytest (test subset)
```

### GitHub Actions (Future)

```yaml
# .github/workflows/test.yml
- name: Run tests
  run: uv run pytest --cov --cov-fail-under=80
```

---

## Troubleshooting

### "Event loop is closed"

Use `pytest-asyncio` with proper fixtures:
```python
@pytest.fixture
async def db_session():
    # Ensure proper async context
    async with session_factory() as session:
        yield session
```

### "Database is locked"

SQLite concurrent access issue. Use `NullPool`:
```python
create_async_engine(url, poolclass=pool.NullPool)
```

### Flaky Async Tests

Avoid timing-dependent assertions:
```python
# Bad - timing dependent
await asyncio.sleep(0.1)
assert task.done()

# Good - wait for completion
await asyncio.wait_for(task, timeout=1.0)
```
