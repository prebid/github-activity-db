"""End-to-end integration tests for PR ingestion pipeline.

Tests the full flow: GitHub API data → Transform → Store → Read back
"""

from datetime import UTC, datetime, timedelta
from typing import Any, cast
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
    PRRead,
)
from tests.fixtures import MERGED_PR_METADATA, OPEN_PR_METADATA, REAL_MERGED_PR, REAL_OPEN_PR


@pytest.fixture
def parse_fixture():
    """Helper to parse PR fixtures into schema objects."""

    def _parse(fixture):
        pr = GitHubPullRequest.model_validate(fixture["pr"])
        files = [GitHubFile.model_validate(f) for f in fixture["files"]]
        commits = [GitHubCommit.model_validate(c) for c in fixture["commits"]]
        reviews = [GitHubReview.model_validate(r) for r in fixture["reviews"]]
        return pr, files, commits, reviews

    return _parse


@pytest.fixture
def mock_github_client():
    """Create a mock GitHub client."""
    client = MagicMock()
    client.get_full_pull_request = AsyncMock()
    return client


@pytest.fixture
async def ingestion_service(db_session, mock_github_client):
    """Create ingestion service with test dependencies."""
    repo_repository = RepositoryRepository(db_session)
    pr_repository = PullRequestRepository(db_session)
    return PRIngestionService(mock_github_client, repo_repository, pr_repository)


class TestOpenPRIngestionE2E:
    """End-to-end tests for open PR ingestion."""

    async def test_ingest_open_pr_creates_correct_data(
        self, db_session, mock_github_client, parse_fixture, ingestion_service
    ):
        """Open PR is correctly stored with all fields."""
        gh_pr, files, commits, reviews = parse_fixture(REAL_OPEN_PR)
        mock_github_client.get_full_pull_request.return_value = (gh_pr, files, commits, reviews)

        result = await ingestion_service.ingest_pr("prebid", "prebid-server", 4663)

        # Verify result
        assert result.success is True
        assert result.created is True
        pr = result.pr

        # Verify immutable fields
        assert pr.number == 4663
        assert pr.submitter == "dev-adverxo"
        assert "github.com" in pr.link

        # Verify synced fields
        assert pr.title == "Adverxo Bid Adapter: New alias alchemyx"
        assert pr.state == PRState.OPEN
        assert pr.files_changed == 1
        assert pr.lines_added == 9
        assert pr.lines_deleted == 0
        assert pr.commits_count == 1

        # Verify lists
        assert len(pr.filenames) == OPEN_PR_METADATA["expected_file_count"]
        assert len(pr.commits_breakdown) == OPEN_PR_METADATA["expected_commit_count"]

        # Verify merge fields are None
        assert pr.close_date is None
        assert pr.merged_by is None

    async def test_open_pr_can_be_read_back(
        self, db_session, mock_github_client, parse_fixture, ingestion_service
    ):
        """Ingested open PR can be read back via repository."""
        gh_pr, files, commits, reviews = parse_fixture(REAL_OPEN_PR)
        mock_github_client.get_full_pull_request.return_value = (gh_pr, files, commits, reviews)

        await ingestion_service.ingest_pr("prebid", "prebid-server", 4663)

        # Read back via repository
        repo_repository = RepositoryRepository(db_session)
        pr_repository = PullRequestRepository(db_session)

        repo = await repo_repository.get_by_owner_and_name("prebid", "prebid-server")
        assert repo is not None
        pr = await pr_repository.get_by_number(repo.id, 4663)

        assert pr is not None
        assert pr.number == 4663
        assert pr.state == PRState.OPEN


class TestMergedPRIngestionE2E:
    """End-to-end tests for merged PR ingestion."""

    async def test_ingest_merged_pr_creates_correct_data(
        self, db_session, mock_github_client, parse_fixture, ingestion_service
    ):
        """Merged PR is correctly stored with merge fields."""
        gh_pr, files, commits, reviews = parse_fixture(REAL_MERGED_PR)
        mock_github_client.get_full_pull_request.return_value = (gh_pr, files, commits, reviews)

        result = await ingestion_service.ingest_pr("prebid", "prebid-server", 4646)

        # Verify result
        assert result.success is True
        assert result.created is True
        pr = result.pr

        # Verify state is MERGED
        assert pr.state == PRState.MERGED
        assert pr.is_merged is True
        assert pr.is_open is False

        # Verify merge fields
        assert pr.close_date is not None
        assert pr.merged_by == MERGED_PR_METADATA["expected_merged_by"]

        # Verify reviewers are captured
        assert len(pr.participants) == MERGED_PR_METADATA["expected_review_count"]

    async def test_merged_pr_has_correct_stats(
        self, db_session, mock_github_client, parse_fixture, ingestion_service
    ):
        """Merged PR has correct file/commit/review counts."""
        gh_pr, files, commits, reviews = parse_fixture(REAL_MERGED_PR)
        mock_github_client.get_full_pull_request.return_value = (gh_pr, files, commits, reviews)

        result = await ingestion_service.ingest_pr("prebid", "prebid-server", 4646)
        pr = result.pr

        assert len(pr.filenames) == MERGED_PR_METADATA["expected_file_count"]
        assert len(pr.commits_breakdown) == MERGED_PR_METADATA["expected_commit_count"]
        assert len(pr.participants) == MERGED_PR_METADATA["expected_review_count"]


class TestIdempotencyE2E:
    """Idempotency tests for PR ingestion."""

    async def test_ingest_same_pr_twice_is_idempotent(
        self, db_session, mock_github_client, parse_fixture, ingestion_service
    ):
        """Ingesting same PR twice doesn't create duplicates."""
        gh_pr, files, commits, reviews = parse_fixture(REAL_OPEN_PR)
        mock_github_client.get_full_pull_request.return_value = (gh_pr, files, commits, reviews)

        # First ingestion
        result1 = await ingestion_service.ingest_pr("prebid", "prebid-server", 4663)
        assert result1.created is True

        # Second ingestion (same data)
        result2 = await ingestion_service.ingest_pr("prebid", "prebid-server", 4663)
        assert result2.created is False
        assert result2.skipped_unchanged is True

        # Verify only one PR in database
        pr_repository = PullRequestRepository(db_session)
        prs = await pr_repository.get_all()

        assert len(prs) == 1
        assert prs[0].id == result1.pr.id

    async def test_updated_pr_reflects_changes(
        self, db_session, mock_github_client, parse_fixture, ingestion_service
    ):
        """Updated PR data is reflected on re-ingest."""
        gh_pr, files, commits, reviews = parse_fixture(REAL_OPEN_PR)
        mock_github_client.get_full_pull_request.return_value = (gh_pr, files, commits, reviews)

        # First ingestion
        result1 = await ingestion_service.ingest_pr("prebid", "prebid-server", 4663)

        # Modify the fixture to simulate an update
        gh_pr_updated = GitHubPullRequest.model_validate(
            {
                **cast(dict[str, Any], REAL_OPEN_PR["pr"]),
                "title": "Updated Title",
                "updated_at": "2030-01-01T00:00:00Z",
            }
        )
        mock_github_client.get_full_pull_request.return_value = (
            gh_pr_updated,
            files,
            commits,
            reviews,
        )

        # Second ingestion with updated data
        result2 = await ingestion_service.ingest_pr("prebid", "prebid-server", 4663)

        assert result2.created is False
        assert result2.updated is True
        assert result2.pr.title == "Updated Title"
        assert result2.pr.id == result1.pr.id


class TestGracePeriodE2E:
    """Grace period handling tests."""

    async def test_recently_merged_pr_can_be_updated(
        self, db_session, mock_github_client, parse_fixture
    ):
        """Merged PR within grace period can still be updated."""
        _, files, commits, reviews = parse_fixture(REAL_MERGED_PR)

        # Set merge date to now (within grace period)
        now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        gh_pr_recent = GitHubPullRequest.model_validate(
            {
                **cast(dict[str, Any], REAL_MERGED_PR["pr"]),
                "merged_at": now_iso,
                "closed_at": now_iso,
                "updated_at": now_iso,
            }
        )
        mock_github_client.get_full_pull_request.return_value = (
            gh_pr_recent,
            files,
            commits,
            reviews,
        )

        repo_repository = RepositoryRepository(db_session)
        pr_repository = PullRequestRepository(db_session, grace_period=timedelta(days=14))
        service = PRIngestionService(mock_github_client, repo_repository, pr_repository)

        # First ingestion
        result1 = await service.ingest_pr("prebid", "prebid-server", 4646)
        assert result1.created is True

        # Modify and re-ingest (should update because within grace period)
        updated_at_iso = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        updated_at_iso = updated_at_iso.replace("+00:00", "Z")
        gh_pr_updated = GitHubPullRequest.model_validate(
            {
                **cast(dict[str, Any], REAL_MERGED_PR["pr"]),
                "title": "Updated Merged PR",
                "merged_at": now_iso,
                "closed_at": now_iso,
                "updated_at": updated_at_iso,
            }
        )
        mock_github_client.get_full_pull_request.return_value = (
            gh_pr_updated,
            files,
            commits,
            reviews,
        )

        result2 = await service.ingest_pr("prebid", "prebid-server", 4646)
        assert result2.updated is True
        assert result2.pr is not None
        assert result2.pr.title == "Updated Merged PR"


class TestPRReadSchema:
    """Tests for converting stored PRs to PRRead schema."""

    async def test_pr_can_be_serialized_to_pread(
        self, db_session, mock_github_client, parse_fixture, ingestion_service
    ):
        """Stored PR can be converted to PRRead schema."""
        gh_pr, files, commits, reviews = parse_fixture(REAL_OPEN_PR)
        mock_github_client.get_full_pull_request.return_value = (gh_pr, files, commits, reviews)

        result = await ingestion_service.ingest_pr("prebid", "prebid-server", 4663)

        # Convert to PRRead schema
        pr_read = PRRead.from_orm(result.pr)

        assert pr_read.number == 4663
        assert pr_read.title == "Adverxo Bid Adapter: New alias alchemyx"
        assert pr_read.state == PRState.OPEN
        assert pr_read.is_open is True
        assert pr_read.is_merged is False

    async def test_merged_pr_serializes_merge_fields(
        self, db_session, mock_github_client, parse_fixture, ingestion_service
    ):
        """Merged PR includes merge fields in PRRead schema."""
        gh_pr, files, commits, reviews = parse_fixture(REAL_MERGED_PR)
        mock_github_client.get_full_pull_request.return_value = (gh_pr, files, commits, reviews)

        result = await ingestion_service.ingest_pr("prebid", "prebid-server", 4646)

        # Refresh to ensure all fields are loaded
        await db_session.refresh(result.pr)

        pr_read = PRRead.from_orm(result.pr)

        assert pr_read.state == PRState.MERGED
        assert pr_read.merged_by == "bsardo"
        assert pr_read.close_date is not None
        assert pr_read.is_merged is True


class TestRepositoryCreation:
    """Tests for automatic repository creation."""

    async def test_repository_created_for_new_repo(
        self, db_session, mock_github_client, parse_fixture, ingestion_service
    ):
        """Repository is created when ingesting first PR."""
        gh_pr, files, commits, reviews = parse_fixture(REAL_OPEN_PR)
        mock_github_client.get_full_pull_request.return_value = (gh_pr, files, commits, reviews)

        repo_repository = RepositoryRepository(db_session)

        # Verify no repository exists
        repo = await repo_repository.get_by_owner_and_name("prebid", "prebid-server")
        assert repo is None

        await ingestion_service.ingest_pr("prebid", "prebid-server", 4663)

        # Verify repository was created
        repo = await repo_repository.get_by_owner_and_name("prebid", "prebid-server")
        assert repo is not None
        assert repo.owner == "prebid"
        assert repo.name == "prebid-server"
        assert repo.full_name == "prebid/prebid-server"

    async def test_existing_repository_reused(
        self, db_session, mock_github_client, parse_fixture, ingestion_service
    ):
        """Existing repository is reused when ingesting more PRs."""
        gh_pr, files, commits, reviews = parse_fixture(REAL_OPEN_PR)
        mock_github_client.get_full_pull_request.return_value = (gh_pr, files, commits, reviews)

        # First PR
        result1 = await ingestion_service.ingest_pr("prebid", "prebid-server", 4663)

        # Second PR (different number, same repo)
        gh_pr2 = GitHubPullRequest.model_validate(
            {**cast(dict[str, Any], REAL_OPEN_PR["pr"]), "number": 4664}
        )
        mock_github_client.get_full_pull_request.return_value = (gh_pr2, files, commits, reviews)
        result2 = await ingestion_service.ingest_pr("prebid", "prebid-server", 4664)

        # Verify both PRs use same repository
        assert result1.pr.repository_id == result2.pr.repository_id

        # Verify only one repository exists
        repo_repository = RepositoryRepository(db_session)
        repos = await repo_repository.get_all()
        assert len(repos) == 1
