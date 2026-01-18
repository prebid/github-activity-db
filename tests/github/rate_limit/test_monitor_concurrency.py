"""Concurrency tests for RateLimitMonitor.

These tests verify thread-safety and concurrent access patterns
for the RateLimitMonitor class using asyncio.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from github_activity_db.github.rate_limit.monitor import RateLimitMonitor
from github_activity_db.github.rate_limit.schemas import (
    PoolRateLimit,
    RateLimitPool,
    RateLimitStatus,
)
from tests.fixtures.rate_limit_responses import (
    HEADERS_HEALTHY,
    HEADERS_WARNING,
    RATE_LIMIT_RESPONSE_HEALTHY,
    make_rate_limit_headers,
)


class TestRateLimitMonitorConcurrency:
    """Tests for concurrent access to RateLimitMonitor."""

    @pytest.mark.asyncio
    async def test_concurrent_update_from_headers_no_data_loss(self) -> None:
        """Multiple concurrent header updates should not lose data."""
        monitor = RateLimitMonitor()

        # Create 50 updates for different remaining values
        async def update_headers(remaining: int) -> None:
            headers = make_rate_limit_headers(
                remaining=remaining,
                limit=5000,
                used=5000 - remaining,
            )
            monitor.update_from_headers(headers)

        # Run 50 concurrent updates
        tasks = [update_headers(i * 100) for i in range(50)]
        await asyncio.gather(*tasks)

        # Monitor should have processed all updates without error
        assert monitor.is_initialized is True
        assert monitor.snapshot is not None

        # Final state should be one of the updates (last wins)
        core = monitor.get_pool_limit(RateLimitPool.CORE)
        assert core is not None
        assert core.limit == 5000

    @pytest.mark.asyncio
    async def test_concurrent_initialize_is_safe(self) -> None:
        """Multiple concurrent initialize calls should not corrupt state.

        Note: Initialize is not strictly idempotent due to check-before-lock,
        so concurrent calls may each make an API call. The key invariant is
        that the final state is valid and consistent.
        """
        mock_github = MagicMock()
        call_count = 0

        async def mock_get() -> MagicMock:
            nonlocal call_count
            call_count += 1
            # Add small delay to increase chance of race conditions
            await asyncio.sleep(0.01)
            mock_resp = MagicMock()
            mock_resp.parsed_data.model_dump.return_value = RATE_LIMIT_RESPONSE_HEALTHY
            return mock_resp

        mock_github.rest.rate_limit.async_get = AsyncMock(side_effect=mock_get)

        monitor = RateLimitMonitor(github=mock_github)

        # Start 10 concurrent initialize calls
        tasks = [monitor.initialize() for _ in range(10)]
        await asyncio.gather(*tasks)

        # All calls should complete without error and leave valid state
        assert monitor.is_initialized is True
        assert monitor.snapshot is not None
        # At least one call was made, but may be more due to race condition
        assert call_count >= 1

    @pytest.mark.asyncio
    async def test_concurrent_refresh_serializes_correctly(self) -> None:
        """Concurrent refresh calls should serialize via lock."""
        mock_github = MagicMock()
        call_count = 0
        max_concurrent = 0
        current_concurrent = 0
        lock = asyncio.Lock()

        async def mock_get() -> MagicMock:
            nonlocal call_count, max_concurrent, current_concurrent

            async with lock:
                current_concurrent += 1
                if current_concurrent > max_concurrent:
                    max_concurrent = current_concurrent

            # Simulate API latency
            await asyncio.sleep(0.05)

            async with lock:
                current_concurrent -= 1
                call_count += 1

            mock_resp = MagicMock()
            mock_resp.parsed_data.model_dump.return_value = RATE_LIMIT_RESPONSE_HEALTHY
            return mock_resp

        mock_github.rest.rate_limit.async_get = AsyncMock(side_effect=mock_get)

        monitor = RateLimitMonitor(github=mock_github)
        await monitor.initialize()

        # Reset count after initialize
        call_count = 0
        max_concurrent = 0

        # Start 5 concurrent refresh calls
        tasks = [monitor.refresh() for _ in range(5)]
        await asyncio.gather(*tasks)

        # All 5 refresh calls should have completed
        assert call_count == 5

        # Due to lock in refresh(), max_concurrent should be 1
        assert max_concurrent == 1

    @pytest.mark.asyncio
    async def test_header_update_during_refresh(self) -> None:
        """Header updates during refresh should not corrupt state."""
        mock_github = MagicMock()
        refresh_started = asyncio.Event()
        refresh_continue = asyncio.Event()

        async def slow_get() -> MagicMock:
            refresh_started.set()
            await refresh_continue.wait()
            mock_resp = MagicMock()
            mock_resp.parsed_data.model_dump.return_value = RATE_LIMIT_RESPONSE_HEALTHY
            return mock_resp

        mock_github.rest.rate_limit.async_get = AsyncMock(side_effect=slow_get)

        monitor = RateLimitMonitor(github=mock_github)

        # Start refresh in background
        refresh_task = asyncio.create_task(monitor.initialize())

        # Wait for refresh to start
        await refresh_started.wait()

        # Update headers while refresh is in progress
        monitor.update_from_headers(HEADERS_WARNING)

        # Allow refresh to complete
        refresh_continue.set()
        await refresh_task

        # Both operations should have completed without error
        assert monitor.is_initialized is True
        assert monitor.snapshot is not None

    @pytest.mark.asyncio
    async def test_callback_execution_during_concurrent_updates(self) -> None:
        """Callbacks should fire correctly under concurrent updates."""
        monitor = RateLimitMonitor()
        callback_count = 0

        def track_callback(
            limit: PoolRateLimit, status: RateLimitStatus
        ) -> None:
            nonlocal callback_count
            # Note: This is sync, but we track to verify it's called correctly
            callback_count += 1

        monitor.on_threshold_crossed(track_callback)

        # First update to establish baseline (HEALTHY)
        monitor.update_from_headers(HEADERS_HEALTHY)

        async def degrade_to_warning() -> None:
            monitor.update_from_headers(HEADERS_WARNING)

        async def degrade_to_critical() -> None:
            monitor.update_from_headers(
                make_rate_limit_headers(remaining=100, limit=5000, used=4900)
            )

        # Run concurrent degradation updates
        # Due to status tracking, each unique degradation should trigger callback
        await asyncio.gather(
            degrade_to_warning(),
            degrade_to_critical(),
        )

        # At least one callback should have fired
        # (exact count depends on execution order)
        assert callback_count >= 1

    @pytest.mark.asyncio
    async def test_concurrent_pool_updates_maintain_isolation(self) -> None:
        """Updates to different pools should not interfere with each other."""
        monitor = RateLimitMonitor()

        async def update_core() -> None:
            for i in range(20):
                headers = make_rate_limit_headers(
                    remaining=4000 + i,
                    limit=5000,
                    resource="core",
                )
                monitor.update_from_headers(headers)
                await asyncio.sleep(0.001)

        async def update_search() -> None:
            for i in range(20):
                headers = make_rate_limit_headers(
                    remaining=20 + i % 10,
                    limit=30,
                    resource="search",
                )
                monitor.update_from_headers(headers)
                await asyncio.sleep(0.001)

        # Run concurrent updates to different pools
        await asyncio.gather(update_core(), update_search())

        # Both pools should exist and have valid data
        core = monitor.get_pool_limit(RateLimitPool.CORE)
        search = monitor.get_pool_limit(RateLimitPool.SEARCH)

        assert core is not None
        assert search is not None
        assert core.limit == 5000
        assert search.limit == 30

    @pytest.mark.asyncio
    async def test_snapshot_access_during_update(self) -> None:
        """Snapshot property access during updates should not raise."""
        monitor = RateLimitMonitor()
        monitor.update_from_headers(HEADERS_HEALTHY)

        errors: list[Exception] = []

        async def read_snapshot() -> None:
            for _ in range(100):
                try:
                    _ = monitor.snapshot
                    _ = monitor.get_status()
                    _ = monitor.can_make_request()
                except Exception as e:
                    errors.append(e)
                await asyncio.sleep(0.001)

        async def write_headers() -> None:
            for i in range(100):
                headers = make_rate_limit_headers(
                    remaining=4500 - i,
                    limit=5000,
                )
                monitor.update_from_headers(headers)
                await asyncio.sleep(0.001)

        # Run concurrent reads and writes
        await asyncio.gather(read_snapshot(), write_headers())

        # No errors should have occurred
        assert len(errors) == 0
