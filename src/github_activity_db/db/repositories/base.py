"""Base repository pattern implementation for async SQLAlchemy.

Provides common session handling and CRUD operations that can be
shared across all repositories.
"""

import asyncio
from typing import Generic, TypeVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from github_activity_db.db.models import Base

# Generic type variable for model classes
ModelT = TypeVar("ModelT", bound=Base)


class BaseRepository(Generic[ModelT]):
    """Base repository with common async session handling.

    All repositories should inherit from this class to get
    consistent session management and common query patterns.

    Usage:
        class UserRepository(BaseRepository[User]):
            def __init__(self, session: AsyncSession) -> None:
                super().__init__(session, User)

            async def get_by_email(self, email: str) -> User | None:
                return await self._get_by_field("email", email)

    Concurrency:
        When multiple coroutines share the same session (e.g., during bulk
        ingestion), pass a shared write_lock to serialize database writes
        and avoid "Session.add() during flush" errors.
    """

    def __init__(
        self,
        session: AsyncSession,
        model_class: type[ModelT],
        write_lock: asyncio.Lock | None = None,
    ) -> None:
        """Initialize the repository with a session.

        Args:
            session: Async SQLAlchemy session (caller manages lifecycle)
            model_class: The SQLAlchemy model class this repository manages
            write_lock: Optional lock to serialize write operations (shared across repos)
        """
        self._session = session
        self._model_class = model_class
        self._write_lock = write_lock

    @property
    def session(self) -> AsyncSession:
        """Access the underlying session."""
        return self._session

    # -------------------------------------------------------------------------
    # Common Read Operations
    # -------------------------------------------------------------------------

    async def get_by_id(self, id: int) -> ModelT | None:
        """Get an entity by its primary key ID.

        Args:
            id: Primary key ID

        Returns:
            Entity or None if not found
        """
        return await self._session.get(self._model_class, id)

    async def _get_by_field(self, field_name: str, value: object) -> ModelT | None:
        """Get an entity by a specific field value.

        Args:
            field_name: Name of the model field
            value: Value to match

        Returns:
            First matching entity or None
        """
        stmt = select(self._model_class).where(
            getattr(self._model_class, field_name) == value
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_all(self, limit: int | None = None) -> list[ModelT]:
        """Get all entities, optionally limited.

        Args:
            limit: Maximum number of entities to return

        Returns:
            List of entities
        """
        stmt = select(self._model_class)
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    # -------------------------------------------------------------------------
    # Common Write Operations
    # -------------------------------------------------------------------------

    def add(self, entity: ModelT) -> ModelT:
        """Add an entity to the session (does not flush).

        The entity will be persisted when the session commits or flushes.

        Args:
            entity: Entity to add

        Returns:
            The same entity (for chaining)
        """
        self._session.add(entity)
        return entity

    async def flush(self) -> None:
        """Flush pending changes to the database.

        This executes SQL but does not commit the transaction.
        Useful for getting generated IDs before commit.

        If a write_lock was provided, acquires it to serialize flushes.
        """
        if self._write_lock:
            async with self._write_lock:
                await self._session.flush()
        else:
            await self._session.flush()

    async def refresh(self, entity: ModelT) -> ModelT:
        """Refresh an entity from the database.

        Args:
            entity: Entity to refresh

        Returns:
            The refreshed entity
        """
        await self._session.refresh(entity)
        return entity

    async def delete(self, entity: ModelT) -> None:
        """Mark an entity for deletion.

        The deletion happens on commit/flush.

        Args:
            entity: Entity to delete
        """
        await self._session.delete(entity)

    # -------------------------------------------------------------------------
    # Utility Methods
    # -------------------------------------------------------------------------

    async def exists(self, id: int) -> bool:
        """Check if an entity with the given ID exists.

        Args:
            id: Primary key ID

        Returns:
            True if entity exists
        """
        entity = await self.get_by_id(id)
        return entity is not None

    async def count(self) -> int:
        """Count total entities of this type.

        Returns:
            Total count
        """
        from sqlalchemy import func

        stmt = select(func.count()).select_from(self._model_class)
        result = await self._session.execute(stmt)
        return result.scalar() or 0
