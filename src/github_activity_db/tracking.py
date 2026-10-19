"""Tracked-repository configuration: tracking.toml, shipped inside this package."""

from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass
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

# The characters automation_emails' CHECK admits: printable ASCII, space to tilde (plan 5.1).
_PRINTABLE_ASCII: Final = re.compile(r"[ -~]+")


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


class AcceptedFailure(BaseModel):
    """An item whose failure is understood: recorded and reported, and kept out of exit 2
    (plan 5.3 step 8). It matches by its configured repository and number, so a rename does not
    unhook it."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    repository: str = Field(min_length=1)
    number: int = Field(ge=1)
    reason: str = Field(min_length=1)


class SyncConfig(BaseModel):
    """``[sync]``: the planner's unsettled window, step 7's delete guard, and the accepted
    failures (plan 5.3, 5.5)."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    # An item is re-hydrated while hydrated_at - github_updated_at is below this (plan 5.3 step 3).
    unsettled_minutes: int = Field(default=10, ge=1, le=MAX_UNSETTLED_MINUTES)
    # Step 7 halts a repository's deletions above max(delete_guard_min_rows, delete_guard_percent
    # of its stored rows).
    delete_guard_min_rows: int = Field(default=25, ge=0)
    delete_guard_percent: float = Field(default=2.0, gt=0, le=100, allow_inf_nan=False)
    # TOML gives a list; each entry is validated strictly.
    accepted_failures: tuple[AcceptedFailure, ...] = Field(default=(), strict=False)


class AutomationAccount(BaseModel):
    """An account the maintainer lists, keyed by GitHub's ``databaseId`` (plan 5.2)."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    github_id: int = Field(ge=1)
    login: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class AutomationEmail(BaseModel):
    """A commit email the maintainer lists, stored lowercase (plan 5.1)."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    email: str = Field(min_length=1)
    reason: str = Field(min_length=1)

    @field_validator("email")
    @classmethod
    def _lowercase_printable_ascii(cls, value: str) -> str:
        # SQLite's lower(), which the views apply to commit emails, folds only ASCII letters.
        if _PRINTABLE_ASCII.fullmatch(value) is None:
            raise ValueError(f"must be printable ASCII (space to tilde): {value!r}")
        return value.lower()


class AutomationConfig(BaseModel):
    """``[automation]``: the accounts and commit emails each run copies into the automation
    tables; the views show the matching entry's reason beside GitHub's account type (plan 5.2)."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    # TOML gives lists; each entry is validated strictly.
    accounts: tuple[AutomationAccount, ...] = Field(default=(), strict=False)
    emails: tuple[AutomationEmail, ...] = Field(default=(), strict=False)

    @model_validator(mode="after")
    def _unique(self) -> AutomationConfig:
        ids = [account.github_id for account in self.accounts]
        repeated_ids = sorted({github_id for github_id in ids if ids.count(github_id) > 1})
        if repeated_ids:
            raise ValueError(f"automation account github_id listed more than once: {repeated_ids}")
        emails = [entry.email for entry in self.emails]
        repeated = sorted({email for email in emails if emails.count(email) > 1})
        if repeated:
            raise ValueError(f"automation email listed more than once, ignoring case: {repeated}")
        return self


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
    automation: AutomationConfig = Field(default_factory=AutomationConfig)
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

    @model_validator(mode="after")
    def _accepted_failures_name_configured_repositories(self) -> TrackingConfig:
        configured = {repo.full_name.lower() for repo in self.repositories}
        seen: set[tuple[str, int]] = set()
        for entry in self.sync.accepted_failures:
            key = (entry.repository.lower(), entry.number)
            if key[0] not in configured:
                raise ValueError(
                    f"accepted failure names {entry.repository}, which is not a configured "
                    "repository"
                )
            if key in seen:
                raise ValueError(
                    f"accepted failure listed more than once: {entry.repository}#{entry.number}"
                )
            seen.add(key)
        return self

    @property
    def full_names(self) -> list[str]:
        """Repositories in owner/name form, in file order."""
        return [repo.full_name for repo in self.repositories]

    def accepted_failure(
        self, repository: TrackedRepository, number: int
    ) -> AcceptedFailure | None:
        """The ``accepted_failures`` entry for this configured repository's item, ignoring case;
        None when the failure is not accepted."""
        name = repository.full_name.lower()
        for entry in self.sync.accepted_failures:
            if entry.repository.lower() == name and entry.number == number:
                return entry
        return None


@dataclass(frozen=True, slots=True)
class TrackingConfigFile:
    """A loaded tracking.toml: the configuration, where it came from, and the SHA-256 of the
    bytes parsed, which ``sync_runs`` records (gap 88)."""

    config: TrackingConfig
    # "packaged", or the path TRACKING_CONFIG_PATH names, made absolute (symlinks not followed).
    source: str
    sha256: str


def load_tracking_config(path: Path | None = None) -> TrackingConfig:
    """Read and validate the tracking configuration.

    Args:
        path: A tracking.toml to read. None reads the file shipped in this package.

    Raises:
        TrackingConfigError: The file is missing or unreadable, is not valid TOML, or fails
            validation.
    """
    return read_tracking_config(path).config


def read_tracking_config(path: Path | None = None) -> TrackingConfigFile:
    """Read and validate the tracking configuration, with its source and the digest of the
    bytes parsed (one read, so the digest describes exactly what the run used).

    Raises:
        TrackingConfigError: as ``load_tracking_config``.
    """
    file: Traversable = (
        resources.files("github_activity_db").joinpath("tracking.toml") if path is None else path
    )
    shown = f"package resource {file}" if path is None else str(path)
    try:
        raw = file.read_bytes()
    except FileNotFoundError as e:
        raise TrackingConfigError(f"tracking config not found: {shown}") from e
    except OSError as e:
        raise TrackingConfigError(f"cannot read tracking config {shown}: {e}") from e
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise TrackingConfigError(f"tracking config is not valid TOML: {shown}: {e}") from e
    try:
        config = TrackingConfig.model_validate(data)
    except ValidationError as e:
        raise TrackingConfigError(f"invalid tracking config {shown}:\n{e}") from e
    return TrackingConfigFile(
        config=config,
        source="packaged" if path is None else str(path.absolute()),
        sha256=hashlib.sha256(raw).hexdigest(),
    )
