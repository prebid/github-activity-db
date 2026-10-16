"""Tests for the tracked-repository configuration."""

from pathlib import Path

import pytest

from github_activity_db.config import DEFAULT_TRACKING_CONFIG, Settings
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
]


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "tracking.toml"
    path.write_text(body)
    return path


REPO = '[[repositories]]\nowner = "prebid"\nname = "{name}"\ngroup = "{group}"\n'


class TestCommittedConfig:
    def test_committed_file_lists_the_tracked_repositories_in_order(self) -> None:
        config = load_tracking_config(DEFAULT_TRACKING_CONFIG)
        assert config.full_names == EXPECTED_REPOS

    def test_every_repository_has_a_group(self) -> None:
        config = load_tracking_config(DEFAULT_TRACKING_CONFIG)
        assert all(repo.group for repo in config.repositories)

    def test_settings_reads_the_configured_path(self, tmp_path: Path, monkeypatch) -> None:
        path = _write(tmp_path, REPO.format(name="only-repo", group="Tools"))
        monkeypatch.setenv("TRACKING_CONFIG_PATH", str(path))
        assert Settings(_env_file=None).tracked_repos == ["prebid/only-repo"]


class TestValidation:
    def test_missing_file(self, tmp_path: Path) -> None:
        missing = tmp_path / "absent.toml"
        with pytest.raises(TrackingConfigError, match="not found"):
            load_tracking_config(missing)

    def test_invalid_toml(self, tmp_path: Path) -> None:
        with pytest.raises(TrackingConfigError, match="not valid TOML"):
            load_tracking_config(_write(tmp_path, "[[repositories]\n"))

    def test_empty_repository_list(self, tmp_path: Path) -> None:
        with pytest.raises(TrackingConfigError, match="repositories"):
            load_tracking_config(_write(tmp_path, "repositories = []\n"))

    def test_unknown_key_is_rejected(self, tmp_path: Path) -> None:
        body = REPO.format(name="a", group="Tools") + 'gropu = "typo"\n'
        with pytest.raises(TrackingConfigError, match="gropu"):
            load_tracking_config(_write(tmp_path, body))

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
