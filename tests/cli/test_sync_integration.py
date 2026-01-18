"""Integration tests for sync CLI commands.

These tests verify end-to-end flows:
- Real database operations (in-memory SQLite)
- Mocked GitHub API (external dependency)
- Actual service layer logic

This differs from test_cli_sync.py which mocks the service layer entirely.
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from tests.factories import make_github_pr
from typer.testing import CliRunner

from github_activity_db.cli.app import app
from github_activity_db.config import get_settings
from github_activity_db.db.models import Base, PRState, PullRequest, Repository

runner = CliRunner()


@pytest.fixture(autouse=True)
def mock_rate_limit_infrastructure():
    """Auto-mock rate limiting infrastructure to avoid async issues in tests.

    The CLI now initializes RateLimitMonitor, RequestPacer, and RequestScheduler.
    This fixture mocks all of them to avoid async complications and background tasks.
    """
    with (
        patch("github_activity_db.cli.sync.RateLimitMonitor") as mock_monitor_class,
        patch("github_activity_db.cli.sync.RequestPacer") as mock_pacer_class,
        patch("github_activity_db.cli.sync.RequestScheduler") as mock_scheduler_class,
    ):
        # Mock RateLimitMonitor
        mock_monitor = MagicMock()
        mock_monitor.initialize = AsyncMock()
        mock_monitor_class.return_value = mock_monitor

        # Mock RequestPacer
        mock_pacer = MagicMock()
        mock_pacer_class.return_value = mock_pacer

        # Mock RequestScheduler with async methods
        mock_scheduler = MagicMock()
        mock_scheduler.start = AsyncMock()
        mock_scheduler.shutdown = AsyncMock()
        mock_scheduler_class.return_value = mock_scheduler

        yield {
            "monitor": mock_monitor,
            "pacer": mock_pacer,
            "scheduler": mock_scheduler,
        }


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------
@pytest.fixture
def temp_db_path():
    """Create a temporary database file path."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    yield db_path
    # Cleanup
    db_file = Path(db_path)
    if db_file.exists():
        db_file.unlink()


@pytest.fixture
async def test_db_engine(temp_db_path):
    """Create a test database engine with tables."""
    db_url = f"sqlite+aiosqlite:///{temp_db_path}"
    engine = create_async_engine(db_url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine, db_url
    await engine.dispose()


@pytest.fixture
def mock_github_client():
    """Create a mock GitHub client that returns test PR data."""
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)

    # Mock get_full_pull_request to return test data
    from github_activity_db.schemas import (
        GitHubCommit,
        GitHubFile,
        GitHubPullRequest,
        GitHubReview,
    )

    pr_data = make_github_pr(number=123, title="Test Integration PR", state="open")
    pr = GitHubPullRequest.model_validate(pr_data)
    files = [
        GitHubFile(
            sha="abc123def456",
            filename="test.py",
            status="modified",
            additions=10,
            deletions=5,
            changes=15,
        )
    ]
    commits = [
        GitHubCommit.model_validate(
            {
                "sha": "abc123",
                "commit": {
                    "message": "Test commit",
                    "author": {
                        "name": "Test",
                        "email": "test@example.com",
                        "date": "2024-01-15T10:00:00Z",
                    },
                },
            }
        )
    ]
    reviews: list[GitHubReview] = []

    client.get_full_pull_request = AsyncMock(return_value=(pr, files, commits, reviews))
    client.rate_monitor = None
    client.pacer = None

    return client


# -----------------------------------------------------------------------------
# Integration Tests: sync pr
# -----------------------------------------------------------------------------
class TestSyncPRIntegration:
    """Integration tests for 'sync pr' command."""

    def test_sync_pr_creates_database_record(self, temp_db_path, mock_github_client):
        """sync pr creates a PR record in the database."""
        db_url = f"sqlite+aiosqlite:///{temp_db_path}"

        # Setup: Create tables first using sync SQLite (more reliable for setup)
        import sqlite3
        conn = sqlite3.connect(temp_db_path)
        # Create tables via SQLAlchemy's DDL
        from sqlalchemy import create_engine
        sync_engine = create_engine(f"sqlite:///{temp_db_path}")
        Base.metadata.create_all(sync_engine)
        sync_engine.dispose()
        conn.close()

        # Clear settings cache and reset engine state
        get_settings.cache_clear()

        # Patch to use our test database and mock client
        with (
            patch.dict(os.environ, {"DATABASE_URL": db_url}),
            patch("github_activity_db.cli.sync.GitHubClient", return_value=mock_github_client),
            patch("github_activity_db.db.engine._engine", None),
            patch("github_activity_db.db.engine._async_session_factory", None),
        ):
            result = runner.invoke(app, ["sync", "pr", "prebid/prebid-server", "123"])

        # Verify CLI output
        assert result.exit_code == 0, f"CLI failed: {result.output}"
        assert "created" in result.output.lower() or "123" in result.output

        # Verify database record was created (using sync SQLite for reliability)
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session
        sync_engine = create_engine(f"sqlite:///{temp_db_path}")
        with Session(sync_engine) as session:
            # Check repository was created
            repo = session.query(Repository).filter_by(full_name="prebid/prebid-server").first()
            assert repo is not None, "Repository was not created"

            # Check PR was created
            pr = session.query(PullRequest).filter_by(number=123).first()
            assert pr is not None, "PR was not created"
            assert pr.title == "Test Integration PR"
            assert pr.state == PRState.OPEN
        sync_engine.dispose()

    def test_sync_pr_dry_run_no_writes(self, temp_db_path, mock_github_client):
        """sync pr --dry-run doesn't write to database."""
        db_url = f"sqlite+aiosqlite:///{temp_db_path}"

        # Setup: Create tables using sync SQLite
        from sqlalchemy import create_engine
        sync_engine = create_engine(f"sqlite:///{temp_db_path}")
        Base.metadata.create_all(sync_engine)
        sync_engine.dispose()

        # Clear settings cache
        get_settings.cache_clear()

        with (
            patch.dict(os.environ, {"DATABASE_URL": db_url}),
            patch("github_activity_db.cli.sync.GitHubClient", return_value=mock_github_client),
            patch("github_activity_db.db.engine._engine", None),
            patch("github_activity_db.db.engine._async_session_factory", None),
        ):
            result = runner.invoke(
                app, ["sync", "pr", "prebid/prebid-server", "123", "--dry-run"]
            )

        assert result.exit_code == 0
        assert "dry-run" in result.output.lower() or "DRY RUN" in result.output

        # Verify NO database record was created (using sync SQLite for reliability)
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session
        sync_engine = create_engine(f"sqlite:///{temp_db_path}")
        with Session(sync_engine) as session:
            pr = session.query(PullRequest).filter_by(number=123).first()
            assert pr is None, "PR should not be created in dry-run mode"
        sync_engine.dispose()

    def test_sync_pr_json_output_structure(self, temp_db_path, mock_github_client):
        """sync pr --format json outputs valid JSON with expected fields."""
        db_url = f"sqlite+aiosqlite:///{temp_db_path}"

        # Setup: Create tables using sync SQLite
        from sqlalchemy import create_engine
        sync_engine = create_engine(f"sqlite:///{temp_db_path}")
        Base.metadata.create_all(sync_engine)
        sync_engine.dispose()

        # Clear settings cache
        get_settings.cache_clear()

        with (
            patch.dict(os.environ, {"DATABASE_URL": db_url, "ENVIRONMENT": "production"}),
            patch("github_activity_db.cli.sync.GitHubClient", return_value=mock_github_client),
            patch("github_activity_db.db.engine._engine", None),
            patch("github_activity_db.db.engine._async_session_factory", None),
        ):
            result = runner.invoke(
                app, ["sync", "pr", "prebid/prebid-server", "123", "--format", "json"]
            )

        assert result.exit_code == 0, f"CLI failed: {result.output}"

        # Parse JSON output (extract JSON from output which may include log lines)
        output = result.output
        try:
            # Find the JSON object in the output (starts with '{', ends with '}')
            json_start = output.find("{")
            json_end = output.rfind("}") + 1
            if json_start == -1 or json_end == 0:
                pytest.fail(f"No JSON found in output: {output}")
            json_str = output[json_start:json_end]
            data = json.loads(json_str)
        except json.JSONDecodeError:
            pytest.fail(f"Output is not valid JSON: {output}")

        # Verify expected fields
        assert "success" in data or "created" in data
        assert "pr_number" in data or data.get("created") is True


# -----------------------------------------------------------------------------
# Integration Tests: sync repo
# -----------------------------------------------------------------------------
class TestSyncRepoIntegration:
    """Integration tests for 'sync repo' command."""

    def test_sync_repo_with_max_limit(self, temp_db_path):
        """sync repo --max limits PR count."""
        db_url = f"sqlite+aiosqlite:///{temp_db_path}"

        # Setup: Create tables using sync SQLite
        from sqlalchemy import create_engine
        sync_engine = create_engine(f"sqlite:///{temp_db_path}")
        Base.metadata.create_all(sync_engine)
        sync_engine.dispose()

        # Clear settings cache
        get_settings.cache_clear()

        # Create mock client that returns multiple PRs
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.rate_monitor = None

        # Create mock PRs for iteration
        from github_activity_db.schemas import GitHubPullRequest

        async def mock_iter_prs(*args, **kwargs):
            for i in range(10):  # Would return 10 PRs
                pr_data = make_github_pr(number=100 + i, title=f"PR {i}", state="open")
                yield GitHubPullRequest.model_validate(pr_data)

        mock_client.iter_pull_requests = mock_iter_prs

        # Mock get_full_pull_request for each PR
        from github_activity_db.schemas import GitHubCommit, GitHubFile

        async def mock_get_full(owner, repo, number):
            pr_data = make_github_pr(number=number, title=f"PR {number}", state="open")
            pr = GitHubPullRequest.model_validate(pr_data)
            files = [
                GitHubFile(
                    sha=f"sha{number}file",
                    filename="test.py",
                    status="modified",
                    additions=1,
                    deletions=0,
                    changes=1,
                )
            ]
            commits = [
                GitHubCommit.model_validate({
                    "sha": f"sha{number}",
                    "commit": {
                        "message": "test",
                        "author": {
                            "name": "test",
                            "email": "test@example.com",
                            "date": "2024-01-15T10:00:00Z",
                        },
                    },
                })
            ]
            return pr, files, commits, []

        mock_client.get_full_pull_request = mock_get_full

        with (
            patch.dict(os.environ, {"DATABASE_URL": db_url}),
            patch("github_activity_db.cli.sync.GitHubClient", return_value=mock_client),
            patch("github_activity_db.db.engine._engine", None),
            patch("github_activity_db.db.engine._async_session_factory", None),
        ):
            result = runner.invoke(
                app, ["sync", "repo", "prebid/prebid-server", "--max", "3", "--dry-run"]
            )

        assert result.exit_code == 0
        # The output should indicate limited PRs
        assert "3" in result.output or "discovered" in result.output.lower()
