"""Enums for sync operations."""

from enum import Enum


class SyncStrategy(str, Enum):
    """Strategy for syncing PRs from GitHub.

    Different strategies trade off between completeness and efficiency.
    """

    SINGLE = "single"
    """Sync a single PR by number. (Phase 1.6)"""

    OPEN_ONLY = "open_only"
    """Sync all currently open PRs. Efficient for regular sync."""

    INCREMENTAL = "incremental"
    """Sync PRs updated since last_synced_at. Most efficient."""

    FULL = "full"
    """Sync all PRs regardless of state. Expensive, use for initial sync."""


class OutputFormat(str, Enum):
    """Output format for CLI commands."""

    TEXT = "text"
    """Human-readable text output."""

    JSON = "json"
    """Machine-readable JSON output."""
