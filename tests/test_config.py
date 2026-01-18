"""Tests for configuration settings."""

import pytest

from github_activity_db.config import Settings, get_settings


class TestSettings:
    """Tests for Settings class."""

    def test_settings_defaults(self):
        """Test default values are correct."""
        # Create settings without env vars
        settings = Settings(
            _env_file=None,  # Don't load .env
        )

        assert settings.database_url == "sqlite+aiosqlite:///./github_activity.db"
        assert settings.github_token == ""
        assert settings.environment == "development"
        assert settings.log_level == "INFO"

    def test_settings_tracked_repos(self):
        """Test tracked_repos property returns 8 Prebid repos."""
        settings = Settings(_env_file=None)
        repos = settings.tracked_repos

        assert len(repos) == 8
        assert "prebid/prebid-server" in repos
        assert "prebid/prebid-server-java" in repos
        assert "prebid/Prebid.js" in repos
        assert "prebid/prebid.github.io" in repos
        assert "prebid/prebid-mobile-android" in repos
        assert "prebid/prebid-mobile-ios" in repos
        assert "prebid/prebid-universal-creative" in repos
        assert "prebid/professor-prebid" in repos

    def test_settings_from_env(self, monkeypatch):
        """Test environment variables override defaults."""
        monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///./test.db")
        monkeypatch.setenv("GITHUB_TOKEN", "test_token_123")
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")

        settings = Settings(_env_file=None)

        assert settings.database_url == "sqlite+aiosqlite:///./test.db"
        assert settings.github_token == "test_token_123"
        assert settings.environment == "production"
        assert settings.log_level == "DEBUG"

    def test_settings_environment_validation(self, monkeypatch):
        """Test that invalid environment value is rejected."""
        monkeypatch.setenv("ENVIRONMENT", "invalid")

        with pytest.raises(ValueError):
            Settings(_env_file=None)

    def test_settings_log_level_validation(self, monkeypatch):
        """Test that invalid log level is rejected."""
        monkeypatch.setenv("LOG_LEVEL", "INVALID")

        with pytest.raises(ValueError):
            Settings(_env_file=None)

    def test_settings_case_insensitive(self, monkeypatch):
        """Test that env var names are case-insensitive."""
        monkeypatch.setenv("database_url", "sqlite+aiosqlite:///./lower.db")
        monkeypatch.setenv("GITHUB_TOKEN", "upper_token")

        settings = Settings(_env_file=None)

        assert settings.database_url == "sqlite+aiosqlite:///./lower.db"
        assert settings.github_token == "upper_token"


class TestGetSettings:
    """Tests for get_settings function."""

    def test_get_settings_returns_settings(self):
        """Test that get_settings returns a Settings instance."""
        # Clear cache to ensure fresh settings
        get_settings.cache_clear()

        settings = get_settings()

        assert isinstance(settings, Settings)

    def test_get_settings_cached(self):
        """Test that get_settings returns cached instance."""
        get_settings.cache_clear()

        settings1 = get_settings()
        settings2 = get_settings()

        # Should be the same object (cached)
        assert settings1 is settings2
