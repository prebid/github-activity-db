"""The recorder: it refuses private repositories, and recording the replay gives back every unit."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from github_activity_db.source.limits import RATE_LIMIT_FALLBACK_WAIT, SERVER_RETRY_DELAYS
from tests.source import recorder
from tests.source.conftest import FakeClock
from tests.source.recorder import PrivateRepository, RecordingFailed, Scenario, record
from tests.source.recordings import RECORDED
from tests.source.replay import Replay, synthetic


async def test_recording_the_replay_gives_back_every_unit() -> None:
    """Split (recorder) after assemble (replay) is the identity on the fixtures, per operation."""
    replay = Replay.from_fixtures()
    units = await record("test-token", inner=replay.transport())
    assert replay.problems == []
    produced = {(unit.kind, json.dumps(unit.key, sort_keys=True)): unit for unit in units}
    assert len(produced) == len(units), "a unit was recorded twice"
    assert produced.keys() == replay.units.keys()
    for key, unit in produced.items():
        original = replay.units[key]
        assert (unit.document_sha256, unit.data, unit.errors, unit.extra, unit.headers) == (
            original.document_sha256,
            original.data,
            original.errors,
            original.extra,
            original.headers,
        ), key


async def test_a_private_repository_stops_the_run_after_its_resolve() -> None:
    replay = Replay.from_fixtures()
    unit = replay.unit("resolve", owner="prebid", name="go-gdpr")
    unit.data["isPrivate"] = True
    replay.add(unit)
    scenarios = (
        Scenario("prebid", "prebid-cache-java", index=True),
        Scenario("prebid", "go-gdpr", index=True),
    )
    with pytest.raises(PrivateRepository, match="prebid/go-gdpr"):
        await record("test-token", inner=replay.transport(), scenarios=scenarios)
    operations = [seen.operation for seen in replay.seen]
    # prebid-cache-java: resolve and two index pages; go-gdpr: its resolve only; no users.
    assert operations == ["Resolve", "PullRequestIndex", "PullRequestIndex", "Resolve"]


async def test_a_failed_item_stops_the_recording() -> None:
    replay = Replay.from_fixtures()
    replay.fault(
        lambda request: httpx.Response(404, json={"message": "Not Found"}),
        when=lambda seen: seen.operation == "rest",
        times=None,
    )
    scenarios = (Scenario("prebid", "Prebid.js", numbers=(8858,)),)
    with pytest.raises(RecordingFailed, match="compare answered 404"):
        await record("test-token", inner=replay.transport(), scenarios=scenarios, users=())


async def test_a_failed_user_lookup_stops_the_recording() -> None:
    replay = Replay.from_fixtures()
    replay.fault(
        lambda request: synthetic("server_error"),
        when=lambda seen: seen.operation == "UserLookup",
        times=None,
    )
    clock = FakeClock()
    with pytest.raises(RecordingFailed, match="user lookup"):
        await record("test-token", inner=replay.transport(), scenarios=(), clock=clock)
    assert clock.sleeps == list(SERVER_RETRY_DELAYS)


async def test_an_answer_without_data_is_retried_not_recorded() -> None:
    # The first lookup answers a secondary limit with no data; the retry's answer is recorded.
    replay = Replay.from_fixtures()
    replay.fault(
        lambda request: synthetic("secondary_limit_graphql"),
        when=lambda seen: seen.operation == "UserLookup",
    )
    clock = FakeClock()
    units = await record("test-token", inner=replay.transport(), scenarios=(), clock=clock)
    assert sorted(unit.key["id"] for unit in units) == sorted(recorder.USERS)
    assert clock.sleeps == [RATE_LIMIT_FALLBACK_WAIT]


async def test_a_listed_private_repository_is_never_requested() -> None:
    replay = Replay.from_fixtures()
    scenarios = (Scenario("prebid", "prebid-agent-skills", index=True),)
    with pytest.raises(PrivateRepository):
        await record("test-token", inner=replay.transport(), scenarios=scenarios)
    assert replay.seen == []


def test_a_failed_recording_writes_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    recorded = tmp_path / "recorded"
    recorded.mkdir()
    (recorded / "kept.json").write_text("{}")

    async def refuse(token: str, **kwargs: Any) -> list[Any]:
        raise PrivateRepository("prebid/private")

    monkeypatch.setattr(recorder, "RECORDED", recorded)
    monkeypatch.setattr(recorder, "record", refuse)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    with pytest.raises(PrivateRepository):
        recorder.main()
    assert [path.name for path in tmp_path.iterdir()] == ["recorded"]
    assert [path.name for path in recorded.iterdir()] == ["kept.json"]


def recorded_units() -> list[Any]:
    """The units the recorder produces from the replay, as a live recording would."""
    return asyncio.run(record("test-token", inner=Replay.from_fixtures().transport()))


def test_a_recording_writes_one_file_per_unit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    units = recorded_units()

    async def replayed(token: str, **kwargs: Any) -> list[Any]:
        return units

    target = tmp_path / "recorded"
    target.mkdir()
    (target / "stale.json").write_text("{}")
    monkeypatch.setattr(recorder, "RECORDED", target)
    monkeypatch.setattr(recorder, "record", replayed)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    assert recorder.main() == 0
    written = sorted(str(path.relative_to(target)) for path in target.rglob("*.json"))
    committed = sorted(str(path.relative_to(RECORDED)) for path in RECORDED.rglob("*.json"))
    assert written == committed  # every unit to its own file; the stale file is gone
    for unit in units:
        assert (target / unit.path()).read_text() == unit.dumps()
    assert [path.name for path in tmp_path.iterdir()] == ["recorded"]


def test_an_oversized_unit_writes_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    units = recorded_units()

    async def replayed(token: str, **kwargs: Any) -> list[Any]:
        return units

    target = tmp_path / "recorded"
    target.mkdir()
    (target / "kept.json").write_text("{}")
    monkeypatch.setattr(recorder, "RECORDED", target)
    monkeypatch.setattr(recorder, "record", replayed)
    monkeypatch.setattr(recorder, "MAX_FILE_BYTES", 10)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    assert recorder.main() == 1
    assert [path.name for path in tmp_path.iterdir()] == ["recorded"]
    assert [path.name for path in target.iterdir()] == ["kept.json"]
