"""Tests for MultiRepoOrchestrator.

Tests cover:
- Repository initialization
- Multi-repo sync orchestration
- Result aggregation across repositories
- Error handling and continuation on failure
- Repo filtering with --repos option
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from github_activity_db.github.sync.bulk_ingestion import (
    BulkIngestionConfig,
    BulkIngestionResult,
)
from github_activity_db.github.sync.multi_repo_orchestrator import (
    MultiRepoOrchestrator,
    MultiRepoSyncResult,
    RepoSyncResult,
)


# -----------------------------------------------------------------------------
# Test Fixtures
# -----------------------------------------------------------------------------
@pytest.fixture
def mock_github_client():
    """Mock GitHub client."""
    client = MagicMock()
    return client


@pytest.fixture
def mock_repo_repository():
    """Mock repository repository."""
    repo = MagicMock()
    repo.get_or_create = AsyncMock(return_value=(MagicMock(), True))
    return repo


@pytest.fixture
def mock_pr_repository():
    """Mock PR repository."""
    repo = MagicMock()
    return repo


@pytest.fixture
def mock_scheduler():
    """Mock request scheduler."""
    scheduler = MagicMock()
    scheduler.start = AsyncMock()
    scheduler.shutdown = AsyncMock()
    return scheduler


@pytest.fixture
def orchestrator(mock_github_client, mock_repo_repository, mock_pr_repository, mock_scheduler):
    """Create orchestrator with mocked dependencies."""
    return MultiRepoOrchestrator(
        client=mock_github_client,
        repo_repository=mock_repo_repository,
        pr_repository=mock_pr_repository,
        scheduler=mock_scheduler,
    )


# -----------------------------------------------------------------------------
# RepoSyncResult Tests
# -----------------------------------------------------------------------------
class TestRepoSyncResult:
    """Tests for RepoSyncResult dataclass."""

    def test_duration_seconds(self):
        """Duration is calculated from start and end times."""
        started = datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)
        completed = datetime(2024, 1, 15, 10, 5, 30, tzinfo=UTC)

        result = RepoSyncResult(
            repository="owner/repo",
            result=BulkIngestionResult(),
            started_at=started,
            completed_at=completed,
        )

        assert result.duration_seconds == 330.0  # 5 minutes 30 seconds

    def test_to_dict(self):
        """to_dict includes all fields."""
        started = datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)
        completed = datetime(2024, 1, 15, 10, 0, 10, tzinfo=UTC)

        bulk_result = BulkIngestionResult(
            total_discovered=10,
            created=5,
            updated=3,
            skipped_frozen=1,
            skipped_unchanged=1,
        )

        result = RepoSyncResult(
            repository="owner/repo",
            result=bulk_result,
            started_at=started,
            completed_at=completed,
        )

        data = result.to_dict()
        assert data["repository"] == "owner/repo"
        assert "started_at" in data
        assert "completed_at" in data
        # duration_seconds comes from the dataclass property (10 seconds)
        assert data["duration_seconds"] == 10.0
        # These come from the bulk_result.to_dict() spread
        assert data["created"] == 5
        assert data["updated"] == 3


# -----------------------------------------------------------------------------
# MultiRepoSyncResult Tests
# -----------------------------------------------------------------------------
class TestMultiRepoSyncResult:
    """Tests for MultiRepoSyncResult dataclass."""

    def test_repos_succeeded_all_ok(self):
        """repos_succeeded counts repos with no failures."""
        result = MultiRepoSyncResult(
            repo_results=[
                RepoSyncResult(
                    repository="a/a",
                    result=BulkIngestionResult(created=5),
                    started_at=datetime.now(UTC),
                    completed_at=datetime.now(UTC),
                ),
                RepoSyncResult(
                    repository="b/b",
                    result=BulkIngestionResult(created=3),
                    started_at=datetime.now(UTC),
                    completed_at=datetime.now(UTC),
                ),
            ]
        )
        assert result.repos_succeeded == 2
        assert result.repos_with_failures == 0

    def test_repos_with_failures(self):
        """repos_with_failures counts repos with at least one failure."""
        result = MultiRepoSyncResult(
            repo_results=[
                RepoSyncResult(
                    repository="a/a",
                    result=BulkIngestionResult(created=5, failed=1),
                    started_at=datetime.now(UTC),
                    completed_at=datetime.now(UTC),
                ),
                RepoSyncResult(
                    repository="b/b",
                    result=BulkIngestionResult(created=3, failed=0),
                    started_at=datetime.now(UTC),
                    completed_at=datetime.now(UTC),
                ),
            ]
        )
        assert result.repos_succeeded == 1
        assert result.repos_with_failures == 1

    def test_to_dict_structure(self):
        """to_dict returns proper structure with summary and repositories."""
        result = MultiRepoSyncResult(
            total_discovered=20,
            total_created=10,
            total_updated=5,
            total_skipped=3,
            total_failed=2,
            duration_seconds=60.5,
        )

        data = result.to_dict()
        assert "summary" in data
        assert "repositories" in data

        summary = data["summary"]
        assert summary["total_repos"] == 0
        assert summary["total_discovered"] == 20
        assert summary["total_created"] == 10
        assert summary["duration_seconds"] == 60.5


# -----------------------------------------------------------------------------
# MultiRepoOrchestrator.initialize_repositories Tests
# -----------------------------------------------------------------------------
class TestInitializeRepositories:
    """Tests for repository initialization."""

    async def test_initialize_creates_missing_repos(
        self,
        mock_github_client,
        mock_repo_repository,
        mock_pr_repository,
        mock_scheduler,
    ):
        """initialize_repositories creates repo records that don't exist."""
        mock_repo_repository.get_or_create = AsyncMock(
            side_effect=[
                (MagicMock(), True),  # Created
                (MagicMock(), False),  # Already existed
            ]
        )

        mock_settings = MagicMock()
        mock_settings.tracked_repos = ["owner/repo1", "owner/repo2"]

        with patch(
            "github_activity_db.github.sync.multi_repo_orchestrator.get_settings",
            return_value=mock_settings,
        ):
            orchestrator = MultiRepoOrchestrator(
                client=mock_github_client,
                repo_repository=mock_repo_repository,
                pr_repository=mock_pr_repository,
                scheduler=mock_scheduler,
            )
            initialized = await orchestrator.initialize_repositories()

        assert len(initialized) == 2
        assert mock_repo_repository.get_or_create.call_count == 2

    async def test_initialize_uses_provided_repos(
        self,
        mock_github_client,
        mock_repo_repository,
        mock_pr_repository,
        mock_scheduler,
    ):
        """initialize_repositories uses provided repos list instead of settings."""
        mock_settings = MagicMock()
        mock_settings.tracked_repos = ["default/repo"]

        with patch(
            "github_activity_db.github.sync.multi_repo_orchestrator.get_settings",
            return_value=mock_settings,
        ):
            orchestrator = MultiRepoOrchestrator(
                client=mock_github_client,
                repo_repository=mock_repo_repository,
                pr_repository=mock_pr_repository,
                scheduler=mock_scheduler,
            )
            await orchestrator.initialize_repositories(repos=["custom/repo"])

        mock_repo_repository.get_or_create.assert_called_once()
        call_kwargs = mock_repo_repository.get_or_create.call_args[1]
        assert call_kwargs["owner"] == "custom"
        assert call_kwargs["name"] == "repo"


# -----------------------------------------------------------------------------
# MultiRepoOrchestrator.sync_all Tests
# -----------------------------------------------------------------------------
class TestSyncAll:
    """Tests for multi-repository sync."""

    async def test_sync_all_delegates_to_bulk_service(
        self,
        mock_github_client,
        mock_repo_repository,
        mock_pr_repository,
        mock_scheduler,
    ):
        """sync_all uses BulkPRIngestionService for each repo."""
        mock_bulk_result = BulkIngestionResult(
            total_discovered=10,
            created=5,
            updated=3,
        )

        mock_settings = MagicMock()
        mock_settings.tracked_repos = ["owner/repo1", "owner/repo2"]

        with (
            patch(
                "github_activity_db.github.sync.multi_repo_orchestrator.get_settings",
                return_value=mock_settings,
            ),
            patch(
                "github_activity_db.github.sync.multi_repo_orchestrator.BulkPRIngestionService"
            ) as mock_bulk_class,
        ):
            mock_bulk_service = MagicMock()
            mock_bulk_service.ingest_repository = AsyncMock(return_value=mock_bulk_result)
            mock_bulk_class.return_value = mock_bulk_service

            orchestrator = MultiRepoOrchestrator(
                client=mock_github_client,
                repo_repository=mock_repo_repository,
                pr_repository=mock_pr_repository,
                scheduler=mock_scheduler,
            )

            config = BulkIngestionConfig()
            result = await orchestrator.sync_all(config)

            # Should call ingest_repository for each repo
            assert mock_bulk_service.ingest_repository.call_count == 2
            assert len(result.repo_results) == 2

    async def test_sync_all_aggregates_results(
        self,
        mock_github_client,
        mock_repo_repository,
        mock_pr_repository,
        mock_scheduler,
    ):
        """sync_all aggregates totals from all repositories."""
        results = [
            BulkIngestionResult(total_discovered=10, created=5, updated=2, failed=1),
            BulkIngestionResult(total_discovered=20, created=8, updated=4, failed=2),
        ]

        mock_settings = MagicMock()
        mock_settings.tracked_repos = ["owner/repo1", "owner/repo2"]

        with (
            patch(
                "github_activity_db.github.sync.multi_repo_orchestrator.get_settings",
                return_value=mock_settings,
            ),
            patch(
                "github_activity_db.github.sync.multi_repo_orchestrator.BulkPRIngestionService"
            ) as mock_bulk_class,
        ):
            mock_bulk_service = MagicMock()
            mock_bulk_service.ingest_repository = AsyncMock(side_effect=results)
            mock_bulk_class.return_value = mock_bulk_service

            orchestrator = MultiRepoOrchestrator(
                client=mock_github_client,
                repo_repository=mock_repo_repository,
                pr_repository=mock_pr_repository,
                scheduler=mock_scheduler,
            )

            config = BulkIngestionConfig()
            result = await orchestrator.sync_all(config)

            assert result.total_discovered == 30  # 10 + 20
            assert result.total_created == 13  # 5 + 8
            assert result.total_updated == 6  # 2 + 4
            assert result.total_failed == 3  # 1 + 2

    async def test_sync_all_continues_on_repo_failure(
        self,
        mock_github_client,
        mock_repo_repository,
        mock_pr_repository,
        mock_scheduler,
    ):
        """sync_all continues to next repo when one fails."""
        good_result = BulkIngestionResult(total_discovered=10, created=5)

        mock_settings = MagicMock()
        mock_settings.tracked_repos = ["owner/repo1", "owner/repo2", "owner/repo3"]

        with (
            patch(
                "github_activity_db.github.sync.multi_repo_orchestrator.get_settings",
                return_value=mock_settings,
            ),
            patch(
                "github_activity_db.github.sync.multi_repo_orchestrator.BulkPRIngestionService"
            ) as mock_bulk_class,
        ):
            mock_bulk_service = MagicMock()
            mock_bulk_service.ingest_repository = AsyncMock(
                side_effect=[
                    good_result,  # repo1 succeeds
                    Exception("API Error"),  # repo2 fails
                    good_result,  # repo3 succeeds
                ]
            )
            mock_bulk_class.return_value = mock_bulk_service

            orchestrator = MultiRepoOrchestrator(
                client=mock_github_client,
                repo_repository=mock_repo_repository,
                pr_repository=mock_pr_repository,
                scheduler=mock_scheduler,
            )

            config = BulkIngestionConfig()
            result = await orchestrator.sync_all(config)

            # Should have results for all 3 repos
            assert len(result.repo_results) == 3
            # Aggregated stats should include the failed repo
            assert result.total_failed == 1  # The repo-level failure

    async def test_sync_all_respects_repos_filter(
        self,
        mock_github_client,
        mock_repo_repository,
        mock_pr_repository,
        mock_scheduler,
    ):
        """sync_all only syncs repos specified in repos parameter."""
        mock_result = BulkIngestionResult(created=5)

        mock_settings = MagicMock()
        mock_settings.tracked_repos = ["default/repo"]

        with (
            patch(
                "github_activity_db.github.sync.multi_repo_orchestrator.get_settings",
                return_value=mock_settings,
            ),
            patch(
                "github_activity_db.github.sync.multi_repo_orchestrator.BulkPRIngestionService"
            ) as mock_bulk_class,
        ):
            mock_bulk_service = MagicMock()
            mock_bulk_service.ingest_repository = AsyncMock(return_value=mock_result)
            mock_bulk_class.return_value = mock_bulk_service

            orchestrator = MultiRepoOrchestrator(
                client=mock_github_client,
                repo_repository=mock_repo_repository,
                pr_repository=mock_pr_repository,
                scheduler=mock_scheduler,
            )

            config = BulkIngestionConfig()
            result = await orchestrator.sync_all(config, repos=["custom/repo1", "custom/repo2"])

            # Should only sync the 2 specified repos
            assert len(result.repo_results) == 2
            calls = mock_bulk_service.ingest_repository.call_args_list
            assert calls[0][0] == ("custom", "repo1", config)
            assert calls[1][0] == ("custom", "repo2", config)

    async def test_sync_all_initializes_repos_first(
        self,
        mock_github_client,
        mock_repo_repository,
        mock_pr_repository,
        mock_scheduler,
    ):
        """sync_all calls initialize_repositories before syncing."""
        mock_result = BulkIngestionResult(created=1)

        mock_settings = MagicMock()
        mock_settings.tracked_repos = ["owner/repo"]

        with (
            patch(
                "github_activity_db.github.sync.multi_repo_orchestrator.get_settings",
                return_value=mock_settings,
            ),
            patch(
                "github_activity_db.github.sync.multi_repo_orchestrator.BulkPRIngestionService"
            ) as mock_bulk_class,
        ):
            mock_bulk_service = MagicMock()
            mock_bulk_service.ingest_repository = AsyncMock(return_value=mock_result)
            mock_bulk_class.return_value = mock_bulk_service

            orchestrator = MultiRepoOrchestrator(
                client=mock_github_client,
                repo_repository=mock_repo_repository,
                pr_repository=mock_pr_repository,
                scheduler=mock_scheduler,
            )

            config = BulkIngestionConfig()
            await orchestrator.sync_all(config)

            # Repository should be initialized first
            mock_repo_repository.get_or_create.assert_called()
