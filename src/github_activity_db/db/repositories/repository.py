"""Repository for GitHub Repository model CRUD operations."""

import asyncio
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from github_activity_db.db.models import Repository

from .base import BaseRepository


class RepositoryRepository(BaseRepository[Repository]):
    """Repository for GitHub Repository entities.

    Handles CRUD operations for tracked GitHub repositories.
    """

    def __init__(
        self,
        session: AsyncSession,
        write_lock: asyncio.Lock | None = None,
    ) -> None:
        """Initialize the repository.

        Args:
            session: Async SQLAlchemy session
            write_lock: Optional lock to serialize write operations (for concurrent use)
        """
        super().__init__(session, Repository, write_lock)

    # -------------------------------------------------------------------------
    # Query Methods
    # -------------------------------------------------------------------------

    async def get_by_full_name(self, full_name: str) -> Repository | None:
        """Get a repository by its full name (owner/repo).

        Args:
            full_name: Full repository name (e.g., "prebid/prebid-server")

        Returns:
            Repository or None if not found
        """
        return await self._get_by_field("full_name", full_name)

    async def get_by_owner_and_name(
        self, owner: str, name: str
    ) -> Repository | None:
        """Get a repository by owner and name.

        Args:
            owner: Repository owner (e.g., "prebid")
            name: Repository name (e.g., "prebid-server")

        Returns:
            Repository or None if not found
        """
        stmt = select(Repository).where(
            Repository.owner == owner,
            Repository.name == name,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_active(self) -> list[Repository]:
        """Get all active repositories.

        Returns:
            List of active repositories
        """
        stmt = select(Repository).where(Repository.is_active.is_(True))
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    # -------------------------------------------------------------------------
    # Create/Update Methods
    # -------------------------------------------------------------------------

    async def create(
        self,
        owner: str,
        name: str,
        *,
        is_active: bool = True,
    ) -> Repository:
        """Create a new repository.

        Args:
            owner: Repository owner
            name: Repository name
            is_active: Whether the repository is active for syncing

        Returns:
            Created repository (not yet committed)
        """
        repo = Repository(
            owner=owner,
            name=name,
            full_name=f"{owner}/{name}",
            is_active=is_active,
        )
        self.add(repo)
        await self.flush()
        return repo

    async def get_or_create(
        self,
        owner: str,
        name: str,
    ) -> tuple[Repository, bool]:
        """Get existing repository or create a new one.

        Args:
            owner: Repository owner
            name: Repository name

        Returns:
            Tuple of (repository, created) where created is True if new
        """
        existing = await self.get_by_owner_and_name(owner, name)
        if existing is not None:
            return existing, False

        repo = await self.create(owner, name)
        return repo, True

    async def update_last_synced(
        self,
        repository_id: int,
        synced_at: datetime,
    ) -> Repository | None:
        """Update the last_synced_at timestamp for a repository.

        Args:
            repository_id: Repository ID
            synced_at: Timestamp of the sync

        Returns:
            Updated repository or None if not found
        """
        repo = await self.get_by_id(repository_id)
        if repo is None:
            return None

        repo.last_synced_at = synced_at
        await self.flush()
        return repo

    async def deactivate(self, repository_id: int) -> Repository | None:
        """Mark a repository as inactive.

        Args:
            repository_id: Repository ID

        Returns:
            Updated repository or None if not found
        """
        repo = await self.get_by_id(repository_id)
        if repo is None:
            return None

        repo.is_active = False
        await self.flush()
        return repo

    async def activate(self, repository_id: int) -> Repository | None:
        """Mark a repository as active.

        Args:
            repository_id: Repository ID

        Returns:
            Updated repository or None if not found
        """
        repo = await self.get_by_id(repository_id)
        if repo is None:
            return None

        repo.is_active = True
        await self.flush()
        return repo
