"""Pydantic schemas for UserTag model."""

import re
from datetime import datetime

from pydantic import Field, field_validator

from .base import SchemaBase

# Regex pattern for hex color codes
HEX_COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")


class UserTagCreate(SchemaBase):
    """Schema for creating a new user tag."""

    name: str = Field(max_length=100, description="Tag name (unique)")
    description: str | None = Field(
        default=None, max_length=500, description="Optional tag description"
    )
    color: str | None = Field(
        default=None, max_length=7, description="Hex color code (e.g., '#ff0000')"
    )

    @field_validator("color")
    @classmethod
    def validate_hex_color(cls, v: str | None) -> str | None:
        """Validate that color is a valid hex color code."""
        if v is None:
            return v
        if not HEX_COLOR_PATTERN.match(v):
            raise ValueError("Color must be a valid hex code (e.g., '#ff0000')")
        return v.lower()  # Normalize to lowercase


class UserTagRead(SchemaBase):
    """Schema for reading user tag data."""

    id: int
    name: str
    description: str | None
    color: str | None
    created_at: datetime
