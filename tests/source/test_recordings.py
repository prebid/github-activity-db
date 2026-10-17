"""The recording format: split and replay agree, and every unit matches the current documents."""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest

from github_activity_db.source import documents
from tests.source.recordings import (
    COMPARE_STAMP,
    RECORDED,
    Unit,
    canonical_batch,
    expected_overflow_stamp,
    expected_pull_request_stamp,
    overflow_stamp,
    pull_request_stamp,
    split_graphql,
    whole_stamp,
)
from tests.source.replay import Replay

UNITS = sorted(RECORDED.rglob("*.json"))
WHOLE_DOCUMENTS = {
    "resolve": documents.RESOLVE,
    "repository_labels": documents.REPOSITORY_LABELS,
    "index": documents.PULL_REQUEST_INDEX,
    "user": documents.USER_LOOKUP,
}


def test_fixtures_were_recorded() -> None:
    kinds = {Unit.load(path).kind for path in UNITS}
    assert {"resolve", "index", "pull_request", "overflow", "compare", "user"} <= kinds


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


@pytest.mark.parametrize("connection", documents.PULL_REQUEST_CONNECTIONS, ids=lambda c: c.field)
def test_a_continuation_stamp_does_not_depend_on_its_neighbours(connection) -> None:
    query = documents.overflow_continuation([documents.LABELS, documents.REVIEWS, connection])
    assert overflow_stamp(query, 2, connection.field) == expected_overflow_stamp(connection.field)


@pytest.mark.parametrize(
    ("document", "edit"),
    [
        (documents.pull_request_batch(2), ("{ databaseId p0:", "{ p0:")),
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
