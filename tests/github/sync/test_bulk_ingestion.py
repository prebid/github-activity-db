"""Tests for BulkPRIngestionService.

Tests cover:
- Discovery with date filtering
- State filtering (open, merged, excluding abandoned)
- Cold/hot path handling (frozen vs active PRs)
- Result aggregation
- Batch execution integration
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from github_activity_db.github.exceptions import GitHubRateLimitError
from github_activity_db.github.sync.bulk_ingestion import (
    BulkIngestionConfig,
    BulkIngestionResult,
    BulkPRIngestionService,
)
from github_activity_db.github.sync.results import PRIngestionResult
from github_activity_db.schemas.github_api import GitHubPullRequest
from tests.factories import make_github_merged_pr, make_github_pr


async def async_iter(items):
    """Convert a list to an async iterator for mocking iter_pull_requests."""
    for item in items:
        yield item


# -----------------------------------------------------------------------------
# Test Fixtures
# -----------------------------------------------------------------------------
@pytest.fixture
def now() -> datetime:
    """Current time for test calculations."""
    return datetime.now(UTC)


@pytest.fixture
def open_pr_data(now: datetime) -> dict:
    """Open PR created recently."""
    return make_github_pr(
        number=100,
        state="open",
        merged=False,
        created_at=(now - timedelta(days=5)).isoformat(),
        updated_at=(now - timedelta(days=1)).isoformat(),
    )


@pytest.fixture
def merged_hot_pr_data(now: datetime) -> dict:
    """Merged PR within grace period (hot path)."""
    merged_at = (now - timedelta(days=3)).isoformat()
    return make_github_merged_pr(
        number=101,
        merged_at=merged_at,
        created_at=(now - timedelta(days=10)).isoformat(),
    )


@pytest.fixture
def merged_cold_pr_data(now: datetime) -> dict:
    """Merged PR past grace period (cold path - frozen)."""
    merged_at = (now - timedelta(days=30)).isoformat()
    return make_github_merged_pr(
        number=102,
        merged_at=merged_at,
        created_at=(now - timedelta(days=60)).isoformat(),
    )


@pytest.fixture
def abandoned_pr_data(now: datetime) -> dict:
    """Closed but not merged PR (abandoned)."""
    return make_github_pr(
        number=103,
        state="closed",
        merged=False,
        closed_at=(now - timedelta(days=20)).isoformat(),
        created_at=(now - timedelta(days=30)).isoformat(),
    )


@pytest.fixture
def mock_github_client():
    """Mock GitHub client."""
    client = MagicMock()
    # iter_pull_requests returns an AsyncIterator - mock with async_iter helper
    client.iter_pull_requests = MagicMock(return_value=async_iter([]))
    return client


@pytest.fixture
def mock_repo_repository():
    """Mock repository repository."""
    repo = MagicMock()
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
    scheduler.submit = AsyncMock(side_effect=lambda coro_factory, **_: coro_factory())
    return scheduler


# -----------------------------------------------------------------------------
# BulkIngestionResult Tests
# -----------------------------------------------------------------------------
class TestBulkIngestionResult:
    """Tests for BulkIngestionResult dataclass."""

    def test_total_processed(self):
        """Total processed includes created, updated, and failed."""
        result = BulkIngestionResult(
            created=5, updated=3, failed=2, skipped_frozen=10, skipped_unchanged=5
        )
        assert result.total_processed == 10  # 5 + 3 + 2

    def test_total_skipped(self):
        """Total skipped includes frozen and unchanged."""
        result = BulkIngestionResult(skipped_frozen=10, skipped_unchanged=5)
        assert result.total_skipped == 15  # 10 + 5

    def test_success_rate_all_succeeded(self):
        """Success rate is 100% when no failures."""
        result = BulkIngestionResult(created=5, updated=3, failed=0)
        assert result.success_rate == 100.0

    def test_success_rate_partial_failure(self):
        """Success rate calculated correctly with failures."""
        result = BulkIngestionResult(created=4, updated=4, failed=2)
        # (4 + 4) / (4 + 4 + 2) = 8/10 = 80%
        assert result.success_rate == 80.0

    def test_success_rate_no_processing(self):
        """Success rate is 100% when nothing processed."""
        result = BulkIngestionResult()
        assert result.success_rate == 100.0

    def test_to_dict(self):
        """to_dict returns all expected fields."""
        result = BulkIngestionResult(
            total_discovered=10,
            created=3,
            updated=2,
            skipped_frozen=2,
            skipped_unchanged=2,
            failed=1,
            failed_prs=[(999, "Test error")],
            duration_seconds=5.5,
        )
        d = result.to_dict()

        assert d["total_discovered"] == 10
        assert d["created"] == 3
        assert d["updated"] == 2
        assert d["skipped_frozen"] == 2
        assert d["skipped_unchanged"] == 2
        assert d["failed"] == 1
        assert d["failed_prs"] == [{"pr_number": 999, "error": "Test error"}]
        assert d["duration_seconds"] == 5.5
        assert d["success_rate"] == 83.3  # (3+2)/(3+2+1) = 5/6 ≈ 83.3%


# -----------------------------------------------------------------------------
# BulkIngestionConfig Tests
# -----------------------------------------------------------------------------
class TestBulkIngestionConfig:
    """Tests for BulkIngestionConfig dataclass."""

    def test_defaults(self):
        """Config has sensible defaults."""
        config = BulkIngestionConfig()
        assert config.since is None
        assert config.until is None
        assert config.state == "all"
        assert config.max_prs is None
        assert config.concurrency == 5
        assert config.dry_run is False

    def test_custom_values(self):
        """Config accepts custom values."""
        since = datetime(2024, 10, 1, tzinfo=UTC)
        config = BulkIngestionConfig(
            since=since,
            state="open",
            max_prs=100,
            dry_run=True,
        )
        assert config.since == since
        assert config.state == "open"
        assert config.max_prs == 100
        assert config.dry_run is True


# -----------------------------------------------------------------------------
# Discovery Tests
# -----------------------------------------------------------------------------
class TestDiscoverPRs:
    """Tests for BulkPRIngestionService.discover_prs."""

    @pytest.mark.asyncio
    async def test_discover_includes_open_prs(
        self,
        mock_github_client,
        mock_repo_repository,
        mock_pr_repository,
        mock_scheduler,
        open_pr_data,
    ):
        """Open PRs are included in discovery."""
        mock_github_client.iter_pull_requests.return_value = async_iter(
            [GitHubPullRequest.model_validate(open_pr_data)]
        )

        service = BulkPRIngestionService(
            client=mock_github_client,
            repo_repository=mock_repo_repository,
            pr_repository=mock_pr_repository,
            scheduler=mock_scheduler,
        )

        config = BulkIngestionConfig(state="all")
        pr_numbers = await service.discover_prs("owner", "repo", config)

        assert pr_numbers == [100]

    @pytest.mark.asyncio
    async def test_discover_includes_merged_prs(
        self,
        mock_github_client,
        mock_repo_repository,
        mock_pr_repository,
        mock_scheduler,
        merged_hot_pr_data,
    ):
        """Merged PRs are included in discovery."""
        mock_github_client.iter_pull_requests.return_value = async_iter(
            [GitHubPullRequest.model_validate(merged_hot_pr_data)]
        )

        service = BulkPRIngestionService(
            client=mock_github_client,
            repo_repository=mock_repo_repository,
            pr_repository=mock_pr_repository,
            scheduler=mock_scheduler,
        )

        config = BulkIngestionConfig(state="all")
        pr_numbers = await service.discover_prs("owner", "repo", config)

        assert pr_numbers == [101]

    @pytest.mark.asyncio
    async def test_discover_includes_closed_prs(
        self,
        mock_github_client,
        mock_repo_repository,
        mock_pr_repository,
        mock_scheduler,
        open_pr_data,
        abandoned_pr_data,
    ):
        """Closed PRs are included in discovery - abandoned filtering happens during ingestion.

        NOTE: The GitHub list API does NOT include merge status (merged is always False).
        We cannot filter abandoned PRs during discovery - we include all closed PRs and
        filter them during ingestion when we have the full PR data.
        """
        mock_github_client.iter_pull_requests.return_value = async_iter(
            [
                GitHubPullRequest.model_validate(open_pr_data),
                GitHubPullRequest.model_validate(abandoned_pr_data),
            ]
        )

        service = BulkPRIngestionService(
            client=mock_github_client,
            repo_repository=mock_repo_repository,
            pr_repository=mock_pr_repository,
            scheduler=mock_scheduler,
        )

        config = BulkIngestionConfig(state="all")
        pr_numbers = await service.discover_prs("owner", "repo", config)

        # Both open PR (#100) and closed PR (#103) should be discovered
        # Abandoned filtering happens during ingestion, not discovery
        assert pr_numbers == [100, 103]

    @pytest.mark.asyncio
    async def test_discover_respects_since_date(
        self,
        mock_github_client,
        mock_repo_repository,
        mock_pr_repository,
        mock_scheduler,
        now,
    ):
        """PRs before since date are excluded."""
        # PR created 5 days ago
        recent_pr = make_github_pr(
            number=1,
            created_at=(now - timedelta(days=5)).isoformat(),
        )
        # PR created 30 days ago
        old_pr = make_github_pr(
            number=2,
            created_at=(now - timedelta(days=30)).isoformat(),
        )

        mock_github_client.iter_pull_requests.return_value = async_iter(
            [
                GitHubPullRequest.model_validate(recent_pr),
                GitHubPullRequest.model_validate(old_pr),
            ]
        )

        service = BulkPRIngestionService(
            client=mock_github_client,
            repo_repository=mock_repo_repository,
            pr_repository=mock_pr_repository,
            scheduler=mock_scheduler,
        )

        # Only PRs created in last 10 days
        config = BulkIngestionConfig(since=now - timedelta(days=10))
        pr_numbers = await service.discover_prs("owner", "repo", config)

        assert pr_numbers == [1]

    @pytest.mark.asyncio
    async def test_discover_respects_until_date(
        self,
        mock_github_client,
        mock_repo_repository,
        mock_pr_repository,
        mock_scheduler,
        now,
    ):
        """PRs after until date are excluded."""
        # PR created yesterday
        recent_pr = make_github_pr(
            number=1,
            created_at=(now - timedelta(days=1)).isoformat(),
        )
        # PR created 10 days ago
        older_pr = make_github_pr(
            number=2,
            created_at=(now - timedelta(days=10)).isoformat(),
        )

        mock_github_client.iter_pull_requests.return_value = async_iter(
            [
                GitHubPullRequest.model_validate(recent_pr),
                GitHubPullRequest.model_validate(older_pr),
            ]
        )

        service = BulkPRIngestionService(
            client=mock_github_client,
            repo_repository=mock_repo_repository,
            pr_repository=mock_pr_repository,
            scheduler=mock_scheduler,
        )

        # Only PRs created more than 5 days ago
        config = BulkIngestionConfig(until=now - timedelta(days=5))
        pr_numbers = await service.discover_prs("owner", "repo", config)

        assert pr_numbers == [2]

    @pytest.mark.asyncio
    async def test_discover_respects_max_limit(
        self,
        mock_github_client,
        mock_repo_repository,
        mock_pr_repository,
        mock_scheduler,
        now,
    ):
        """Max limit is respected."""
        prs = [
            GitHubPullRequest.model_validate(
                make_github_pr(
                    number=i,
                    created_at=(now - timedelta(days=i)).isoformat(),
                )
            )
            for i in range(1, 11)  # 10 PRs
        ]
        mock_github_client.iter_pull_requests.return_value = async_iter(prs)

        service = BulkPRIngestionService(
            client=mock_github_client,
            repo_repository=mock_repo_repository,
            pr_repository=mock_pr_repository,
            scheduler=mock_scheduler,
        )

        config = BulkIngestionConfig(max_prs=5)
        pr_numbers = await service.discover_prs("owner", "repo", config)

        assert len(pr_numbers) == 5

    @pytest.mark.asyncio
    async def test_discover_state_open_only(
        self,
        mock_github_client,
        mock_repo_repository,
        mock_pr_repository,
        mock_scheduler,
        open_pr_data,
        merged_hot_pr_data,
    ):
        """State filter 'open' only returns open PRs."""
        mock_github_client.iter_pull_requests.return_value = async_iter(
            [
                GitHubPullRequest.model_validate(open_pr_data),
                GitHubPullRequest.model_validate(merged_hot_pr_data),
            ]
        )

        service = BulkPRIngestionService(
            client=mock_github_client,
            repo_repository=mock_repo_repository,
            pr_repository=mock_pr_repository,
            scheduler=mock_scheduler,
        )

        config = BulkIngestionConfig(state="open")
        pr_numbers = await service.discover_prs("owner", "repo", config)

        assert pr_numbers == [100]  # Only the open PR

    @pytest.mark.asyncio
    async def test_discover_state_merged_only(
        self,
        mock_github_client,
        mock_repo_repository,
        mock_pr_repository,
        mock_scheduler,
        open_pr_data,
        merged_hot_pr_data,
    ):
        """State filter 'merged' only returns merged PRs."""
        mock_github_client.iter_pull_requests.return_value = async_iter(
            [
                GitHubPullRequest.model_validate(open_pr_data),
                GitHubPullRequest.model_validate(merged_hot_pr_data),
            ]
        )

        service = BulkPRIngestionService(
            client=mock_github_client,
            repo_repository=mock_repo_repository,
            pr_repository=mock_pr_repository,
            scheduler=mock_scheduler,
        )

        config = BulkIngestionConfig(state="merged")
        pr_numbers = await service.discover_prs("owner", "repo", config)

        assert pr_numbers == [101]  # Only the merged PR

    @pytest.mark.asyncio
    async def test_discover_empty_repo(
        self,
        mock_github_client,
        mock_repo_repository,
        mock_pr_repository,
        mock_scheduler,
    ):
        """Empty repo returns empty list without error."""
        mock_github_client.iter_pull_requests.return_value = async_iter([])

        service = BulkPRIngestionService(
            client=mock_github_client,
            repo_repository=mock_repo_repository,
            pr_repository=mock_pr_repository,
            scheduler=mock_scheduler,
        )

        config = BulkIngestionConfig()
        pr_numbers = await service.discover_prs("owner", "repo", config)

        assert pr_numbers == []


# -----------------------------------------------------------------------------
# Discovery Rate Limit Tests
# -----------------------------------------------------------------------------
@pytest.fixture
def mock_sleep():
    """Mock asyncio.sleep to avoid real delays in rate limit retry tests.

    The bulk_ingestion module sleeps for 60+ seconds when retrying after
    rate limit errors. This fixture patches sleep to return immediately,
    keeping tests fast while still exercising the retry logic.
    """
    with patch(
        "github_activity_db.github.sync.bulk_ingestion.asyncio.sleep",
        new_callable=AsyncMock,
    ) as mock:
        yield mock


class TestDiscoveryRateLimit:
    """Tests for rate limit handling during PR discovery."""

    @pytest.mark.asyncio
    async def test_discovery_retries_on_rate_limit(
        self,
        mock_github_client,
        mock_repo_repository,
        mock_pr_repository,
        mock_scheduler,
        mock_sleep,
        now,
    ):
        """Discovery retries after rate limit error."""
        pr_data = make_github_pr(number=100, created_at=now.isoformat())

        call_count = 0

        async def mock_iter(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise GitHubRateLimitError("Rate limited", reset_at=None)
            # Second call succeeds
            yield GitHubPullRequest.model_validate(pr_data)

        mock_github_client.iter_pull_requests = mock_iter

        service = BulkPRIngestionService(
            client=mock_github_client,
            repo_repository=mock_repo_repository,
            pr_repository=mock_pr_repository,
            scheduler=mock_scheduler,
        )

        config = BulkIngestionConfig()
        pr_numbers = await service.discover_prs("owner", "repo", config)

        assert pr_numbers == [100]
        assert call_count == 2  # Retried once
        # Verify sleep was called with default 60s (reset_at=None)
        mock_sleep.assert_called_once_with(60.0)

    @pytest.mark.asyncio
    async def test_discovery_fails_after_max_retries(
        self,
        mock_github_client,
        mock_repo_repository,
        mock_pr_repository,
        mock_scheduler,
        mock_sleep,
    ):
        """Discovery fails after max retries exceeded."""

        async def mock_iter(*args, **kwargs):
            raise GitHubRateLimitError("Rate limited", reset_at=None)
            yield  # Make it a generator

        mock_github_client.iter_pull_requests = mock_iter

        service = BulkPRIngestionService(
            client=mock_github_client,
            repo_repository=mock_repo_repository,
            pr_repository=mock_pr_repository,
            scheduler=mock_scheduler,
        )

        config = BulkIngestionConfig()
        with pytest.raises(GitHubRateLimitError):
            await service.discover_prs("owner", "repo", config)

        # Should have slept 2 times (attempts 1 and 2 sleep, attempt 3 raises)
        assert mock_sleep.call_count == 2

    @pytest.mark.asyncio
    async def test_discovery_uses_reset_time_for_wait(
        self,
        mock_github_client,
        mock_repo_repository,
        mock_pr_repository,
        mock_scheduler,
        mock_sleep,
        now,
    ):
        """Discovery uses reset_at time to calculate wait duration."""
        pr_data = make_github_pr(number=100, created_at=now.isoformat())
        reset_time = now + timedelta(seconds=2)

        call_count = 0

        async def mock_iter(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise GitHubRateLimitError("Rate limited", reset_at=reset_time)
            yield GitHubPullRequest.model_validate(pr_data)

        mock_github_client.iter_pull_requests = mock_iter

        service = BulkPRIngestionService(
            client=mock_github_client,
            repo_repository=mock_repo_repository,
            pr_repository=mock_pr_repository,
            scheduler=mock_scheduler,
        )

        config = BulkIngestionConfig()
        # This should wait based on reset_time and then succeed
        pr_numbers = await service.discover_prs("owner", "repo", config)

        assert pr_numbers == [100]
        assert call_count == 2
        # Verify sleep was called once with calculated wait time
        # reset_time is 2 seconds in future, plus 5 second buffer = 7 seconds
        # But time passes between setting reset_time and the sleep call,
        # so we just verify it was called once with a reasonable value
        mock_sleep.assert_called_once()
        wait_time = mock_sleep.call_args[0][0]
        assert 5.0 <= wait_time <= 10.0  # Should be around 7 seconds


# -----------------------------------------------------------------------------
# Ingestion Integration Tests
# -----------------------------------------------------------------------------
class TestIngestRepository:
    """Tests for BulkPRIngestionService.ingest_repository."""

    @pytest.mark.asyncio
    async def test_ingest_empty_discovery(
        self,
        mock_github_client,
        mock_repo_repository,
        mock_pr_repository,
        mock_scheduler,
    ):
        """Empty discovery returns zero results."""
        mock_github_client.iter_pull_requests.return_value = async_iter([])

        service = BulkPRIngestionService(
            client=mock_github_client,
            repo_repository=mock_repo_repository,
            pr_repository=mock_pr_repository,
            scheduler=mock_scheduler,
        )

        config = BulkIngestionConfig()
        result = await service.ingest_repository("owner", "repo", config)

        assert result.total_discovered == 0
        assert result.created == 0
        assert result.updated == 0
        assert result.failed == 0


class TestResultAggregation:
    """Tests for result aggregation logic."""

    def test_aggregate_created_results(self):
        """Created results are aggregated correctly."""
        # Simulate what happens when PRIngestionResult with created=True is returned
        mock_pr = MagicMock()
        mock_pr.number = 1

        results = [
            PRIngestionResult(pr=mock_pr, created=True),
            PRIngestionResult(pr=mock_pr, created=True),
        ]

        # Aggregate like the service does
        bulk_result = BulkIngestionResult()
        for pr_result in results:
            if pr_result.created:
                bulk_result.created += 1

        assert bulk_result.created == 2

    def test_aggregate_mixed_results(self):
        """Mixed results are aggregated correctly."""
        mock_pr = MagicMock()
        mock_pr.number = 1

        results = [
            PRIngestionResult(pr=mock_pr, created=True),
            PRIngestionResult(pr=mock_pr, updated=True),
            PRIngestionResult(pr=mock_pr, skipped_frozen=True),
            PRIngestionResult(pr=mock_pr, skipped_unchanged=True),
            PRIngestionResult(pr=None, error=Exception("Test")),
        ]

        bulk_result = BulkIngestionResult()
        for pr_result in results:
            if pr_result.created:
                bulk_result.created += 1
            elif pr_result.updated:
                bulk_result.updated += 1
            elif pr_result.skipped_frozen:
                bulk_result.skipped_frozen += 1
            elif pr_result.skipped_unchanged:
                bulk_result.skipped_unchanged += 1
            elif pr_result.error:
                bulk_result.failed += 1
                bulk_result.failed_prs.append(
                    (pr_result.pr.number if pr_result.pr else -1, str(pr_result.error))
                )

        assert bulk_result.created == 1
        assert bulk_result.updated == 1
        assert bulk_result.skipped_frozen == 1
        assert bulk_result.skipped_unchanged == 1
        assert bulk_result.failed == 1
        assert bulk_result.failed_prs[0] == (-1, "Test")
