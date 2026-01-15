"""Configuration settings for GitHub Activity DB."""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
