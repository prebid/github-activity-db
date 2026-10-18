"""Tests for configuration settings."""

import re
from pathlib import Path

import pytest

from github_activity_db.config import Settings, get_settings

REPO_ROOT = Path(__file__).resolve().parents[1]
# The git-ignored local file, never the tracked github_activity.db (D26).
LOCAL_DATABASE_URL = "sqlite+aiosqlite:///./github_activity.local.db"


class TestSettings:
    """Tests for Settings class."""

    def test_settings_defaults(self, monkeypatch):
        """Test default values are correct."""
        for name in ("DATABASE_URL", "GITHUB_TOKEN", "ENVIRONMENT", "LOG_LEVEL"):
            monkeypatch.delenv(name, raising=False)
        settings = Settings(
            _env_file=None,  # Don't load .env
        )

        assert settings.database_url == LOCAL_DATABASE_URL
        assert settings.github_token is None
        assert settings.environment == "development"
        assert settings.log_level == "INFO"

    def test_settings_tracked_repos(self):
        """Test tracked_repos property returns 29 Prebid repos."""
        settings = Settings(_env_file=None)
        repos = settings.tracked_repos

        assert len(repos) == 29
        assert len(set(repos)) == len(repos)
        assert "prebid/prebid-server" in repos
        assert "prebid/prebid-server-java" in repos
        assert "prebid/Prebid.js" in repos
        assert "prebid/prebid.github.io" in repos
        assert "prebid/prebid-mobile-android" in repos
        assert "prebid/prebid-mobile-ios" in repos
        assert "prebid/prebid-universal-creative" in repos
        assert "prebid/professor-prebid" in repos
        assert "prebid/salesagent" in repos
        assert "prebid/prebid-agent-skills" in repos
        assert "prebid/line-item-manager" in repos
        assert "prebid/go-gdpr" in repos
        assert "prebid/prebid-cache-java" in repos
        assert "prebid/repo-activity-dashboard" in repos
        assert "prebid/prebid-mobile-ios-sdk" in repos
        assert "prebid/openrtb" in repos
        assert "prebid/github-activity-db" in repos
        assert "prebid/prebid-integration-monitor" in repos
        assert "prebid/prebid-cache" in repos
        assert "prebid/user-sync" in repos
        assert "prebid/shared-id" in repos
        assert "prebid/Shared-id-v2" in repos
        assert "prebid/prebid-mobile-ios-adapters" in repos
        assert "prebid/prebid-js-build-generator" in repos
        assert "prebid/code-scanner" in repos
        assert "prebid/currency-file" in repos
        assert "prebid/currency-file-generator" in repos
        assert "prebid/prebidjs-releaser" in repos
        assert "prebid/automation-testing" in repos

    def test_settings_from_env(self, monkeypatch):
        """Test environment variables override defaults."""
        monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///./test.db")
        monkeypatch.setenv("GITHUB_TOKEN", "test_token_123")
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")

        settings = Settings(_env_file=None)

        assert settings.database_url == "sqlite+aiosqlite:///./test.db"
        assert settings.github_token is not None
        assert settings.github_token.get_secret_value() == "test_token_123"
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
        assert settings.github_token is not None
        assert settings.github_token.get_secret_value() == "upper_token"

    def test_token_is_hidden_in_repr(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "secret-token-value")
        settings = Settings(_env_file=None)
        assert "secret-token-value" not in repr(settings)
        assert "secret-token-value" not in str(settings.model_dump())

    def test_nested_settings_read_double_underscore_variables(self, monkeypatch):
        """LOGGING__ROTATION sets logging.rotation (redesign plan gap 64)."""
        monkeypatch.setenv("LOGGING__ROTATION", "1 day")
        monkeypatch.setenv("LOGGING__RETENTION", "3 days")
        settings = Settings(_env_file=None)
        assert settings.logging.rotation == "1 day"
        assert settings.logging.retention == "3 days"

    def test_removed_pipeline_settings_are_gone(self):
        fields = set(Settings.model_fields)
        assert fields.isdisjoint({"rate_limit", "pacing", "sync"})


class TestDocumentedDatabaseUrl:
    """The documented DATABASE_URL lines show the default, which names the local file (D26)."""

    @pytest.mark.parametrize("document", [".env.example", "docs/development.md"])
    def test_names_the_local_database(self, document: str) -> None:
        lines = re.findall(r"^#? ?DATABASE_URL=(.*)$", (REPO_ROOT / document).read_text(), re.M)
        assert lines == [LOCAL_DATABASE_URL]

    def test_the_field_default_is_the_local_database(self) -> None:
        assert Settings.model_fields["database_url"].default == LOCAL_DATABASE_URL


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
