"""Tracked-repository configuration loaded from config/tracking.toml."""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class TrackingConfigError(ValueError):
    """The tracking configuration file is missing or invalid."""


class TrackedRepository(BaseModel):
    """One repository to sync."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    owner: str = Field(min_length=1)
    name: str = Field(min_length=1)
    group: str = Field(min_length=1)

    @field_validator("owner", "name")
    @classmethod
    def _single_path_segment(cls, value: str) -> str:
        if "/" in value or value != value.strip() or any(c.isspace() for c in value):
            raise ValueError(f"must be a single path segment without whitespace: {value!r}")
        return value

    @property
    def full_name(self) -> str:
        """Repository in owner/name form."""
        return f"{self.owner}/{self.name}"


class TrackingConfig(BaseModel):
    """Contents of config/tracking.toml."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repositories: tuple[TrackedRepository, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_repositories(self) -> TrackingConfig:
        seen: set[str] = set()
        for repo in self.repositories:
            key = repo.full_name.lower()  # GitHub repository names are case-insensitive
            if key in seen:
                raise ValueError(f"repository listed more than once: {repo.full_name}")
            seen.add(key)
        return self

    @property
    def full_names(self) -> list[str]:
        """Repositories in owner/name form, in file order."""
        return [repo.full_name for repo in self.repositories]


def load_tracking_config(path: Path) -> TrackingConfig:
    """Read and validate the tracking configuration.

    Raises:
        TrackingConfigError: The file is missing, is not valid TOML, or fails validation.
    """
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError as e:
        raise TrackingConfigError(f"tracking config not found: {path}") from e
    except tomllib.TOMLDecodeError as e:
        raise TrackingConfigError(f"tracking config is not valid TOML: {path}: {e}") from e
    try:
        return TrackingConfig.model_validate(data)
    except ValidationError as e:
        raise TrackingConfigError(f"invalid tracking config {path}:\n{e}") from e
