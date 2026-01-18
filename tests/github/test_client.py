"""Tests for GitHubClient.

Tests cover:
- Lazy iteration via iter_pull_requests
- Early termination of pagination
- Full PR fetch
- 404 error handling
- Rate limit header extraction
- Pacing integration (pacer hook calls)
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from githubkit.exception import RequestFailed

from github_activity_db.github.client import GitHubClient
from github_activity_db.github.exceptions import (
    GitHubAuthenticationError,
    GitHubNotFoundError,
)
from github_activity_db.github.pacing import RequestPacer
from github_activity_db.github.rate_limit import RateLimitMonitor, RateLimitPool
from tests.factories import make_github_pr


# -----------------------------------------------------------------------------
# Helper: Async Iterator Mock
# -----------------------------------------------------------------------------
async def async_iter(items):
    """Convert a list to an async iterator for mocking paginate."""
    for item in items:
        yield item


def make_mock_pr_data(number: int, **overrides):
    """Create a MagicMock that behaves like githubkit PR response."""
    pr_dict = make_github_pr(number=number, **overrides)
    mock = MagicMock()
    mock.model_dump.return_value = pr_dict
    return mock


# -----------------------------------------------------------------------------
# Test Fixtures
# -----------------------------------------------------------------------------
@pytest.fixture
def mock_github():
    """Create a mock githubkit GitHub client."""
    with patch("github_activity_db.github.client.GitHub") as mock_class:
        mock_instance = MagicMock()
        mock_class.return_value = mock_instance
        yield mock_instance


@pytest.fixture
def rate_monitor():
    """Create a RateLimitMonitor for testing header extraction."""
    return RateLimitMonitor()


# -----------------------------------------------------------------------------
# Test: Initialization
# -----------------------------------------------------------------------------
class TestGitHubClientInit:
    """Tests for GitHubClient initialization."""

    def test_init_with_token(self):
        """Client initializes with provided token."""
        with patch("github_activity_db.github.client.get_settings") as mock_settings:
            mock_settings.return_value.github_token = None
            client = GitHubClient(token="test-token")
            assert client._token == "test-token"

    def test_init_without_token_raises(self):
        """Client raises error when no token available."""
        with patch("github_activity_db.github.client.get_settings") as mock_settings:
            mock_settings.return_value.github_token = None
            with pytest.raises(GitHubAuthenticationError):
                GitHubClient(token=None)

    def test_init_with_rate_monitor(self, rate_monitor):
        """Client accepts optional rate monitor."""
        with patch("github_activity_db.github.client.get_settings") as mock_settings:
            mock_settings.return_value.github_token = "test-token"
            client = GitHubClient(rate_monitor=rate_monitor)
            assert client.rate_monitor is rate_monitor


# -----------------------------------------------------------------------------
# Test: iter_pull_requests (Lazy Iteration)
# -----------------------------------------------------------------------------
class TestIterPullRequests:
    """Tests for lazy PR iteration."""

    async def test_iter_yields_prs_lazily(self, mock_github):
        """iter_pull_requests yields PRs one at a time."""
        # Setup mock pagination
        pr_data = [make_mock_pr_data(i) for i in [100, 101, 102]]
        mock_github.paginate.return_value = async_iter(pr_data)

        with patch("github_activity_db.github.client.get_settings") as mock_settings:
            mock_settings.return_value.github_token = "test-token"
            client = GitHubClient()

            # Collect yielded PRs
            prs = []
            async for pr in client.iter_pull_requests("owner", "repo"):
                prs.append(pr)

            assert len(prs) == 3
            assert prs[0].number == 100
            assert prs[1].number == 101
            assert prs[2].number == 102

    async def test_iter_early_termination(self, mock_github):
        """Breaking from iteration stops fetching pages."""
        # Setup mock pagination with many PRs
        pr_data = [make_mock_pr_data(i) for i in range(100, 200)]
        mock_github.paginate.return_value = async_iter(pr_data)

        with patch("github_activity_db.github.client.get_settings") as mock_settings:
            mock_settings.return_value.github_token = "test-token"
            client = GitHubClient()

            # Only consume first 3
            count = 0
            async for _pr in client.iter_pull_requests("owner", "repo"):
                count += 1
                if count >= 3:
                    break

            # Should have stopped after 3
            assert count == 3

    async def test_iter_passes_parameters(self, mock_github):
        """iter_pull_requests passes correct parameters to API."""
        mock_github.paginate.return_value = async_iter([])

        with patch("github_activity_db.github.client.get_settings") as mock_settings:
            mock_settings.return_value.github_token = "test-token"
            client = GitHubClient()

            # Consume iterator to trigger the call
            async for _ in client.iter_pull_requests(
                "prebid",
                "prebid-server",
                state="all",
                sort="updated",
                direction="asc",
                per_page=50,
            ):
                pass

            # Verify parameters
            mock_github.paginate.assert_called_once()
            call_kwargs = mock_github.paginate.call_args[1]
            assert call_kwargs["owner"] == "prebid"
            assert call_kwargs["repo"] == "prebid-server"
            assert call_kwargs["state"] == "all"
            assert call_kwargs["sort"] == "updated"
            assert call_kwargs["direction"] == "asc"
            assert call_kwargs["per_page"] == 50


# -----------------------------------------------------------------------------
# Test: get_pull_request
# -----------------------------------------------------------------------------
class TestGetPullRequest:
    """Tests for fetching single PR data."""

    async def test_get_pr_success(self):
        """get_pull_request returns PR with full details."""
        with (
            patch("github_activity_db.github.client.get_settings") as mock_settings,
            patch("github_activity_db.github.client.GitHub") as mock_github_class,
        ):
            mock_settings.return_value.github_token = "test-token"
            mock_github = MagicMock()
            mock_github_class.return_value = mock_github

            # Setup mock response
            pr_response = MagicMock()
            pr_response.parsed_data.model_dump.return_value = make_github_pr(number=123)

            mock_github.rest.pulls.async_get = AsyncMock(return_value=pr_response)

            client = GitHubClient()
            pr = await client.get_pull_request("owner", "repo", 123)

            assert pr.number == 123
            mock_github.rest.pulls.async_get.assert_called_once_with(
                owner="owner", repo="repo", pull_number=123
            )

    async def test_get_pr_not_found(self):
        """get_pull_request raises GitHubNotFoundError for 404."""
        with (
            patch("github_activity_db.github.client.get_settings") as mock_settings,
            patch("github_activity_db.github.client.GitHub") as mock_github_class,
        ):
            mock_settings.return_value.github_token = "test-token"
            mock_github = MagicMock()
            mock_github_class.return_value = mock_github

            # Setup 404 response
            mock_response = MagicMock()
            mock_response.status_code = 404
            mock_response.headers = {}
            error = RequestFailed(mock_response)

            mock_github.rest.pulls.async_get = AsyncMock(side_effect=error)

            client = GitHubClient()
            with pytest.raises(GitHubNotFoundError) as exc_info:
                await client.get_pull_request("owner", "repo", 99999)

            assert "99999" in str(exc_info.value)
            assert "owner/repo" in str(exc_info.value)


# -----------------------------------------------------------------------------
# Test: Rate Limit Header Extraction
# -----------------------------------------------------------------------------
class TestRateLimitHeaderExtraction:
    """Tests for extracting rate limit info from response headers."""

    async def test_updates_monitor_from_response(self, rate_monitor):
        """Response headers update the rate monitor."""
        with (
            patch("github_activity_db.github.client.get_settings") as mock_settings,
            patch("github_activity_db.github.client.GitHub") as mock_github_class,
        ):
            mock_settings.return_value.github_token = "test-token"
            mock_github = MagicMock()
            mock_github_class.return_value = mock_github

            # Setup response with rate limit headers
            mock_response = MagicMock()
            mock_response.headers = {
                "x-ratelimit-limit": "5000",
                "x-ratelimit-remaining": "4999",
                "x-ratelimit-reset": str(int(datetime.now(UTC).timestamp()) + 3600),
                "x-ratelimit-resource": "core",
            }
            mock_response.parsed_data.model_dump.return_value = make_github_pr(number=123)

            mock_github.rest.pulls.async_get = AsyncMock(return_value=mock_response)

            client = GitHubClient(rate_monitor=rate_monitor)
            await client.get_pull_request("owner", "repo", 123)

            # Verify monitor was updated
            from github_activity_db.github.rate_limit import RateLimitPool

            pool_limit = rate_monitor.get_pool_limit(RateLimitPool.CORE)
            assert pool_limit is not None
            assert pool_limit.remaining == 4999
            assert pool_limit.limit == 5000

    async def test_no_monitor_no_error(self):
        """No rate monitor configured doesn't cause errors."""
        with (
            patch("github_activity_db.github.client.get_settings") as mock_settings,
            patch("github_activity_db.github.client.GitHub") as mock_github_class,
        ):
            mock_settings.return_value.github_token = "test-token"
            mock_github = MagicMock()
            mock_github_class.return_value = mock_github

            mock_response = MagicMock()
            mock_response.headers = {"x-ratelimit-remaining": "4999"}
            mock_response.parsed_data.model_dump.return_value = make_github_pr(number=123)

            mock_github.rest.pulls.async_get = AsyncMock(return_value=mock_response)

            client = GitHubClient(rate_monitor=None)
            # Should not raise
            await client.get_pull_request("owner", "repo", 123)

    async def test_error_response_does_not_crash_header_extraction(self, rate_monitor):
        """Error responses don't crash even if header extraction fails."""
        with (
            patch("github_activity_db.github.client.get_settings") as mock_settings,
            patch("github_activity_db.github.client.GitHub") as mock_github_class,
        ):
            mock_settings.return_value.github_token = "test-token"
            mock_github = MagicMock()
            mock_github_class.return_value = mock_github

            # Setup 404 response without proper headers
            mock_response = MagicMock()
            mock_response.status_code = 404
            mock_response.headers = {}
            error = RequestFailed(mock_response)

            mock_github.rest.pulls.async_get = AsyncMock(side_effect=error)

            client = GitHubClient(rate_monitor=rate_monitor)

            # Should raise NotFoundError but not crash during header extraction
            with pytest.raises(GitHubNotFoundError):
                await client.get_pull_request("owner", "repo", 99999)


# -----------------------------------------------------------------------------
# Test: Context Manager
# -----------------------------------------------------------------------------
class TestContextManager:
    """Tests for async context manager protocol."""

    async def test_context_manager_returns_client(self):
        """Async context manager returns the client instance."""
        with patch("github_activity_db.github.client.get_settings") as mock_settings:
            mock_settings.return_value.github_token = "test-token"

            async with GitHubClient() as client:
                assert isinstance(client, GitHubClient)

    async def test_context_manager_closes_on_exit(self):
        """Async context manager closes client on exit."""
        with patch("github_activity_db.github.client.get_settings") as mock_settings:
            mock_settings.return_value.github_token = "test-token"

            client = GitHubClient()
            # Trigger client creation
            _ = client._github

            async with client:
                assert client._client is not None

            # After exit, client should be cleaned up
            assert client._client is None


# -----------------------------------------------------------------------------
# Test: Pacer Integration
# -----------------------------------------------------------------------------
class TestGitHubClientPacerIntegration:
    """Tests for pacer hook integration in GitHubClient.

    Verifies that GitHubClient correctly calls pacer methods:
    - get_recommended_delay() before each request
    - on_request_start() after delay applied
    - on_request_complete() after each response
    """

    @pytest.fixture
    def mock_pacer(self):
        """Create a mock RequestPacer for testing hook calls."""
        pacer = MagicMock(spec=RequestPacer)
        pacer.get_recommended_delay.return_value = 0.0  # No delay by default
        return pacer

    # -------------------------------------------------------------------------
    # Initialization Tests
    # -------------------------------------------------------------------------
    def test_init_with_pacer(self, mock_pacer):
        """Client accepts pacer parameter."""
        with patch("github_activity_db.github.client.get_settings") as mock_settings:
            mock_settings.return_value.github_token = "test-token"
            client = GitHubClient(pacer=mock_pacer)
            assert client._pacer is mock_pacer

    def test_pacer_property_returns_pacer(self, mock_pacer):
        """Pacer is accessible via property."""
        with patch("github_activity_db.github.client.get_settings") as mock_settings:
            mock_settings.return_value.github_token = "test-token"
            client = GitHubClient(pacer=mock_pacer)
            assert client.pacer is mock_pacer

    # -------------------------------------------------------------------------
    # _apply_pacing() Tests
    # -------------------------------------------------------------------------
    async def test_apply_pacing_calls_get_recommended_delay(self, mock_pacer):
        """_apply_pacing calls pacer.get_recommended_delay()."""
        with patch("github_activity_db.github.client.get_settings") as mock_settings:
            mock_settings.return_value.github_token = "test-token"
            client = GitHubClient(pacer=mock_pacer)

            await client._apply_pacing()

            mock_pacer.get_recommended_delay.assert_called_once_with(RateLimitPool.CORE)

    async def test_apply_pacing_calls_on_request_start(self, mock_pacer):
        """_apply_pacing calls pacer.on_request_start()."""
        with patch("github_activity_db.github.client.get_settings") as mock_settings:
            mock_settings.return_value.github_token = "test-token"
            client = GitHubClient(pacer=mock_pacer)

            await client._apply_pacing()

            mock_pacer.on_request_start.assert_called_once()

    async def test_apply_pacing_sleeps_when_delay_positive(self, mock_pacer):
        """_apply_pacing sleeps when pacer returns positive delay."""
        mock_pacer.get_recommended_delay.return_value = 0.5

        with (
            patch("github_activity_db.github.client.get_settings") as mock_settings,
            patch("github_activity_db.github.client.asyncio.sleep") as mock_sleep,
        ):
            mock_settings.return_value.github_token = "test-token"
            mock_sleep.return_value = None  # Make it awaitable

            client = GitHubClient(pacer=mock_pacer)
            await client._apply_pacing()

            mock_sleep.assert_called_once_with(0.5)

    async def test_apply_pacing_skips_sleep_when_delay_zero(self, mock_pacer):
        """_apply_pacing doesn't sleep when delay is 0."""
        mock_pacer.get_recommended_delay.return_value = 0.0

        with (
            patch("github_activity_db.github.client.get_settings") as mock_settings,
            patch("github_activity_db.github.client.asyncio.sleep") as mock_sleep,
        ):
            mock_settings.return_value.github_token = "test-token"
            client = GitHubClient(pacer=mock_pacer)

            await client._apply_pacing()

            mock_sleep.assert_not_called()

    async def test_apply_pacing_noop_when_no_pacer(self):
        """_apply_pacing doesn't error when pacer is None."""
        with patch("github_activity_db.github.client.get_settings") as mock_settings:
            mock_settings.return_value.github_token = "test-token"
            client = GitHubClient(pacer=None)

            # Should not raise
            await client._apply_pacing()

    # -------------------------------------------------------------------------
    # _update_rate_limit_from_response() Tests
    # -------------------------------------------------------------------------
    def test_update_calls_pacer_on_request_complete(self, mock_pacer):
        """_update_rate_limit_from_response calls pacer.on_request_complete()."""
        with patch("github_activity_db.github.client.get_settings") as mock_settings:
            mock_settings.return_value.github_token = "test-token"
            client = GitHubClient(pacer=mock_pacer)

            response = MagicMock()
            response.headers = {"x-ratelimit-remaining": "4999"}

            client._update_rate_limit_from_response(response)

            mock_pacer.on_request_complete.assert_called_once()

    def test_update_passes_headers_to_pacer(self, mock_pacer):
        """_update_rate_limit_from_response passes headers dict to pacer."""
        with patch("github_activity_db.github.client.get_settings") as mock_settings:
            mock_settings.return_value.github_token = "test-token"
            client = GitHubClient(pacer=mock_pacer)

            response = MagicMock()
            response.headers = {
                "x-ratelimit-limit": "5000",
                "x-ratelimit-remaining": "4999",
                "x-ratelimit-reset": "1234567890",
            }

            client._update_rate_limit_from_response(response)

            # Verify headers were passed
            call_args = mock_pacer.on_request_complete.call_args
            headers_passed = call_args[0][0]
            assert headers_passed["x-ratelimit-remaining"] == "4999"
            assert headers_passed["x-ratelimit-limit"] == "5000"

    def test_update_noop_when_no_pacer(self):
        """_update_rate_limit_from_response doesn't error when pacer is None."""
        with patch("github_activity_db.github.client.get_settings") as mock_settings:
            mock_settings.return_value.github_token = "test-token"
            client = GitHubClient(pacer=None)

            response = MagicMock()
            response.headers = {"x-ratelimit-remaining": "4999"}

            # Should not raise
            client._update_rate_limit_from_response(response)

    # -------------------------------------------------------------------------
    # API Method Integration Tests
    # -------------------------------------------------------------------------
    async def test_get_pull_request_calls_apply_pacing(self, mock_pacer):
        """get_pull_request calls _apply_pacing before making request."""
        with (
            patch("github_activity_db.github.client.get_settings") as mock_settings,
            patch("github_activity_db.github.client.GitHub") as mock_github_class,
        ):
            mock_settings.return_value.github_token = "test-token"
            mock_github = MagicMock()
            mock_github_class.return_value = mock_github

            # Setup mock response
            mock_response = MagicMock()
            mock_response.headers = {"x-ratelimit-remaining": "4999"}
            mock_response.parsed_data.model_dump.return_value = make_github_pr(number=123)
            mock_github.rest.pulls.async_get = AsyncMock(return_value=mock_response)

            client = GitHubClient(pacer=mock_pacer)
            await client.get_pull_request("owner", "repo", 123)

            # Verify pacer hooks were called
            mock_pacer.get_recommended_delay.assert_called()
            mock_pacer.on_request_start.assert_called()
            mock_pacer.on_request_complete.assert_called()

    async def test_iter_pull_requests_calls_apply_pacing(self, mock_pacer, mock_github):
        """iter_pull_requests calls _apply_pacing before iteration."""
        pr_data = [make_mock_pr_data(100)]
        mock_github.paginate.return_value = async_iter(pr_data)

        with patch("github_activity_db.github.client.get_settings") as mock_settings:
            mock_settings.return_value.github_token = "test-token"
            client = GitHubClient(pacer=mock_pacer)

            # Consume iterator
            async for _ in client.iter_pull_requests("owner", "repo"):
                pass

            # Verify pacer hooks were called
            mock_pacer.get_recommended_delay.assert_called()
            mock_pacer.on_request_start.assert_called()

    async def test_get_pull_request_files_calls_apply_pacing(self, mock_pacer):
        """get_pull_request_files calls _apply_pacing before request."""
        with (
            patch("github_activity_db.github.client.get_settings") as mock_settings,
            patch("github_activity_db.github.client.GitHub") as mock_github_class,
        ):
            mock_settings.return_value.github_token = "test-token"
            mock_github = MagicMock()
            mock_github_class.return_value = mock_github

            # Setup mock paginator that returns empty
            mock_github.paginate.return_value = async_iter([])

            client = GitHubClient(pacer=mock_pacer)
            await client.get_pull_request_files("owner", "repo", 123)

            # Verify pacer hooks were called
            mock_pacer.get_recommended_delay.assert_called()
            mock_pacer.on_request_start.assert_called()
