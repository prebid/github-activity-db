"""Pytest configuration and shared fixtures.

Usage Guide:
- For ORM model tests: import factories from tests.factories
- For schema validation tests: use dict fixtures (sample_pr_open, etc.)
- For GitHub API tests: import schema factories from tests.factories
"""

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from github_activity_db.db.models import Base, PRState

# -----------------------------------------------------------------------------
# Test Timeline Constants
#
# Define a consistent "test epoch" for deterministic date matching across tests.
# All hardcoded dates should reference these constants for consistency.
# -----------------------------------------------------------------------------

# Base dates (datetime objects for Pydantic/ORM)
JAN_10 = datetime(2024, 1, 10, 9, 0, 0, tzinfo=UTC)   # Merged PR opened
JAN_12 = datetime(2024, 1, 12, 16, 0, 0, tzinfo=UTC)  # Merged PR closed
JAN_15 = datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)  # Open PR opened, first commit
JAN_16 = datetime(2024, 1, 16, 14, 0, 0, tzinfo=UTC)  # Open PR updated
JAN_20 = datetime(2024, 1, 20, 16, 0, 0, tzinfo=UTC)  # Alternate merge date

# ISO 8601 strings (for GitHub API mocks)
JAN_10_ISO = "2024-01-10T09:00:00Z"
JAN_12_ISO = "2024-01-12T16:00:00Z"
JAN_15_ISO = "2024-01-15T10:00:00Z"
JAN_15_AFTERNOON_ISO = "2024-01-15T14:00:00Z"  # Second commit same day
JAN_15_EVENING_ISO = "2024-01-15T16:00:00Z"    # First review
JAN_16_MORNING_ISO = "2024-01-16T09:00:00Z"    # Third commit
JAN_16_MID_ISO = "2024-01-16T10:00:00Z"        # Second review
JAN_16_LATE_ISO = "2024-01-16T11:00:00Z"       # Third review
JAN_16_ISO = "2024-01-16T14:00:00Z"
JAN_16_UPDATED_ISO = "2024-01-16T14:30:00Z"    # PR updated_at
JAN_20_ISO = "2024-01-20T16:00:00Z"


# -----------------------------------------------------------------------------
# Database Fixtures
# -----------------------------------------------------------------------------
@pytest.fixture
async def test_engine():
    """Create an in-memory SQLite engine for tests.

    Each test gets a fresh database with all tables created.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session(test_engine):
    """Create an async session with auto-rollback.

    Changes are rolled back after each test to ensure isolation.
    """
    session_factory = async_sessionmaker(
        test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with session_factory() as session:
        yield session
        await session.rollback()


# -----------------------------------------------------------------------------
# Sample Data Fixtures (Dict-based)
#
# Use these for testing Pydantic schema parsing/validation.
# For ORM model tests, prefer factory functions (make_repository, etc.)
# -----------------------------------------------------------------------------
@pytest.fixture
def sample_repository() -> dict[str, Any]:
    """Sample repository data for creating Repository models."""
    return {
        "owner": "prebid",
        "name": "prebid-server",
        "full_name": "prebid/prebid-server",
    }


@pytest.fixture
def sample_pr_open() -> dict:
    """Sample open PR data with all required fields."""
    return {
        # Immutable fields (PRCreate)
        "number": 1234,
        "link": "https://github.com/prebid/prebid-server/pull/1234",
        "open_date": JAN_15,
        "submitter": "testuser",
        # Synced fields (PRSync)
        "title": "Add new bidder adapter",
        "description": "This PR adds support for a new bidder.",
        "last_update_date": JAN_16,
        "state": PRState.OPEN,
        "files_changed": 5,
        "lines_added": 250,
        "lines_deleted": 10,
        "commits_count": 3,
        "github_labels": ["enhancement", "needs-review"],
        "filenames": ["adapters/newbidder.go", "adapters/newbidder_test.go"],
        "reviewers": ["reviewer1", "reviewer2"],
        "assignees": ["testuser"],
        "commits_breakdown": [
            {"date": JAN_15_ISO, "author": "testuser"},
            {"date": JAN_16_ISO, "author": "testuser"},
        ],
        "participants": {
            "reviewer1": ["comment", "changes_requested"],
            "reviewer2": ["approval"],
        },
        "classify_tags": None,
    }


@pytest.fixture
def sample_pr_merged() -> dict:
    """Sample merged PR data with merge fields populated."""
    return {
        # Immutable fields (PRCreate)
        "number": 1235,
        "link": "https://github.com/prebid/prebid-server/pull/1235",
        "open_date": JAN_10,
        "submitter": "testuser",
        # Synced fields (PRSync)
        "title": "Fix timeout handling",
        "description": "Fixes auction timeout issues.",
        "last_update_date": JAN_12,
        "state": PRState.MERGED,
        "files_changed": 3,
        "lines_added": 45,
        "lines_deleted": 12,
        "commits_count": 2,
        "github_labels": ["bug"],
        "filenames": ["exchange/auction.go"],
        "reviewers": [],
        "assignees": [],
        "commits_breakdown": [
            {"date": JAN_10_ISO, "author": "testuser"},
        ],
        "participants": {"maintainer": ["approval"]},
        "classify_tags": "bugfix,performance",
        # Merge fields (PRMerge)
        "close_date": JAN_12,
        "merged_by": "maintainer",
        "ai_summary": "Fixed timeout handling in auction endpoint.",
    }


@pytest.fixture
def sample_user_tag() -> dict:
    """Sample user tag data."""
    return {
        "name": "needs-review",
        "description": "PRs that need code review",
        "color": "#ff9900",
    }


# -----------------------------------------------------------------------------
# Utility Fixtures
# -----------------------------------------------------------------------------
@pytest.fixture
def utc_now() -> datetime:
    """Current UTC datetime for tests."""
    return datetime.now(UTC)
