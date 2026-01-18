"""Commit Manager - Batch commit boundaries for database resilience.

Manages commit boundaries during bulk operations to prevent data loss when
failures occur. Instead of committing all changes at session exit (all-or-nothing),
commits happen in configurable batches, limiting data loss to the last uncommitted batch.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from github_activity_db.logging import get_logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger(__name__)


class CommitManager:
    """Manages commit boundaries for batch operations.

    Integrates with the existing write_lock pattern to ensure commits
    don't conflict with concurrent flush operations.

    Usage:
        async with get_session(auto_commit=False) as session:
            write_lock = asyncio.Lock()
            commit_manager = CommitManager(session, write_lock, batch_size=25)

            # ... process PRs ...
            await commit_manager.record_success()  # Auto-commits at batch_size

            await commit_manager.finalize()  # Commit remaining

    Attributes:
        uncommitted_count: Number of items pending commit.
        total_committed: Total items committed across all batches.
    """

    def __init__(
        self,
        session: AsyncSession,
        write_lock: asyncio.Lock | None = None,
        batch_size: int = 25,
    ) -> None:
        """Initialize the commit manager.

        Args:
            session: Async SQLAlchemy session to commit on.
            write_lock: Optional lock to serialize commits with flush operations.
                        Should be the same lock shared with repositories.
            batch_size: Number of successful operations before auto-commit.
                        Default is 25, limiting max data loss to ~25 items.
        """
        self._session = session
        self._write_lock = write_lock
        self._batch_size = batch_size
        self._uncommitted_count = 0
        self._total_committed = 0

    @property
    def uncommitted_count(self) -> int:
        """Number of items pending commit."""
        return self._uncommitted_count

    @property
    def total_committed(self) -> int:
        """Total items committed across all batches."""
        return self._total_committed

    @property
    def batch_size(self) -> int:
        """Configured batch size."""
        return self._batch_size

    async def record_success(self) -> int:
        """Record a successful operation, commit if batch size reached.

        Call this after each successful database write operation (e.g., after
        each PR is ingested). When the count reaches batch_size, an automatic
        commit is triggered.

        Returns:
            Number of items committed (0 if batch not full yet, batch_size if committed).
        """
        self._uncommitted_count += 1
        if self._uncommitted_count >= self._batch_size:
            return await self.commit()
        return 0

    async def commit(self) -> int:
        """Force commit of pending changes.

        Acquires write_lock if present to serialize with flush operations.
        This ensures the commit doesn't conflict with concurrent repository
        flush operations.

        Returns:
            Number of items committed (0 if nothing to commit).
        """
        if self._uncommitted_count == 0:
            return 0

        if self._write_lock:
            async with self._write_lock:
                await self._session.commit()
        else:
            await self._session.commit()

        committed = self._uncommitted_count
        self._total_committed += committed
        self._uncommitted_count = 0

        logger.debug(
            "Committed batch of %d items (total: %d)",
            committed,
            self._total_committed,
        )
        return committed

    async def finalize(self) -> int:
        """Commit any remaining uncommitted changes.

        Call this at the end of a batch operation to ensure all pending
        changes are committed. This is essential for partial batches that
        didn't reach batch_size.

        Returns:
            Number of items committed (0 if nothing pending).
        """
        return await self.commit()
