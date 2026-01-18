"""Unit tests for CommitManager batch commit functionality."""

import asyncio

import pytest

from github_activity_db.github.sync.commit_manager import CommitManager


class TestCommitManagerRecordSuccess:
    """Test record_success tracking and batch triggering."""

    @pytest.mark.asyncio
    async def test_record_success_increments_count(self, db_session):
        """Verify record_success increments uncommitted_count."""
        # Arrange
        manager = CommitManager(db_session, batch_size=5)

        # Act
        await manager.record_success()

        # Assert
        assert manager.uncommitted_count == 1
        assert manager.total_committed == 0

    @pytest.mark.asyncio
    async def test_record_success_no_commit_before_batch_size(self, db_session):
        """Verify no commit happens until batch_size reached."""
        # Arrange
        manager = CommitManager(db_session, batch_size=5)

        # Act
        for _ in range(4):
            result = await manager.record_success()

        # Assert
        assert result == 0  # No commit yet
        assert manager.uncommitted_count == 4
        assert manager.total_committed == 0

    @pytest.mark.asyncio
    async def test_record_success_triggers_commit_at_batch_size(self, db_session):
        """Verify commit triggers exactly at batch_size."""
        # Arrange
        manager = CommitManager(db_session, batch_size=5)

        # Act
        for _ in range(4):
            await manager.record_success()
        result = await manager.record_success()  # 5th call

        # Assert
        assert result == 5  # Commit returned count
        assert manager.uncommitted_count == 0
        assert manager.total_committed == 5


class TestCommitManagerCommit:
    """Test explicit commit behavior."""

    @pytest.mark.asyncio
    async def test_commit_returns_uncommitted_count(self, db_session):
        """Verify commit returns number of items committed."""
        # Arrange
        manager = CommitManager(db_session, batch_size=10)
        for _ in range(7):
            await manager.record_success()

        # Act
        result = await manager.commit()

        # Assert
        assert result == 7
        assert manager.uncommitted_count == 0
        assert manager.total_committed == 7

    @pytest.mark.asyncio
    async def test_commit_noop_when_empty(self, db_session):
        """Verify commit does nothing when no pending changes."""
        # Arrange
        manager = CommitManager(db_session, batch_size=10)

        # Act
        result = await manager.commit()

        # Assert
        assert result == 0
        assert manager.total_committed == 0


class TestCommitManagerFinalize:
    """Test finalize behavior for partial batches."""

    @pytest.mark.asyncio
    async def test_finalize_commits_remaining(self, db_session):
        """Verify finalize commits partial batch."""
        # Arrange
        manager = CommitManager(db_session, batch_size=10)
        for _ in range(7):
            await manager.record_success()

        # Act
        result = await manager.finalize()

        # Assert
        assert result == 7
        assert manager.uncommitted_count == 0

    @pytest.mark.asyncio
    async def test_finalize_noop_when_empty(self, db_session):
        """Verify finalize does nothing when no pending changes."""
        # Arrange
        manager = CommitManager(db_session, batch_size=10)

        # Act
        result = await manager.finalize()

        # Assert
        assert result == 0


class TestCommitManagerWriteLock:
    """Test CommitManager respects write_lock serialization."""

    @pytest.mark.asyncio
    async def test_commit_acquires_write_lock(self, db_session):
        """Verify commit serializes with write_lock."""
        # Arrange
        write_lock = asyncio.Lock()
        manager = CommitManager(db_session, write_lock=write_lock, batch_size=1)

        # Act - Hold lock and verify commit blocks
        async with write_lock:
            task = asyncio.create_task(manager.record_success())
            await asyncio.sleep(0.01)  # Give task time to attempt lock
            assert not task.done()  # Should be waiting for lock

        # Assert - Lock released, commit should complete
        await task
        assert manager.total_committed == 1

    @pytest.mark.asyncio
    async def test_commit_without_write_lock(self, db_session):
        """Verify commit works without write_lock."""
        # Arrange
        manager = CommitManager(db_session, write_lock=None, batch_size=1)

        # Act
        await manager.record_success()

        # Assert
        assert manager.total_committed == 1


class TestCommitManagerMultipleBatches:
    """Test behavior across multiple batch cycles."""

    @pytest.mark.asyncio
    async def test_multiple_batches_accumulate_total(self, db_session):
        """Verify total_committed accumulates across batches."""
        # Arrange
        manager = CommitManager(db_session, batch_size=3)

        # Act - Process 10 items (3 full batches + 1 pending)
        for _ in range(10):
            await manager.record_success()

        # Assert
        assert manager.total_committed == 9  # 3 batches of 3
        assert manager.uncommitted_count == 1

        # Act - Finalize remaining
        await manager.finalize()

        # Assert
        assert manager.total_committed == 10
        assert manager.uncommitted_count == 0

    @pytest.mark.asyncio
    async def test_batch_size_boundary(self, db_session):
        """Verify exactly batch_size items trigger commit."""
        # Arrange
        manager = CommitManager(db_session, batch_size=5)

        # Act - Add exactly batch_size items
        for i in range(5):
            result = await manager.record_success()
            if i < 4:
                assert result == 0, f"Commit triggered early at item {i+1}"

        # Assert - Last item should have triggered commit
        assert result == 5
        assert manager.uncommitted_count == 0
        assert manager.total_committed == 5


class TestCommitManagerProperties:
    """Test CommitManager property accessors."""

    @pytest.mark.asyncio
    async def test_batch_size_property(self, db_session):
        """Verify batch_size property returns configured value."""
        # Arrange
        manager = CommitManager(db_session, batch_size=42)

        # Assert
        assert manager.batch_size == 42

    @pytest.mark.asyncio
    async def test_properties_update_correctly(self, db_session):
        """Verify properties track state correctly through operations."""
        # Arrange
        manager = CommitManager(db_session, batch_size=3)

        # Initial state
        assert manager.uncommitted_count == 0
        assert manager.total_committed == 0

        # After some records
        await manager.record_success()
        await manager.record_success()
        assert manager.uncommitted_count == 2
        assert manager.total_committed == 0

        # After batch commit
        await manager.record_success()  # Triggers commit at 3
        assert manager.uncommitted_count == 0
        assert manager.total_committed == 3

        # After more records
        await manager.record_success()
        assert manager.uncommitted_count == 1
        assert manager.total_committed == 3

        # After finalize
        await manager.finalize()
        assert manager.uncommitted_count == 0
        assert manager.total_committed == 4
