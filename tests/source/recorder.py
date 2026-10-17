"""Record the source-layer fixtures from live GitHub (run ``scripts/record_fixtures.py``).

Runs the real source layer through a recording transport and writes one file per unit (see
``tests.source.recordings``). Public repositories only: the recorder refuses a private
repository before writing anything. Response bodies and the headers the source layer reads are
kept; request headers, which carry the token, never are.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from github_activity_db.source import GitHubSource
from tests.source.recordings import RECORDED, Unit, split_graphql, split_rest

MAX_FILE_BYTES = 1000 * 1024  # check-added-large-files --maxkb=1000
PRIVATE = frozenset({"prebid/prebid-agent-skills"})


@dataclass(frozen=True)
class Scenario:
    owner: str
    name: str
    index: bool = False
    numbers: tuple[int, ...] = ()


# Each case names why it is recorded; tests refer to these numbers.
SCENARIOS = (
    # Index: one page, and two pages (157 PRs).
    Scenario("prebid", "go-gdpr", index=True),
    Scenario("prebid", "prebid-cache-java", index=True),
    Scenario(
        "prebid",
        "Prebid.js",
        numbers=(
            15724,  # dependabot as PR author (Bot) and as commit user (declared User)
            13481,  # changedFiles 2, files 3: one path listed twice
            12975,  # null PR author; one of two comments from a deleted account
            13769,  # merged with null mergedBy
            11155,  # 101 commits: commit overflow through node(id:)
            8858,  # 254 commits: compare fallback, 3 pages
            999999,  # no such PR: NOT_FOUND alias
        ),
    ),
    Scenario("prebid", "prebid-server-java", numbers=(4637,)),  # a dismissed verdict
    Scenario("prebid", "prebid-mobile-ios", numbers=(851,)),  # 101 files: file overflow
    Scenario("prebid", "salesagent", numbers=(1699,)),  # 106 reviews: review overflow
    Scenario("prebid", "prebid-server", numbers=(4343,)),  # 109 comments: comment overflow
)

USERS = (
    "MDM6Qm90NDk2OTkzMzM=",  # dependabot, as GitActor.user returns it
    "MDQ6VXNlcjE2ODMxNzU=",  # patmmccann
    "MDQ6VXNlcjk5OTk5OTk5OTk=",  # no such account: NOT_FOUND
)


class PrivateRepository(Exception):
    pass


class RecordingTransport(httpx.AsyncBaseTransport):
    """Passes requests to GitHub and splits each successful response into units."""

    def __init__(self, inner: httpx.AsyncBaseTransport, recorded_at: str) -> None:
        self._inner = inner
        self._recorded_at = recorded_at
        self.units: list[Unit] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        await response.aread()
        if response.status_code != 200:
            return response
        body = json.loads(response.content)
        headers = dict(response.headers)
        if request.url.path == "/graphql":
            units = split_graphql(json.loads(request.content), body, headers, self._recorded_at)
        else:
            units = split_rest(str(request.url), body, headers, self._recorded_at)
        for unit in units:
            if (
                unit.kind == "resolve"
                and isinstance(unit.data, dict)
                and unit.data.get("isPrivate")
            ):
                raise PrivateRepository(unit.data.get("nameWithOwner"))
        self.units.extend(units)
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


async def record(token: str) -> list[Unit]:
    recorded_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    recording = RecordingTransport(httpx.AsyncHTTPTransport(), recorded_at)
    async with GitHubSource(token, http_transport=recording) as source:
        for scenario in SCENARIOS:
            full_name = f"{scenario.owner}/{scenario.name}"
            if full_name in PRIVATE:
                raise PrivateRepository(full_name)
            repository = await source.resolve(scenario.owner, scenario.name)
            if scenario.index:
                await source.index_pull_requests(repository)
            if scenario.numbers:
                await source.hydrate_pull_requests(repository, scenario.numbers)
        await source.lookup_users(USERS)
        print(f"usage: {source.usage}", file=sys.stderr)
    return recording.units


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print(
            'GITHUB_TOKEN is required (for example GITHUB_TOKEN="$(gh auth token)")',
            file=sys.stderr,
        )
        return 2
    units = asyncio.run(record(token))
    staging = RECORDED.with_name("recorded.new")
    shutil.rmtree(staging, ignore_errors=True)
    for unit in units:
        path = staging / unit.path()
        path.parent.mkdir(parents=True, exist_ok=True)
        text = unit.dumps()
        if len(text.encode()) > MAX_FILE_BYTES:
            print(f"{path} is {len(text.encode())} bytes, over {MAX_FILE_BYTES}", file=sys.stderr)
            return 1
        path.write_text(text)
    shutil.rmtree(RECORDED, ignore_errors=True)
    staging.rename(RECORDED)
    print(f"wrote {len(units)} units under {RECORDED}", file=sys.stderr)
    return 0
