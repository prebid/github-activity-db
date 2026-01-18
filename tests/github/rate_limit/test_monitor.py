"""Unit tests for RateLimitMonitor class.

These tests verify the state machine behavior, threshold callbacks,
and PAT verification logic.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from github_activity_db.config import RateLimitConfig
from github_activity_db.github.rate_limit.monitor import RateLimitMonitor
from github_activity_db.github.rate_limit.schemas import (
    PoolRateLimit,
    RateLimitPool,
    RateLimitStatus,
)
from tests.fixtures.rate_limit_responses import (
    HEADERS_CRITICAL,
    HEADERS_EXHAUSTED,
    HEADERS_HEALTHY,
    HEADERS_UNAUTHENTICATED,
    HEADERS_WARNING,
    RATE_LIMIT_RESPONSE_HEALTHY,
    RATE_LIMIT_RESPONSE_UNAUTHENTICATED,
    make_rate_limit_headers,
)


class TestRateLimitMonitorInit:
    """Tests for monitor initialization."""

    def test_init_without_github_client(self) -> None:
        """Monitor can be created without GitHub client."""
        monitor = RateLimitMonitor()
        assert monitor.is_initialized is False
        assert monitor.snapshot is None

    def test_init_with_custom_config(self) -> None:
        """Monitor accepts custom configuration."""
        config = RateLimitConfig(
            healthy_threshold_pct=60.0,
            warning_threshold_pct=30.0,
            min_remaining_buffer=200,
        )
        monitor = RateLimitMonitor(config=config)
        assert monitor._config.healthy_threshold_pct == 60.0
        assert monitor._config.min_remaining_buffer == 200

    @pytest.mark.asyncio
    async def test_initialize_without_client(self) -> None:
        """Initialize without client just marks as initialized."""
        monitor = RateLimitMonitor()
        await monitor.initialize()
        assert monitor.is_initialized is True
        # Should still work, just no data
        assert monitor.snapshot is None

    @pytest.mark.asyncio
    async def test_initialize_with_mock_client(self) -> None:
        """Initialize with mock client fetches rate limits."""
        mock_github = MagicMock()
        mock_response = MagicMock()
        mock_response.parsed_data.model_dump.return_value = RATE_LIMIT_RESPONSE_HEALTHY
        mock_github.rest.rate_limit.async_get = AsyncMock(return_value=mock_response)

        monitor = RateLimitMonitor(github=mock_github)
        await monitor.initialize()

        assert monitor.is_initialized is True
        assert monitor.snapshot is not None
        assert monitor.token_info is not None
        assert monitor.token_info.is_pat is True

    @pytest.mark.asyncio
    async def test_initialize_detects_unauthenticated(self) -> None:
        """Initialize detects unauthenticated token from 60 limit."""
        mock_github = MagicMock()
        mock_response = MagicMock()
        mock_response.parsed_data.model_dump.return_value = RATE_LIMIT_RESPONSE_UNAUTHENTICATED
        mock_github.rest.rate_limit.async_get = AsyncMock(return_value=mock_response)

        monitor = RateLimitMonitor(github=mock_github)
        await monitor.initialize()

        assert monitor.token_info is not None
        assert monitor.token_info.is_pat is False
        assert monitor.token_info.rate_limit == 60

    @pytest.mark.asyncio
    async def test_initialize_idempotent(self) -> None:
        """Multiple initialize calls are idempotent."""
        mock_github = MagicMock()
        mock_response = MagicMock()
        mock_response.parsed_data.model_dump.return_value = RATE_LIMIT_RESPONSE_HEALTHY
        mock_github.rest.rate_limit.async_get = AsyncMock(return_value=mock_response)

        monitor = RateLimitMonitor(github=mock_github)
        await monitor.initialize()
        await monitor.initialize()  # Should not fetch again

        assert mock_github.rest.rate_limit.async_get.call_count == 1


class TestVerifyPAT:
    """Tests for PAT verification."""

    def test_verify_pat_not_initialized(self) -> None:
        """verify_pat returns False if not initialized."""
        monitor = RateLimitMonitor()
        assert monitor.verify_pat() is False

    @pytest.mark.asyncio
    async def test_verify_pat_authenticated(self) -> None:
        """verify_pat returns True for authenticated PAT."""
        mock_github = MagicMock()
        mock_response = MagicMock()
        mock_response.parsed_data.model_dump.return_value = RATE_LIMIT_RESPONSE_HEALTHY
        mock_github.rest.rate_limit.async_get = AsyncMock(return_value=mock_response)

        monitor = RateLimitMonitor(github=mock_github)
        await monitor.initialize()

        assert monitor.verify_pat() is True

    @pytest.mark.asyncio
    async def test_verify_pat_unauthenticated(self) -> None:
        """verify_pat returns False for unauthenticated."""
        mock_github = MagicMock()
        mock_response = MagicMock()
        mock_response.parsed_data.model_dump.return_value = RATE_LIMIT_RESPONSE_UNAUTHENTICATED
        mock_github.rest.rate_limit.async_get = AsyncMock(return_value=mock_response)

        monitor = RateLimitMonitor(github=mock_github)
        await monitor.initialize()

        assert monitor.verify_pat() is False

    def test_verify_pat_from_headers(self) -> None:
        """verify_pat works after updating from headers."""
        monitor = RateLimitMonitor()
        monitor.update_from_headers(HEADERS_HEALTHY)

        assert monitor.verify_pat() is True

    def test_verify_pat_from_unauthenticated_headers(self) -> None:
        """verify_pat returns False for unauthenticated headers."""
        monitor = RateLimitMonitor()
        monitor.update_from_headers(HEADERS_UNAUTHENTICATED)

        assert monitor.verify_pat() is False


class TestUpdateFromHeaders:
    """Tests for passive header tracking."""

    def test_update_from_headers_basic(self) -> None:
        """Basic header update creates snapshot."""
        monitor = RateLimitMonitor()
        monitor.update_from_headers(HEADERS_HEALTHY)

        assert monitor.snapshot is not None
        core = monitor.snapshot.get_core()
        assert core is not None
        assert core.remaining == 4500

    def test_update_from_headers_sets_initialized(self) -> None:
        """Header update sets initialized flag."""
        monitor = RateLimitMonitor()
        assert monitor.is_initialized is False

        monitor.update_from_headers(HEADERS_HEALTHY)

        assert monitor.is_initialized is True

    def test_update_from_headers_merges_pools(self) -> None:
        """Multiple updates merge different pools."""
        monitor = RateLimitMonitor()

        # First update with core
        monitor.update_from_headers(HEADERS_HEALTHY)
        assert monitor.snapshot is not None
        assert RateLimitPool.CORE in monitor.snapshot.pools

        # Second update with search
        search_headers = make_rate_limit_headers(remaining=28, limit=30, resource="search")
        monitor.update_from_headers(search_headers)

        # Both should be present
        assert monitor.snapshot is not None
        assert RateLimitPool.CORE in monitor.snapshot.pools
        assert RateLimitPool.SEARCH in monitor.snapshot.pools

    def test_update_from_headers_overwrites_same_pool(self) -> None:
        """Same pool update overwrites previous data."""
        monitor = RateLimitMonitor()

        # First update
        headers1 = make_rate_limit_headers(remaining=4500)
        monitor.update_from_headers(headers1)

        # Second update with different remaining
        headers2 = make_rate_limit_headers(remaining=4000)
        monitor.update_from_headers(headers2)

        core = monitor.get_pool_limit()
        assert core is not None
        assert core.remaining == 4000

    def test_update_from_headers_respects_config(self) -> None:
        """Header tracking can be disabled via config."""
        config = RateLimitConfig(track_from_headers=False)
        monitor = RateLimitMonitor(config=config)

        monitor.update_from_headers(HEADERS_HEALTHY)

        assert monitor.snapshot is None


class TestStatusTransitions:
    """Tests for rate limit status state machine."""

    def test_get_status_healthy(self) -> None:
        """Status is HEALTHY when > 50% remaining."""
        monitor = RateLimitMonitor()
        monitor.update_from_headers(HEADERS_HEALTHY)

        assert monitor.get_status() == RateLimitStatus.HEALTHY

    def test_get_status_warning(self) -> None:
        """Status is WARNING when 20-50% remaining."""
        monitor = RateLimitMonitor()
        monitor.update_from_headers(HEADERS_WARNING)

        assert monitor.get_status() == RateLimitStatus.WARNING

    def test_get_status_critical(self) -> None:
        """Status is CRITICAL when < 20% remaining."""
        monitor = RateLimitMonitor()
        monitor.update_from_headers(HEADERS_CRITICAL)

        assert monitor.get_status() == RateLimitStatus.CRITICAL

    def test_get_status_exhausted(self) -> None:
        """Status is EXHAUSTED when 0 remaining."""
        monitor = RateLimitMonitor()
        monitor.update_from_headers(HEADERS_EXHAUSTED)

        assert monitor.get_status() == RateLimitStatus.EXHAUSTED

    def test_get_status_no_data(self) -> None:
        """Status defaults to HEALTHY when no data."""
        monitor = RateLimitMonitor()
        assert monitor.get_status() == RateLimitStatus.HEALTHY

    def test_get_status_custom_thresholds(self) -> None:
        """Status respects custom thresholds."""
        # 30% remaining would normally be WARNING with default thresholds
        headers = make_rate_limit_headers(remaining=1500, limit=5000, used=3500)

        # With default config (50/20/5), 30% is WARNING
        monitor_default = RateLimitMonitor()
        monitor_default.update_from_headers(headers)
        assert monitor_default.get_status() == RateLimitStatus.WARNING

        # With custom config (30% healthy), 30% is HEALTHY
        config = RateLimitConfig(healthy_threshold_pct=30.0)
        monitor_custom = RateLimitMonitor(config=config)
        monitor_custom.update_from_headers(headers)
        assert monitor_custom.get_status() == RateLimitStatus.HEALTHY


class TestCanMakeRequest:
    """Tests for can_make_request method."""

    def test_can_make_request_healthy(self) -> None:
        """Can make request when healthy."""
        monitor = RateLimitMonitor()
        monitor.update_from_headers(HEADERS_HEALTHY)

        assert monitor.can_make_request() is True
        assert monitor.can_make_request(count=100) is True

    def test_can_make_request_respects_buffer(self) -> None:
        """Can make request respects buffer configuration."""
        config = RateLimitConfig(min_remaining_buffer=100)
        monitor = RateLimitMonitor(config=config)

        # 150 remaining, buffer is 100, so only 50 available
        headers = make_rate_limit_headers(remaining=150)
        monitor.update_from_headers(headers)

        assert monitor.can_make_request(count=50) is True
        assert monitor.can_make_request(count=51) is False

    def test_can_make_request_exhausted(self) -> None:
        """Cannot make request when exhausted."""
        monitor = RateLimitMonitor()
        monitor.update_from_headers(HEADERS_EXHAUSTED)

        assert monitor.can_make_request() is False

    def test_can_make_request_no_data(self) -> None:
        """Assumes OK when no data (with warning)."""
        monitor = RateLimitMonitor()

        with patch.object(RateLimitMonitor, "_check_thresholds_sync"):
            # Should return True but log warning
            assert monitor.can_make_request() is True


class TestRequestsAvailable:
    """Tests for requests_available method."""

    def test_requests_available_healthy(self) -> None:
        """Returns remaining minus buffer when healthy."""
        config = RateLimitConfig(min_remaining_buffer=100)
        monitor = RateLimitMonitor(config=config)
        monitor.update_from_headers(HEADERS_HEALTHY)  # 4500 remaining

        # 4500 - 100 buffer = 4400
        assert monitor.requests_available() == 4400

    def test_requests_available_below_buffer(self) -> None:
        """Returns 0 when remaining is below buffer."""
        config = RateLimitConfig(min_remaining_buffer=100)
        monitor = RateLimitMonitor(config=config)

        headers = make_rate_limit_headers(remaining=50)
        monitor.update_from_headers(headers)

        assert monitor.requests_available() == 0

    def test_requests_available_no_data(self) -> None:
        """Returns 0 when no data."""
        monitor = RateLimitMonitor()
        assert monitor.requests_available() == 0


class TestTimeUntilReset:
    """Tests for time_until_reset method."""

    def test_time_until_reset_future(self) -> None:
        """Returns positive seconds for future reset."""
        monitor = RateLimitMonitor()

        # Reset in 1 hour
        headers = make_rate_limit_headers(reset_in_seconds=3600)
        monitor.update_from_headers(headers)

        # Should be around 3600, allow some tolerance
        assert 3595 <= monitor.time_until_reset() <= 3605

    def test_time_until_reset_past(self) -> None:
        """Returns 0 for past reset."""
        monitor = RateLimitMonitor()

        # Reset was 1 hour ago (negative)
        headers = make_rate_limit_headers(reset_in_seconds=-3600)
        monitor.update_from_headers(headers)

        assert monitor.time_until_reset() == 0

    def test_time_until_reset_no_data(self) -> None:
        """Returns 0 when no data."""
        monitor = RateLimitMonitor()
        assert monitor.time_until_reset() == 0


class TestThresholdCallbacks:
    """Tests for threshold crossing callbacks."""

    def test_callback_fires_on_degradation(self) -> None:
        """Callback fires when status degrades."""
        callback_fired = False
        received_status: RateLimitStatus | None = None

        def callback(limit: PoolRateLimit, status: RateLimitStatus) -> None:
            nonlocal callback_fired, received_status
            callback_fired = True
            received_status = status

        monitor = RateLimitMonitor()
        monitor.on_threshold_crossed(callback)

        # Start healthy
        monitor.update_from_headers(HEADERS_HEALTHY)
        assert callback_fired is False  # No degradation yet

        # Degrade to warning
        monitor.update_from_headers(HEADERS_WARNING)
        assert callback_fired is True
        assert received_status == RateLimitStatus.WARNING

    def test_callback_not_fired_on_improvement(self) -> None:
        """Callback does NOT fire when status improves."""
        callback_count = 0

        def callback(limit: PoolRateLimit, status: RateLimitStatus) -> None:
            nonlocal callback_count
            callback_count += 1

        monitor = RateLimitMonitor()
        monitor.on_threshold_crossed(callback)

        # Start critical
        monitor.update_from_headers(HEADERS_CRITICAL)
        # First update, no previous status, callback fires
        assert callback_count == 1

        # Improve to warning
        monitor.update_from_headers(HEADERS_WARNING)
        # No callback - this is improvement
        assert callback_count == 1

    def test_callback_fires_for_each_degradation(self) -> None:
        """Callback fires for each degradation step."""
        statuses: list[RateLimitStatus] = []

        def callback(limit: PoolRateLimit, status: RateLimitStatus) -> None:
            statuses.append(status)

        monitor = RateLimitMonitor()
        monitor.on_threshold_crossed(callback)

        # Progress through degradation
        monitor.update_from_headers(HEADERS_HEALTHY)
        monitor.update_from_headers(HEADERS_WARNING)
        monitor.update_from_headers(HEADERS_CRITICAL)
        monitor.update_from_headers(HEADERS_EXHAUSTED)

        assert statuses == [
            RateLimitStatus.WARNING,
            RateLimitStatus.CRITICAL,
            RateLimitStatus.EXHAUSTED,
        ]

    @pytest.mark.asyncio
    async def test_async_callback_supported(self) -> None:
        """Async callbacks are supported."""
        callback_fired = False

        async def async_callback(limit: PoolRateLimit, status: RateLimitStatus) -> None:
            nonlocal callback_fired
            callback_fired = True

        monitor = RateLimitMonitor()
        monitor.on_threshold_crossed(async_callback)

        monitor.update_from_headers(HEADERS_HEALTHY)
        monitor.update_from_headers(HEADERS_WARNING)

        # Give async task time to run
        import asyncio

        await asyncio.sleep(0.1)

        assert callback_fired is True

    def test_remove_callback(self) -> None:
        """Callbacks can be removed."""
        callback_count = 0

        def callback(limit: PoolRateLimit, status: RateLimitStatus) -> None:
            nonlocal callback_count
            callback_count += 1

        monitor = RateLimitMonitor()
        monitor.on_threshold_crossed(callback)

        monitor.update_from_headers(HEADERS_HEALTHY)
        monitor.update_from_headers(HEADERS_WARNING)
        assert callback_count == 1

        # Remove callback
        result = monitor.remove_callback(callback)
        assert result is True

        # Further degradation should not fire callback
        monitor.update_from_headers(HEADERS_CRITICAL)
        assert callback_count == 1

    def test_remove_nonexistent_callback(self) -> None:
        """Removing nonexistent callback returns False."""
        monitor = RateLimitMonitor()

        def callback(limit: PoolRateLimit, status: RateLimitStatus) -> None:
            pass

        result = monitor.remove_callback(callback)
        assert result is False


class TestRefresh:
    """Tests for explicit refresh."""

    @pytest.mark.asyncio
    async def test_refresh_fetches_new_data(self) -> None:
        """Refresh fetches new rate limits from API."""
        mock_github = MagicMock()
        mock_response = MagicMock()
        mock_response.parsed_data.model_dump.return_value = RATE_LIMIT_RESPONSE_HEALTHY
        mock_github.rest.rate_limit.async_get = AsyncMock(return_value=mock_response)

        monitor = RateLimitMonitor(github=mock_github)

        snapshot = await monitor.refresh()

        assert snapshot is not None
        assert RateLimitPool.CORE in snapshot.pools
        mock_github.rest.rate_limit.async_get.assert_called_once()

    @pytest.mark.asyncio
    async def test_refresh_without_client_raises(self) -> None:
        """Refresh without client raises RuntimeError."""
        monitor = RateLimitMonitor()

        with pytest.raises(RuntimeError, match="Cannot refresh without GitHub client"):
            await monitor.refresh()


class TestStateTransitionsIntegration:
    """Integration tests for rate limit state machine transitions.

    These tests verify the complete state machine behavior including
    recovery scenarios that simulate real-world usage patterns.
    """

    def test_healthy_to_warning_transition(self) -> None:
        """Track explicit transition from HEALTHY to WARNING."""
        monitor = RateLimitMonitor()

        # Start healthy using predefined headers
        monitor.update_from_headers(HEADERS_HEALTHY)
        assert monitor.get_status() == RateLimitStatus.HEALTHY

        # Transition to WARNING using predefined headers
        monitor.update_from_headers(HEADERS_WARNING)
        assert monitor.get_status() == RateLimitStatus.WARNING

        # Verify pool limit reflects the change
        pool_limit = monitor.get_pool_limit()
        assert pool_limit is not None
        assert pool_limit.remaining == 1500

    def test_warning_to_critical_transition(self) -> None:
        """Track explicit transition from WARNING to CRITICAL."""
        monitor = RateLimitMonitor()

        # Start at WARNING
        monitor.update_from_headers(HEADERS_WARNING)
        assert monitor.get_status() == RateLimitStatus.WARNING

        # Drop to CRITICAL
        monitor.update_from_headers(HEADERS_CRITICAL)
        assert monitor.get_status() == RateLimitStatus.CRITICAL

    def test_recovery_after_reset(self) -> None:
        """Verify state recovers after rate limit reset."""
        monitor = RateLimitMonitor()

        # Start exhausted
        monitor.update_from_headers(HEADERS_EXHAUSTED)
        assert monitor.get_status() == RateLimitStatus.EXHAUSTED
        assert monitor.can_make_request() is False

        # Simulate reset - full quota restored
        monitor.update_from_headers(HEADERS_HEALTHY)

        # Should be healthy again
        assert monitor.get_status() == RateLimitStatus.HEALTHY
        assert monitor.can_make_request() is True

    def test_gradual_exhaustion_scenario(self) -> None:
        """Simulate gradual rate limit exhaustion during heavy usage."""
        monitor = RateLimitMonitor()
        statuses: list[RateLimitStatus] = []

        def track_status(limit: PoolRateLimit, status: RateLimitStatus) -> None:
            statuses.append(status)

        monitor.on_threshold_crossed(track_status)

        # Simulate heavy API usage pattern using predefined headers
        monitor.update_from_headers(HEADERS_HEALTHY)  # 90% - HEALTHY
        monitor.update_from_headers(HEADERS_WARNING)  # 30% - WARNING (callback)
        monitor.update_from_headers(HEADERS_CRITICAL)  # 5% - CRITICAL (callback)
        monitor.update_from_headers(HEADERS_EXHAUSTED)  # 0% - EXHAUSTED (callback)

        # Should have recorded WARNING, CRITICAL, EXHAUSTED transitions
        assert RateLimitStatus.WARNING in statuses
        assert RateLimitStatus.CRITICAL in statuses
        assert RateLimitStatus.EXHAUSTED in statuses

    def test_partial_recovery_scenario(self) -> None:
        """Test partial recovery doesn't trigger callbacks."""
        monitor = RateLimitMonitor()
        callback_count = 0

        def count_callbacks(limit: PoolRateLimit, status: RateLimitStatus) -> None:
            nonlocal callback_count
            callback_count += 1

        monitor.on_threshold_crossed(count_callbacks)

        # Go to CRITICAL
        monitor.update_from_headers(HEADERS_CRITICAL)
        initial_count = callback_count  # Should be 1 (degradation from default HEALTHY)

        # Partial recovery to WARNING (improvement, no callback)
        monitor.update_from_headers(HEADERS_WARNING)
        assert callback_count == initial_count  # No new callback

        # Further recovery to HEALTHY (improvement, no callback)
        monitor.update_from_headers(HEADERS_HEALTHY)
        assert callback_count == initial_count  # Still no new callback

        # Now degrade again - should trigger callback
        monitor.update_from_headers(HEADERS_WARNING)
        assert callback_count == initial_count + 1  # New callback


class TestToDict:
    """Tests for to_dict export."""

    def test_to_dict_not_initialized(self) -> None:
        """to_dict returns minimal data when not initialized."""
        monitor = RateLimitMonitor()
        data = monitor.to_dict()

        assert data["initialized"] is False
        assert data["pools"] == {}

    def test_to_dict_with_data(self) -> None:
        """to_dict exports all rate limit data."""
        monitor = RateLimitMonitor()
        monitor.update_from_headers(HEADERS_HEALTHY)

        data = monitor.to_dict()

        assert data["initialized"] is True
        assert "timestamp" in data
        assert "core" in data["pools"]

        core = data["pools"]["core"]
        assert core["limit"] == 5000
        assert core["remaining"] == 4500
        assert core["status"] == "healthy"
        assert "usage_percent" in core
        assert "seconds_until_reset" in core

    def test_to_dict_includes_token_info(self) -> None:
        """to_dict includes token information."""
        monitor = RateLimitMonitor()
        monitor.update_from_headers(HEADERS_HEALTHY)

        data = monitor.to_dict()

        assert data["token"] is not None
        assert data["token"]["is_authenticated"] is True
        assert data["token"]["rate_limit"] == 5000


# =============================================================================
# HIGH PRIORITY: Error Handling Tests
# =============================================================================


class TestFetchRateLimitsErrors:
    """Tests for _fetch_rate_limits() error handling."""

    @pytest.mark.asyncio
    async def test_fetch_rate_limits_api_exception_propagates(self) -> None:
        """_fetch_rate_limits() propagates API exceptions."""
        mock_github = MagicMock()
        mock_github.rest.rate_limit.async_get = AsyncMock(
            side_effect=Exception("API connection failed")
        )

        monitor = RateLimitMonitor(github=mock_github)

        with pytest.raises(Exception, match="API connection failed"):
            await monitor.initialize()

    @pytest.mark.asyncio
    async def test_fetch_rate_limits_network_error(self) -> None:
        """_fetch_rate_limits() handles network errors."""
        mock_github = MagicMock()
        mock_github.rest.rate_limit.async_get = AsyncMock(
            side_effect=ConnectionError("Network unreachable")
        )

        monitor = RateLimitMonitor(github=mock_github)

        with pytest.raises(ConnectionError, match="Network unreachable"):
            await monitor.initialize()

        # Monitor should not be initialized after failure
        assert monitor.is_initialized is False

    @pytest.mark.asyncio
    async def test_fetch_rate_limits_timeout_error(self) -> None:
        """_fetch_rate_limits() handles timeout errors."""
        mock_github = MagicMock()
        mock_github.rest.rate_limit.async_get = AsyncMock(
            side_effect=TimeoutError("Request timed out")
        )

        monitor = RateLimitMonitor(github=mock_github)

        with pytest.raises(TimeoutError, match="Request timed out"):
            await monitor.initialize()

    @pytest.mark.asyncio
    async def test_fetch_rate_limits_malformed_response(self) -> None:
        """_fetch_rate_limits() handles malformed API response gracefully."""
        mock_github = MagicMock()
        mock_response = MagicMock()
        # Response without 'resources' key - schema handles gracefully
        mock_response.parsed_data.model_dump.return_value = {"invalid": "data"}
        mock_github.rest.rate_limit.async_get = AsyncMock(return_value=mock_response)

        monitor = RateLimitMonitor(github=mock_github)

        # Should not raise - schema handles missing fields with defaults
        await monitor.initialize()

        # Monitor should be initialized but with empty pools
        assert monitor.is_initialized is True
        assert monitor.snapshot is not None


class TestCallbackErrorHandling:
    """Tests for callback exception handling."""

    def test_sync_callback_exception_caught(self) -> None:
        """Sync callback exceptions are caught, not propagated."""
        monitor = RateLimitMonitor()

        def failing_callback(limit: PoolRateLimit, status: RateLimitStatus) -> None:
            raise ValueError("Callback exploded!")

        monitor.on_threshold_crossed(failing_callback)

        # Should not raise - exception should be caught
        monitor.update_from_headers(HEADERS_WARNING)

        # Monitor should still work
        assert monitor.get_status() == RateLimitStatus.WARNING

    def test_failing_callback_doesnt_prevent_other_callbacks(self) -> None:
        """One failing callback doesn't block others."""
        monitor = RateLimitMonitor()
        success_called = []

        def failing_callback(limit: PoolRateLimit, status: RateLimitStatus) -> None:
            raise RuntimeError("First callback fails")

        def success_callback(limit: PoolRateLimit, status: RateLimitStatus) -> None:
            success_called.append(status)

        # Register failing callback first, then success callback
        monitor.on_threshold_crossed(failing_callback)
        monitor.on_threshold_crossed(success_callback)

        # Trigger degradation
        monitor.update_from_headers(HEADERS_WARNING)

        # Success callback should still have been called
        assert len(success_called) == 1
        assert success_called[0] == RateLimitStatus.WARNING

    @pytest.mark.asyncio
    async def test_async_callback_exception_handled(self) -> None:
        """Async callback exceptions are handled gracefully."""
        import asyncio

        monitor = RateLimitMonitor()

        async def failing_async_callback(limit: PoolRateLimit, status: RateLimitStatus) -> None:
            raise RuntimeError("Async callback exploded!")

        monitor.on_threshold_crossed(failing_async_callback)

        # Should not raise
        monitor.update_from_headers(HEADERS_WARNING)

        # Give async task time to complete/fail
        await asyncio.sleep(0.05)

        # Monitor should still work
        assert monitor.get_status() == RateLimitStatus.WARNING


class TestRefreshErrorScenarios:
    """Tests for refresh() error handling."""

    @pytest.mark.asyncio
    async def test_refresh_preserves_state_on_error(self) -> None:
        """Refresh error should preserve existing snapshot state."""
        mock_github = MagicMock()
        mock_response = MagicMock()
        mock_response.parsed_data.model_dump.return_value = RATE_LIMIT_RESPONSE_HEALTHY
        mock_github.rest.rate_limit.async_get = AsyncMock(return_value=mock_response)

        monitor = RateLimitMonitor(github=mock_github)
        await monitor.initialize()

        # Capture original state
        pool_limit = monitor.get_pool_limit(RateLimitPool.CORE)
        assert pool_limit is not None
        original_remaining = pool_limit.remaining

        # Now make refresh fail
        mock_github.rest.rate_limit.async_get = AsyncMock(side_effect=Exception("Refresh failed"))

        with pytest.raises(Exception, match="Refresh failed"):
            await monitor.refresh()

        # Original snapshot should still be accessible
        assert monitor.snapshot is not None
        pool_limit_after = monitor.get_pool_limit(RateLimitPool.CORE)
        assert pool_limit_after is not None
        assert pool_limit_after.remaining == original_remaining

    @pytest.mark.asyncio
    async def test_refresh_without_client_raises(self) -> None:
        """Refresh without GitHub client raises RuntimeError."""
        monitor = RateLimitMonitor()
        await monitor.initialize()  # Initialize without client

        with pytest.raises(RuntimeError, match="Cannot refresh without GitHub client"):
            await monitor.refresh()

    @pytest.mark.asyncio
    async def test_multiple_refresh_failures_recoverable(self) -> None:
        """Monitor recovers after multiple refresh failures."""
        mock_github = MagicMock()
        call_count = 0

        async def flaky_get() -> MagicMock:
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise Exception(f"Failure #{call_count}")
            mock_resp = MagicMock()
            mock_resp.parsed_data.model_dump.return_value = RATE_LIMIT_RESPONSE_HEALTHY
            return mock_resp

        mock_github.rest.rate_limit.async_get = AsyncMock(side_effect=flaky_get)

        monitor = RateLimitMonitor(github=mock_github)

        # First two calls fail
        with pytest.raises(Exception, match="Failure #1"):
            await monitor.initialize()
        with pytest.raises(Exception, match="Failure #2"):
            await monitor.initialize()

        # Reset initialized flag to allow retry
        monitor._initialized = False

        # Third call succeeds
        await monitor.initialize()
        assert monitor.is_initialized is True
        assert monitor.snapshot is not None


# =============================================================================
# MEDIUM PRIORITY: Multi-Pool Tracking Tests
# =============================================================================


class TestMultiplePoolTracking:
    """Tests for tracking multiple rate limit pools."""

    def test_get_status_for_each_pool_type(self) -> None:
        """get_status works for all RateLimitPool values."""
        monitor = RateLimitMonitor()

        # Update with core pool
        monitor.update_from_headers(HEADERS_HEALTHY)

        # Update with search pool (different remaining)
        monitor.update_from_headers(
            make_rate_limit_headers(remaining=5, limit=30, used=25, resource="search")
        )

        # Core should be healthy, search should be critical (5/30 = 16.7%)
        assert monitor.get_status(RateLimitPool.CORE) == RateLimitStatus.HEALTHY
        assert monitor.get_status(RateLimitPool.SEARCH) == RateLimitStatus.CRITICAL

    def test_can_make_request_different_pools(self) -> None:
        """can_make_request respects per-pool remaining counts."""
        monitor = RateLimitMonitor()

        # Core: healthy
        monitor.update_from_headers(HEADERS_HEALTHY)

        # Search: exhausted
        monitor.update_from_headers(
            make_rate_limit_headers(remaining=0, limit=30, used=30, resource="search")
        )

        assert monitor.can_make_request(RateLimitPool.CORE) is True
        assert monitor.can_make_request(RateLimitPool.SEARCH) is False

    def test_requests_available_per_pool(self) -> None:
        """requests_available returns correct counts per pool."""
        monitor = RateLimitMonitor()

        monitor.update_from_headers(
            make_rate_limit_headers(remaining=4500, limit=5000, resource="core")
        )
        monitor.update_from_headers(
            make_rate_limit_headers(remaining=20, limit=30, resource="search")
        )

        # Default buffer is 100, so:
        # Core: 4500 - 100 = 4400
        # Search: 20 - 100 = -80 -> 0 (clamped)
        assert monitor.requests_available(RateLimitPool.CORE) == 4400
        assert monitor.requests_available(RateLimitPool.SEARCH) == 0

    def test_time_until_reset_varies_by_pool(self) -> None:
        """Different pools have different reset times."""
        monitor = RateLimitMonitor()

        # Core: reset in 1 hour
        monitor.update_from_headers(
            make_rate_limit_headers(
                remaining=4500, limit=5000, reset_in_seconds=3600, resource="core"
            )
        )
        # Search: reset in 1 minute
        monitor.update_from_headers(
            make_rate_limit_headers(remaining=20, limit=30, reset_in_seconds=60, resource="search")
        )

        core_reset = monitor.time_until_reset(RateLimitPool.CORE)
        search_reset = monitor.time_until_reset(RateLimitPool.SEARCH)

        # Allow 5 second tolerance for test execution time
        assert 3590 <= core_reset <= 3605
        assert 55 <= search_reset <= 65

    def test_status_transitions_tracked_per_pool(self) -> None:
        """Status transitions tracked independently per pool."""
        monitor = RateLimitMonitor()
        callbacks_received: list[tuple[str, RateLimitStatus]] = []

        def track_callback(limit: PoolRateLimit, status: RateLimitStatus) -> None:
            # Determine pool from limit value
            pool_name = "search" if limit.limit == 30 else "core"
            callbacks_received.append((pool_name, status))

        monitor.on_threshold_crossed(track_callback)

        # Degrade core to WARNING
        monitor.update_from_headers(HEADERS_WARNING)
        assert len(callbacks_received) == 1
        assert callbacks_received[0] == ("core", RateLimitStatus.WARNING)

        # Degrade search to CRITICAL (independent)
        monitor.update_from_headers(
            make_rate_limit_headers(remaining=2, limit=30, used=28, resource="search")
        )
        assert len(callbacks_received) == 2
        assert callbacks_received[1] == ("search", RateLimitStatus.CRITICAL)

    def test_to_dict_includes_all_pools(self) -> None:
        """to_dict exports data for all tracked pools."""
        monitor = RateLimitMonitor()

        monitor.update_from_headers(HEADERS_HEALTHY)  # core
        monitor.update_from_headers(
            make_rate_limit_headers(remaining=25, limit=30, resource="search")
        )
        monitor.update_from_headers(
            make_rate_limit_headers(remaining=4500, limit=5000, resource="graphql")
        )

        data = monitor.to_dict()

        assert "core" in data["pools"]
        assert "search" in data["pools"]
        assert "graphql" in data["pools"]
        assert len(data["pools"]) == 3
