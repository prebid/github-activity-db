"""Unit tests for ProgressTracker class."""

import time
from datetime import UTC, datetime

from github_activity_db.github.pacing.progress import (
    ProgressState,
    ProgressTracker,
    ProgressUpdate,
)


class TestProgressUpdate:
    """Tests for ProgressUpdate dataclass."""

    def test_remaining_calculation(self) -> None:
        """remaining calculates correctly."""
        update = ProgressUpdate(
            total=100,
            completed=30,
            failed=10,
            state=ProgressState.IN_PROGRESS,
        )
        assert update.remaining == 60

    def test_remaining_never_negative(self) -> None:
        """remaining is never negative."""
        update = ProgressUpdate(
            total=10,
            completed=15,  # More than total (edge case)
            failed=0,
            state=ProgressState.COMPLETED,
        )
        assert update.remaining == 0

    def test_progress_percent(self) -> None:
        """progress_percent calculates correctly."""
        update = ProgressUpdate(
            total=100,
            completed=45,
            failed=5,
            state=ProgressState.IN_PROGRESS,
        )
        assert update.progress_percent == 50.0

    def test_progress_percent_empty(self) -> None:
        """progress_percent is 100 when total is zero."""
        update = ProgressUpdate(
            total=0,
            completed=0,
            failed=0,
            state=ProgressState.PENDING,
        )
        assert update.progress_percent == 100.0

    def test_success_rate(self) -> None:
        """success_rate calculates correctly."""
        update = ProgressUpdate(
            total=100,
            completed=80,
            failed=20,
            state=ProgressState.IN_PROGRESS,
        )
        assert update.success_rate == 80.0

    def test_success_rate_no_processed(self) -> None:
        """success_rate is 100 when nothing processed."""
        update = ProgressUpdate(
            total=100,
            completed=0,
            failed=0,
            state=ProgressState.PENDING,
        )
        assert update.success_rate == 100.0


class TestProgressTrackerInit:
    """Tests for ProgressTracker initialization."""

    def test_init_defaults(self) -> None:
        """Tracker initializes with defaults."""
        tracker = ProgressTracker()

        assert tracker.total == 0
        assert tracker.completed == 0
        assert tracker.failed == 0
        assert tracker.state == ProgressState.PENDING

    def test_init_with_total(self) -> None:
        """Tracker accepts initial total."""
        tracker = ProgressTracker(total=100)

        assert tracker.total == 100

    def test_init_with_name(self) -> None:
        """Tracker accepts operation name."""
        tracker = ProgressTracker(name="test-op")

        # Name is used in logging, verify it's stored
        assert tracker._name == "test-op"


class TestProgressTrackerLifecycle:
    """Tests for progress state transitions."""

    def test_start_sets_state(self) -> None:
        """start() transitions to IN_PROGRESS."""
        tracker = ProgressTracker(total=10)
        tracker.start()

        assert tracker.state == ProgressState.IN_PROGRESS
        assert tracker.is_running is True
        assert tracker.is_done is False

    def test_complete_sets_state(self) -> None:
        """complete() transitions to COMPLETED."""
        tracker = ProgressTracker(total=10)
        tracker.start()
        tracker.complete()

        assert tracker.state == ProgressState.COMPLETED
        assert tracker.is_running is False
        assert tracker.is_done is True

    def test_fail_sets_state(self) -> None:
        """fail() transitions to FAILED with error."""
        tracker = ProgressTracker(total=10)
        tracker.start()
        tracker.fail("Something went wrong")

        assert tracker.state == ProgressState.FAILED
        assert tracker.is_done is True

    def test_cancel_sets_state(self) -> None:
        """cancel() transitions to CANCELLED."""
        tracker = ProgressTracker(total=10)
        tracker.start()
        tracker.cancel()

        assert tracker.state == ProgressState.CANCELLED
        assert tracker.is_done is True

    def test_started_at_recorded(self) -> None:
        """start() records start time."""
        tracker = ProgressTracker(total=10)
        before = datetime.now(UTC)
        tracker.start()
        after = datetime.now(UTC)

        update = tracker.get_update()
        assert update.started_at is not None
        assert before <= update.started_at <= after


class TestProgressTrackerUpdates:
    """Tests for progress update methods."""

    def test_increment(self) -> None:
        """increment() increases completed count."""
        tracker = ProgressTracker(total=10)
        tracker.start()

        tracker.increment()
        assert tracker.completed == 1

        tracker.increment(5)
        assert tracker.completed == 6

    def test_increment_failed(self) -> None:
        """increment_failed() increases failed count."""
        tracker = ProgressTracker(total=10)
        tracker.start()

        tracker.increment_failed()
        assert tracker.failed == 1

        tracker.increment_failed(3)
        assert tracker.failed == 4

    def test_set_current(self) -> None:
        """set_current() updates current item."""
        tracker = ProgressTracker(total=10)
        tracker.start()

        tracker.set_current("processing item 1")
        update = tracker.get_update()
        assert update.current_item == "processing item 1"

    def test_increment_clears_current(self) -> None:
        """increment() clears current item."""
        tracker = ProgressTracker(total=10)
        tracker.start()

        tracker.set_current("item 1")
        tracker.increment()

        update = tracker.get_update()
        assert update.current_item is None

    def test_add_total(self) -> None:
        """add_total() increases total count."""
        tracker = ProgressTracker(total=10)

        tracker.add_total(5)
        assert tracker.total == 15

    def test_total_setter(self) -> None:
        """total can be set directly."""
        tracker = ProgressTracker(total=10)

        tracker.total = 50
        assert tracker.total == 50


class TestProgressTrackerCallbacks:
    """Tests for callback notifications."""

    def test_callback_on_start(self) -> None:
        """Callback called on start()."""
        tracker = ProgressTracker(total=10)
        updates: list[ProgressUpdate] = []

        tracker.on_progress(updates.append)
        tracker.start()

        assert len(updates) == 1
        assert updates[0].state == ProgressState.IN_PROGRESS

    def test_callback_on_increment(self) -> None:
        """Callback called on increment()."""
        tracker = ProgressTracker(total=10)
        updates: list[ProgressUpdate] = []

        tracker.on_progress(updates.append)
        tracker.start()
        tracker.increment()

        assert len(updates) == 2
        assert updates[1].completed == 1

    def test_callback_on_complete(self) -> None:
        """Callback called on complete()."""
        tracker = ProgressTracker(total=10)
        updates: list[ProgressUpdate] = []

        tracker.on_progress(updates.append)
        tracker.start()
        tracker.complete()

        assert updates[-1].state == ProgressState.COMPLETED

    def test_multiple_callbacks(self) -> None:
        """Multiple callbacks all receive updates."""
        tracker = ProgressTracker(total=10)
        updates1: list[ProgressUpdate] = []
        updates2: list[ProgressUpdate] = []

        tracker.on_progress(updates1.append)
        tracker.on_progress(updates2.append)
        tracker.start()

        assert len(updates1) == 1
        assert len(updates2) == 1

    def test_callback_error_handled(self) -> None:
        """Callback errors don't break tracking."""
        tracker = ProgressTracker(total=10)

        def bad_callback(update: ProgressUpdate) -> None:
            raise ValueError("callback error")

        tracker.on_progress(bad_callback)

        # Should not raise
        tracker.start()
        tracker.increment()
        tracker.complete()

        assert tracker.state == ProgressState.COMPLETED


class TestProgressTrackerElapsedTime:
    """Tests for elapsed time tracking."""

    def test_elapsed_before_start(self) -> None:
        """elapsed_seconds is 0 before start."""
        tracker = ProgressTracker(total=10)
        assert tracker.elapsed_seconds == 0.0

    def test_elapsed_tracks_time(self) -> None:
        """elapsed_seconds increases after start."""
        tracker = ProgressTracker(total=10)
        tracker.start()

        time.sleep(0.05)
        elapsed = tracker.elapsed_seconds

        assert elapsed >= 0.04  # Allow some timing variance


class TestProgressTrackerMetadata:
    """Tests for metadata storage."""

    def test_set_get_metadata(self) -> None:
        """Metadata can be set and retrieved."""
        tracker = ProgressTracker()

        tracker.set_metadata("key1", "value1")
        tracker.set_metadata("key2", 42)

        assert tracker.get_metadata("key1") == "value1"
        assert tracker.get_metadata("key2") == 42

    def test_get_metadata_default(self) -> None:
        """get_metadata returns default for missing key."""
        tracker = ProgressTracker()

        assert tracker.get_metadata("missing") is None
        assert tracker.get_metadata("missing", "default") == "default"

    def test_reset_clears_metadata(self) -> None:
        """reset() clears metadata."""
        tracker = ProgressTracker()
        tracker.set_metadata("key", "value")

        tracker.reset()

        assert tracker.get_metadata("key") is None


class TestProgressTrackerReset:
    """Tests for reset functionality."""

    def test_reset_clears_state(self) -> None:
        """reset() clears all state."""
        tracker = ProgressTracker(total=100)
        tracker.start()
        tracker.increment(50)
        tracker.increment_failed(10)
        tracker.complete()

        tracker.reset()

        assert tracker.completed == 0
        assert tracker.failed == 0
        assert tracker.state == ProgressState.PENDING
        assert tracker.total == 100  # Total is preserved

    def test_reset_notifies(self) -> None:
        """reset() triggers callback."""
        tracker = ProgressTracker(total=10)
        updates: list[ProgressUpdate] = []

        tracker.on_progress(updates.append)
        tracker.reset()

        assert len(updates) == 1
        assert updates[0].state == ProgressState.PENDING


class TestProgressState:
    """Tests for ProgressState enum."""

    def test_all_states_exist(self) -> None:
        """All expected states are defined."""
        states = {s.name for s in ProgressState}
        expected = {"PENDING", "IN_PROGRESS", "COMPLETED", "FAILED", "CANCELLED"}
        assert expected == states
