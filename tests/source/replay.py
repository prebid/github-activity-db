"""An httpx transport that answers the source layer from recorded units.

It assembles each GraphQL request from per-unit recordings, checks every unit's document stamp
against the request text, and lets a test inject failures in front of chosen requests. Anything
it cannot answer is recorded in ``problems`` and answered with HTTP 418, which the source layer
treats as fatal; the ``replay`` fixture fails the test when ``problems`` is not empty.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from github_activity_db.source import documents
from github_activity_db.source.documents import pull_request_alias
from tests.source.recordings import (
    COMPARE_STAMP,
    RECORDED,
    SYNTHETIC,
    Unit,
    canonical_batch,
    compare_key,
    overflow_stamp,
    pull_request_stamp,
    whole_stamp,
)

RATE_LIMIT = {"cost": 1, "remaining": 4900, "resetAt": "2099-01-01T00:00:00Z"}


@dataclass(frozen=True)
class Seen:
    """A request the replay received."""

    operation: str
    variables: dict[str, Any]
    query: str
    path: str

    @property
    def numbers(self) -> list[int]:
        """Pull request numbers of a batch request, in alias order."""
        return [
            value
            for name, value in sorted(
                self.variables.items(), key=lambda item: _alias_order(item[0])
            )
            if re.fullmatch(r"n\d+", name)
        ]


def _alias_order(name: str) -> tuple[int, str]:
    digits = re.sub(r"\D", "", name)
    return (int(digits) if digits else -1, name)


@dataclass
class Fault:
    """A response served in place of the recording for requests that match."""

    matches: Callable[[Seen], bool]
    respond: Callable[[httpx.Request], httpx.Response]
    times: int | None = 1


@dataclass
class Replay:
    units: dict[tuple[str, str], Unit] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    seen: list[Seen] = field(default_factory=list)
    faults: list[Fault] = field(default_factory=list)
    # Wraps the handler, for a test that needs to observe requests as they run.
    wrap: Callable[[Callable[[httpx.Request], httpx.Response]], Any] | None = None

    @classmethod
    def from_fixtures(cls) -> Replay:
        replay = cls()
        for unit in _loaded_units():
            replay.add(copy.deepcopy(unit))
        return replay

    def add(self, unit: Unit) -> None:
        """Serve ``unit``, replacing any unit with the same kind and key."""
        self.units[(unit.kind, _key(unit.key))] = unit

    def unit(self, kind: str, **key: Any) -> Unit:
        """A copy of a loaded unit, to modify and ``add`` back."""
        return copy.deepcopy(self.units[(kind, _key(key))])

    def fault(
        self,
        respond: Callable[[httpx.Request], httpx.Response],
        *,
        when: Callable[[Seen], bool] = lambda seen: True,
        times: int | None = 1,
    ) -> None:
        """Answer the next ``times`` matching requests with ``respond`` (None: every one)."""
        self.faults.append(Fault(when, respond, times))

    def modify(
        self,
        change: Callable[[dict[str, Any]], None],
        *,
        when: Callable[[Seen], bool] = lambda seen: True,
        times: int | None = 1,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Serve the recorded response for matching requests after ``change`` edits its body.

        ``headers`` add to or replace the recorded response headers.
        """

        def respond(request: httpx.Request) -> httpx.Response:
            seen = self._seen(request)
            try:
                normal = self._rest(request) if seen.operation == "rest" else self._graphql(seen)
            except _Miss as miss:
                self.problems.append(str(miss))
                return httpx.Response(418, json={"message": str(miss)})
            body = json.loads(normal.content)
            change(body)
            kept = {
                name: value
                for name, value in normal.headers.items()
                if name.lower() not in ("content-length", "content-type")
            }
            merged = {**kept, **(headers or {})}
            return httpx.Response(normal.status_code, json=body, headers=merged)

        self.fault(respond, when=when, times=times)

    def transport(self) -> httpx.MockTransport:
        handler = self.wrap(self._handle) if self.wrap is not None else self._handle
        return httpx.MockTransport(handler)

    # ----------------------------------------------------------------------------------------
    def _handle(self, request: httpx.Request) -> httpx.Response:
        seen = self._seen(request)
        self.seen.append(seen)
        for fault in self.faults:
            if fault.times != 0 and fault.matches(seen):
                if fault.times is not None:
                    fault.times -= 1
                return fault.respond(request)
        try:
            if seen.operation == "rest":
                return self._rest(request)
            return self._graphql(seen)
        except _Miss as miss:
            self.problems.append(str(miss))
            return httpx.Response(418, json={"message": str(miss)})

    def _seen(self, request: httpx.Request) -> Seen:
        if request.url.path == "/graphql":
            payload = json.loads(request.content)
            operation = re.match(r"\s*query (\w+)", payload["query"])
            return Seen(
                operation=operation.group(1) if operation else "?",
                variables=payload.get("variables") or {},
                query=payload["query"],
                path=request.url.path,
            )
        return Seen(operation="rest", variables={}, query="", path=str(request.url))

    def _lookup(self, kind: str, key: dict[str, Any], stamp: str | None = None) -> Unit:
        unit = self.units.get((kind, _key(key)))
        if unit is None:
            raise _Miss(f"no {kind} unit for {key}: record it with scripts/record_fixtures.py")
        if stamp is not None and unit.document_sha256 != stamp:
            raise _Miss(
                f"{kind} unit {key} was recorded for another query document: re-record with "
                "scripts/record_fixtures.py"
            )
        return unit

    def _graphql(self, seen: Seen) -> httpx.Response:
        variables = seen.variables
        match seen.operation:
            case "Resolve":
                unit = self._lookup(
                    "resolve",
                    {"owner": variables["owner"], "name": variables["name"]},
                    whole_stamp(seen.query),
                )
                return _graphql_response(
                    seen, [unit], {"repository": unit.data}, [(unit, ["repository"])]
                )
            case "RepositoryLabels":
                unit = self._lookup(
                    "repository_labels",
                    {"id": variables["id"], "after": variables["after"]},
                    whole_stamp(seen.query),
                )
                return _graphql_response(seen, [unit], {"node": unit.data}, [(unit, ["node"])])
            case "PullRequestIndex":
                key = {
                    "owner": variables["owner"],
                    "name": variables["name"],
                    "after": variables.get("after"),
                }
                unit = self._lookup("index", key, whole_stamp(seen.query))
                return _graphql_response(
                    seen,
                    [unit],
                    {"repository": {"pullRequests": unit.data}},
                    [(unit, ["repository"])],
                )
            case "PullRequestBatch":
                stamp = pull_request_stamp(seen.query)
                container: dict[str, Any] = {}
                units: list[tuple[Unit, list[Any]]] = []
                for index, number in enumerate(seen.numbers):
                    key = {"owner": variables["owner"], "name": variables["name"], "number": number}
                    unit = self._lookup("pull_request", key, stamp)
                    container[pull_request_alias(index)] = unit.data
                    if "{ databaseId p0:" in canonical_batch(seen.query):
                        container["databaseId"] = unit.extra["repository_database_id"]
                    units.append((unit, ["repository", f"p{index}"]))
                return _graphql_response(
                    seen, [unit for unit, _ in units], {"repository": container}, units
                )
            case "Overflow":
                data: dict[str, Any] = {}
                units = []
                for alias in re.findall(r"\b(o\d+_\w+): node\(", seen.query):
                    index_text, connection = alias[1:].split("_", 1)
                    index = int(index_text)
                    key = {
                        "id": variables[f"id{index}"],
                        "connection": connection,
                        "after": variables[f"after{index}"],
                    }
                    unit = self._lookup(
                        "overflow", key, overflow_stamp(seen.query, index, connection)
                    )
                    data[alias] = unit.data
                    units.append((unit, [alias]))
                return _graphql_response(seen, [unit for unit, _ in units], data, units)
            case "UserLookup":
                stamp = whole_stamp(seen.query)
                nodes = []
                units = []
                for position, node_id in enumerate(variables["ids"]):
                    unit = self._lookup("user", {"id": node_id}, stamp)
                    nodes.append(unit.data)
                    units.append((unit, ["nodes", position]))
                return _graphql_response(seen, [unit for unit, _ in units], {"nodes": nodes}, units)
        raise _Miss(f"no replay for GraphQL operation {seen.operation}")

    def _rest(self, request: httpx.Request) -> httpx.Response:
        key = compare_key(str(request.url))
        if key is None:
            raise _Miss(f"no replay for REST {request.url}")
        unit = self._lookup("compare", key, whole_stamp(COMPARE_STAMP))
        return httpx.Response(200, json=unit.data, headers=_headers([unit]))


class _Miss(Exception):
    pass


def _graphql_response(
    seen: Seen,
    units: list[Unit],
    data: dict[str, Any],
    errors_from: list[tuple[Unit, list[Any]]],
) -> httpx.Response:
    errors = [
        {**error, "path": [*prefix, *error.get("path", [])]}
        for unit, prefix in errors_from
        for error in unit.errors
    ]
    # Only what the request selects: a document without rateLimit gets none, as from GitHub.
    selected = documents.normalize(seen.query)
    if documents.normalize(documents.RATE_LIMIT) in selected:
        data = {**data, "rateLimit": dict(RATE_LIMIT)}
    body: dict[str, Any] = {"data": data}
    if errors:
        body["errors"] = errors
    return httpx.Response(200, json=body, headers=_headers(units))


def _headers(units: list[Unit]) -> dict[str, str]:
    headers = {"content-type": "application/json"}
    if units:
        headers.update(units[0].headers)
    return headers


def _key(key: dict[str, Any]) -> str:
    return json.dumps(key, sort_keys=True)


_CACHE: list[Unit] | None = None


def _loaded_units() -> list[Unit]:
    global _CACHE
    if _CACHE is None:
        _CACHE = [Unit.load(path) for path in sorted(RECORDED.rglob("*.json"))]
    return _CACHE


def synthetic(name: str, *, headers: dict[str, str] | None = None) -> httpx.Response:
    """A response built from ``tests/fixtures/source/synthetic/<name>.json``.

    Each file names its ``source``: GitHub's documented shape, or the recorded unit it modifies.
    ``headers`` add to or replace the file's headers (a reset time relative to the test clock).
    """
    spec = json.loads((SYNTHETIC / f"{name}.json").read_text())
    merged = {"content-type": "application/json", **spec.get("headers", {}), **(headers or {})}
    return httpx.Response(spec["status"], json=spec["body"], headers=merged)
