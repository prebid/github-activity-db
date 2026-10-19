"""The recording format: split and replay agree, and every unit matches the current documents."""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest

from github_activity_db.source import documents
from tests.source import recordings
from tests.source.recordings import (
    COMPARE_STAMP,
    RECORDED,
    REST_RATE_LIMIT_STAMP,
    Unit,
    canonical_batch,
    compare_key,
    expected_overflow_stamp,
    expected_pull_request_stamp,
    overflow_stamp,
    pull_request_stamp,
    split_graphql,
    whole_stamp,
)
from tests.source.replay import Replay, unused_faults

UNITS = sorted(RECORDED.rglob("*.json"))
WHOLE_DOCUMENTS = {
    "resolve": documents.RESOLVE,
    "repository_labels": documents.REPOSITORY_LABELS,
    "index": documents.PULL_REQUEST_INDEX,
    "user": documents.USER_LOOKUP,
    "rate_limit": documents.RATE_LIMIT_QUERY,
}


def test_fixtures_were_recorded() -> None:
    kinds = {Unit.load(path).kind for path in UNITS}
    # Every kind of unit is recorded. Within overflow, the labels and closing-issue continuations
    # have only synthetic pages: no tracked PR has more than 100 of either (docs/testing.md).
    assert kinds == {
        "resolve",
        "repository_labels",
        "index",
        "pull_request",
        "overflow",
        "compare",
        "user",
        "rate_limit",
        "rest_rate_limit",
    }


@pytest.mark.parametrize("path", UNITS, ids=lambda path: str(path.relative_to(RECORDED)))
def test_unit_stamp_matches_current_documents(path: Path) -> None:
    unit = Unit.load(path)
    match unit.kind:
        case "pull_request":
            expected = expected_pull_request_stamp()
        case "overflow":
            expected = expected_overflow_stamp(unit.key["connection"])
        case "compare":
            expected = whole_stamp(COMPARE_STAMP)
        case "rest_rate_limit":
            expected = whole_stamp(REST_RATE_LIMIT_STAMP)
        case kind:
            expected = whole_stamp(WHOLE_DOCUMENTS[kind])
    assert unit.document_sha256 == expected, f"re-record {path}: scripts/record_fixtures.py"


@pytest.mark.parametrize("path", UNITS, ids=lambda path: str(path.relative_to(RECORDED)))
def test_unit_keeps_no_request_headers(path: Path) -> None:
    unit = Unit.load(path)
    assert set(unit.headers) <= {"date", "retry-after"} | {
        name for name in unit.headers if name.startswith("x-ratelimit-")
    }
    text = path.read_text()
    assert "authorization" not in text.lower()
    assert not re.search(r"\bgh[pousr]_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{30,}", text)


@pytest.mark.parametrize("size", [1, 2, 7, 25])
def test_a_batch_stamp_does_not_depend_on_its_size(size: int) -> None:
    batch = documents.pull_request_batch(size)
    assert canonical_batch(batch) == documents.normalize(documents.pull_request_batch(1))
    assert pull_request_stamp(batch) == expected_pull_request_stamp()


@pytest.mark.parametrize("connection", documents.PULL_REQUEST_CONNECTIONS, ids=lambda c: c.key)
def test_a_continuation_stamp_does_not_depend_on_its_neighbours(connection) -> None:
    query = documents.overflow_continuation([documents.LABELS, documents.REVIEWS, connection])
    assert overflow_stamp(query, 2, connection.key) == expected_overflow_stamp(connection.key)


@pytest.mark.parametrize(
    ("document", "edit"),
    [
        (
            documents.pull_request_batch(2),
            ("repository(owner: $owner, name: $name)", "repository(name: $name, owner: $owner)"),
        ),
        (documents.pull_request_batch(2), ("rateLimit { cost remaining resetAt }", "")),
        (documents.pull_request_batch(2), ("additions deletions changedFiles", "additions")),
    ],
    ids=["batch-wrapper", "batch-rate-limit", "batch-fields"],
)
def test_a_changed_batch_document_changes_the_stamp(document: str, edit: tuple[str, str]) -> None:
    assert edit[0] in document
    assert pull_request_stamp(document.replace(*edit)) != expected_pull_request_stamp()


@pytest.mark.parametrize(
    "edit",
    [("headRefOid updatedAt ", ""), ("rateLimit { cost remaining resetAt }", "")],
    ids=["continuation-head", "continuation-rate-limit"],
)
def test_a_changed_continuation_document_changes_the_stamp(edit: tuple[str, str]) -> None:
    query = documents.overflow_continuation([documents.FILES])
    assert edit[0] in query
    assert overflow_stamp(query.replace(*edit), 0, "files") != expected_overflow_stamp("files")


def test_the_replay_refuses_a_request_for_another_document() -> None:
    replay = Replay.from_fixtures()
    query = documents.pull_request_batch(1).replace("additions deletions changedFiles", "additions")
    payload = {"query": query, "variables": {"owner": "prebid", "name": "Prebid.js", "n0": 13481}}
    request = httpx.Request("POST", "https://api.github.com/graphql", json=payload)
    response = replay.transport().handle_request(request)
    assert response.status_code == 418
    assert "re-record" in replay.problems[0]


def _refused(replay: Replay, payload: dict) -> str:
    request = httpx.Request("POST", "https://api.github.com/graphql", json=payload)
    response = replay.transport().handle_request(request)
    assert response.status_code == 418
    return replay.problems[0]


def test_the_replay_refuses_a_batch_alias_bound_to_another_number() -> None:
    query = documents.pull_request_batch(2).replace(
        "p1: pullRequest(number: $n1)", "p1: pullRequest(number: $n0)"
    )
    assert "pullRequest(number: $n0)" in query
    payload = {
        "query": query,
        "variables": {"owner": "prebid", "name": "Prebid.js", "n0": 13481, "n1": 15724},
    }
    assert "bind" in _refused(Replay.from_fixtures(), payload)


def test_the_replay_refuses_a_continuation_bound_to_another_cursor() -> None:
    connections = [documents.CONNECTIONS_BY_KEY["commits"]] * 2
    query = documents.overflow_continuation(connections)
    block = recordings.extract_block(query, "o1_commits: node(")
    query = query.replace(block, block.replace("$after1", "$after0"))
    payload = {
        "query": query,
        "variables": {"id0": "PR_a", "after0": "x", "id1": "PR_b", "after1": "y"},
    }
    assert "binds" in _refused(Replay.from_fixtures(), payload)


def test_the_replay_refuses_a_batch_declaring_an_unsent_number() -> None:
    query = documents.pull_request_batch(1).replace("$n0: Int!", "$n0: Int! $n1: Int!")
    payload = {"query": query, "variables": {"owner": "prebid", "name": "Prebid.js", "n0": 13481}}
    assert "declares" in _refused(Replay.from_fixtures(), payload)


def test_the_replay_refuses_a_continuation_with_an_unbound_variable() -> None:
    query = documents.overflow_continuation([documents.CONNECTIONS_BY_KEY["commits"]])
    payload = {"query": query, "variables": {"id0": "PR_a", "after0": "x", "id1": "PR_b"}}
    assert "variables" in _refused(Replay.from_fixtures(), payload)


def test_the_replay_refuses_batch_aliases_out_of_order() -> None:
    query = documents.pull_request_batch(2)
    first = "p0: pullRequest(number: $n0) { ...PullRequestFields }"
    second = "p1: pullRequest(number: $n1) { ...PullRequestFields }"
    query = query.replace(f"{first} {second}", f"{second} {first}")
    assert query.index("p1:") < query.index("p0:")
    payload = {
        "query": query,
        "variables": {"owner": "prebid", "name": "Prebid.js", "n0": 13481, "n1": 15724},
    }
    assert "bind" in _refused(Replay.from_fixtures(), payload)


def test_the_replay_refuses_a_continuation_reading_another_alias_id() -> None:
    connections = [documents.CONNECTIONS_BY_KEY["commits"]] * 2
    query = documents.overflow_continuation(connections).replace(
        "o1_commits: node(id: $id1)", "o1_commits: node(id: $id0)"
    )
    payload = {
        "query": query,
        "variables": {"id0": "PR_a", "after0": "x", "id1": "PR_b", "after1": "y"},
    }
    assert "binds" in _refused(Replay.from_fixtures(), payload)


def test_the_replay_refuses_a_continuation_declaring_an_unused_alias() -> None:
    query = documents.overflow_continuation([documents.CONNECTIONS_BY_KEY["commits"]]).replace(
        "$id0: ID! $after0: String!", "$id0: ID! $after0: String! $id1: ID! $after1: String!"
    )
    payload = {"query": query, "variables": {"id0": "PR_a", "after0": "x"}}
    assert "declarations" in _refused(Replay.from_fixtures(), payload)


def test_the_replay_refuses_a_request_it_has_no_unit_for() -> None:
    payload = {
        "query": documents.pull_request_batch(1),
        "variables": {"owner": "prebid", "name": "Prebid.js", "n0": 1},
    }
    assert "no pull_request unit" in _refused(Replay.from_fixtures(), payload)


@pytest.mark.parametrize(
    "query",
    ["per_page=100&page=1&sha=main", "per_page=100", "per_page=100&page=1&page=2"],
    ids=["extra-parameter", "missing-page", "repeated-page"],
)
def test_a_compare_request_with_other_parameters_finds_no_unit(query: str) -> None:
    base = "/repos/prebid/Prebid.js/compare/" + "a" * 40 + "..." + "b" * 40
    assert compare_key(f"https://api.github.com{base}?{query}") is None
    assert compare_key(f"https://api.github.com{base}?per_page=100&page=1") is not None


def test_a_fault_never_served_or_not_used_up_is_reported() -> None:
    replay = Replay.from_fixtures()
    replay.fault(lambda request: httpx.Response(500), when=lambda seen: seen.operation == "Resolve")
    replay.fault(lambda request: httpx.Response(500), times=2)
    assert unused_faults(replay) == ["fault 0: served 0, 1 left", "fault 1: served 0, 2 left"]
    payload = {"query": documents.RESOLVE, "variables": {"owner": "prebid", "name": "go-gdpr"}}
    request = httpx.Request("POST", "https://api.github.com/graphql", json=payload)
    replay.transport().handle_request(request)
    assert unused_faults(replay) == ["fault 1: served 0, 2 left"]
    replay.transport().handle_request(request)
    replay.transport().handle_request(request)
    assert unused_faults(replay) == []


def test_splitting_a_replayed_batch_gives_back_its_units() -> None:
    replay = Replay.from_fixtures()
    numbers = [15724, 999999, 13481]
    payload = {
        "query": documents.pull_request_batch(len(numbers)),
        "variables": {
            "owner": "prebid",
            "name": "Prebid.js",
            **{f"n{i}": number for i, number in enumerate(numbers)},
        },
    }
    transport = replay.transport()
    request = httpx.Request("POST", "https://api.github.com/graphql", json=payload)
    response = transport.handle_request(request)
    body = json.loads(response.read())
    units = split_graphql(payload, body, dict(response.headers), "now")
    for unit in units:
        original = replay.units[("pull_request", json.dumps(unit.key, sort_keys=True))]
        assert unit.data == original.data
        assert [
            {key: value for key, value in error.items() if key != "locations"}
            for error in unit.errors
        ] == [
            {key: value for key, value in error.items() if key != "locations"}
            for error in original.errors
        ]
        assert unit.document_sha256 == original.document_sha256
    assert replay.problems == []
