"""`ghactivity github rate-limit` and `github test` over the source layer, from recordings."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from rich.console import Console
from typer.testing import CliRunner

from github_activity_db.cli import common
from github_activity_db.cli import github as github_cli
from github_activity_db.cli.app import app
from github_activity_db.config import get_settings
from github_activity_db.source import GitHubSource
from tests.source.conftest import FakeClock
from tests.source.replay import Replay, synthetic, unused_faults

runner = CliRunner()
# Rich's width when none of stdin, stdout, and stderr is a terminal and COLUMNS is unset, as in
# CI and under pytest's capture.
DEFAULT_WIDTH = 80
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_BOX_DRAWING = re.compile(r"[\u2500-\u257f]")


def _plain(text: str) -> str:
    """The output's words, without terminal styling, table borders, or line layout."""
    return " ".join(_BOX_DRAWING.sub(" ", _ANSI_ESCAPE.sub("", text)).split())


@pytest.fixture
def replay(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Replay]:
    """Every command reads GitHub through the replay; it must answer every request it gets."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(github_cli, "console", Console(width=DEFAULT_WIDTH))
    get_settings.cache_clear()
    recorded = Replay.from_fixtures()
    monkeypatch.setattr(
        common,
        "open_source",
        lambda token: GitHubSource(token, http_transport=recorded.transport(), clock=FakeClock()),
    )
    yield recorded
    get_settings.cache_clear()
    assert recorded.problems == []
    assert unused_faults(recorded) == []


class TestRateLimit:
    def test_shows_the_graphql_reading_and_the_core_and_graphql_pools(self, replay: Replay) -> None:
        result = runner.invoke(app, ["github", "rate-limit"])
        assert result.exit_code == 0, result.output
        out = _plain(result.stdout)
        # tests/fixtures/source/recorded/rate_limit/{graphql,rest}.json
        assert "GraphQL rateLimit: 4786 remaining, resets 2026-10-17 20:19:29 UTC" in out
        assert "core 4931 5000 69 2026-10-17 20:19:29 UTC" in out
        assert "graphql 5000 5000 0 2026-10-17 21:15:50 UTC" in out
        assert "search" not in out
        assert [seen.operation for seen in replay.seen] == ["RateLimit", "rest"]

    def test_all_shows_every_pool(self, replay: Replay) -> None:
        result = runner.invoke(app, ["github", "rate-limit", "--all"])
        assert result.exit_code == 0, result.output
        out = _plain(result.stdout)
        recorded = replay.unit("rest_rate_limit").data["resources"]
        assert len(recorded) == 15
        for name in recorded:
            assert f" {name} " in f" {out} ", name

    def test_fields_github_adds_to_rest_rate_limit_are_ignored(self, replay: Replay) -> None:
        """REST returns what GitHub adds over time; an added field is not a failed reading."""
        unit = replay.unit("rest_rate_limit")
        unit.data["resources"]["core"]["added_field"] = 1
        unit.data["added_top_level"] = {"x": 1}
        replay.add(unit)
        result = runner.invoke(app, ["github", "rate-limit"])
        assert result.exit_code == 0, result.output
        assert "core 4931 5000 69 2026-10-17 20:19:29 UTC" in _plain(result.stdout)

    def test_a_dropped_rate_object_is_not_a_failed_reading(self, replay: Replay) -> None:
        """Nothing reads the top-level ``rate``, which the recording shows equal to
        ``resources.core``."""
        unit = replay.unit("rest_rate_limit")
        del unit.data["rate"]
        replay.add(unit)
        result = runner.invoke(app, ["github", "rate-limit"])
        assert result.exit_code == 0, result.output
        assert "core 4931 5000 69 2026-10-17 20:19:29 UTC" in _plain(result.stdout)

    def test_a_pool_without_a_field_it_reads_exits_1(self, replay: Replay) -> None:
        unit = replay.unit("rest_rate_limit")
        del unit.data["resources"]["core"]["remaining"]
        replay.add(unit)
        result = runner.invoke(app, ["github", "rate-limit"])
        assert result.exit_code == 1
        assert "unexpected /rate_limit shape" in _plain(result.stderr)
        assert "resources.core.remaining" in _plain(result.stderr)

    def test_a_refused_rate_limit_request_exits_1(self, replay: Replay) -> None:
        replay.fault(
            lambda request: httpx.Response(404, json={"message": "Not Found"}),
            when=lambda seen: seen.operation == "rest",
        )
        result = runner.invoke(app, ["github", "rate-limit"])
        assert result.exit_code == 1
        assert "/rate_limit answered 404" in _plain(result.stderr)


class TestTest:
    def test_hydrates_one_pull_request(self, replay: Replay) -> None:
        result = runner.invoke(
            app, ["github", "test", "--repo", "prebid/Prebid.js", "--pr", "13481"]
        )
        assert result.exit_code == 0, result.output
        out = _plain(result.stdout)
        assert "prebid/Prebid.js: default branch master" in out
        assert "#13481 MERGED: link CLAUDE.md to AGENTS.md" in out
        assert "author dgirardi" in out
        assert (
            "labels 1, closing issues 0, reviews 1, comments 0, commits 4 (graphql), files 3" in out
        )
        assert [seen.operation for seen in replay.seen] == ["Resolve", "PullRequestBatch"]

    def test_defaults_to_the_first_repository_in_tracking_config(self, replay: Replay) -> None:
        assert get_settings().tracked_repos[0] == "prebid/prebid-server"
        result = runner.invoke(app, ["github", "test", "--pr", "4343"])
        assert result.exit_code == 0, result.output
        assert replay.seen[0].variables == {"owner": "prebid", "name": "prebid-server"}

    def test_a_missing_pull_request_exits_1(self, replay: Replay) -> None:
        result = runner.invoke(
            app, ["github", "test", "--repo", "prebid/Prebid.js", "--pr", "999999"]
        )
        assert result.exit_code == 1
        assert "#999999 not found in prebid/Prebid.js" in _plain(result.stderr)

    def test_a_failed_pull_request_exits_1(self, replay: Replay) -> None:
        replay.fault(
            lambda request: synthetic("server_error"),
            when=lambda seen: seen.operation == "PullRequestBatch",
            times=None,
        )
        result = runner.invoke(
            app, ["github", "test", "--repo", "prebid/Prebid.js", "--pr", "13481"]
        )
        assert result.exit_code == 1
        assert "#13481" in _plain(result.stderr)

    def test_an_invalid_repository_exits_1(self, replay: Replay) -> None:
        result = runner.invoke(app, ["github", "test", "--repo", "prebid", "--pr", "1"])
        assert result.exit_code == 1
        assert "repository must be owner/name: 'prebid'" in _plain(result.stderr)
        assert replay.seen == []

    def test_markup_in_github_text_is_printed_as_written(self, replay: Replay) -> None:
        unit = replay.unit("pull_request", owner="prebid", name="Prebid.js", number=13481)
        unit.data["title"] = "[bold]not markup[/bold]"
        replay.add(unit)
        result = runner.invoke(
            app, ["github", "test", "--repo", "prebid/Prebid.js", "--pr", "13481"]
        )
        assert result.exit_code == 0, result.output
        assert "#13481 MERGED: [bold]not markup[/bold]" in _plain(result.stdout)
