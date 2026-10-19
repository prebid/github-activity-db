"""Tests for the tracked-repository configuration."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from github_activity_db.config import Settings
from github_activity_db.tracking import (
    MAX_UNSETTLED_MINUTES,
    SyncConfig,
    TrackedRepository,
    TrackingConfigError,
    load_tracking_config,
)

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
COVERAGE = "[coverage]\nfloor = 2023-01-01T00:00:00Z\n"
ONE_REPO = REPO.format(name="a", group="Tools")


def _errors(tmp_path: Path, body: str) -> list[tuple[str, str]]:
    """Each validation error the loader reports for the file, as (location, error type)."""
    with pytest.raises(TrackingConfigError) as error:
        load_tracking_config(_write(tmp_path, body))
    cause = error.value.__cause__
    assert isinstance(cause, ValidationError), error.value
    return [(".".join(str(part) for part in e["loc"]), e["type"]) for e in cause.errors()]


# pydantic reports a failed repository entry and then the tuple left without it.
NO_REPOSITORY = ("repositories", "too_short")


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
        path = _write(tmp_path, COVERAGE + REPO.format(name="only-repo", group="Tools"))
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
        assert _errors(tmp_path, "repositories = []\n" + COVERAGE) == [NO_REPOSITORY]

    def test_unknown_key_is_rejected(self, tmp_path: Path) -> None:
        body = COVERAGE + ONE_REPO + 'gropu = "typo"\n'
        assert _errors(tmp_path, body) == [
            ("repositories.0.gropu", "extra_forbidden"),
            NO_REPOSITORY,
        ]

    def test_unknown_top_level_key_is_rejected(self, tmp_path: Path) -> None:
        body = "extra_setting = 1\n" + COVERAGE + ONE_REPO
        assert _errors(tmp_path, body) == [("extra_setting", "extra_forbidden")]

    @pytest.mark.parametrize("field", ["owner", "name", "group"])
    def test_empty_value_is_rejected(self, tmp_path: Path, field: str) -> None:
        values = {"owner": "prebid", "name": "a", "group": "Tools", field: ""}
        body = "[[repositories]]\n" + "".join(f'{k} = "{v}"\n' for k, v in values.items())
        assert _errors(tmp_path, COVERAGE + body) == [
            (f"repositories.0.{field}", "string_too_short"),
            NO_REPOSITORY,
        ]

    def test_missing_group_is_rejected(self, tmp_path: Path) -> None:
        body = COVERAGE + '[[repositories]]\nowner = "prebid"\nname = "a"\n'
        assert _errors(tmp_path, body) == [("repositories.0.group", "missing"), NO_REPOSITORY]

    def test_duplicate_repository_ignores_case(self, tmp_path: Path) -> None:
        body = REPO.format(name="Prebid.js", group="Client") + REPO.format(
            name="prebid.js", group="Client"
        )
        assert _errors(tmp_path, COVERAGE + body) == [("", "value_error")]
        with pytest.raises(TrackingConfigError, match=r"listed more than once: prebid/prebid\.js"):
            load_tracking_config(tmp_path / "tracking.toml")

    @pytest.mark.parametrize("name", ["owner/repo", "has space", " leading"])
    def test_name_must_be_one_path_segment(self, tmp_path: Path, name: str) -> None:
        body = COVERAGE + REPO.format(name=name, group="Tools")
        assert _errors(tmp_path, body) == [("repositories.0.name", "value_error"), NO_REPOSITORY]
        with pytest.raises(TrackingConfigError, match="single path segment"):
            load_tracking_config(tmp_path / "tracking.toml")


FLOOR = datetime(2023, 1, 1, tzinfo=UTC)


class TestCoverage:
    def test_the_packaged_floor(self) -> None:
        assert load_tracking_config().coverage.floor == FLOOR

    def test_a_utc_floor(self, tmp_path: Path) -> None:
        floor = load_tracking_config(_write(tmp_path, COVERAGE + ONE_REPO)).coverage.floor
        assert floor == FLOOR
        assert floor.tzinfo is UTC

    @pytest.mark.parametrize("spelling", ["2023-01-01T02:00:00+02:00", "2022-12-31T19:00:00-05:00"])
    def test_a_floor_with_another_offset_is_converted_to_utc(
        self, tmp_path: Path, spelling: str
    ) -> None:
        body = f"[coverage]\nfloor = {spelling}\n" + ONE_REPO
        floor = load_tracking_config(_write(tmp_path, body)).coverage.floor
        assert floor.tzinfo is UTC
        assert floor.isoformat() == "2023-01-01T00:00:00+00:00"

    def test_a_missing_coverage_table(self, tmp_path: Path) -> None:
        assert _errors(tmp_path, ONE_REPO) == [("coverage", "missing")]

    def test_a_missing_floor(self, tmp_path: Path) -> None:
        assert _errors(tmp_path, "[coverage]\n" + ONE_REPO) == [("coverage.floor", "missing")]

    @pytest.mark.parametrize(
        ("spelling", "error"),
        [
            ('"2023-01-01T00:00:00Z"', "datetime_type"),
            ("1672531200", "datetime_type"),
            ("2023-01-01T00:00:00", "timezone_aware"),
            ("2023-01-01", "datetime_type"),
            ("00:00:00", "datetime_type"),
            ("true", "datetime_type"),
        ],
        ids=["string", "integer", "local-date-time", "local-date", "local-time", "boolean"],
    )
    def test_a_floor_that_is_not_an_offset_date_time(
        self, tmp_path: Path, spelling: str, error: str
    ) -> None:
        body = f"[coverage]\nfloor = {spelling}\n" + ONE_REPO
        assert _errors(tmp_path, body) == [("coverage.floor", error)]

    @pytest.mark.parametrize("spelling", ["0001-01-01T00:00:00+01:00", "9999-12-31T23:30:00-01:00"])
    def test_a_floor_outside_the_utc_range(self, tmp_path: Path, spelling: str) -> None:
        body = f"[coverage]\nfloor = {spelling}\n" + ONE_REPO
        assert _errors(tmp_path, body) == [("coverage.floor", "value_error")]
        with pytest.raises(TrackingConfigError, match="outside the range of a UTC datetime"):
            load_tracking_config(tmp_path / "tracking.toml")

    @pytest.mark.parametrize("spelling", ["0001-01-01T00:00:00Z", "9999-12-31T23:59:59Z"])
    def test_a_floor_at_the_ends_of_the_utc_range(self, tmp_path: Path, spelling: str) -> None:
        body = f"[coverage]\nfloor = {spelling}\n" + ONE_REPO
        floor = load_tracking_config(_write(tmp_path, body)).coverage.floor
        assert floor.isoformat() == spelling.replace("Z", "+00:00")

    def test_an_unknown_coverage_key(self, tmp_path: Path) -> None:
        body = COVERAGE + "flor = 2023-01-01T00:00:00Z\n" + ONE_REPO
        assert _errors(tmp_path, body) == [("coverage.flor", "extra_forbidden")]


DEFAULTS = SyncConfig(unsettled_minutes=10, delete_guard_min_rows=25, delete_guard_percent=2.0)


class TestSyncSettings:
    def test_the_packaged_values(self) -> None:
        assert load_tracking_config().sync == DEFAULTS

    def test_defaults_without_a_sync_table(self, tmp_path: Path) -> None:
        assert load_tracking_config(_write(tmp_path, COVERAGE + ONE_REPO)).sync == DEFAULTS

    def test_defaults_with_an_empty_sync_table(self, tmp_path: Path) -> None:
        body = COVERAGE + "[sync]\n" + ONE_REPO
        assert load_tracking_config(_write(tmp_path, body)).sync == DEFAULTS

    def test_given_values(self, tmp_path: Path) -> None:
        body = (
            COVERAGE
            + "[sync]\nunsettled_minutes = 1\ndelete_guard_min_rows = 0\n"
            + "delete_guard_percent = 100\n"
            + ONE_REPO
        )
        sync = load_tracking_config(_write(tmp_path, body)).sync
        assert (sync.unsettled_minutes, sync.delete_guard_min_rows) == (1, 0)
        assert sync.delete_guard_percent == 100.0
        assert type(sync.delete_guard_percent) is float

    def test_one_value_given_and_the_others_default(self, tmp_path: Path) -> None:
        body = COVERAGE + "[sync]\ndelete_guard_percent = 0.5\n" + ONE_REPO
        sync = load_tracking_config(_write(tmp_path, body)).sync
        assert sync == SyncConfig(
            unsettled_minutes=10, delete_guard_min_rows=25, delete_guard_percent=0.5
        )

    @pytest.mark.parametrize(
        ("key", "value", "error"),
        [
            ("unsettled_minutes", "0", "greater_than_equal"),
            ("unsettled_minutes", "-1", "greater_than_equal"),
            ("unsettled_minutes", "1.5", "int_type"),
            ("unsettled_minutes", "10.0", "int_type"),
            ("unsettled_minutes", "true", "int_type"),
            ("unsettled_minutes", '"10"', "int_type"),
            ("unsettled_minutes", "1440000000000", "less_than_equal"),
            ("delete_guard_min_rows", "-1", "greater_than_equal"),
            ("delete_guard_min_rows", "25.0", "int_type"),
            ("delete_guard_min_rows", "false", "int_type"),
            ("delete_guard_min_rows", '"25"', "int_type"),
            ("delete_guard_percent", "0", "greater_than"),
            ("delete_guard_percent", "0.0", "greater_than"),
            ("delete_guard_percent", "-2.0", "greater_than"),
            ("delete_guard_percent", "100.000001", "less_than_equal"),
            ("delete_guard_percent", "101", "less_than_equal"),
            ("delete_guard_percent", "nan", "finite_number"),
            ("delete_guard_percent", "inf", "finite_number"),
            ("delete_guard_percent", "true", "float_type"),
            ("delete_guard_percent", '"2.0"', "float_type"),
        ],
    )
    def test_an_invalid_value(self, tmp_path: Path, key: str, value: str, error: str) -> None:
        body = COVERAGE + f"[sync]\n{key} = {value}\n" + ONE_REPO
        assert _errors(tmp_path, body) == [(f"sync.{key}", error)]

    def test_the_longest_unsettled_window(self, tmp_path: Path) -> None:
        """The most minutes a timedelta holds, so the planner can build the window."""
        assert MAX_UNSETTLED_MINUTES == 1_439_999_999_999
        body = COVERAGE + f"[sync]\nunsettled_minutes = {MAX_UNSETTLED_MINUTES}\n" + ONE_REPO
        sync = load_tracking_config(_write(tmp_path, body)).sync
        assert sync.unsettled_minutes == MAX_UNSETTLED_MINUTES

    def test_an_unknown_sync_key(self, tmp_path: Path) -> None:
        body = COVERAGE + "[sync]\nunsettled_minute = 5\n" + ONE_REPO
        assert _errors(tmp_path, body) == [("sync.unsettled_minute", "extra_forbidden")]


ACCOUNT = '{ github_id = 5, login = "bot", reason = "r" }'
EMAIL = '{ email = "bot@example.com", reason = "r" }'


def _automation(accounts: str = "", emails: str = "") -> str:
    return COVERAGE + ONE_REPO + f"[automation]\naccounts = [{accounts}]\nemails = [{emails}]\n"


class TestAutomation:
    def test_the_packaged_entries(self) -> None:
        automation = load_tracking_config().automation
        assert [(a.github_id, a.login, a.reason) for a in automation.accounts] == [
            (99054218, "prebidjs-release", "release automation"),
            (81847, "claude", "receives commits authored as noreply@anthropic.com"),
        ]
        assert [(e.email, e.reason) for e in automation.emails] == [
            ("noreply@anthropic.com", "Claude commits without a linked account")
        ]

    def test_defaults_without_an_automation_table(self, tmp_path: Path) -> None:
        automation = load_tracking_config(_write(tmp_path, COVERAGE + ONE_REPO)).automation
        assert (automation.accounts, automation.emails) == ((), ())

    def test_an_email_is_stored_lowercase(self, tmp_path: Path) -> None:
        body = _automation(emails='{ email = "Bot@Example.COM", reason = "r" }')
        (entry,) = load_tracking_config(_write(tmp_path, body)).automation.emails
        assert entry.email == "bot@example.com"

    @pytest.mark.parametrize(
        ("entry", "location", "error"),
        [
            ('{ github_id = 0, login = "bot", reason = "r" }', "github_id", "greater_than_equal"),
            ('{ github_id = true, login = "bot", reason = "r" }', "github_id", "int_type"),
            ('{ github_id = "5", login = "bot", reason = "r" }', "github_id", "int_type"),
            ('{ github_id = 5, login = "", reason = "r" }', "login", "string_too_short"),
            ('{ github_id = 5, login = "bot", reason = "" }', "reason", "string_too_short"),
            ('{ github_id = 5, login = "bot" }', "reason", "missing"),
            (
                '{ github_id = 5, login = "bot", reason = "r", note = "x" }',
                "note",
                "extra_forbidden",
            ),
        ],
    )
    def test_an_invalid_account(
        self, tmp_path: Path, entry: str, location: str, error: str
    ) -> None:
        assert _errors(tmp_path, _automation(accounts=entry)) == [
            (f"automation.accounts.0.{location}", error)
        ]

    @pytest.mark.parametrize(
        ("email", "error"),
        [
            ('""', "string_too_short"),
            ('"bot@exämple.com"', "value_error"),
            ('"a\\tb@example.com"', "value_error"),
            ('"a\\u007fb@example.com"', "value_error"),
            ('"\\u0000@example.com"', "value_error"),
        ],
        ids=["empty", "non-ascii", "tab", "del", "nul"],
    )
    def test_an_email_outside_printable_ascii(self, tmp_path: Path, email: str, error: str) -> None:
        body = _automation(emails=f'{{ email = {email}, reason = "r" }}')
        assert _errors(tmp_path, body) == [("automation.emails.0.email", error)]

    def test_a_space_is_printable(self, tmp_path: Path) -> None:
        body = _automation(emails='{ email = "a b@example.com", reason = "r" }')
        (entry,) = load_tracking_config(_write(tmp_path, body)).automation.emails
        assert entry.email == "a b@example.com"

    def test_an_account_listed_twice(self, tmp_path: Path) -> None:
        body = _automation(
            accounts=f'{ACCOUNT}, {{ github_id = 5, login = "other", reason = "r" }}'
        )
        assert _errors(tmp_path, body) == [("automation", "value_error")]
        with pytest.raises(TrackingConfigError, match=r"github_id listed more than once: \[5\]"):
            load_tracking_config(tmp_path / "tracking.toml")

    def test_an_email_listed_twice_ignoring_case(self, tmp_path: Path) -> None:
        body = _automation(emails=f'{EMAIL}, {{ email = "BOT@example.com", reason = "r" }}')
        assert _errors(tmp_path, body) == [("automation", "value_error")]
        with pytest.raises(TrackingConfigError, match=r"bot@example\.com"):
            load_tracking_config(tmp_path / "tracking.toml")

    def test_an_unknown_automation_key(self, tmp_path: Path) -> None:
        body = COVERAGE + ONE_REPO + "[automation]\nlogins = []\n"
        assert _errors(tmp_path, body) == [("automation.logins", "extra_forbidden")]


FAILURE = '{ repository = "prebid/a", number = 5, reason = "a permanent GitHub error" }'


def _accepted(*entries: str) -> str:
    return COVERAGE + f"[sync]\naccepted_failures = [{', '.join(entries)}]\n" + ONE_REPO


class TestAcceptedFailures:
    def test_the_packaged_list_is_empty(self) -> None:
        assert load_tracking_config().sync.accepted_failures == ()

    def test_an_entry_matches_its_configured_repository_and_number(self, tmp_path: Path) -> None:
        body = _accepted('{ repository = "PREBID/A", number = 5, reason = "r" }')
        config = load_tracking_config(_write(tmp_path, body))
        (repo,) = config.repositories
        entry = config.accepted_failure(repo, 5)
        assert entry is not None and (entry.repository, entry.number, entry.reason) == (
            "PREBID/A",
            5,
            "r",
        )
        assert config.accepted_failure(repo, 6) is None
        other = TrackedRepository(owner="prebid", name="b", group="Tools")
        assert config.accepted_failure(other, 5) is None

    def test_a_repository_that_is_not_configured(self, tmp_path: Path) -> None:
        body = _accepted('{ repository = "prebid/zzz", number = 5, reason = "r" }')
        assert _errors(tmp_path, body) == [("", "value_error")]
        with pytest.raises(TrackingConfigError, match="prebid/zzz, which is not a configured"):
            load_tracking_config(tmp_path / "tracking.toml")

    def test_an_entry_listed_twice_ignoring_case(self, tmp_path: Path) -> None:
        body = _accepted(FAILURE, '{ repository = "Prebid/A", number = 5, reason = "again" }')
        assert _errors(tmp_path, body) == [("", "value_error")]
        with pytest.raises(TrackingConfigError, match="listed more than once: Prebid/A#5"):
            load_tracking_config(tmp_path / "tracking.toml")

    def test_two_numbers_of_one_repository(self, tmp_path: Path) -> None:
        body = _accepted(FAILURE, '{ repository = "prebid/a", number = 6, reason = "r" }')
        assert len(load_tracking_config(_write(tmp_path, body)).sync.accepted_failures) == 2

    @pytest.mark.parametrize(
        ("entry", "location", "error"),
        [
            (
                '{ repository = "prebid/a", number = 0, reason = "r" }',
                "number",
                "greater_than_equal",
            ),
            ('{ repository = "prebid/a", number = true, reason = "r" }', "number", "int_type"),
            ('{ repository = "prebid/a", number = "5", reason = "r" }', "number", "int_type"),
            ('{ repository = "prebid/a", number = 5, reason = "" }', "reason", "string_too_short"),
            ('{ repository = "", number = 5, reason = "r" }', "repository", "string_too_short"),
            ('{ repository = "prebid/a", number = 5 }', "reason", "missing"),
            (
                '{ repository = "prebid/a", number = 5, reason = "r", x = 1 }',
                "x",
                "extra_forbidden",
            ),
        ],
    )
    def test_an_invalid_entry(self, tmp_path: Path, entry: str, location: str, error: str) -> None:
        assert _errors(tmp_path, _accepted(entry)) == [
            (f"sync.accepted_failures.0.{location}", error)
        ]


class TestReadTrackingConfig:
    def test_the_packaged_source_and_digest(self) -> None:
        import hashlib

        from github_activity_db.tracking import read_tracking_config

        packaged = (
            Path(__file__).resolve().parents[1] / "src" / "github_activity_db" / "tracking.toml"
        )
        loaded = read_tracking_config()
        assert loaded.source == "packaged"
        assert loaded.sha256 == hashlib.sha256(packaged.read_bytes()).hexdigest()

    def test_a_named_file_s_absolute_source_and_digest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import hashlib

        from github_activity_db.tracking import read_tracking_config

        path = _write(tmp_path, COVERAGE + ONE_REPO)
        monkeypatch.chdir(tmp_path)
        loaded = read_tracking_config(Path("tracking.toml"))
        assert loaded.source == str(path.resolve())
        assert loaded.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
