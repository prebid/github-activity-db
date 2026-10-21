"""The recorder: it refuses private repositories, and recording the replay gives back every unit."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from github_activity_db.source.limits import RATE_LIMIT_FALLBACK_WAIT, SERVER_RETRY_DELAYS
from tests.source import recorder
from tests.source.conftest import FakeClock
from tests.source.recorder import (
    FIRST_SYNC_REPOSITORY,
    PrivateRepository,
    RecordingFailed,
    Scenario,
    record,
    record_first_sync,
)
from tests.source.recordings import FIRST_SYNC, RECORDED, Unit
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
    units = await record(
        "test-token", inner=replay.transport(), scenarios=(), rate_limits=False, clock=clock
    )
    assert sorted(unit.key["id"] for unit in units) == sorted(recorder.USERS)
    assert clock.sleeps == [RATE_LIMIT_FALLBACK_WAIT]


async def test_a_private_answer_comes_after_the_resolves_label_pages() -> None:
    """golang/go has more than 100 labels, so its resolve asks for a second label page; a private
    answer raises after that page and before its index."""
    replay = Replay.from_fixtures()
    unit = replay.unit("resolve", owner="golang", name="go")
    unit.data["isPrivate"] = True
    replay.add(unit)
    scenarios = (Scenario("golang", "go", index=True),)
    with pytest.raises(PrivateRepository, match="golang/go"):
        await record("test-token", inner=replay.transport(), scenarios=scenarios, users=())
    assert [seen.operation for seen in replay.seen] == ["Resolve", "RepositoryLabels"]


@pytest.mark.parametrize(
    ("owner", "name"), [("prebid", "prebid-agent-skills"), ("Prebid", "Prebid-Agent-Skills")]
)
async def test_a_listed_private_repository_is_never_requested(owner: str, name: str) -> None:
    replay = Replay.from_fixtures()
    scenarios = (Scenario(owner, name, index=True),)
    with pytest.raises(PrivateRepository):
        await record("test-token", inner=replay.transport(), scenarios=scenarios)
    with pytest.raises(PrivateRepository):
        await record_first_sync("test-token", owner, name, inner=replay.transport())
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
        recorder.main([])
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
    assert recorder.main([]) == 0
    written = sorted(str(path.relative_to(target)) for path in target.rglob("*.json"))
    committed = sorted(str(path.relative_to(RECORDED)) for path in RECORDED.rglob("*.json"))
    assert written == committed  # every unit to its own file; the stale file is gone
    for unit in units:
        assert (target / unit.path()).read_text() == unit.dumps()
    assert [path.name for path in tmp_path.iterdir()] == ["recorded"]


def test_only_rate_limits_adds_them_and_keeps_every_other_unit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    units = [unit for unit in recorded_units() if unit.kind in ("rate_limit", "rest_rate_limit")]
    assert sorted(unit.kind for unit in units) == ["rate_limit", "rest_rate_limit"]
    calls: list[dict[str, Any]] = []

    async def replayed(token: str, **kwargs: Any) -> list[Any]:
        calls.append(kwargs)
        return units

    target = tmp_path / "recorded"
    (target / "rate_limit").mkdir(parents=True)
    (target / "kept.json").write_text("{}")
    (target / "rate_limit" / "rest.json").write_text("stale")
    monkeypatch.setattr(recorder, "RECORDED", target)
    monkeypatch.setattr(recorder, "record", replayed)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    assert recorder.main(["--only", "rate-limits"]) == 0
    assert calls == [{"scenarios": (), "users": ()}]
    assert (target / "kept.json").read_text() == "{}"
    for unit in units:
        assert (target / unit.path()).read_text() == unit.dumps()
    assert [path.name for path in tmp_path.iterdir()] == ["recorded"]


def test_only_documents_replaces_graphql_units_and_keeps_compare_and_rate_limits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    units = recorded_units()
    kept_kinds = recorder.KEPT_BY_DOCUMENTS
    compare = [unit for unit in units if unit.kind == "compare"]
    assert len(compare) > 1 and any(unit.kind == "rate_limit" for unit in units)
    missing = compare[0]
    target = tmp_path / "recorded"

    def place(unit: Unit) -> Path:
        path = target / unit.path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(unit.dumps())
        return path

    # The existing recordings: every compare page but one and both readings, marked so a
    # replacement shows, and a GraphQL unit no scenario records any more.
    for unit in units:
        if unit.kind in kept_kinds and unit is not missing:
            place(dataclasses.replace(unit, recorded_at="kept"))
    stale = next(unit for unit in units if unit.kind == "resolve")
    stale_path = place(dataclasses.replace(stale, key={"owner": "prebid", "name": "retired"}))
    calls: list[dict[str, Any]] = []

    async def replayed(token: str, **kwargs: Any) -> list[Any]:
        calls.append(kwargs)
        return [unit for unit in units if unit.kind not in ("rate_limit", "rest_rate_limit")]

    monkeypatch.setattr(recorder, "RECORDED", target)
    monkeypatch.setattr(recorder, "record", replayed)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    assert recorder.main(["--only", "documents"]) == 0
    assert calls == [{"rate_limits": False}]
    assert not stale_path.exists()
    written = sorted(str(path.relative_to(target)) for path in target.rglob("*.json"))
    assert written == sorted(str(unit.path()) for unit in units)
    for unit in units:
        text = (target / unit.path()).read_text()
        if unit.kind in kept_kinds and unit is not missing:
            assert json.loads(text)["recorded_at"] == "kept", unit.path()
        else:
            assert text == unit.dumps(), unit.path()
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
    assert recorder.main([]) == 1
    assert [path.name for path in tmp_path.iterdir()] == ["recorded"]
    assert [path.name for path in target.iterdir()] == ["kept.json"]


# --------------------------------------------------------------------------------------------
# --only first-sync
# --------------------------------------------------------------------------------------------
def _batch_alias_error(body: dict[str, Any]) -> None:
    body["data"]["repository"]["p0"] = None
    body["errors"] = [
        {"type": "FORBIDDEN", "message": "not accessible", "path": ["repository", "p0"]}
    ]


async def test_recording_a_first_sync_of_its_recordings_gives_them_back() -> None:
    """The first sync asks for exactly the recorded units, and records each as it was."""
    replay = Replay.from_directory(FIRST_SYNC)
    owner, name = FIRST_SYNC_REPOSITORY
    units = await record_first_sync("test-token", owner, name, inner=replay.transport())
    assert replay.problems == []
    # One resolve, the sync's own: the resolution the privacy check reads is the one it uses.
    assert [seen.operation for seen in replay.seen] == [
        "Resolve",
        "PullRequestIndex",
        "PullRequestBatch",
        "UserLookup",
    ]
    produced = {(unit.kind, json.dumps(unit.key, sort_keys=True)): unit for unit in units}
    assert len(units) == len(produced)
    assert produced.keys() == replay.units.keys()
    for key, unit in produced.items():
        original = replay.units[key]
        assert (unit.document_sha256, unit.data, unit.errors, unit.headers) == (
            original.document_sha256,
            original.data,
            original.errors,
            original.headers,
        ), key


async def test_a_private_repository_stops_a_first_sync_after_its_resolve() -> None:
    replay = Replay.from_directory(FIRST_SYNC)
    owner, name = FIRST_SYNC_REPOSITORY
    unit = replay.unit("resolve", owner=owner, name=name)
    unit.data["isPrivate"] = True
    replay.add(unit)
    with pytest.raises(PrivateRepository, match=f"{owner}/{name}"):
        await record_first_sync("test-token", owner, name, inner=replay.transport())
    assert [seen.operation for seen in replay.seen] == ["Resolve"]


async def test_a_retried_item_records_one_unit_per_file() -> None:
    """The batch's first alias fails once and its item's solo retry answers it: the recording
    keeps the retry's unit, one per file, and the run exits 0."""
    replay = Replay.from_directory(FIRST_SYNC)
    replay.modify(_batch_alias_error, when=lambda seen: seen.operation == "PullRequestBatch")
    owner, name = FIRST_SYNC_REPOSITORY
    units = await record_first_sync("test-token", owner, name, inner=replay.transport())
    assert [seen.operation for seen in replay.seen].count("PullRequestBatch") == 2
    assert len({unit.path() for unit in units}) == len(units) == len(replay.units)
    pulls = [unit for unit in units if unit.kind == "pull_request"]
    assert [(unit.data is None, unit.errors) for unit in pulls] == [(False, [])] * len(pulls)


async def test_a_first_sync_that_does_not_exit_0_stops_the_recording() -> None:
    replay = Replay.from_directory(FIRST_SYNC)
    replay.modify(
        _batch_alias_error, when=lambda seen: seen.operation == "PullRequestBatch", times=2
    )
    owner, name = FIRST_SYNC_REPOSITORY
    with pytest.raises(RecordingFailed, match="first sync exited 2"):
        await record_first_sync("test-token", owner, name, inner=replay.transport())


def _first_sync_units() -> list[Any]:
    owner, name = FIRST_SYNC_REPOSITORY
    replay = Replay.from_directory(FIRST_SYNC)
    return asyncio.run(record_first_sync("test-token", owner, name, inner=replay.transport()))


@pytest.mark.parametrize("oversized", [False, True])
def test_only_first_sync_writes_its_directory_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, oversized: bool
) -> None:
    """``--only first-sync`` replaces the first-sync directory with every unit, or with an
    oversized unit writes nothing; the other recordings are never touched."""
    units = _first_sync_units()

    async def replayed(token: str, owner: str, name: str, **kwargs: Any) -> list[Any]:
        return units

    target = tmp_path / "first_sync"
    target.mkdir()
    (target / "stale.json").write_text("{}")
    others = tmp_path / "recorded"
    others.mkdir()
    (others / "kept.json").write_text("{}")
    monkeypatch.setattr(recorder, "FIRST_SYNC", target)
    monkeypatch.setattr(recorder, "RECORDED", others)
    monkeypatch.setattr(recorder, "record_first_sync", replayed)
    if oversized:
        monkeypatch.setattr(recorder, "MAX_FILE_BYTES", 10)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")

    code = recorder.main(["--only", "first-sync"])

    assert sorted(path.name for path in tmp_path.iterdir()) == ["first_sync", "recorded"]
    assert [path.name for path in others.iterdir()] == ["kept.json"]
    if oversized:
        assert code == 1
        assert [path.name for path in target.iterdir()] == ["stale.json"]
        return
    assert code == 0
    written = sorted(str(path.relative_to(target)) for path in target.rglob("*.json"))
    committed = sorted(str(path.relative_to(FIRST_SYNC)) for path in FIRST_SYNC.rglob("*.json"))
    assert written == committed
    for unit in units:
        assert (target / unit.path()).read_text() == unit.dumps()
