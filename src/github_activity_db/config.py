"""Configuration settings for GitHub Activity DB."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from github_activity_db.tracking import load_tracking_config


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
        # LOGGING__ROTATION sets logging.rotation (redesign plan gap 64).
        env_nested_delimiter="__",
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
    github_token: SecretStr | None = Field(
        default=None,
        description="GitHub personal access token; cli.common.github_token() requires it",
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
    # Logging Configuration
    # --------------------------------------------------------------------------
    logging: LoggingConfig = Field(
        default_factory=LoggingConfig,
        description="Logging configuration (file output, rotation)",
    )

    # --------------------------------------------------------------------------
    # Tracked Repositories
    # --------------------------------------------------------------------------
    tracking_config_path: Path | None = Field(
        default=None,
        description="tracking.toml to read instead of the one shipped in the package",
    )

    @field_validator("tracking_config_path", mode="before")
    @classmethod
    def _empty_path_means_packaged(cls, value: object) -> object:
        # `TRACKING_CONFIG_PATH=` in .env would otherwise become Path("."), the working directory.
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def tracked_repos(self) -> list[str]:
        """Tracked repositories in owner/name form, in tracking.toml order."""
        return load_tracking_config(self.tracking_config_path).full_names


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
