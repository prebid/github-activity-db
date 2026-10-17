"""The rate-limit budget pool (plan 5.3 step 4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from github_activity_db.source.budget import BudgetPool

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
SOON = NOW + timedelta(minutes=10)
LATER = NOW + timedelta(minutes=50)


def test_no_reading_never_blocks() -> None:
    assert BudgetPool(reserve=100).blocked_until(NOW) is None


def test_blocks_below_the_reserve_until_the_reset() -> None:
    pool = BudgetPool(reserve=100)
    pool.record(99, SOON)
    assert pool.blocked_until(NOW) == SOON
    pool_at_reserve = BudgetPool(reserve=100)
    pool_at_reserve.record(100, SOON)
    assert pool_at_reserve.blocked_until(NOW) is None


def test_keeps_the_lowest_reading_per_window() -> None:
    pool = BudgetPool(reserve=100)
    pool.record(50, SOON)
    pool.record(4000, SOON)  # a later, higher reading of the same window does not raise it
    assert pool.blocked_until(NOW) == SOON


def test_alternating_windows_gate_on_the_lowest_unexpired() -> None:
    # One token's readings alternate between two windows (plan section 7, "Rate-limit windows").
    pool = BudgetPool(reserve=100)
    pool.record(4990, LATER)
    pool.record(40, SOON)
    pool.record(4989, LATER)
    assert pool.blocked_until(NOW) == SOON
    assert pool.blocked_until(SOON + timedelta(seconds=1)) is None


def test_expired_windows_are_dropped() -> None:
    pool = BudgetPool(reserve=100)
    pool.record(10, SOON)
    assert pool.blocked_until(SOON) is None
    assert pool.latest_reset(SOON) is None


def test_latest_reset_is_the_latest_pending_window() -> None:
    pool = BudgetPool(reserve=100)
    pool.record(4000, SOON)
    pool.record(4000, LATER)
    assert pool.latest_reset(NOW) == LATER
