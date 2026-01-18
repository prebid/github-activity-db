"""Tests for FailureRetryService."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from github_activity_db.db.models import SyncFailureStatus
from github_activity_db.db.repositories import (
    RepositoryRepository,
    SyncFailureRepository,
)
from github_activity_db.github.sync import FailureRetryService
from github_activity_db.github.sync.results import PRIngestionResult
from tests.factories import make_pull_request, make_repository, make_sync_failure


@pytest.fixture
def mock_ingestion_service():
    """Create a mock PRIngestionService."""
    service = MagicMock()
    service.ingest_pr = AsyncMock()
    return service


class TestFailureRetryServiceBasic:
    """Basic retry functionality tests."""

    async def test_retry_no_pending_failures(self, db_session, mock_ingestion_service):
        """Retry with no pending failures returns empty result."""
        service = FailureRetryService(
            ingestion_service=mock_ingestion_service,
            failure_repository=SyncFailureRepository(db_session),
            repo_repository=RepositoryRepository(db_session),
        )

        result = await service.retry_failures()

        assert result.total_pending == 0
        assert result.succeeded == 0
        assert result.failed_again == 0
        mock_ingestion_service.ingest_pr.assert_not_called()

    async def test_retry_single_success(self, db_session, mock_ingestion_service):
        """Retry a single failure that succeeds."""
        repo = make_repository(db_session, owner="prebid", name="prebid-server")
        await db_session.flush()

        pr = make_pull_request(db_session, repo, number=123)
        await db_session.flush()

        failure = make_sync_failure(db_session, repo, pr_number=123)
        await db_session.flush()

        # Mock successful ingestion
        mock_ingestion_service.ingest_pr.return_value = PRIngestionResult.from_created(pr)

        service = FailureRetryService(
            ingestion_service=mock_ingestion_service,
            failure_repository=SyncFailureRepository(db_session),
            repo_repository=RepositoryRepository(db_session),
        )

        result = await service.retry_failures()

        assert result.total_pending == 1
        assert result.succeeded == 1
        assert result.failed_again == 0
        mock_ingestion_service.ingest_pr.assert_called_once_with(
            "prebid", "prebid-server", 123, dry_run=False
        )

        # Verify failure was marked resolved
        failure_repo = SyncFailureRepository(db_session)
        updated_failure = await failure_repo.get_by_id(failure.id)
        assert updated_failure is not None
        assert updated_failure.status == SyncFailureStatus.RESOLVED

    async def test_retry_single_failure(self, db_session, mock_ingestion_service):
        """Retry a single failure that fails again."""
        repo = make_repository(db_session, owner="prebid", name="prebid-server")
        await db_session.flush()

        failure = make_sync_failure(db_session, repo, pr_number=123, retry_count=0)
        await db_session.flush()

        # Mock failed ingestion
        mock_ingestion_service.ingest_pr.return_value = PRIngestionResult.from_error(
            ValueError("API Error")
        )

        service = FailureRetryService(
            ingestion_service=mock_ingestion_service,
            failure_repository=SyncFailureRepository(db_session),
            repo_repository=RepositoryRepository(db_session),
        )

        result = await service.retry_failures()

        assert result.total_pending == 1
        assert result.succeeded == 0
        assert result.failed_again == 1
        assert result.marked_permanent == 0

        # Verify failure was updated with incremented retry count
        failure_repo = SyncFailureRepository(db_session)
        updated_failure = await failure_repo.get_by_id(failure.id)
        assert updated_failure is not None
        assert updated_failure.status == SyncFailureStatus.PENDING
        assert updated_failure.retry_count == 1


class TestFailureRetryServiceMaxRetries:
    """Tests for max retry limit handling."""

    async def test_marks_permanent_after_max_retries(self, db_session, mock_ingestion_service):
        """Failure is marked permanent after max retries exceeded."""
        repo = make_repository(db_session, owner="prebid", name="prebid-server")
        await db_session.flush()

        # Create failure at max retry count (2, so next attempt is 3rd = max)
        failure = make_sync_failure(db_session, repo, pr_number=123, retry_count=2)
        await db_session.flush()

        # Mock failed ingestion
        mock_ingestion_service.ingest_pr.return_value = PRIngestionResult.from_error(
            ValueError("Still failing")
        )

        service = FailureRetryService(
            ingestion_service=mock_ingestion_service,
            failure_repository=SyncFailureRepository(db_session),
            repo_repository=RepositoryRepository(db_session),
        )

        result = await service.retry_failures()

        assert result.marked_permanent == 1
        assert result.failed_again == 0

        # Verify failure was marked permanent
        failure_repo = SyncFailureRepository(db_session)
        updated_failure = await failure_repo.get_by_id(failure.id)
        assert updated_failure is not None
        assert updated_failure.status == SyncFailureStatus.PERMANENT


class TestFailureRetryServiceFiltering:
    """Tests for filtering retry scope."""

    async def test_filters_by_repository_id(self, db_session, mock_ingestion_service):
        """Retry filters by repository ID when specified."""
        repo1 = make_repository(db_session, owner="prebid", name="repo1")
        repo2 = make_repository(db_session, owner="prebid", name="repo2")
        await db_session.flush()

        pr1 = make_pull_request(db_session, repo1, number=1)
        await db_session.flush()

        make_sync_failure(db_session, repo1, pr_number=1)
        make_sync_failure(db_session, repo2, pr_number=2)
        await db_session.flush()

        # Mock successful ingestion
        mock_ingestion_service.ingest_pr.return_value = PRIngestionResult.from_created(pr1)

        service = FailureRetryService(
            ingestion_service=mock_ingestion_service,
            failure_repository=SyncFailureRepository(db_session),
            repo_repository=RepositoryRepository(db_session),
        )

        result = await service.retry_failures(repository_id=repo1.id)

        assert result.total_pending == 1
        assert result.succeeded == 1
        mock_ingestion_service.ingest_pr.assert_called_once_with(
            "prebid", "repo1", 1, dry_run=False
        )

    async def test_respects_max_items(self, db_session, mock_ingestion_service):
        """Retry respects max_items limit."""
        repo = make_repository(db_session, owner="prebid", name="prebid-server")
        await db_session.flush()

        pr = make_pull_request(db_session, repo, number=1)
        await db_session.flush()

        for i in range(5):
            make_sync_failure(db_session, repo, pr_number=i)
        await db_session.flush()

        # Mock successful ingestion
        mock_ingestion_service.ingest_pr.return_value = PRIngestionResult.from_created(pr)

        service = FailureRetryService(
            ingestion_service=mock_ingestion_service,
            failure_repository=SyncFailureRepository(db_session),
            repo_repository=RepositoryRepository(db_session),
        )

        result = await service.retry_failures(max_items=2)

        assert result.total_pending == 2
        assert mock_ingestion_service.ingest_pr.call_count == 2


class TestFailureRetryServiceDryRun:
    """Tests for dry-run mode."""

    async def test_dry_run_does_not_modify_database(self, db_session, mock_ingestion_service):
        """Dry run doesn't modify failure records."""
        repo = make_repository(db_session, owner="prebid", name="prebid-server")
        await db_session.flush()

        pr = make_pull_request(db_session, repo, number=123)
        await db_session.flush()

        failure = make_sync_failure(db_session, repo, pr_number=123)
        await db_session.flush()

        # Mock successful ingestion
        mock_ingestion_service.ingest_pr.return_value = PRIngestionResult.from_created(pr)

        service = FailureRetryService(
            ingestion_service=mock_ingestion_service,
            failure_repository=SyncFailureRepository(db_session),
            repo_repository=RepositoryRepository(db_session),
        )

        result = await service.retry_failures(dry_run=True)

        assert result.skipped_dry_run == 1
        assert result.succeeded == 0  # Not counted as succeeded in dry-run

        # Verify failure was NOT marked resolved
        failure_repo = SyncFailureRepository(db_session)
        updated_failure = await failure_repo.get_by_id(failure.id)
        assert updated_failure is not None
        assert updated_failure.status == SyncFailureStatus.PENDING

        # Verify ingestion was called with dry_run=True
        mock_ingestion_service.ingest_pr.assert_called_once_with(
            "prebid", "prebid-server", 123, dry_run=True
        )


class TestFailureRetryServiceResult:
    """Tests for RetryResult data structure."""

    async def test_result_to_dict(self, db_session, mock_ingestion_service):
        """RetryResult.to_dict() returns expected structure."""
        repo = make_repository(db_session, owner="prebid", name="prebid-server")
        await db_session.flush()

        pr = make_pull_request(db_session, repo, number=123)
        await db_session.flush()

        make_sync_failure(db_session, repo, pr_number=123)
        await db_session.flush()

        # Mock successful ingestion
        mock_ingestion_service.ingest_pr.return_value = PRIngestionResult.from_created(pr)

        service = FailureRetryService(
            ingestion_service=mock_ingestion_service,
            failure_repository=SyncFailureRepository(db_session),
            repo_repository=RepositoryRepository(db_session),
        )

        result = await service.retry_failures()
        result_dict = result.to_dict()

        assert "total_pending" in result_dict
        assert "succeeded" in result_dict
        assert "failed_again" in result_dict
        assert "marked_permanent" in result_dict
        assert "duration_seconds" in result_dict
        assert "results" in result_dict
        assert isinstance(result_dict["results"], list)


class TestFailureRetryServiceStats:
    """Tests for get_failure_stats method."""

    async def test_get_failure_stats(self, db_session, mock_ingestion_service):
        """get_failure_stats returns statistics."""
        repo = make_repository(db_session, owner="prebid", name="prebid-server")
        await db_session.flush()

        make_sync_failure(db_session, repo, pr_number=1, status=SyncFailureStatus.PENDING)
        make_sync_failure(db_session, repo, pr_number=2, status=SyncFailureStatus.RESOLVED)
        await db_session.flush()

        service = FailureRetryService(
            ingestion_service=mock_ingestion_service,
            failure_repository=SyncFailureRepository(db_session),
            repo_repository=RepositoryRepository(db_session),
        )

        stats = await service.get_failure_stats()

        assert stats["pending"] == 1
        assert stats["resolved"] == 1
        assert stats["total"] == 2
