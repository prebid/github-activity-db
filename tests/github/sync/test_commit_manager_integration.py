"""Integration tests for CommitManager with real database operations."""

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from github_activity_db.db.repositories import PullRequestRepository
from github_activity_db.github.sync.commit_manager import CommitManager
from tests.factories import make_pull_request, make_repository


class TestCommitManagerDataPersistence:
    """Test that committed data survives session failures."""

    @pytest.mark.asyncio
    async def test_partial_data_survives_failure(self, test_engine):
        """Verify committed PRs persist even when later operations fail.

        This test creates 5 PRs with batch_size=3:
        - PRs 1-3: Should be committed (first batch) and persist
        - PRs 4-5: Should be rolled back (no finalize called)
        """
        # Arrange
        session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
        repo_id = None

        # Act - Create items, let session close without finalizing
        async with session_factory() as session:
            write_lock = asyncio.Lock()
            commit_manager = CommitManager(session, write_lock, batch_size=3)

            # Create repository first (not counted in commit tracking)
            repo = make_repository(session, owner="test", name="repo")
            await session.flush()
            repo_id = repo.id

            # Create 5 PRs (commit triggers at PR#3)
            for i in range(5):
                make_pull_request(session, repo, number=i + 1)
                await session.flush()
                await commit_manager.record_success()

            # At this point: 5 items recorded, 3 committed (batch 1), 2 pending
            assert commit_manager.total_committed == 3
            assert commit_manager.uncommitted_count == 2
            # Session closes WITHOUT finalize - simulating crash

        # Assert - Verify persisted data in new session
        async with session_factory() as session:
            pr_repo = PullRequestRepository(session)

            # First 3 PRs should be committed
            for i in range(1, 4):
                pr = await pr_repo.get_by_number(repo_id, i)
                assert pr is not None, f"PR #{i} should be committed"

            # PRs 4-5 should NOT exist (rolled back)
            for i in range(4, 6):
                pr = await pr_repo.get_by_number(repo_id, i)
                assert pr is None, f"PR #{i} should have been rolled back"

    @pytest.mark.asyncio
    async def test_all_data_persists_with_finalize(self, test_engine):
        """Verify all data persists when finalize is called."""
        # Arrange
        session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
        repo_id = None

        # Act - Create items and finalize
        async with session_factory() as session:
            write_lock = asyncio.Lock()
            commit_manager = CommitManager(session, write_lock, batch_size=3)

            repo = make_repository(session, owner="test", name="repo")
            await session.flush()
            repo_id = repo.id

            # Create 5 PRs
            for i in range(5):
                make_pull_request(session, repo, number=i + 1)
                await session.flush()
                await commit_manager.record_success()

            # Finalize to commit remaining
            await commit_manager.finalize()

            assert commit_manager.total_committed == 5
            assert commit_manager.uncommitted_count == 0

        # Assert - Verify ALL data persisted
        async with session_factory() as session:
            pr_repo = PullRequestRepository(session)

            for i in range(1, 6):
                pr = await pr_repo.get_by_number(repo_id, i)
                assert pr is not None, f"PR #{i} should be committed"


class TestCommitManagerWithWriteLockIntegration:
    """Test CommitManager with write_lock and real repositories."""

    @pytest.mark.asyncio
    async def test_sequential_operations_with_lock(self, test_engine):
        """Verify operations work correctly with write_lock."""
        session_factory = async_sessionmaker(test_engine, expire_on_commit=False)

        async with session_factory() as session:
            write_lock = asyncio.Lock()
            commit_manager = CommitManager(session, write_lock, batch_size=2)

            # Create repository
            repo = make_repository(session, owner="test", name="repo")
            await session.flush()

            # Create PRs with commit manager
            for i in range(4):
                make_pull_request(session, repo, number=i + 1)
                await session.flush()
                await commit_manager.record_success()

            # Finalize
            await commit_manager.finalize()

            # All 4 should be committed (2 batches of 2)
            assert commit_manager.total_committed == 4


class TestKeyboardInterruptRecovery:
    """Test data recovery after simulated failures."""

    @pytest.mark.asyncio
    async def test_keyboard_interrupt_preserves_committed_batches(self, test_engine):
        """Verify committed batches persist after KeyboardInterrupt.

        Simulates Ctrl+C during sync: committed batches should be saved,
        current batch should be lost.
        """
        # Arrange
        session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
        repo_id = None

        # Act - Process PRs, interrupt after 2 full batches
        with pytest.raises(KeyboardInterrupt, match="Simulated Ctrl\\+C"):
            async with session_factory() as session:
                write_lock = asyncio.Lock()
                commit_manager = CommitManager(session, write_lock, batch_size=5)

                repo = make_repository(session, owner="test", name="repo")
                await session.flush()
                repo_id = repo.id

                # Create 12 PRs (2 full batches + 2 in progress)
                for i in range(12):
                    make_pull_request(session, repo, number=i + 1)
                    await session.flush()
                    await commit_manager.record_success()

                    # Simulate interrupt after PR #12
                    if i == 11:
                        raise KeyboardInterrupt("Simulated Ctrl+C")

        # Assert - Verify only complete batches persisted
        async with session_factory() as session:
            pr_repo = PullRequestRepository(session)
            # Should have 10 PRs (2 batches of 5)
            # PRs 11-12 should be lost (partial batch)
            assert repo_id is not None
            existing_count = 0
            for i in range(1, 13):
                pr = await pr_repo.get_by_number(repo_id, i)
                if pr is not None:
                    existing_count += 1

            # 2 full batches of 5 = 10 PRs saved
            assert existing_count == 10, f"Expected 10 PRs, got {existing_count}"

    @pytest.mark.asyncio
    async def test_exception_preserves_committed_batches(self, test_engine):
        """Verify committed batches persist after general exception."""
        # Arrange
        session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
        repo_id = None

        # Act - Process PRs, exception after some batches
        with pytest.raises(ValueError, match="Simulated error"):
            async with session_factory() as session:
                write_lock = asyncio.Lock()
                commit_manager = CommitManager(session, write_lock, batch_size=3)

                repo = make_repository(session, owner="test", name="repo")
                await session.flush()
                repo_id = repo.id

                # Create 7 PRs (2 full batches + 1 in progress)
                for i in range(7):
                    make_pull_request(session, repo, number=i + 1)
                    await session.flush()
                    await commit_manager.record_success()

                # Simulate exception
                raise ValueError("Simulated error")

        # Assert - 6 PRs (2 batches of 3) should be saved
        async with session_factory() as session:
            pr_repo = PullRequestRepository(session)
            assert repo_id is not None
            existing_count = 0
            for i in range(1, 8):
                pr = await pr_repo.get_by_number(repo_id, i)
                if pr is not None:
                    existing_count += 1

            assert existing_count == 6, f"Expected 6 PRs, got {existing_count}"
