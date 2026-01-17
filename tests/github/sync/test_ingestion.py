"""Tests for PRIngestionService."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from github_activity_db.db.models import PRState
from github_activity_db.db.repositories import PullRequestRepository, RepositoryRepository
from github_activity_db.github.sync import PRIngestionService
from github_activity_db.schemas import (
    GitHubCommit,
    GitHubFile,
    GitHubPullRequest,
    GitHubReview,
)
from tests.factories import make_merged_pr, make_pull_request, make_repository
from tests.fixtures import REAL_MERGED_PR, REAL_OPEN_PR


@pytest.fixture
def mock_client():
    """Create a mock GitHub client."""
    client = MagicMock()
    client.get_full_pull_request = AsyncMock()
    return client


@pytest.fixture
def parse_real_pr():
    """Helper to parse real PR fixtures into schema objects."""
    def _parse(fixture):
        pr = GitHubPullRequest(**fixture["pr"])
        files = [GitHubFile(**f) for f in fixture["files"]]
        commits = [GitHubCommit(**c) for c in fixture["commits"]]
        reviews = [GitHubReview(**r) for r in fixture["reviews"]]
        return pr, files, commits, reviews
    return _parse


class TestPRIngestionServiceCreate:
    """Tests for creating new PRs via ingestion."""

    async def test_ingest_creates_new_pr(self, db_session, mock_client, parse_real_pr):
        """Ingesting a PR that doesn't exist creates it."""
        gh_pr, files, commits, reviews = parse_real_pr(REAL_OPEN_PR)
        mock_client.get_full_pull_request.return_value = (gh_pr, files, commits, reviews)

        repo_repository = RepositoryRepository(db_session)
        pr_repository = PullRequestRepository(db_session)
        service = PRIngestionService(mock_client, repo_repository, pr_repository)

        result = await service.ingest_pr("prebid", "prebid-server", 4663)

        assert result.success is True
        assert result.created is True
        assert result.updated is False
        assert result.pr is not None
        assert result.pr.number == 4663
        assert result.pr.state == PRState.OPEN

    async def test_ingest_creates_repository(self, db_session, mock_client, parse_real_pr):
        """Ingesting a PR creates the repository if it doesn't exist."""
        gh_pr, files, commits, reviews = parse_real_pr(REAL_OPEN_PR)
        mock_client.get_full_pull_request.return_value = (gh_pr, files, commits, reviews)

        repo_repository = RepositoryRepository(db_session)
        pr_repository = PullRequestRepository(db_session)
        service = PRIngestionService(mock_client, repo_repository, pr_repository)

        # Verify repository doesn't exist
        repo = await repo_repository.get_by_owner_and_name("prebid", "prebid-server")
        assert repo is None

        result = await service.ingest_pr("prebid", "prebid-server", 4663)

        # Verify repository was created
        repo = await repo_repository.get_by_owner_and_name("prebid", "prebid-server")
        assert repo is not None
        assert result.pr.repository_id == repo.id


class TestPRIngestionServiceUpdate:
    """Tests for updating existing PRs via ingestion."""

    async def test_ingest_updates_existing_open_pr(self, db_session, mock_client, parse_real_pr):
        """Ingesting an existing open PR updates it."""
        # Create existing PR
        repo = make_repository(db_session, owner="prebid", name="prebid-server")
        await db_session.flush()
        existing = make_pull_request(
            db_session, repo,
            number=4663,
            title="Old Title",
            state=PRState.OPEN,
            last_update_date=datetime(2020, 1, 1, tzinfo=UTC),  # Old date
        )
        await db_session.flush()

        gh_pr, files, commits, reviews = parse_real_pr(REAL_OPEN_PR)
        mock_client.get_full_pull_request.return_value = (gh_pr, files, commits, reviews)

        repo_repository = RepositoryRepository(db_session)
        pr_repository = PullRequestRepository(db_session)
        service = PRIngestionService(mock_client, repo_repository, pr_repository)

        result = await service.ingest_pr("prebid", "prebid-server", 4663)

        assert result.success is True
        assert result.created is False
        assert result.updated is True
        assert result.pr.id == existing.id
        assert result.pr.title == "Adverxo Bid Adapter: New alias alchemyx"


class TestPRIngestionServiceSkip:
    """Tests for skipping PR updates."""

    async def test_ingest_skips_unchanged_pr(self, db_session, mock_client, parse_real_pr):
        """Ingesting an unchanged PR skips the update."""
        gh_pr, files, commits, reviews = parse_real_pr(REAL_OPEN_PR)
        mock_client.get_full_pull_request.return_value = (gh_pr, files, commits, reviews)

        # Create existing PR with same last_update_date
        repo = make_repository(db_session, owner="prebid", name="prebid-server")
        await db_session.flush()
        make_pull_request(
            db_session, repo,
            number=4663,
            state=PRState.OPEN,
            last_update_date=gh_pr.updated_at,  # Same date
        )
        await db_session.flush()

        repo_repository = RepositoryRepository(db_session)
        pr_repository = PullRequestRepository(db_session)
        service = PRIngestionService(mock_client, repo_repository, pr_repository)

        result = await service.ingest_pr("prebid", "prebid-server", 4663)

        assert result.success is True
        assert result.skipped_unchanged is True
        assert result.created is False
        assert result.updated is False

    async def test_ingest_skips_frozen_pr(self, db_session, mock_client, parse_real_pr):
        """Ingesting a frozen (old merged) PR skips the update."""
        gh_pr, files, commits, reviews = parse_real_pr(REAL_MERGED_PR)
        mock_client.get_full_pull_request.return_value = (gh_pr, files, commits, reviews)

        # Create existing merged PR that's past grace period
        repo = make_repository(db_session, owner="prebid", name="prebid-server")
        await db_session.flush()
        old_merge_date = datetime.now(UTC) - timedelta(days=30)
        make_merged_pr(
            db_session, repo,
            number=4646,
            close_date=old_merge_date,
        )
        await db_session.flush()

        repo_repository = RepositoryRepository(db_session)
        pr_repository = PullRequestRepository(db_session, grace_period=timedelta(days=14))
        service = PRIngestionService(mock_client, repo_repository, pr_repository)

        result = await service.ingest_pr("prebid", "prebid-server", 4646)

        assert result.success is True
        assert result.skipped_frozen is True
        assert result.created is False
        assert result.updated is False


class TestPRIngestionServiceAbandoned:
    """Tests for abandoned PR handling.

    Abandoned PRs are closed without being merged. They are filtered out
    during ingestion because the list API doesn't include merge status.
    """

    async def test_ingest_skips_abandoned_pr(self, db_session, mock_client, parse_real_pr):
        """Ingesting an abandoned PR (closed but not merged) skips it.

        NOTE: The GitHub list API does NOT include merge status. We can only
        determine if a closed PR is actually merged vs abandoned by fetching
        the full PR data.
        """
        gh_pr, files, commits, reviews = parse_real_pr(REAL_OPEN_PR)
        # Modify the PR to be closed but not merged (abandoned)
        gh_pr = GitHubPullRequest(
            **{
                **gh_pr.model_dump(),
                "state": "closed",
                "merged": False,
                "closed_at": "2025-01-15T10:00:00Z",
            }
        )
        mock_client.get_full_pull_request.return_value = (gh_pr, files, commits, reviews)

        repo_repository = RepositoryRepository(db_session)
        pr_repository = PullRequestRepository(db_session)
        service = PRIngestionService(mock_client, repo_repository, pr_repository)

        result = await service.ingest_pr("prebid", "prebid-server", 4663)

        assert result.success is True
        assert result.skipped_abandoned is True
        assert result.created is False
        assert result.updated is False
        assert result.action == "skipped (abandoned)"

    async def test_ingest_abandoned_returns_existing_if_present(
        self, db_session, mock_client, parse_real_pr
    ):
        """If an abandoned PR was previously tracked, return the existing record."""
        # Create existing PR (maybe it was tracked when open)
        repo = make_repository(db_session, owner="prebid", name="prebid-server")
        await db_session.flush()
        existing = make_pull_request(
            db_session, repo,
            number=4663,
            state=PRState.OPEN,
        )
        await db_session.flush()

        gh_pr, files, commits, reviews = parse_real_pr(REAL_OPEN_PR)
        # Modify the PR to be closed but not merged (abandoned)
        gh_pr = GitHubPullRequest(
            **{
                **gh_pr.model_dump(),
                "state": "closed",
                "merged": False,
                "closed_at": "2025-01-15T10:00:00Z",
            }
        )
        mock_client.get_full_pull_request.return_value = (gh_pr, files, commits, reviews)

        repo_repository = RepositoryRepository(db_session)
        pr_repository = PullRequestRepository(db_session)
        service = PRIngestionService(mock_client, repo_repository, pr_repository)

        result = await service.ingest_pr("prebid", "prebid-server", 4663)

        assert result.success is True
        assert result.skipped_abandoned is True
        assert result.pr is not None
        assert result.pr.id == existing.id


class TestPRIngestionServiceMerge:
    """Tests for merge handling."""

    async def test_ingest_merged_pr_applies_merge_data(
        self, db_session, mock_client, parse_real_pr
    ):
        """Ingesting a merged PR applies merge data."""
        gh_pr, files, commits, reviews = parse_real_pr(REAL_MERGED_PR)
        mock_client.get_full_pull_request.return_value = (gh_pr, files, commits, reviews)

        repo_repository = RepositoryRepository(db_session)
        pr_repository = PullRequestRepository(db_session)
        service = PRIngestionService(mock_client, repo_repository, pr_repository)

        result = await service.ingest_pr("prebid", "prebid-server", 4646)

        assert result.success is True
        assert result.created is True
        assert result.pr.state == PRState.MERGED
        assert result.pr.merged_by == "bsardo"
        assert result.pr.close_date is not None

    async def test_ingest_detects_new_merge(self, db_session, mock_client, parse_real_pr):
        """Ingesting PR that was merged since last sync applies merge data."""
        # First, create as open PR
        repo = make_repository(db_session, owner="prebid", name="prebid-server")
        await db_session.flush()
        existing = make_pull_request(
            db_session, repo,
            number=4646,
            state=PRState.OPEN,
            last_update_date=datetime(2020, 1, 1, tzinfo=UTC),
        )
        await db_session.flush()

        # Now fetch merged PR data
        gh_pr, files, commits, reviews = parse_real_pr(REAL_MERGED_PR)
        mock_client.get_full_pull_request.return_value = (gh_pr, files, commits, reviews)

        repo_repository = RepositoryRepository(db_session)
        pr_repository = PullRequestRepository(db_session)
        service = PRIngestionService(mock_client, repo_repository, pr_repository)

        result = await service.ingest_pr("prebid", "prebid-server", 4646)

        assert result.success is True
        assert result.pr.id == existing.id
        assert result.pr.state == PRState.MERGED
        assert result.pr.merged_by == "bsardo"


class TestPRIngestionServiceDryRun:
    """Tests for dry-run mode."""

    async def test_dry_run_does_not_create(self, db_session, mock_client, parse_real_pr):
        """Dry run doesn't create PR in database."""
        gh_pr, files, commits, reviews = parse_real_pr(REAL_OPEN_PR)
        mock_client.get_full_pull_request.return_value = (gh_pr, files, commits, reviews)

        repo_repository = RepositoryRepository(db_session)
        pr_repository = PullRequestRepository(db_session)
        service = PRIngestionService(mock_client, repo_repository, pr_repository)

        result = await service.ingest_pr("prebid", "prebid-server", 4663, dry_run=True)

        assert result.success is True
        assert result.created is True

        # Verify PR was NOT created in database
        pr = await pr_repository.get_by_number(1, 4663)  # repo_id 1 doesn't exist
        assert pr is None


class TestPRIngestionServiceError:
    """Tests for error handling."""

    async def test_ingest_captures_github_error(self, db_session, mock_client):
        """GitHub API errors are captured in result."""
        mock_client.get_full_pull_request.side_effect = Exception("API Error")

        repo_repository = RepositoryRepository(db_session)
        pr_repository = PullRequestRepository(db_session)
        service = PRIngestionService(mock_client, repo_repository, pr_repository)

        result = await service.ingest_pr("prebid", "prebid-server", 9999)

        assert result.success is False
        assert result.error is not None
        assert "API Error" in str(result.error)
        assert result.pr is None


class TestPRIngestionResult:
    """Tests for PRIngestionResult helper methods."""

    async def test_result_to_dict(self, db_session, mock_client, parse_real_pr):
        """Result can be converted to dict."""
        gh_pr, files, commits, reviews = parse_real_pr(REAL_OPEN_PR)
        mock_client.get_full_pull_request.return_value = (gh_pr, files, commits, reviews)

        repo_repository = RepositoryRepository(db_session)
        pr_repository = PullRequestRepository(db_session)
        service = PRIngestionService(mock_client, repo_repository, pr_repository)

        result = await service.ingest_pr("prebid", "prebid-server", 4663)
        result_dict = result.to_dict()

        assert result_dict["success"] is True
        assert result_dict["action"] == "created"
        assert result_dict["pr_number"] == 4663
        assert result_dict["state"] == "open"

    async def test_result_action_property(self, db_session, mock_client, parse_real_pr):
        """Result action property returns correct string."""
        gh_pr, files, commits, reviews = parse_real_pr(REAL_OPEN_PR)
        mock_client.get_full_pull_request.return_value = (gh_pr, files, commits, reviews)

        repo_repository = RepositoryRepository(db_session)
        pr_repository = PullRequestRepository(db_session)
        service = PRIngestionService(mock_client, repo_repository, pr_repository)

        result = await service.ingest_pr("prebid", "prebid-server", 4663)

        assert result.action == "created"
