"""Repository for PullRequest model CRUD operations."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from github_activity_db.config import get_settings
from github_activity_db.db.models import PRState, PullRequest

from .base import BaseRepository

if TYPE_CHECKING:
    from github_activity_db.schemas.pr import PRCreate, PRMerge, PRSync


class PullRequestRepository(BaseRepository[PullRequest]):
    """Repository for PullRequest entities.

    Handles CRUD operations for GitHub Pull Requests with
    awareness of the PR state machine (OPEN → MERGED).

    State Rules:
        - OPEN PRs can be updated with sync data
        - MERGED PRs within grace period can be updated
        - MERGED PRs past grace period are frozen (no updates)
        - We don't care about CLOSED (without merge) PRs
    """

    def __init__(
        self,
        session: AsyncSession,
        grace_period: timedelta | None = None,
        write_lock: asyncio.Lock | None = None,
    ) -> None:
        """Initialize the repository.

        Args:
            session: Async SQLAlchemy session
            grace_period: Override for merge grace period (uses config if None)
            write_lock: Optional lock to serialize write operations (for concurrent use)
        """
        super().__init__(session, PullRequest, write_lock)
        self._grace_period = grace_period or get_settings().sync.merge_grace_period

    # -------------------------------------------------------------------------
    # Query Methods
    # -------------------------------------------------------------------------

    async def get_by_number(
        self,
        repository_id: int,
        number: int,
    ) -> PullRequest | None:
        """Get a PR by repository and PR number.

        Args:
            repository_id: Repository ID
            number: PR number

        Returns:
            PullRequest or None if not found
        """
        stmt = select(PullRequest).where(
            PullRequest.repository_id == repository_id,
            PullRequest.number == number,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_state(
        self,
        repository_id: int,
        state: PRState,
    ) -> list[PullRequest]:
        """Get all PRs in a given state.

        Args:
            repository_id: Repository ID
            state: PR state to filter by

        Returns:
            List of matching PRs
        """
        stmt = select(PullRequest).where(
            PullRequest.repository_id == repository_id,
            PullRequest.state == state,
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_open_prs(self, repository_id: int) -> list[PullRequest]:
        """Get all open PRs for a repository.

        Args:
            repository_id: Repository ID

        Returns:
            List of open PRs
        """
        return await self.get_by_state(repository_id, PRState.OPEN)

    async def get_numbers_by_state(
        self,
        repository_id: int,
        state: PRState,
    ) -> list[int]:
        """Get just PR numbers for a state (efficient for diffing).

        Args:
            repository_id: Repository ID
            state: PR state to filter by

        Returns:
            List of PR numbers
        """
        stmt = select(PullRequest.number).where(
            PullRequest.repository_id == repository_id,
            PullRequest.state == state,
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    # -------------------------------------------------------------------------
    # Create Methods
    # -------------------------------------------------------------------------

    async def create(
        self,
        repository_id: int,
        create_data: PRCreate,
        sync_data: PRSync,
    ) -> PullRequest:
        """Create a new PR with immutable and sync fields.

        Args:
            repository_id: Repository ID
            create_data: Immutable fields (number, link, submitter, etc.)
            sync_data: Synced fields (title, stats, etc.)

        Returns:
            Created PullRequest (flushed, has ID)
        """
        pr = PullRequest(
            repository_id=repository_id,
            # Immutable fields from PRCreate
            number=create_data.number,
            link=create_data.link,
            open_date=create_data.open_date,
            submitter=create_data.submitter,
            # Synced fields from PRSync
            **self._sync_data_to_dict(sync_data),
        )
        self.add(pr)
        await self.flush()
        return pr

    # -------------------------------------------------------------------------
    # Update Methods
    # -------------------------------------------------------------------------

    async def update(
        self,
        pr_id: int,
        sync_data: PRSync,
    ) -> PullRequest | None:
        """Update an existing PR with sync data.

        Only updates if PR is OPEN or within grace period.

        Args:
            pr_id: PR ID
            sync_data: Updated sync fields

        Returns:
            Updated PR or None if not found
        """
        pr = await self.get_by_id(pr_id)
        if pr is None:
            return None

        # Check if frozen
        if self._is_frozen(pr):
            return pr  # Return as-is without updating

        # Apply sync data
        for key, value in self._sync_data_to_dict(sync_data).items():
            setattr(pr, key, value)

        await self.flush()
        return pr

    async def create_or_update(
        self,
        repository_id: int,
        create_data: PRCreate,
        sync_data: PRSync,
    ) -> tuple[PullRequest, bool]:
        """Upsert PR: create if new, update if exists and not frozen.

        Args:
            repository_id: Repository ID
            create_data: Immutable fields
            sync_data: Synced fields

        Returns:
            Tuple of (PullRequest, created) where created=True if new
        """
        existing = await self.get_by_number(repository_id, create_data.number)

        if existing is None:
            # Create new PR
            pr = await self.create(repository_id, create_data, sync_data)
            return pr, True

        # Check if frozen (MERGED past grace period)
        if self._is_frozen(existing):
            # Return as-is without updating
            return existing, False

        # Update existing open or in-grace-period PR
        for key, value in self._sync_data_to_dict(sync_data).items():
            setattr(existing, key, value)

        await self.flush()
        return existing, False

    async def apply_merge(
        self,
        pr_id: int,
        merge_data: PRMerge,
    ) -> PullRequest | None:
        """Apply merge data to a PR.

        Sets state to MERGED and populates close_date and merged_by.

        Args:
            pr_id: PR ID
            merge_data: Merge fields (close_date, merged_by)

        Returns:
            Updated PR or None if not found
        """
        pr = await self.get_by_id(pr_id)
        if pr is None:
            return None

        pr.state = PRState.MERGED
        pr.close_date = merge_data.close_date
        pr.merged_by = merge_data.merged_by
        if merge_data.ai_summary is not None:
            pr.ai_summary = merge_data.ai_summary

        await self.flush()
        return pr

    # -------------------------------------------------------------------------
    # State Helpers
    # -------------------------------------------------------------------------

    def _is_frozen(self, pr: PullRequest) -> bool:
        """Check if a PR is frozen (merged past grace period).

        Args:
            pr: PullRequest to check

        Returns:
            True if PR should not be updated
        """
        if pr.state != PRState.MERGED:
            return False

        if pr.close_date is None:
            # Merged but no close_date - shouldn't happen, allow updates
            return False

        now = datetime.now(UTC)
        # Ensure close_date is timezone-aware
        close_date = pr.close_date
        if close_date.tzinfo is None:
            close_date = close_date.replace(tzinfo=UTC)

        return (now - close_date) > self._grace_period

    def is_unchanged(self, pr: PullRequest, sync_data: PRSync) -> bool:
        """Check if PR data matches sync data (no update needed).

        Used for diff detection to avoid unnecessary updates.

        Args:
            pr: Existing PR
            sync_data: Incoming sync data

        Returns:
            True if last_update_date matches (no changes)
        """
        pr_update_date = pr.last_update_date
        # Ensure timezone-aware comparison
        if pr_update_date.tzinfo is None:
            pr_update_date = pr_update_date.replace(tzinfo=UTC)

        sync_update_date = sync_data.last_update_date
        if sync_update_date.tzinfo is None:
            sync_update_date = sync_update_date.replace(tzinfo=UTC)

        return pr_update_date >= sync_update_date

    # -------------------------------------------------------------------------
    # Data Conversion Helpers
    # -------------------------------------------------------------------------

    def _sync_data_to_dict(self, sync_data: PRSync) -> dict[str, object]:
        """Convert PRSync schema to dict for model assignment.

        Handles special conversions:
        - CommitBreakdown → dict format for JSON column
        - ParticipantEntry list → dict format for JSON column

        Args:
            sync_data: PRSync schema

        Returns:
            Dict ready for model field assignment
        """
        # Convert commits_breakdown to JSON-serializable format
        commits_breakdown = [
            {"date": cb.date.isoformat(), "author": cb.author} for cb in sync_data.commits_breakdown
        ]

        # Convert participants list to dict format
        participants: dict[str, list[str]] = {}
        for entry in sync_data.participants:
            participants[entry.username] = [a.value for a in entry.actions]

        return {
            "title": sync_data.title,
            "description": sync_data.description,
            "last_update_date": sync_data.last_update_date,
            "state": sync_data.state,
            "files_changed": sync_data.files_changed,
            "lines_added": sync_data.lines_added,
            "lines_deleted": sync_data.lines_deleted,
            "commits_count": sync_data.commits_count,
            "github_labels": sync_data.github_labels,
            "filenames": sync_data.filenames,
            "reviewers": sync_data.reviewers,
            "assignees": sync_data.assignees,
            "commits_breakdown": commits_breakdown,
            "participants": participants,
            "classify_tags": sync_data.classify_tags,
        }
