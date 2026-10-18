"""Tests for the tracked-repository configuration."""

from pathlib import Path

import pytest

from github_activity_db.config import Settings
from github_activity_db.tracking import TrackingConfigError, load_tracking_config

# The list the hardcoded Settings.tracked_repos returned before tracking.toml.
EXPECTED_REPOS = [
    "prebid/prebid-server",
    "prebid/prebid-server-java",
    "prebid/Prebid.js",
    "prebid/prebid.github.io",
    "prebid/prebid-mobile-android",
    "prebid/prebid-mobile-ios",
    "prebid/prebid-universal-creative",
    "prebid/professor-prebid",
    "prebid/salesagent",
    "prebid/prebid-agent-skills",
    "prebid/line-item-manager",
    "prebid/go-gdpr",
    "prebid/prebid-cache-java",
    "prebid/repo-activity-dashboard",
    "prebid/prebid-mobile-ios-sdk",
    "prebid/openrtb",
    "prebid/github-activity-db",
    "prebid/prebid-integration-monitor",
    "prebid/prebid-cache",
    "prebid/user-sync",
    "prebid/shared-id",
    "prebid/Shared-id-v2",
    "prebid/prebid-mobile-ios-adapters",
    "prebid/prebid-js-build-generator",
    "prebid/code-scanner",
    "prebid/currency-file",
    "prebid/currency-file-generator",
    "prebid/prebidjs-releaser",
    "prebid/automation-testing",
]


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "tracking.toml"
    path.write_text(body)
    return path


REPO = '[[repositories]]\nowner = "prebid"\nname = "{name}"\ngroup = "{group}"\n'


class TestPackagedConfig:
    def test_packaged_file_lists_the_tracked_repositories_in_order(self) -> None:
        config = load_tracking_config()
        assert config.full_names == EXPECTED_REPOS

    def test_every_repository_has_a_group(self) -> None:
        config = load_tracking_config()
        assert all(repo.group for repo in config.repositories)

    def test_settings_default_reads_the_packaged_file(self, monkeypatch) -> None:
        monkeypatch.delenv("TRACKING_CONFIG_PATH", raising=False)
        settings = Settings(_env_file=None)
        assert settings.tracking_config_path is None
        assert settings.tracked_repos == EXPECTED_REPOS

    @pytest.mark.parametrize("value", ["", "  "])
    def test_empty_setting_reads_the_packaged_file(self, monkeypatch, value: str) -> None:
        monkeypatch.setenv("TRACKING_CONFIG_PATH", value)
        settings = Settings(_env_file=None)
        assert settings.tracking_config_path is None
        assert settings.tracked_repos == EXPECTED_REPOS

    def test_empty_setting_in_env_file_reads_the_packaged_file(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.delenv("TRACKING_CONFIG_PATH", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("TRACKING_CONFIG_PATH=\n")
        settings = Settings(_env_file=env_file)
        assert settings.tracking_config_path is None
        assert settings.tracked_repos == EXPECTED_REPOS

    def test_settings_reads_the_configured_path(self, tmp_path: Path, monkeypatch) -> None:
        path = _write(tmp_path, REPO.format(name="only-repo", group="Tools"))
        monkeypatch.setenv("TRACKING_CONFIG_PATH", str(path))
        assert Settings(_env_file=None).tracked_repos == ["prebid/only-repo"]


class TestValidation:
    def test_missing_file(self, tmp_path: Path) -> None:
        missing = tmp_path / "absent.toml"
        with pytest.raises(TrackingConfigError, match="not found"):
            load_tracking_config(missing)

    def test_unreadable_path(self, tmp_path: Path) -> None:
        # A directory raises IsADirectoryError, an OSError other than FileNotFoundError.
        with pytest.raises(TrackingConfigError, match="cannot read tracking config"):
            load_tracking_config(tmp_path)

    def test_invalid_toml(self, tmp_path: Path) -> None:
        with pytest.raises(TrackingConfigError, match="not valid TOML"):
            load_tracking_config(_write(tmp_path, "[[repositories]\n"))

    def test_non_utf8_file(self, tmp_path: Path) -> None:
        path = tmp_path / "tracking.toml"
        path.write_bytes(b"\xff\xfe")
        with pytest.raises(TrackingConfigError, match="not valid TOML"):
            load_tracking_config(path)

    def test_empty_repository_list(self, tmp_path: Path) -> None:
        with pytest.raises(TrackingConfigError, match="repositories"):
            load_tracking_config(_write(tmp_path, "repositories = []\n"))

    def test_unknown_key_is_rejected(self, tmp_path: Path) -> None:
        body = REPO.format(name="a", group="Tools") + 'gropu = "typo"\n'
        with pytest.raises(TrackingConfigError, match="gropu"):
            load_tracking_config(_write(tmp_path, body))

    def test_unknown_top_level_key_is_rejected(self, tmp_path: Path) -> None:
        body = "extra_setting = 1\n" + REPO.format(name="a", group="Tools")
        with pytest.raises(TrackingConfigError, match="extra_setting"):
            load_tracking_config(_write(tmp_path, body))

    @pytest.mark.parametrize("field", ["owner", "name", "group"])
    def test_empty_value_is_rejected(self, tmp_path: Path, field: str) -> None:
        values = {"owner": "prebid", "name": "a", "group": "Tools", field: ""}
        body = "[[repositories]]\n" + "".join(f'{k} = "{v}"\n' for k, v in values.items())
        with pytest.raises(TrackingConfigError, match="at least 1 character") as error:
            load_tracking_config(_write(tmp_path, body))
        assert f"repositories.0.{field}" in str(error.value)

    def test_missing_group_is_rejected(self, tmp_path: Path) -> None:
        body = '[[repositories]]\nowner = "prebid"\nname = "a"\n'
        with pytest.raises(TrackingConfigError, match="group"):
            load_tracking_config(_write(tmp_path, body))

    def test_duplicate_repository_ignores_case(self, tmp_path: Path) -> None:
        body = REPO.format(name="Prebid.js", group="Client") + REPO.format(
            name="prebid.js", group="Client"
        )
        with pytest.raises(TrackingConfigError, match="more than once"):
            load_tracking_config(_write(tmp_path, body))

    @pytest.mark.parametrize("name", ["owner/repo", "has space", " leading"])
    def test_name_must_be_one_path_segment(self, tmp_path: Path, name: str) -> None:
        with pytest.raises(TrackingConfigError, match="single path segment"):
            load_tracking_config(_write(tmp_path, REPO.format(name=name, group="Tools")))
