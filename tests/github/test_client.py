"""Tests for GitHubClient.

Tests cover:
- Lazy iteration via iter_pull_requests
- Early termination of pagination
- Per-page pacing (every page gates through the pacer/bucket)
- Full PR fetch and 404 handling
- Rate-limit header extraction (per page, including paginated calls)
- Primary/Secondary rate-limit error mapping
- Context manager protocol
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from githubkit.exception import (
    PrimaryRateLimitExceeded,
    RequestFailed,
    SecondaryRateLimitExceeded,
)

from github_activity_db.github.client import GitHubClient
from github_activity_db.github.exceptions import (
    GitHubAuthenticationError,
    GitHubNotFoundError,
    GitHubRateLimitError,
)
from github_activity_db.github.pacing import RequestPacer
from github_activity_db.github.rate_limit import RateLimitMonitor, RateLimitPool
from tests.factories import make_github_pr


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def make_mock_pr_data(number: int, **overrides):
    """Create a MagicMock that behaves like a githubkit PR response item."""
    pr_dict = make_github_pr(number=number, **overrides)
    mock = MagicMock()
    mock.model_dump.return_value = pr_dict
    return mock


def make_paginated_response(items, headers=None):
    """Build a mock githubkit Response for a single page of a list endpoint."""
    resp = MagicMock()
    resp.parsed_data = list(items)
    resp.headers = headers or {
        "x-ratelimit-limit": "5000",
        "x-ratelimit-remaining": "4999",
        "x-ratelimit-reset": str(int(datetime.now(UTC).timestamp()) + 3600),
        "x-ratelimit-resource": "core",
    }
    return resp


# -----------------------------------------------------------------------------
# Test Fixtures
# -----------------------------------------------------------------------------
@pytest.fixture
def mock_github():
    """Patch the githubkit GitHub class so the client wraps a MagicMock."""
    with patch("github_activity_db.github.client.GitHub") as mock_class:
        mock_instance = MagicMock()
        mock_class.return_value = mock_instance
        yield mock_instance


@pytest.fixture
def rate_monitor():
    """Fresh RateLimitMonitor for header-extraction tests."""
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
                GitHubClient()

    def test_init_with_rate_monitor(self, rate_monitor):
        """Client accepts rate_monitor parameter."""
        with patch("github_activity_db.github.client.get_settings") as mock_settings:
            mock_settings.return_value.github_token = "test-token"
            client = GitHubClient(rate_monitor=rate_monitor)
            assert client.rate_monitor is rate_monitor


# -----------------------------------------------------------------------------
# Test: iter_pull_requests (Lazy Iteration)
# -----------------------------------------------------------------------------
class TestIterPullRequests:
    """Tests for lazy PR iteration over the paged ``async_list`` endpoint."""

    async def test_iter_yields_prs_lazily(self, mock_github):
        """iter_pull_requests yields PRs one at a time, single page."""
        page1 = [make_mock_pr_data(i) for i in [100, 101, 102]]
        mock_github.rest.pulls.async_list = AsyncMock(return_value=make_paginated_response(page1))

        with patch("github_activity_db.github.client.get_settings") as mock_settings:
            mock_settings.return_value.github_token = "test-token"
            client = GitHubClient()

            prs = []
            async for pr in client.iter_pull_requests("owner", "repo", per_page=100):
                prs.append(pr)

            assert [pr.number for pr in prs] == [100, 101, 102]

    async def test_iter_early_termination_stops_fetching(self, mock_github):
        """Breaking out of the iterator avoids fetching subsequent pages."""
        # Page 1 is full (100 items) so the paginator would request page 2.
        page1 = [make_mock_pr_data(i) for i in range(100, 200)]

        async_list = AsyncMock(return_value=make_paginated_response(page1))
        mock_github.rest.pulls.async_list = async_list

        with patch("github_activity_db.github.client.get_settings") as mock_settings:
            mock_settings.return_value.github_token = "test-token"
            client = GitHubClient()

            count = 0
            async for _ in client.iter_pull_requests("owner", "repo", per_page=100):
                count += 1
                if count >= 3:
                    break

            assert count == 3
            # Only the first page should have been fetched
            assert async_list.call_count == 1

    async def test_iter_paginates_until_short_page(self, mock_github):
        """Multi-page: keeps fetching until a page returns fewer than per_page."""
        page1 = [make_mock_pr_data(i) for i in range(100, 200)]  # 100 → full
        page2 = [make_mock_pr_data(i) for i in range(200, 250)]  # 50 → short, stop

        async_list = AsyncMock(
            side_effect=[
                make_paginated_response(page1),
                make_paginated_response(page2),
            ]
        )
        mock_github.rest.pulls.async_list = async_list

        with patch("github_activity_db.github.client.get_settings") as mock_settings:
            mock_settings.return_value.github_token = "test-token"
            client = GitHubClient()

            count = 0
            async for _ in client.iter_pull_requests("o", "r", per_page=100):
                count += 1

            assert count == 150
            assert async_list.call_count == 2
            # Page numbers should be 1 then 2
            assert async_list.call_args_list[0].kwargs["page"] == 1
            assert async_list.call_args_list[1].kwargs["page"] == 2

    async def test_iter_passes_parameters(self, mock_github):
        """iter_pull_requests forwards owner/repo/state/sort/direction."""
        async_list = AsyncMock(return_value=make_paginated_response([]))
        mock_github.rest.pulls.async_list = async_list

        with patch("github_activity_db.github.client.get_settings") as mock_settings:
            mock_settings.return_value.github_token = "test-token"
            client = GitHubClient()

            async for _ in client.iter_pull_requests(
                "prebid",
                "prebid-server",
                state="all",
                sort="updated",
                direction="asc",
                per_page=50,
            ):
                pass

            kwargs = async_list.call_args.kwargs
            assert kwargs["owner"] == "prebid"
            assert kwargs["repo"] == "prebid-server"
            assert kwargs["state"] == "all"
            assert kwargs["sort"] == "updated"
            assert kwargs["direction"] == "asc"
            assert kwargs["per_page"] == 50


# -----------------------------------------------------------------------------
# Test: get_pull_request
# -----------------------------------------------------------------------------
class TestGetPullRequest:
    """Tests for fetching single PR data."""

    async def test_get_pr_success(self, mock_github):
        """get_pull_request returns PR with full details."""
        pr_response = MagicMock()
        pr_response.parsed_data.model_dump.return_value = make_github_pr(number=123)
        mock_github.rest.pulls.async_get = AsyncMock(return_value=pr_response)

        with patch("github_activity_db.github.client.get_settings") as mock_settings:
            mock_settings.return_value.github_token = "test-token"
            client = GitHubClient()
            pr = await client.get_pull_request("owner", "repo", 123)

            assert pr.number == 123
            mock_github.rest.pulls.async_get.assert_called_once_with(
                owner="owner", repo="repo", pull_number=123
            )

    async def test_get_pr_not_found(self, mock_github):
        """get_pull_request raises GitHubNotFoundError for 404."""
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.headers = {}
        error = RequestFailed(mock_response)
        mock_github.rest.pulls.async_get = AsyncMock(side_effect=error)

        with patch("github_activity_db.github.client.get_settings") as mock_settings:
            mock_settings.return_value.github_token = "test-token"
            client = GitHubClient()
            with pytest.raises(GitHubNotFoundError) as exc_info:
                await client.get_pull_request("owner", "repo", 99999)

            assert "99999" in str(exc_info.value)
            assert "owner/repo" in str(exc_info.value)


# -----------------------------------------------------------------------------
# Test: Rate Limit Header Extraction
# -----------------------------------------------------------------------------
class TestRateLimitHeaderExtraction:
    """Tests for extracting rate-limit info from response headers."""

    async def test_updates_monitor_from_response(self, rate_monitor, mock_github):
        """Response headers update the rate monitor."""
        response = MagicMock()
        response.headers = {
            "x-ratelimit-limit": "5000",
            "x-ratelimit-remaining": "4999",
            "x-ratelimit-reset": str(int(datetime.now(UTC).timestamp()) + 3600),
            "x-ratelimit-resource": "core",
        }
        response.parsed_data.model_dump.return_value = make_github_pr(number=123)
        mock_github.rest.pulls.async_get = AsyncMock(return_value=response)

        with patch("github_activity_db.github.client.get_settings") as mock_settings:
            mock_settings.return_value.github_token = "test-token"
            client = GitHubClient(rate_monitor=rate_monitor)
            await client.get_pull_request("owner", "repo", 123)

        pool_limit = rate_monitor.get_pool_limit(RateLimitPool.CORE)
        assert pool_limit is not None
        assert pool_limit.remaining == 4999
        assert pool_limit.limit == 5000

    async def test_no_monitor_no_error(self, mock_github):
        """No rate monitor configured doesn't cause errors."""
        response = MagicMock()
        response.headers = {"x-ratelimit-remaining": "4999"}
        response.parsed_data.model_dump.return_value = make_github_pr(number=123)
        mock_github.rest.pulls.async_get = AsyncMock(return_value=response)

        with patch("github_activity_db.github.client.get_settings") as mock_settings:
            mock_settings.return_value.github_token = "test-token"
            client = GitHubClient(rate_monitor=None)
            await client.get_pull_request("owner", "repo", 123)

    async def test_error_response_does_not_crash_header_extraction(self, rate_monitor, mock_github):
        """Error responses with missing headers don't crash the handler."""
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.headers = {}
        error = RequestFailed(mock_response)
        mock_github.rest.pulls.async_get = AsyncMock(side_effect=error)

        with patch("github_activity_db.github.client.get_settings") as mock_settings:
            mock_settings.return_value.github_token = "test-token"
            client = GitHubClient(rate_monitor=rate_monitor)
            with pytest.raises(GitHubNotFoundError):
                await client.get_pull_request("owner", "repo", 99999)

    async def test_paginated_call_updates_monitor_per_page(self, rate_monitor, mock_github):
        """Each page of a paginated request feeds headers back to the monitor.

        This regression-tests a prior gap where ``githubkit.paginate`` was
        used directly and only the first page's headers reached the monitor.
        """
        page1_response = MagicMock()
        page1_response.parsed_data = [make_mock_pr_data(i) for i in range(100, 200)]
        page1_response.headers = {
            "x-ratelimit-limit": "5000",
            "x-ratelimit-remaining": "4900",
            "x-ratelimit-reset": str(int(datetime.now(UTC).timestamp()) + 3600),
            "x-ratelimit-resource": "core",
        }
        page2_response = MagicMock()
        page2_response.parsed_data = [make_mock_pr_data(i) for i in range(200, 220)]
        page2_response.headers = {
            "x-ratelimit-limit": "5000",
            "x-ratelimit-remaining": "4800",  # decreased
            "x-ratelimit-reset": str(int(datetime.now(UTC).timestamp()) + 3600),
            "x-ratelimit-resource": "core",
        }
        mock_github.rest.pulls.async_list = AsyncMock(side_effect=[page1_response, page2_response])

        with patch("github_activity_db.github.client.get_settings") as mock_settings:
            mock_settings.return_value.github_token = "test-token"
            client = GitHubClient(rate_monitor=rate_monitor)
            async for _ in client.iter_pull_requests("o", "r", per_page=100):
                pass

        # Final state must reflect page-2 headers
        pool_limit = rate_monitor.get_pool_limit(RateLimitPool.CORE)
        assert pool_limit is not None
        assert pool_limit.remaining == 4800


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
            _ = client._github

            async with client:
                assert client._client is not None

            assert client._client is None


# -----------------------------------------------------------------------------
# Test: Pacer Integration
# -----------------------------------------------------------------------------
class TestGitHubClientPacerIntegration:
    """Verify GitHubClient drives the pacer correctly.

    With the shared-bucket model the contract is: every API call ``await``s
    ``pacer.acquire()`` first, and every response (including each paginated
    page) feeds headers back via ``pacer.on_request_complete(headers)``.
    """

    @pytest.fixture
    def mock_pacer(self):
        """Mock RequestPacer with the new acquire-based API."""
        pacer = MagicMock(spec=RequestPacer)
        pacer.acquire = AsyncMock()
        pacer.on_request_complete = MagicMock()
        pacer.force_wait = MagicMock()
        pacer.force_wait_until = MagicMock()
        return pacer

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

    async def test_apply_pacing_calls_acquire(self, mock_pacer):
        """_apply_pacing delegates to pacer.acquire()."""
        with patch("github_activity_db.github.client.get_settings") as mock_settings:
            mock_settings.return_value.github_token = "test-token"
            client = GitHubClient(pacer=mock_pacer)
            await client._apply_pacing()
            mock_pacer.acquire.assert_awaited_once()

    async def test_apply_pacing_noop_when_no_pacer(self):
        """_apply_pacing is a no-op when no pacer is configured."""
        with patch("github_activity_db.github.client.get_settings") as mock_settings:
            mock_settings.return_value.github_token = "test-token"
            client = GitHubClient(pacer=None)
            await client._apply_pacing()  # must not raise

    def test_update_calls_pacer_on_request_complete(self, mock_pacer):
        """_update_rate_limit_from_response forwards headers to the pacer."""
        with patch("github_activity_db.github.client.get_settings") as mock_settings:
            mock_settings.return_value.github_token = "test-token"
            client = GitHubClient(pacer=mock_pacer)

            response = MagicMock()
            response.headers = {"x-ratelimit-remaining": "4999"}
            client._update_rate_limit_from_response(response)
            mock_pacer.on_request_complete.assert_called_once()
            (headers,) = mock_pacer.on_request_complete.call_args.args
            assert headers["x-ratelimit-remaining"] == "4999"

    def test_update_noop_when_no_pacer_or_monitor(self):
        """_update_rate_limit_from_response doesn't error when nothing configured."""
        with patch("github_activity_db.github.client.get_settings") as mock_settings:
            mock_settings.return_value.github_token = "test-token"
            client = GitHubClient(pacer=None)

            response = MagicMock()
            response.headers = {"x-ratelimit-remaining": "4999"}
            client._update_rate_limit_from_response(response)

    async def test_get_pull_request_acquires_token(self, mock_pacer, mock_github):
        """Single-PR fetch acquires a token before issuing the request."""
        response = MagicMock()
        response.headers = {"x-ratelimit-remaining": "4999"}
        response.parsed_data.model_dump.return_value = make_github_pr(number=123)
        mock_github.rest.pulls.async_get = AsyncMock(return_value=response)

        with patch("github_activity_db.github.client.get_settings") as mock_settings:
            mock_settings.return_value.github_token = "test-token"
            client = GitHubClient(pacer=mock_pacer)
            await client.get_pull_request("owner", "repo", 123)

        mock_pacer.acquire.assert_awaited()
        mock_pacer.on_request_complete.assert_called()

    async def test_iter_pull_requests_acquires_token_per_page(self, mock_pacer, mock_github):
        """Paginated iteration acquires once per page, not just per call."""
        page1 = [make_mock_pr_data(i) for i in range(100, 200)]  # 100 → full
        page2 = [make_mock_pr_data(i) for i in range(200, 220)]  # 20 → short, stop
        mock_github.rest.pulls.async_list = AsyncMock(
            side_effect=[
                make_paginated_response(page1),
                make_paginated_response(page2),
            ]
        )

        with patch("github_activity_db.github.client.get_settings") as mock_settings:
            mock_settings.return_value.github_token = "test-token"
            client = GitHubClient(pacer=mock_pacer)
            async for _ in client.iter_pull_requests("o", "r", per_page=100):
                pass

        # Two pages → two acquires AND two on_request_complete calls.
        assert mock_pacer.acquire.await_count == 2
        assert mock_pacer.on_request_complete.call_count == 2


# -----------------------------------------------------------------------------
# Test: Error Mapping (rate-limit subclasses)
# -----------------------------------------------------------------------------
class TestRateLimitErrorMapping:
    """Primary/Secondary rate-limit exceptions map to GitHubRateLimitError."""

    def _make_response(self, status: int = 403) -> MagicMock:
        resp = MagicMock()
        resp.status_code = status
        resp.headers = {
            "x-ratelimit-limit": "5000",
            "x-ratelimit-remaining": "0",
            "x-ratelimit-reset": str(int(datetime.now(UTC).timestamp()) + 600),
        }
        return resp

    async def test_primary_rate_limit_maps_to_rate_limit_error(self, mock_github):
        """PrimaryRateLimitExceeded → GitHubRateLimitError + pacer.force_wait."""
        from datetime import timedelta

        from github_activity_db.config import PacingConfig

        resp = self._make_response()
        err = PrimaryRateLimitExceeded(resp, timedelta(seconds=60))
        mock_github.rest.pulls.async_get = AsyncMock(side_effect=err)

        # Use a real pacer wrapping a real monitor so we can assert on its state.
        monitor = RateLimitMonitor()
        pacer = RequestPacer(monitor, config=PacingConfig())

        with patch("github_activity_db.github.client.get_settings") as mock_settings:
            mock_settings.return_value.github_token = "test-token"
            client = GitHubClient(pacer=pacer, rate_monitor=monitor)
            with pytest.raises(GitHubRateLimitError):
                await client.get_pull_request("owner", "repo", 1)

        # force_wait engaged → bucket reports forced wait active.
        assert pacer.is_forced_wait_active is True
        assert pacer.forced_wait_remaining > 50  # ~60s + 5s buffer

    async def test_secondary_rate_limit_maps_to_rate_limit_error(self, mock_github):
        """SecondaryRateLimitExceeded → GitHubRateLimitError + pacer.force_wait."""
        from datetime import timedelta

        from github_activity_db.config import PacingConfig

        resp = self._make_response()
        err = SecondaryRateLimitExceeded(resp, timedelta(seconds=30))
        mock_github.rest.pulls.async_get = AsyncMock(side_effect=err)

        monitor = RateLimitMonitor()
        pacer = RequestPacer(monitor, config=PacingConfig())

        with patch("github_activity_db.github.client.get_settings") as mock_settings:
            mock_settings.return_value.github_token = "test-token"
            client = GitHubClient(pacer=pacer, rate_monitor=monitor)
            with pytest.raises(GitHubRateLimitError):
                await client.get_pull_request("owner", "repo", 1)

        assert pacer.is_forced_wait_active is True

    async def test_rate_limit_with_no_pacer_does_not_crash(self, mock_github):
        """A primary/secondary rate-limit error must be safe with pacer=None.

        The error-handling path tries to forward the wait to the pacer; a
        guard prevents NPE when no pacer is configured.
        """
        from datetime import timedelta

        resp = self._make_response()
        err = PrimaryRateLimitExceeded(resp, timedelta(seconds=10))
        mock_github.rest.pulls.async_get = AsyncMock(side_effect=err)

        with patch("github_activity_db.github.client.get_settings") as mock_settings:
            mock_settings.return_value.github_token = "test-token"
            client = GitHubClient(pacer=None)
            with pytest.raises(GitHubRateLimitError) as exc_info:
                await client.get_pull_request("owner", "repo", 1)
            # The exception should still surface its reset_at for callers
            assert exc_info.value.reset_at is not None

    async def test_rate_limit_double_deadline_takes_longer(self, mock_github):
        """When the bucket already has a forced-wait and an error adds another,
        the longer deadline is preserved (monotonic guard).

        Order matters: first the response headers (with remaining=0) update
        the bucket via on_request_complete → forced_wait_until reset_at;
        then the explicit force_wait(retry_after + 5) runs. Whichever yields
        the later deadline is the one we keep.
        """
        from datetime import timedelta

        from github_activity_db.config import PacingConfig

        # Construct a 403 with a far-future reset (1 hour) but a short
        # Retry-After (5 seconds). The bucket should keep the 1-hour wait.
        resp = MagicMock()
        resp.status_code = 403
        far_reset = int(datetime.now(UTC).timestamp()) + 3600
        resp.headers = {
            "x-ratelimit-limit": "5000",
            "x-ratelimit-remaining": "0",
            "x-ratelimit-reset": str(far_reset),
        }
        err = PrimaryRateLimitExceeded(resp, timedelta(seconds=5))
        mock_github.rest.pulls.async_get = AsyncMock(side_effect=err)

        monitor = RateLimitMonitor()
        pacer = RequestPacer(monitor, config=PacingConfig())

        with patch("github_activity_db.github.client.get_settings") as mock_settings:
            mock_settings.return_value.github_token = "test-token"
            client = GitHubClient(pacer=pacer, rate_monitor=monitor)
            with pytest.raises(GitHubRateLimitError):
                await client.get_pull_request("owner", "repo", 1)

        # Forced wait should reflect the 1-hour reset_at, not the 5-second
        # Retry-After, because the monotonic guard keeps the later deadline.
        assert pacer.is_forced_wait_active is True
        assert pacer.forced_wait_remaining > 600


class TestPaginatorPerPageDefault:
    """Regression tests for ``_paginate_paced`` per_page semantics."""

    async def test_no_per_page_defaults_to_github_default(self, mock_github):
        """When the caller omits ``per_page``, paginator must not truncate.

        GitHub's documented default is 30. If the helper assumed 100, a
        full-30-item response would look "short" and we'd stop after page 1
        even when more data exists.
        """
        # Two full pages of 30 items each, then a short third page.
        page1 = [make_mock_pr_data(i) for i in range(1, 31)]  # 30 items, full
        page2 = [make_mock_pr_data(i) for i in range(31, 61)]  # 30 items, full
        page3 = [make_mock_pr_data(i) for i in range(61, 75)]  # 14 items, short
        async_list = AsyncMock(
            side_effect=[
                make_paginated_response(page1),
                make_paginated_response(page2),
                make_paginated_response(page3),
            ]
        )
        mock_github.rest.pulls.async_list = async_list

        with patch("github_activity_db.github.client.get_settings") as mock_settings:
            mock_settings.return_value.github_token = "test-token"
            client = GitHubClient()

            # Manually drive _paginate_paced WITHOUT passing per_page
            count = 0
            async for _ in client._paginate_paced(
                mock_github.rest.pulls.async_list, owner="o", repo="r"
            ):
                count += 1

            assert count == 74
            assert async_list.call_count == 3
