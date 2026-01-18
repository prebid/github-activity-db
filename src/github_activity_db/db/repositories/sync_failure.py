"""Repository for SyncFailure model CRUD operations."""

import asyncio
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from github_activity_db.db.models import SyncFailure, SyncFailureStatus

from .base import BaseRepository


class SyncFailureRepository(BaseRepository[SyncFailure]):
    """Repository for tracking failed PR ingestion attempts.

    Manages the lifecycle of sync failures:
    - Recording new failures
    - Querying pending failures for retry
    - Marking failures as resolved or permanent
    - Tracking retry counts
    """

    def __init__(
        self,
        session: AsyncSession,
        write_lock: asyncio.Lock | None = None,
    ) -> None:
        """Initialize the repository.

        Args:
            session: Async SQLAlchemy session
            write_lock: Optional lock to serialize write operations
        """
        super().__init__(session, SyncFailure, write_lock)

    # -------------------------------------------------------------------------
    # Query Methods
    # -------------------------------------------------------------------------

    async def get_pending(
        self,
        repository_id: int | None = None,
        limit: int = 100,
    ) -> list[SyncFailure]:
        """Get pending failures ready for retry.

        Args:
            repository_id: Filter by repository (optional)
            limit: Maximum number of failures to return

        Returns:
            List of pending failures ordered by failed_at (oldest first)
        """
        stmt = (
            select(SyncFailure)
            .where(SyncFailure.status == SyncFailureStatus.PENDING)
            .order_by(SyncFailure.failed_at)
            .limit(limit)
        )

        if repository_id is not None:
            stmt = stmt.where(SyncFailure.repository_id == repository_id)

        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_repo_and_pr(
        self,
        repository_id: int,
        pr_number: int,
        status: SyncFailureStatus | None = None,
    ) -> SyncFailure | None:
        """Get a failure record by repository and PR number.

        Args:
            repository_id: Repository ID
            pr_number: PR number
            status: Filter by status (optional, defaults to PENDING)

        Returns:
            SyncFailure or None if not found
        """
        stmt = select(SyncFailure).where(
            SyncFailure.repository_id == repository_id,
            SyncFailure.pr_number == pr_number,
        )

        if status is not None:
            stmt = stmt.where(SyncFailure.status == status)
        else:
            # Default to pending since that's the most common query
            stmt = stmt.where(SyncFailure.status == SyncFailureStatus.PENDING)

        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_stats(
        self,
        repository_id: int | None = None,
    ) -> dict[str, Any]:
        """Get failure statistics by status.

        Args:
            repository_id: Filter by repository (optional)

        Returns:
            Dictionary with counts by status and total
        """
        base_stmt = select(SyncFailure.status, func.count(SyncFailure.id))

        if repository_id is not None:
            base_stmt = base_stmt.where(SyncFailure.repository_id == repository_id)

        stmt = base_stmt.group_by(SyncFailure.status)
        result = await self._session.execute(stmt)
        rows = result.all()

        stats: dict[str, Any] = {
            "pending": 0,
            "resolved": 0,
            "permanent": 0,
            "total": 0,
        }

        for status, count in rows:
            stats[status.value] = count
            stats["total"] += count

        return stats

    # -------------------------------------------------------------------------
    # Create/Update Methods
    # -------------------------------------------------------------------------

    async def record_failure(
        self,
        repository_id: int,
        pr_number: int,
        error: Exception | str,
    ) -> SyncFailure:
        """Record a new failure or increment retry count on existing.

        If a pending failure already exists for this repo+PR, increments
        the retry count. Otherwise, creates a new failure record.

        Args:
            repository_id: Repository ID
            pr_number: PR number that failed
            error: Exception or error message string

        Returns:
            The failure record (new or updated)
        """
        error_message = str(error)
        error_type = type(error).__name__ if isinstance(error, Exception) else "Unknown"

        # Check for existing pending failure
        existing = await self.get_by_repo_and_pr(
            repository_id, pr_number, SyncFailureStatus.PENDING
        )

        if existing is not None:
            # Update existing failure
            existing.retry_count += 1
            existing.error_message = error_message
            existing.error_type = error_type
            existing.failed_at = datetime.now(UTC)
            await self.flush()
            return existing

        # Create new failure
        failure = SyncFailure(
            repository_id=repository_id,
            pr_number=pr_number,
            error_message=error_message,
            error_type=error_type,
            retry_count=0,
            status=SyncFailureStatus.PENDING,
            failed_at=datetime.now(UTC),
        )
        self.add(failure)
        await self.flush()
        return failure

    async def mark_resolved(self, failure_id: int) -> SyncFailure | None:
        """Mark a failure as resolved after successful retry.

        Args:
            failure_id: Failure record ID

        Returns:
            Updated failure or None if not found
        """
        failure = await self.get_by_id(failure_id)
        if failure is None:
            return None

        failure.status = SyncFailureStatus.RESOLVED
        failure.resolved_at = datetime.now(UTC)
        await self.flush()
        return failure

    async def mark_permanent(self, failure_id: int) -> SyncFailure | None:
        """Mark a failure as permanent (no more retries).

        Use when max retries exceeded or error is non-retryable.

        Args:
            failure_id: Failure record ID

        Returns:
            Updated failure or None if not found
        """
        failure = await self.get_by_id(failure_id)
        if failure is None:
            return None

        failure.status = SyncFailureStatus.PERMANENT
        await self.flush()
        return failure

    async def delete_resolved(
        self,
        before: datetime | None = None,
    ) -> int:
        """Delete resolved failures (optional cleanup).

        Args:
            before: Only delete failures resolved before this time

        Returns:
            Number of deleted records
        """
        from sqlalchemy import delete

        stmt = delete(SyncFailure).where(SyncFailure.status == SyncFailureStatus.RESOLVED)

        if before is not None:
            stmt = stmt.where(SyncFailure.resolved_at < before)

        cursor_result = await self._session.execute(stmt)
        # CursorResult has rowcount attribute for DELETE/UPDATE statements
        row_count: int = getattr(cursor_result, "rowcount", 0) or 0
        return row_count
