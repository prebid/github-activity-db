"""Tests for SyncFailureRepository."""

from datetime import UTC, datetime, timedelta

import pytest

from github_activity_db.db.models import SyncFailureStatus
from github_activity_db.db.repositories import SyncFailureRepository
from tests.factories import make_repository, make_sync_failure


class TestSyncFailureRepositoryQuery:
    """Query method tests for SyncFailureRepository."""

    async def test_get_by_id(self, db_session):
        """Get failure by ID."""
        repo = make_repository(db_session, owner="prebid", name="prebid-server")
        await db_session.flush()

        failure = make_sync_failure(db_session, repo, pr_number=123)
        await db_session.flush()

        repository = SyncFailureRepository(db_session)
        result = await repository.get_by_id(failure.id)

        assert result is not None
        assert result.id == failure.id
        assert result.pr_number == 123
        assert result.status == SyncFailureStatus.PENDING

    async def test_get_by_id_not_found(self, db_session):
        """Get failure by ID returns None if not found."""
        repository = SyncFailureRepository(db_session)
        result = await repository.get_by_id(9999)

        assert result is None

    async def test_get_pending_returns_only_pending(self, db_session):
        """Get pending failures excludes resolved and permanent."""
        repo = make_repository(db_session, owner="prebid", name="prebid-server")
        await db_session.flush()

        pending1 = make_sync_failure(
            db_session, repo, pr_number=1, status=SyncFailureStatus.PENDING
        )
        pending2 = make_sync_failure(
            db_session, repo, pr_number=2, status=SyncFailureStatus.PENDING
        )
        make_sync_failure(
            db_session, repo, pr_number=3, status=SyncFailureStatus.RESOLVED
        )
        make_sync_failure(
            db_session, repo, pr_number=4, status=SyncFailureStatus.PERMANENT
        )
        await db_session.flush()

        repository = SyncFailureRepository(db_session)
        results = await repository.get_pending()

        assert len(results) == 2
        pr_numbers = {r.pr_number for r in results}
        assert pr_numbers == {1, 2}

    async def test_get_pending_filters_by_repository(self, db_session):
        """Get pending failures filters by repository ID."""
        repo1 = make_repository(db_session, owner="prebid", name="repo1")
        repo2 = make_repository(db_session, owner="prebid", name="repo2")
        await db_session.flush()

        make_sync_failure(db_session, repo1, pr_number=1)
        make_sync_failure(db_session, repo1, pr_number=2)
        make_sync_failure(db_session, repo2, pr_number=3)
        await db_session.flush()

        repository = SyncFailureRepository(db_session)
        results = await repository.get_pending(repository_id=repo1.id)

        assert len(results) == 2
        assert all(r.repository_id == repo1.id for r in results)

    async def test_get_pending_respects_limit(self, db_session):
        """Get pending failures respects limit parameter."""
        repo = make_repository(db_session, owner="prebid", name="prebid-server")
        await db_session.flush()

        for i in range(10):
            make_sync_failure(db_session, repo, pr_number=i)
        await db_session.flush()

        repository = SyncFailureRepository(db_session)
        results = await repository.get_pending(limit=3)

        assert len(results) == 3

    async def test_get_pending_orders_by_failed_at(self, db_session):
        """Get pending failures returns oldest first."""
        repo = make_repository(db_session, owner="prebid", name="prebid-server")
        await db_session.flush()

        now = datetime.now(UTC)
        make_sync_failure(
            db_session, repo, pr_number=1, failed_at=now - timedelta(hours=1)
        )
        make_sync_failure(
            db_session, repo, pr_number=2, failed_at=now - timedelta(hours=3)
        )
        make_sync_failure(
            db_session, repo, pr_number=3, failed_at=now - timedelta(hours=2)
        )
        await db_session.flush()

        repository = SyncFailureRepository(db_session)
        results = await repository.get_pending()

        # Should be ordered by failed_at ascending (oldest first)
        assert results[0].pr_number == 2  # 3 hours ago
        assert results[1].pr_number == 3  # 2 hours ago
        assert results[2].pr_number == 1  # 1 hour ago

    async def test_get_by_repo_and_pr_finds_pending(self, db_session):
        """Get by repo and PR finds pending failure."""
        repo = make_repository(db_session, owner="prebid", name="prebid-server")
        await db_session.flush()

        failure = make_sync_failure(db_session, repo, pr_number=123)
        await db_session.flush()

        repository = SyncFailureRepository(db_session)
        result = await repository.get_by_repo_and_pr(repo.id, 123)

        assert result is not None
        assert result.id == failure.id

    async def test_get_by_repo_and_pr_not_found(self, db_session):
        """Get by repo and PR returns None if not found."""
        repo = make_repository(db_session, owner="prebid", name="prebid-server")
        await db_session.flush()

        repository = SyncFailureRepository(db_session)
        result = await repository.get_by_repo_and_pr(repo.id, 999)

        assert result is None

    async def test_get_by_repo_and_pr_with_status_filter(self, db_session):
        """Get by repo and PR respects status filter."""
        repo = make_repository(db_session, owner="prebid", name="prebid-server")
        await db_session.flush()

        make_sync_failure(
            db_session, repo, pr_number=123, status=SyncFailureStatus.RESOLVED
        )
        await db_session.flush()

        repository = SyncFailureRepository(db_session)

        # Default (PENDING) should not find it
        result = await repository.get_by_repo_and_pr(repo.id, 123)
        assert result is None

        # Explicit RESOLVED should find it
        result = await repository.get_by_repo_and_pr(
            repo.id, 123, status=SyncFailureStatus.RESOLVED
        )
        assert result is not None

    async def test_get_stats(self, db_session):
        """Get failure statistics."""
        repo = make_repository(db_session, owner="prebid", name="prebid-server")
        await db_session.flush()

        make_sync_failure(db_session, repo, pr_number=1, status=SyncFailureStatus.PENDING)
        make_sync_failure(db_session, repo, pr_number=2, status=SyncFailureStatus.PENDING)
        make_sync_failure(db_session, repo, pr_number=3, status=SyncFailureStatus.RESOLVED)
        make_sync_failure(db_session, repo, pr_number=4, status=SyncFailureStatus.PERMANENT)
        await db_session.flush()

        repository = SyncFailureRepository(db_session)
        stats = await repository.get_stats()

        assert stats["pending"] == 2
        assert stats["resolved"] == 1
        assert stats["permanent"] == 1
        assert stats["total"] == 4

    async def test_get_stats_filters_by_repository(self, db_session):
        """Get failure statistics filters by repository."""
        repo1 = make_repository(db_session, owner="prebid", name="repo1")
        repo2 = make_repository(db_session, owner="prebid", name="repo2")
        await db_session.flush()

        make_sync_failure(db_session, repo1, pr_number=1)
        make_sync_failure(db_session, repo1, pr_number=2)
        make_sync_failure(db_session, repo2, pr_number=3)
        await db_session.flush()

        repository = SyncFailureRepository(db_session)
        stats = await repository.get_stats(repository_id=repo1.id)

        assert stats["pending"] == 2
        assert stats["total"] == 2


class TestSyncFailureRepositoryCreate:
    """Create method tests for SyncFailureRepository."""

    async def test_record_failure_creates_new(self, db_session):
        """record_failure creates new failure record."""
        repo = make_repository(db_session, owner="prebid", name="prebid-server")
        await db_session.flush()

        repository = SyncFailureRepository(db_session)
        failure = await repository.record_failure(
            repo.id, 123, ValueError("Test error")
        )

        assert failure.id is not None
        assert failure.repository_id == repo.id
        assert failure.pr_number == 123
        assert failure.error_message == "Test error"
        assert failure.error_type == "ValueError"
        assert failure.retry_count == 0
        assert failure.status == SyncFailureStatus.PENDING

    async def test_record_failure_increments_retry_count(self, db_session):
        """record_failure increments retry count on existing failure."""
        repo = make_repository(db_session, owner="prebid", name="prebid-server")
        await db_session.flush()

        repository = SyncFailureRepository(db_session)

        # First failure
        failure1 = await repository.record_failure(
            repo.id, 123, ValueError("Error 1")
        )
        assert failure1.retry_count == 0

        # Second failure for same PR
        failure2 = await repository.record_failure(
            repo.id, 123, ValueError("Error 2")
        )

        # Should be same record with incremented count
        assert failure2.id == failure1.id
        assert failure2.retry_count == 1
        assert failure2.error_message == "Error 2"

    async def test_record_failure_accepts_string_error(self, db_session):
        """record_failure accepts string error message."""
        repo = make_repository(db_session, owner="prebid", name="prebid-server")
        await db_session.flush()

        repository = SyncFailureRepository(db_session)
        failure = await repository.record_failure(
            repo.id, 123, "String error message"
        )

        assert failure.error_message == "String error message"
        assert failure.error_type == "Unknown"


class TestSyncFailureRepositoryUpdate:
    """Update method tests for SyncFailureRepository."""

    async def test_mark_resolved(self, db_session):
        """mark_resolved sets status to RESOLVED."""
        repo = make_repository(db_session, owner="prebid", name="prebid-server")
        await db_session.flush()

        failure = make_sync_failure(db_session, repo, pr_number=123)
        await db_session.flush()

        repository = SyncFailureRepository(db_session)
        result = await repository.mark_resolved(failure.id)

        assert result is not None
        assert result.status == SyncFailureStatus.RESOLVED
        assert result.resolved_at is not None

    async def test_mark_resolved_not_found(self, db_session):
        """mark_resolved returns None if not found."""
        repository = SyncFailureRepository(db_session)
        result = await repository.mark_resolved(9999)

        assert result is None

    async def test_mark_permanent(self, db_session):
        """mark_permanent sets status to PERMANENT."""
        repo = make_repository(db_session, owner="prebid", name="prebid-server")
        await db_session.flush()

        failure = make_sync_failure(db_session, repo, pr_number=123)
        await db_session.flush()

        repository = SyncFailureRepository(db_session)
        result = await repository.mark_permanent(failure.id)

        assert result is not None
        assert result.status == SyncFailureStatus.PERMANENT

    async def test_mark_permanent_not_found(self, db_session):
        """mark_permanent returns None if not found."""
        repository = SyncFailureRepository(db_session)
        result = await repository.mark_permanent(9999)

        assert result is None


class TestSyncFailureRepositoryDelete:
    """Delete method tests for SyncFailureRepository."""

    async def test_delete_resolved(self, db_session):
        """delete_resolved removes resolved failures."""
        repo = make_repository(db_session, owner="prebid", name="prebid-server")
        await db_session.flush()

        make_sync_failure(db_session, repo, pr_number=1, status=SyncFailureStatus.RESOLVED)
        make_sync_failure(db_session, repo, pr_number=2, status=SyncFailureStatus.RESOLVED)
        make_sync_failure(db_session, repo, pr_number=3, status=SyncFailureStatus.PENDING)
        await db_session.flush()

        repository = SyncFailureRepository(db_session)
        deleted = await repository.delete_resolved()

        assert deleted == 2

        # Verify pending is still there
        pending = await repository.get_pending()
        assert len(pending) == 1
        assert pending[0].pr_number == 3

    async def test_delete_resolved_with_before_filter(self, db_session):
        """delete_resolved respects before filter."""
        repo = make_repository(db_session, owner="prebid", name="prebid-server")
        await db_session.flush()

        now = datetime.now(UTC)
        old_resolved = make_sync_failure(
            db_session,
            repo,
            pr_number=1,
            status=SyncFailureStatus.RESOLVED,
            resolved_at=now - timedelta(days=10),
        )
        recent_resolved = make_sync_failure(
            db_session,
            repo,
            pr_number=2,
            status=SyncFailureStatus.RESOLVED,
            resolved_at=now - timedelta(hours=1),
        )
        await db_session.flush()

        repository = SyncFailureRepository(db_session)
        deleted = await repository.delete_resolved(before=now - timedelta(days=1))

        assert deleted == 1

        # Verify recent is still there
        result = await repository.get_by_repo_and_pr(
            repo.id, 2, status=SyncFailureStatus.RESOLVED
        )
        assert result is not None
