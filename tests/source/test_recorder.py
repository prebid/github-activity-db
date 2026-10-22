"""The recorder: it refuses private repositories, and recording the replay gives back every unit."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import shutil
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from github_activity_db.source.errors import CompareRefused, ServerError
from github_activity_db.source.limits import RATE_LIMIT_FALLBACK_WAIT, SERVER_RETRY_DELAYS
from tests.source import recorder
from tests.source.conftest import START, FakeClock
from tests.source.recorder import (
    FIRST_SYNC_REPOSITORY,
    SCENARIOS,
    PrivateRepository,
    RecordingFailed,
    Scenario,
    record,
    record_first_sync,
    write_units,
)
from tests.source.recordings import FIRST_SYNC, RECORDED, Unit
from tests.source.replay import Replay, synthetic

# An earlier recording's clock; the later one runs on FakeClock's default, START.
EARLIER = START - timedelta(hours=1)
# The scenario a later recording no longer lists: its units are its resolve and its one PR.
DROPPED = next(scenario for scenario in SCENARIOS if scenario.name == "prebid-cache")
# Prebid.js #8858 (254 commits) is the recorded compare fallback, the only one (SCENARIOS).
COMPARED = 8858


def stamp(clock: FakeClock) -> str:
    """``recorded_at`` of a unit recorded on ``clock``."""
    return clock.now().strftime("%Y-%m-%dT%H:%M:%SZ")


async def test_recording_the_replay_gives_back_every_unit() -> None:
    """Split (recorder) after assemble (replay) is the identity on the fixtures, per operation."""
    replay = Replay.from_fixtures()
    clock = FakeClock()
    units = await record("test-token", inner=replay.transport(), clock=clock)
    assert replay.problems == []
    assert clock.sleeps == []
    requests = {(s.operation, json.dumps(s.variables, sort_keys=True), s.path) for s in replay.seen}
    assert len(requests) == len(replay.seen), "a request was sent twice"
    produced = {(unit.kind, json.dumps(unit.key, sort_keys=True)): unit for unit in units}
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
    assert {unit.recorded_at for unit in units} == {stamp(clock)}


async def test_a_private_repository_stops_the_run_after_its_resolve() -> None:
    replay = Replay.from_fixtures()
    unit = replay.unit("resolve", owner="prebid", name="go-gdpr")
    unit.data["isPrivate"] = True
    replay.add(unit)
    scenarios = (
        Scenario("prebid", "prebid-cache-java", index=True),
        Scenario("prebid", "go-gdpr", index=True),
    )
    with pytest.raises(PrivateRepository) as raised:
        await record("test-token", inner=replay.transport(), scenarios=scenarios, clock=FakeClock())
    assert raised.value.full_name == "prebid/go-gdpr"
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
    scenarios = (Scenario("prebid", "Prebid.js", numbers=(COMPARED,)),)
    with pytest.raises(RecordingFailed) as raised:
        await record(
            "test-token", inner=replay.transport(), scenarios=scenarios, users=(), clock=FakeClock()
        )
    failed = [
        (outcome.number, type(outcome.error), getattr(outcome.error, "status", None))
        for outcome in raised.value.failed
    ]
    assert failed == [(COMPARED, CompareRefused, 404)]
    assert (raised.value.lookup_failed, raised.value.report) == ({}, None)


async def test_a_failed_user_lookup_stops_the_recording() -> None:
    replay = Replay.from_fixtures()
    replay.fault(
        lambda request: synthetic("server_error"),
        when=lambda seen: seen.operation == "UserLookup",
        times=None,
    )
    clock = FakeClock()
    with pytest.raises(RecordingFailed) as raised:
        await record("test-token", inner=replay.transport(), scenarios=(), clock=clock)
    causes = {node_id: type(error.cause) for node_id, error in raised.value.lookup_failed.items()}
    assert causes == dict.fromkeys(recorder.USERS, ServerError)
    assert raised.value.failed == ()
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
    with pytest.raises(PrivateRepository) as raised:
        await record(
            "test-token", inner=replay.transport(), scenarios=scenarios, users=(), clock=FakeClock()
        )
    assert raised.value.full_name == "golang/go"
    assert [seen.operation for seen in replay.seen] == ["Resolve", "RepositoryLabels"]


@pytest.mark.parametrize(
    ("owner", "name"), [("prebid", "prebid-agent-skills"), ("Prebid", "Prebid-Agent-Skills")]
)
async def test_a_listed_private_repository_is_never_requested(owner: str, name: str) -> None:
    replay = Replay.from_fixtures()
    scenarios = (Scenario(owner, name, index=True),)
    with pytest.raises(PrivateRepository) as recording:
        await record("test-token", inner=replay.transport(), scenarios=scenarios, clock=FakeClock())
    with pytest.raises(PrivateRepository) as first_sync:
        await record_first_sync(
            "test-token", owner, name, inner=replay.transport(), clock=FakeClock()
        )
    assert recording.value.full_name == first_sync.value.full_name == f"{owner}/{name}"
    assert replay.seen == []


# --------------------------------------------------------------------------------------------
# Writing: main() over the replay
# --------------------------------------------------------------------------------------------
@dataclasses.dataclass
class Replayed:
    """``record`` as ``main`` calls it, over ``replay`` on ``clock``: ``scenarios`` unless ``main``
    names its own. ``calls`` holds the arguments ``main`` passed, ``units`` what was recorded."""

    replay: Replay
    clock: FakeClock
    scenarios: tuple[Scenario, ...] = SCENARIOS
    calls: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    units: list[Unit] = dataclasses.field(default_factory=list)

    async def __call__(self, token: str, **kwargs: Any) -> list[Unit]:
        self.calls.append(kwargs)
        arguments = {"scenarios": self.scenarios, **kwargs}
        self.units = await record(
            token, inner=self.replay.transport(), clock=self.clock, **arguments
        )
        return self.units


def written(units: list[Unit]) -> dict[str, bytes]:
    """The files ``units`` are written to, by path, with their text."""
    return {str(unit.path()): unit.dumps().encode() for unit in units}


def snapshot(directory: Path) -> dict[str, bytes]:
    """Every file under ``directory``, by relative path, with its bytes."""
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def of_repository(units: list[Unit], scenario: Scenario) -> set[str]:
    """The files of ``units`` keyed by ``scenario``'s repository."""
    return {
        str(unit.path())
        for unit in units
        if (unit.key.get("owner"), unit.key.get("name")) == (scenario.owner, scenario.name)
    }


def oversized(replay: Replay) -> Unit:
    """The largest recorded compare page, with its first commit's message repeated until the page
    is over ``MAX_FILE_BYTES``: compare returns each commit's message, which git does not bound."""
    unit = max(
        (unit for unit in replay.units.values() if unit.kind == "compare"),
        key=lambda unit: len(unit.dumps().encode()),
    )
    unit = replay.unit("compare", **unit.key)
    commit = unit.data["commits"][0]["commit"]
    short = recorder.MAX_FILE_BYTES - len(unit.dumps().encode())
    commit["message"] = "\n".join([commit["message"]] * (short // len(commit["message"]) + 2))
    assert len(unit.dumps().encode()) > recorder.MAX_FILE_BYTES
    return unit


@pytest.fixture
def target(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Where ``main`` writes the recordings: ``recorded`` in the test's directory."""
    directory = tmp_path / "recorded"
    monkeypatch.setattr(recorder, "RECORDED", directory)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    return directory


def run(monkeypatch: pytest.MonkeyPatch, replayed: Replayed, argv: list[str]) -> int:
    monkeypatch.setattr(recorder, "record", replayed)
    return recorder.main(argv)


def test_a_recording_writes_one_file_per_unit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, target: Path
) -> None:
    """Every unit to its own file, the directory replaced whole: a scenario no longer recorded
    leaves no file."""
    earlier = Replayed(Replay.from_fixtures(), FakeClock(EARLIER))
    assert run(monkeypatch, earlier, []) == 0
    committed = sorted(str(path.relative_to(RECORDED)) for path in RECORDED.rglob("*.json"))
    assert sorted(snapshot(target)) == committed
    assert snapshot(target) == written(earlier.units)

    later = Replayed(
        Replay.from_fixtures(),
        FakeClock(),
        scenarios=tuple(scenario for scenario in SCENARIOS if scenario != DROPPED),
    )
    assert run(monkeypatch, later, []) == 0
    gone = set(written(earlier.units)) - set(written(later.units))
    assert gone == of_repository(earlier.units, DROPPED) != set()
    assert snapshot(target) == written(later.units)
    assert {unit.recorded_at for unit in later.units} == {stamp(later.clock)}
    assert [path.name for path in tmp_path.iterdir()] == ["recorded"]


def test_a_failed_recording_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, target: Path
) -> None:
    earlier = Replayed(Replay.from_fixtures(), FakeClock(EARLIER))
    assert run(monkeypatch, earlier, []) == 0
    before = snapshot(target)
    assert len(before) == len(earlier.units)

    replay = Replay.from_fixtures()
    unit = replay.unit("resolve", owner="prebid", name="go-gdpr")
    unit.data["isPrivate"] = True
    replay.add(unit)
    with pytest.raises(PrivateRepository) as raised:
        run(monkeypatch, Replayed(replay, FakeClock()), [])
    assert raised.value.full_name == "prebid/go-gdpr"
    assert snapshot(target) == before
    assert [path.name for path in tmp_path.iterdir()] == ["recorded"]


def test_only_rate_limits_adds_them_and_keeps_every_other_unit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, target: Path
) -> None:
    earlier = Replayed(Replay.from_fixtures(), FakeClock(EARLIER))
    assert run(monkeypatch, earlier, []) == 0
    before = snapshot(target)

    readings = Replayed(Replay.from_fixtures(), FakeClock())
    assert run(monkeypatch, readings, ["--only", "rate-limits"]) == 0
    assert readings.calls == [{"scenarios": (), "users": ()}]
    assert sorted(unit.kind for unit in readings.units) == ["rate_limit", "rest_rate_limit"]
    replaced = written(readings.units)
    # The readings are dated by the later clock, so each file changed.
    assert [path for path in replaced if before[path] == replaced[path]] == []
    after = snapshot(target)
    assert after == {**before, **replaced}
    assert len(after) == len(earlier.units)
    assert [path.name for path in tmp_path.iterdir()] == ["recorded"]


def test_only_documents_replaces_graphql_units_and_keeps_compare_and_rate_limits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, target: Path
) -> None:
    """The GraphQL units are recorded again, so a scenario no longer listed leaves no file; the
    compare pages and readings stay as the earlier recording wrote them."""
    earlier = Replayed(Replay.from_fixtures(), FakeClock(EARLIER))
    assert run(monkeypatch, earlier, []) == 0
    before = snapshot(target)

    documents = Replayed(
        Replay.from_fixtures(),
        FakeClock(),
        scenarios=tuple(scenario for scenario in SCENARIOS if scenario != DROPPED),
    )
    assert run(monkeypatch, documents, ["--only", "documents"]) == 0
    assert documents.calls == [{"rate_limits": False}]
    kept_kinds = recorder.KEPT_BY_DOCUMENTS
    kept = {
        path: before[path]
        for path in written([unit for unit in earlier.units if unit.kind in kept_kinds])
    }
    assert len(kept) == sum(
        1 for unit in documents.replay.units.values() if unit.kind in kept_kinds
    )
    graphql = written([unit for unit in documents.units if unit.kind not in kept_kinds])
    assert snapshot(target) == {**kept, **graphql}
    assert of_repository(earlier.units, DROPPED) & set(snapshot(target)) == set()
    assert of_repository(earlier.units, DROPPED) != set()
    assert [path.name for path in tmp_path.iterdir()] == ["recorded"]


def test_only_documents_adds_a_compare_page_the_recordings_lack(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, target: Path
) -> None:
    without_compare = tuple(
        dataclasses.replace(
            scenario, numbers=tuple(number for number in scenario.numbers if number != COMPARED)
        )
        for scenario in SCENARIOS
    )
    earlier = Replayed(Replay.from_fixtures(), FakeClock(EARLIER), scenarios=without_compare)
    assert run(monkeypatch, earlier, []) == 0
    assert [unit for unit in earlier.units if unit.kind == "compare"] == []
    before = snapshot(target)

    documents = Replayed(Replay.from_fixtures(), FakeClock())
    assert run(monkeypatch, documents, ["--only", "documents"]) == 0
    compare = written([unit for unit in documents.units if unit.kind == "compare"])
    assert len(compare) == sum(
        1 for unit in documents.replay.units.values() if unit.kind == "compare"
    )
    readings = {
        path: before[path]
        for path in written(
            [unit for unit in earlier.units if unit.kind in ("rate_limit", "rest_rate_limit")]
        )
    }
    assert snapshot(target) == {**readings, **written(documents.units)}
    assert set(compare) <= set(snapshot(target))
    assert [path.name for path in tmp_path.iterdir()] == ["recorded"]


def test_an_oversized_unit_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, target: Path
) -> None:
    earlier = Replayed(Replay.from_fixtures(), FakeClock(EARLIER))
    assert run(monkeypatch, earlier, []) == 0
    before = snapshot(target)
    assert len(before) == len(earlier.units)

    replay = Replay.from_fixtures()
    replay.add(oversized(replay))
    later = Replayed(replay, FakeClock())
    assert run(monkeypatch, later, []) == 1
    sizes = [len(unit.dumps().encode()) for unit in later.units]
    assert [size > recorder.MAX_FILE_BYTES for size in sizes].count(True) == 1
    assert snapshot(target) == before
    assert [path.name for path in tmp_path.iterdir()] == ["recorded"]


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
    clock = FakeClock()
    units = await record_first_sync(
        "test-token", owner, name, inner=replay.transport(), clock=clock
    )
    assert replay.problems == []
    # One resolve, the sync's own: the resolution the privacy check reads is the one it uses.
    assert [seen.operation for seen in replay.seen] == [
        "Resolve",
        "PullRequestIndex",
        "PullRequestBatch",
        "UserLookup",
    ]
    produced = {(unit.kind, json.dumps(unit.key, sort_keys=True)): unit for unit in units}
    assert produced.keys() == replay.units.keys()
    for key, unit in produced.items():
        original = replay.units[key]
        assert (unit.document_sha256, unit.data, unit.errors, unit.headers) == (
            original.document_sha256,
            original.data,
            original.errors,
            original.headers,
        ), key
    assert {unit.recorded_at for unit in units} == {stamp(clock)}


async def test_a_private_repository_stops_a_first_sync_after_its_resolve() -> None:
    replay = Replay.from_directory(FIRST_SYNC)
    owner, name = FIRST_SYNC_REPOSITORY
    unit = replay.unit("resolve", owner=owner, name=name)
    unit.data["isPrivate"] = True
    replay.add(unit)
    with pytest.raises(PrivateRepository) as raised:
        await record_first_sync(
            "test-token", owner, name, inner=replay.transport(), clock=FakeClock()
        )
    assert raised.value.full_name == f"{owner}/{name}"
    assert [seen.operation for seen in replay.seen] == ["Resolve"]


@pytest.mark.parametrize("first_sync", [False, True], ids=["record", "first sync"])
async def test_a_retried_item_records_one_unit_per_file(first_sync: bool) -> None:
    """The first batch's first alias fails once and its item's solo retry answers it: the
    recording holds the replay's units, one per file, the retry's answer kept."""
    replay = Replay.from_directory(FIRST_SYNC) if first_sync else Replay.from_fixtures()
    replay.modify(_batch_alias_error, when=lambda seen: seen.operation == "PullRequestBatch")
    if first_sync:
        owner, name = FIRST_SYNC_REPOSITORY
        units = await record_first_sync(
            "test-token", owner, name, inner=replay.transport(), clock=FakeClock()
        )
    else:
        units = await record("test-token", inner=replay.transport(), clock=FakeClock())
    batches = [seen.numbers for seen in replay.seen if seen.operation == "PullRequestBatch"]
    assert batches[1] == batches[0][:1]  # the solo retry
    assert len(units) == len(replay.units)
    produced = {(unit.kind, json.dumps(unit.key, sort_keys=True)): unit for unit in units}
    original = replay.units
    assert [
        key
        for key, unit in produced.items()
        if (unit.data, unit.errors) != (original[key].data, original[key].errors)
    ] == []


def _label_count_mismatch(body: dict[str, Any]) -> None:
    body["data"]["repository"]["labels"]["totalCount"] += 1


def _class(text: str | None) -> str | None:
    """The class an error's text names: ``error_text`` writes "<class>: <message>"."""
    return None if text is None else text.partition(": ")[0]


@pytest.mark.parametrize(
    ("change", "error", "reason"),
    [
        pytest.param(
            lambda replay: replay.fault(
                lambda request: httpx.Response(401, json={"message": "Bad credentials"}),
                times=None,
            ),
            "Unauthorized",
            None,
            id="401",
        ),
        pytest.param(
            lambda replay: replay.modify(
                _label_count_mismatch, when=lambda seen: seen.operation == "Resolve"
            ),
            "NoRepositoryResolved",
            "LabelCountMismatch",
            id="label count mismatch",
        ),
    ],
)
async def test_a_first_sync_that_fails_names_why(
    change: Callable[[Replay], None], error: str, reason: str | None
) -> None:
    """A run that fails at its resolve raises with the run's report, which names the cause: the
    run's error, and the repository's when its resolve failed."""
    replay = Replay.from_directory(FIRST_SYNC)
    change(replay)
    owner, name = FIRST_SYNC_REPOSITORY
    with pytest.raises(RecordingFailed) as raised:
        await record_first_sync(
            "test-token", owner, name, inner=replay.transport(), clock=FakeClock()
        )
    report = raised.value.report
    assert report is not None
    assert (report.exit_code, report.status, report.items_hydrated, report.failures) == (
        1,
        "failed",
        0,
        (),
    )
    assert _class(report.error) == error
    assert [
        (repository.configured.full_name, repository.record, repository.outcome)
        for repository in report.repositories
    ] == [(f"{owner}/{name}", None, None)]
    assert [_class(repository.reason) for repository in report.repositories] == [reason]
    assert [seen.operation for seen in replay.seen] == ["Resolve"]


async def test_a_first_sync_that_does_not_exit_0_stops_the_recording() -> None:
    replay = Replay.from_directory(FIRST_SYNC)
    replay.modify(
        _batch_alias_error, when=lambda seen: seen.operation == "PullRequestBatch", times=2
    )
    owner, name = FIRST_SYNC_REPOSITORY
    with pytest.raises(RecordingFailed) as raised:
        await record_first_sync(
            "test-token", owner, name, inner=replay.transport(), clock=FakeClock()
        )
    report = raised.value.report
    assert report is not None
    first = next(seen for seen in replay.seen if seen.operation == "PullRequestBatch").numbers[0]
    assert report.exit_code == 2
    assert [(failure.number, _class(failure.error)) for failure in report.failures] == [
        (first, "AliasError")
    ]


@pytest.mark.parametrize("too_large", [False, True], ids=["written", "oversized"])
def test_only_first_sync_writes_its_directory_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, too_large: bool
) -> None:
    """``--only first-sync`` replaces the first-sync directory with every unit, or with an
    oversized unit writes nothing; the other recordings are never touched."""
    owner, name = FIRST_SYNC_REPOSITORY
    target = tmp_path / "first_sync"
    # What the directory held before: an earlier recording of go-gdpr, which also has a pull
    # request the first sync does not ask for.
    earlier = asyncio.run(
        record(
            "test-token",
            inner=Replay.from_fixtures().transport(),
            scenarios=(Scenario(owner, name, index=True, numbers=(21,)),),
            users=(),
            rate_limits=False,
            clock=FakeClock(EARLIER),
        )
    )
    assert write_units(earlier, target) == 0
    before = snapshot(target)
    others = tmp_path / "recorded"
    shutil.copytree(RECORDED, others)
    recordings = snapshot(others)
    assert len(recordings) == len(list(RECORDED.rglob("*.json")))
    extra = [oversized(Replay.from_fixtures())] if too_large else []
    units: list[Unit] = []

    async def replayed(token: str, owner: str, name: str, **kwargs: Any) -> list[Unit]:
        assert kwargs == {}
        replay = Replay.from_directory(FIRST_SYNC)
        recorded = await record_first_sync(
            token, owner, name, inner=replay.transport(), clock=FakeClock()
        )
        # A first sync that reads a pull request's compare pages records them too.
        units.extend([*recorded, *extra])
        return units

    monkeypatch.setattr(recorder, "FIRST_SYNC", target)
    monkeypatch.setattr(recorder, "RECORDED", others)
    monkeypatch.setattr(recorder, "record_first_sync", replayed)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")

    code = recorder.main(["--only", "first-sync"])

    assert sorted(path.name for path in tmp_path.iterdir()) == ["first_sync", "recorded"]
    assert snapshot(others) == recordings
    if too_large:
        assert code == 1
        assert snapshot(target) == before
        return
    assert code == 0
    assert snapshot(target) == written(units)
    committed = sorted(str(path.relative_to(FIRST_SYNC)) for path in FIRST_SYNC.rglob("*.json"))
    assert sorted(snapshot(target)) == committed
    # The earlier recording's pull request is gone with the directory it was in.
    pull_requests = set(written([unit for unit in earlier if unit.kind == "pull_request"]))
    assert set(written(earlier)) - set(written(units)) == pull_requests != set()
