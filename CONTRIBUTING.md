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
| `type: ignore` comments in src/ | ≤5 | Pre-commit audit |
| `noqa` comments in src/ | ≤3 | Pre-commit audit |
| `Any` type aliases | 0 | Pre-commit blocks |
| Mypy errors (src/ + tests/) | 0 | Pre-commit blocks |
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
| `ruff` | Linting | Auto-fixes or blocks |
| `ruff-format` | Formatting | Auto-fixes |
| `audit-type-ignores` | Count suppressions | Reports (target ≤5) |
| `audit-noqa` | Count suppressions | Reports (target ≤3) |
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
| ORM factories | `from_orm(obj: Any)` | N/A (accepts any ORM) |
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
github_client.py
rate_limit_monitor.py

# Classes: PascalCase
class PullRequestRepository:
class GitHubClient:

# Functions/methods: lowercase_with_underscores
def get_pull_request():
async def sync_repository():

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
from github_activity_db.schemas import PRCreate

if TYPE_CHECKING:
    from githubkit import GitHub
```

### Async Patterns

Use `async`/`await` consistently:

```python
# Good: async context manager
async with GitHubClient() as client:
    result = await client.get_pull_request(owner, repo, number)

# Good: async iteration
async for pr in client.iter_pull_requests(owner, repo):
    await process_pr(pr)
```

### CLI Commands

All CLI commands should use `run_async_command()` for unified error handling:

```python
from github_activity_db.cli.common import console, run_async_command

@app.command()
def my_command() -> None:
    """Command description."""
    async def _impl() -> dict[str, Any]:
        async with GitHubClient() as client:
            return await client.some_method()

    result = run_async_command(_impl())
    console.print(f"Result: {result}")
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

**1. Use `model_validate()` for Pydantic models:**

```python
# Good
pr = GitHubPullRequest.model_validate(GITHUB_PR_RESPONSE)

# Bad - loses type information
pr = GitHubPullRequest(**GITHUB_PR_RESPONSE)
```

**2. Assert on optional values before access:**

```python
result = await repo.get_by_number(...)
assert result is not None  # Narrows type
assert result.state == PRState.MERGED
```

**3. Use factory functions with explicit parameters:**

```python
pr = make_pull_request(
    db_session,
    repo,
    number=1234,
    state=PRState.OPEN,
    title="Test PR",
)
```

### Running Tests

```bash
# Run all tests
uv run pytest

# Run with coverage
uv run pytest --cov=src/github_activity_db --cov-report=term-missing

# Run specific test file
uv run pytest tests/test_db_models.py -v
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
   uv run mypy src/ tests/
   ```

3. **Check quality metrics:**
   ```bash
   grep -r "type: ignore" src/ | wc -l  # Should be ≤5
   grep -r "noqa:" src/ | wc -l         # Should be ≤3
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
