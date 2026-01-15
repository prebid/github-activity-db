"""Pydantic schemas for Repository model."""

from datetime import datetime

from pydantic import Field

from .base import SchemaBase


class RepositoryCreate(SchemaBase):
    """Schema for creating a new repository."""

    owner: str = Field(max_length=100, description="GitHub org or user (e.g., 'prebid')")
    name: str = Field(max_length=100, description="Repository name (e.g., 'prebid-server')")
    full_name: str = Field(
        max_length=200,
        description="Full repository path (e.g., 'prebid/prebid-server')",
    )

    @classmethod
    def from_full_name(cls, full_name: str) -> "RepositoryCreate":
        """
        Factory method to create from a full repository name.

        Args:
            full_name: Full repo path like 'prebid/prebid-server'

        Returns:
            RepositoryCreate instance with owner and name extracted
        """
        owner, name = full_name.split("/", 1)
        return cls(owner=owner, name=name, full_name=full_name)


class RepositoryRead(SchemaBase):
    """Schema for reading repository data."""

    id: int
    owner: str
    name: str
    full_name: str
    is_active: bool
    last_synced_at: datetime | None
    created_at: datetime
