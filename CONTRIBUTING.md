# Contributing to GitHub Activity DB

Thank you for your interest in contributing to GitHub Activity DB! This document outlines our quality standards, coding conventions, and the process for submitting contributions.

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Getting Started](#getting-started)
- [Quality Standards](#quality-standards)
- [Type Safety Policy](#type-safety-policy)
- [Code Style Guidelines](#code-style-guidelines)
- [Testing Requirements](#testing-requirements)
- [Pull Request Process](#pull-request-process)

---

## Code of Conduct

- Be respectful and inclusive
- Provide constructive feedback
- Focus on the code, not the person
- Help others learn and grow

---

## Getting Started

1. **Fork the repository** and clone your fork
2. **Install dependencies**: `uv sync --all-extras`
3. **Set up pre-commit hooks**: `uv run pre-commit install`
4. **Run tests** to verify your setup: `uv run pytest`

See [Development Guide](docs/development.md) for detailed setup instructions.

---

## Quality Standards

This project maintains strict code quality standards with automated enforcement. All contributions must pass these checks before merging.

### Quality Metrics

| Metric | Target | Enforcement |
|--------|--------|-------------|
| `type: ignore` comments in src/ | ≤5 | Pre-commit blocks |
| `noqa` comments in src/ | ≤3 | Pre-commit blocks |
| `Any` type aliases | 0 | Pre-commit blocks |
| Mypy errors (src/, tests/, scripts/) | 0 | Pre-commit blocks |
| Test coverage (critical paths) | 100% | Manual review |

### Pre-commit Hooks

All commits must pass the following automated checks:

```bash
# Run all checks before committing
uv run pre-commit run --all-files
```

| Hook | Purpose | Action on Failure |
|------|---------|-------------------|
| `mypy` | Type checking | Blocks commit |
| `ruff-check` | Linting | Auto-fixes or blocks |
| `ruff-format` | Formatting | Auto-fixes |
| `suppression-caps` | Count `type: ignore` and `noqa` comments in src/ | Blocks above 5 and 3 |
| `prevent-any-aliases` | Block lazy types | Blocks commit |

### Quality Gate Philosophy

We believe in:

1. **Fix the root cause, not the symptom** - Don't suppress type errors; fix the underlying type issue
2. **Explicit over implicit** - Type annotations should be clear and specific
3. **Maintainability over convenience** - A few extra lines of typed code is better than `Any`
4. **Automation over documentation** - Enforce standards through tooling, not just guidelines

---

## Type Safety Policy

### Required Type Annotations

- All public functions must have type annotations
- All class attributes must have type annotations
- Use `from __future__ import annotations` for forward references

### `Any` Usage Guidelines

| Use Case | Allowed | Alternative |
|----------|---------|-------------|
| JSON/dict serialization | `dict[str, Any]` | N/A (legitimate use) |
| Pydantic validators | `v: Any` | N/A (Pydantic pattern) |
| Log context binding | `**context: Any` | N/A (dynamic logging) |
| **Lazy initialization** | **Forbidden** | Use `T \| None` with `TYPE_CHECKING` |
| **Avoiding generics** | **Forbidden** | Use `TYPE_CHECKING` block |
| **Third-party without types** | Document | Add stub or `TYPE_CHECKING` |

### `type: ignore` Policy

1. **Must include error code**: `# type: ignore[arg-type]`
2. **Must have justification** if not obvious from context
3. **Prefer fixing** the underlying type issue
4. **Tracked**: Pre-commit reports count (target: ≤5)

**Good:**
```python
# Pydantic computed_field returns property, not method
@computed_field  # type: ignore[prop-decorator]
@property
def usage_percent(self) -> float:
    return ...
```

**Bad:**
```python
# Avoid: suppressing without understanding
result = something()  # type: ignore
```

### `noqa` Policy

1. **Centralize** in shared utility functions when possible
2. **Document** why the rule doesn't apply
3. **Tracked**: Pre-commit reports count (target: ≤3)

---

## Code Style Guidelines

### General Principles

- **DRY (Don't Repeat Yourself)** - Extract common patterns into shared utilities
- **Single Responsibility** - Each function/class should do one thing well
- **Explicit over Implicit** - Be clear about types and intentions
- **Fail Fast** - Validate inputs early and raise clear errors

### Naming Conventions

```python
# Modules: lowercase_with_underscores
rate_limits.py
check_frozen_db.py

# Classes: PascalCase
class GitHubSource:
class PullRequestHydrator:

# Functions/methods: lowercase_with_underscores
def link_key():
async def read_rate_limits():

# Constants: UPPERCASE_WITH_UNDERSCORES
MAX_RETRIES = 3
DEFAULT_TIMEOUT = 30
```

### Import Organization

Imports are automatically organized by `ruff`. The order is:

1. Standard library
2. Third-party packages
3. Local application imports

```python
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import typer
from pydantic import BaseModel

from github_activity_db.db.models import PullRequest
from github_activity_db.source import GitHubSource

if TYPE_CHECKING:
    from github_activity_db.source.records import PullRequestRecord
```

### Async Patterns

Use `async`/`await` consistently:

```python
# Good: async context manager
async with GitHubSource(token) as source:
    repository = await source.resolve(owner, name)

    # Good: async iteration
    async for outcomes in source.hydrate_pull_requests(repository, numbers):
        await write(outcomes)
```

### CLI Commands

All CLI commands should use `run_async_command()` for unified error handling:

```python
from github_activity_db.cli import common
from github_activity_db.source import RateLimits

@app.command()
def my_command() -> None:
    """Command description."""
    token = common.github_token()

    async def _impl() -> RateLimits:
        async with common.open_source(token) as source:
            return await source.rate_limits()

    result = common.run_async_command(_impl())
    common.console.print(f"GraphQL remaining: {result.graphql_remaining}")
```

---

## Testing Requirements

### Test Coverage

| Category | Requirement |
|----------|-------------|
| Critical paths | 100% coverage |
| New features | Tests required |
| Bug fixes | Regression test required |
| Edge cases | High coverage |

### Test Patterns

**1. Assert on optional values before access:**

```python
stored = await session.get(UserTag, tag_id)
assert stored is not None  # Narrows type
assert stored.name == "committed"
```

**2. Build databases with migrations, and seed them through the models:**

```python
def test_reviewers(migrated_database: Path) -> None:  # a copy of the alembic-built template
    with connect(migrated_database) as connection:     # tests/db/seed.py
        seed = Seed(connection)
        pr = seed.pull_request(seed.repository("prebid/Prebid.js"), 13481, seed.user("author"))
```

**3. Test both sides with inputs the test builds; never mutate the code** (redesign plan D39). No
mutation testing, under any name: the code under test and the checks are never changed, on disk or
in memory, to see whether a test notices. No false content is planted in the project's real files or
documents (the plan, a document, review material, a tracked lock or database) to see whether a check
or a reviewer catches it, and no reviewer or agent is scored on how many seeded flaws it finds.
Negative tests are expected: a test builds its own invalid or failing input on purpose (a sample
test that fails, an invalid configuration, a malformed or edited copy of a file or database in its
temporary directory, a test-only revision that raises, a stub or fake that raises, a recorded GitHub
response changed in memory) and feeds it to the unchanged code. Each test asserts the exact result.
Every check is tested on both sides, and its passing side states the exact count of the rows, files,
or values it compared, so it cannot pass on nothing. A fix comes with a regression test that fails
against the parent commit's code. See the [Testing Guide](docs/testing.md#principles).

**4. Write tests that give the same result every time** (redesign plan D40). The same code gives
every test the same outcome on any machine, on any date, and in any run order: a test, and every
code path it runs, reads no wall clock (a fixed clock such as `FakeClock` stands in), reaches no
network (GitHub answers from recordings and stubs), uses no unseeded randomness, depends on no state
outside its own temporary directory, the repository, and the session's database template, and bounds
every wait with a timeout. A test exercises code and asserts on what the code returns, writes, or
exits with: where the code gives a structured value (an exit code, rows, parsed fields, an
exception's class and fields), the test asserts on it rather than on printed wording, and no test
checks the wording of a document. Where a test needs an input the code cannot easily be given, the
project builds software that produces it (a fake clock, the scenario model, the replay, a recorder
mode) or the test builds the input itself (D39); no test changes the project's real files. Evidence
is what software reproduces: an acceptance criterion names a test, a check, or a command whose
output shows it, and a search of text, a reading, or a review finds work to do but is never
acceptance.

### Running Tests

```bash
# Run all tests
uv run pytest

# Run with coverage
uv run pytest --cov=src/github_activity_db --cov-report=term-missing

# Run specific test file
uv run pytest tests/db/test_schema.py -v
```

See [Testing Guide](docs/testing.md) for comprehensive testing documentation.

---

## Pull Request Process

### Before Submitting

1. **Run all checks locally:**
   ```bash
   uv run pre-commit run --all-files
   uv run pytest
   ```

2. **Ensure mypy passes:**
   ```bash
   uv run mypy src tests scripts
   ```

3. **Check quality metrics:**
   ```bash
   uv run python scripts/count_suppressions.py src --max-type-ignore 5 --max-noqa 3
   ```

### PR Requirements

- [ ] All pre-commit hooks pass
- [ ] All tests pass
- [ ] New code has test coverage
- [ ] Type annotations are complete
- [ ] No new `type: ignore` without justification
- [ ] No new `noqa` without justification
- [ ] Documentation updated if needed

### PR Description Template

```markdown
## Summary
Brief description of changes

## Changes
- Change 1
- Change 2

## Testing
How was this tested?

## Checklist
- [ ] Pre-commit hooks pass
- [ ] Tests pass
- [ ] Documentation updated
```

### Review Process

1. Automated checks must pass
2. At least one approval required
3. All comments must be resolved
4. Squash and merge preferred

---

## Questions?

- Check the [Development Guide](docs/development.md)
- Check the [Testing Guide](docs/testing.md)
- Open an issue for questions or suggestions

Thank you for contributing!
