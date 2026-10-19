"""Tracked-repository configuration: tracking.toml, shipped inside this package."""

from __future__ import annotations

import tomllib
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Final

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

if TYPE_CHECKING:
    from importlib.resources.abc import Traversable


class TrackingConfigError(ValueError):
    """The tracking configuration file is missing or invalid."""


# The most minutes a timedelta holds, so the planner can build any window the loader accepts.
MAX_UNSETTLED_MINUTES: Final = timedelta.max // timedelta(minutes=1)


class CoverageConfig(BaseModel):
    """``[coverage]``: the floor of the coverage rule (redesign plan section 3).

    Strict, because lax mode accepts the TOML string ``"2023-01-01T00:00:00Z"`` and the integer
    1672531200 (plan section 7, "Lax datetimes"); a date-time without an offset is rejected too.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    floor: AwareDatetime

    @field_validator("floor")
    @classmethod
    def _in_utc(cls, value: datetime) -> datetime:
        try:
            return value.astimezone(UTC)
        except OverflowError as e:
            # pydantic reports a ValueError as a validation error; an OverflowError escapes it.
            raise ValueError(f"{value.isoformat()} is outside the range of a UTC datetime") from e


class SyncConfig(BaseModel):
    """``[sync]``: the planner's unsettled window and step 7's delete guard (plan 5.3, 5.5)."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    # An item is re-hydrated while hydrated_at - github_updated_at is below this (plan 5.3 step 3).
    unsettled_minutes: int = Field(default=10, ge=1, le=MAX_UNSETTLED_MINUTES)
    # Step 7 halts a repository's deletions above max(delete_guard_min_rows, delete_guard_percent
    # of its stored rows).
    delete_guard_min_rows: int = Field(default=25, ge=0)
    delete_guard_percent: float = Field(default=2.0, gt=0, le=100, allow_inf_nan=False)


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
    """Contents of tracking.toml."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    coverage: CoverageConfig
    repositories: tuple[TrackedRepository, ...] = Field(min_length=1)
    sync: SyncConfig = Field(default_factory=SyncConfig)

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


def load_tracking_config(path: Path | None = None) -> TrackingConfig:
    """Read and validate the tracking configuration.

    Args:
        path: A tracking.toml to read. None reads the file shipped in this package.

    Raises:
        TrackingConfigError: The file is missing or unreadable, is not valid TOML, or fails
            validation.
    """
    file: Traversable = (
        resources.files("github_activity_db").joinpath("tracking.toml") if path is None else path
    )
    source = f"package resource {file}" if path is None else str(path)
    try:
        raw = file.read_bytes()
    except FileNotFoundError as e:
        raise TrackingConfigError(f"tracking config not found: {source}") from e
    except OSError as e:
        raise TrackingConfigError(f"cannot read tracking config {source}: {e}") from e
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise TrackingConfigError(f"tracking config is not valid TOML: {source}: {e}") from e
    try:
        return TrackingConfig.model_validate(data)
    except ValidationError as e:
        raise TrackingConfigError(f"invalid tracking config {source}:\n{e}") from e
