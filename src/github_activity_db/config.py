"""Configuration settings for GitHub Activity DB."""

from datetime import timedelta
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class RateLimitConfig(BaseModel):
    """Configuration for rate limit monitoring.

    Controls thresholds for health status determination and
    behavior of the rate limit monitor.
    """

    # Threshold percentages for status determination
    healthy_threshold_pct: float = Field(
        default=50.0,
        ge=0.0,
        le=100.0,
        description="% remaining above which status is HEALTHY",
    )
    warning_threshold_pct: float = Field(
        default=20.0,
        ge=0.0,
        le=100.0,
        description="% remaining above which status is WARNING (below healthy)",
    )
    critical_threshold_pct: float = Field(
        default=5.0,
        ge=0.0,
        le=100.0,
        description="% remaining above which status is CRITICAL (below warning)",
    )

    # Safety margins
    min_remaining_buffer: int = Field(
        default=100,
        ge=0,
        description="Reserve buffer of requests to keep available",
    )

    # Behavior
    track_from_headers: bool = Field(
        default=True,
        description="Passively track limits from response headers",
    )


class PacingConfig(BaseModel):
    """Configuration for request pacing and scheduling.

    Controls timing, concurrency, and batch behavior.
    """

    # Concurrency
    max_concurrent_requests: int = Field(
        default=5,
        ge=1,
        le=100,
        description="Maximum parallel GitHub API requests",
    )
    max_batch_size: int = Field(
        default=50,
        ge=1,
        le=1000,
        description="Maximum items per batch operation",
    )

    # Timing bounds
    min_request_interval_ms: int = Field(
        default=50,
        ge=0,
        description="Minimum milliseconds between requests",
    )
    max_request_interval_ms: int = Field(
        default=60000,
        ge=100,
        description="Maximum milliseconds between requests (60 seconds)",
    )

    # Safety margins
    reserve_buffer_pct: float = Field(
        default=10.0,
        ge=0.0,
        le=50.0,
        description="Percentage of quota to reserve as buffer",
    )
    burst_allowance: int = Field(
        default=10,
        ge=0,
        description="Number of requests allowed in short bursts",
    )


class SyncConfig(BaseModel):
    """Configuration for PR sync behavior.

    Controls how PRs are synced from GitHub to the database,
    including grace periods for merged PRs and commit batch sizes.
    """

    merge_grace_period_days: int = Field(
        default=14,
        ge=0,
        description="Days after merge before PR is frozen (0 = freeze immediately)",
    )

    commit_batch_size: int = Field(
        default=25,
        ge=1,
        le=100,
        description="PRs to commit per batch (limits data loss on failure)",
    )

    @property
    def merge_grace_period(self) -> timedelta:
        """Get the grace period as a timedelta."""
        return timedelta(days=self.merge_grace_period_days)


class LoggingConfig(BaseModel):
    """Configuration for logging behavior.

    Controls file logging, rotation, and output format.
    """

    log_file: str | None = Field(
        default=None,
        description="Optional path for file logging (enables rotation)",
    )
    rotation: str = Field(
        default="10 MB",
        description="When to rotate log file (e.g., '10 MB', '1 day')",
    )
    retention: str = Field(
        default="7 days",
        description="How long to keep rotated logs",
    )
    serialize: bool = Field(
        default=False,
        description="If True, output JSON format to file",
    )


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --------------------------------------------------------------------------
    # Database
    # --------------------------------------------------------------------------
    database_url: str = Field(
        default="sqlite+aiosqlite:///./github_activity.db",
        description="Async SQLite database connection string",
    )

    # --------------------------------------------------------------------------
    # GitHub API
    # --------------------------------------------------------------------------
    github_token: str = Field(
        default="",
        description="GitHub personal access token",
    )

    # --------------------------------------------------------------------------
    # Application
    # --------------------------------------------------------------------------
    environment: Literal["development", "staging", "production"] = Field(
        default="development",
        description="Application environment",
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO",
        description="Logging level",
    )

    # --------------------------------------------------------------------------
    # Rate Limiting & Pacing
    # --------------------------------------------------------------------------
    rate_limit: RateLimitConfig = Field(
        default_factory=RateLimitConfig,
        description="Rate limit monitoring configuration",
    )
    pacing: PacingConfig = Field(
        default_factory=PacingConfig,
        description="Request pacing configuration",
    )

    # --------------------------------------------------------------------------
    # Sync Configuration
    # --------------------------------------------------------------------------
    sync: SyncConfig = Field(
        default_factory=SyncConfig,
        description="PR sync behavior configuration",
    )

    # --------------------------------------------------------------------------
    # Logging Configuration
    # --------------------------------------------------------------------------
    logging: LoggingConfig = Field(
        default_factory=LoggingConfig,
        description="Logging configuration (file output, rotation)",
    )

    # --------------------------------------------------------------------------
    # Tracked Repositories
    # --------------------------------------------------------------------------
    @property
    def tracked_repos(self) -> list[str]:
        """List of GitHub repositories to track."""
        return [
            "prebid/prebid-server",
            "prebid/prebid-server-java",
            "prebid/Prebid.js",
            "prebid/prebid.github.io",
            "prebid/prebid-mobile-android",
            "prebid/prebid-mobile-ios",
            "prebid/prebid-universal-creative",
            "prebid/professor-prebid",
        ]


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
