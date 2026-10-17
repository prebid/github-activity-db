"""Record the source-layer fixtures from live GitHub (run ``scripts/record_fixtures.py``).

Runs the real source layer through a recording transport and writes one file per unit (see
``tests.source.recordings``). Public repositories only: the recorder refuses a private
repository before writing anything. A failed item or user lookup stops the recording, so the
fixtures are replaced only by a complete set. Response bodies and the headers the source layer
reads are kept; request headers, which carry the token, never are.
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

from github_activity_db.source import Clock, Failed, GitHubSource
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
    # 144 labels: a second label page. No tracked repository has more than 100 labels.
    Scenario("golang", "go"),
)

USERS = (
    "MDM6Qm90NDk2OTkzMzM=",  # dependabot, as GitActor.user returns it
    "MDQ6VXNlcjE2ODMxNzU=",  # patmmccann
    "MDQ6VXNlcjk5OTk5OTk5OTk=",  # no such account: NOT_FOUND
)


class PrivateRepository(Exception):
    pass


class RecordingFailed(Exception):
    """An item or a user lookup failed, so the recording would be incomplete."""


class RecordingTransport(httpx.AsyncBaseTransport):
    """Passes requests to GitHub and splits each response that carries data into units.

    A 200 without data (a GraphQL error page, a body that is not JSON) is passed on unrecorded,
    and the source layer classifies it: when it retries or waits, the next answer is recorded;
    when it fails the request or the run, the recording stops as a live run would.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport, recorded_at: str) -> None:
        self._inner = inner
        self._recorded_at = recorded_at
        self.units: list[Unit] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        await response.aread()
        if response.status_code != 200:
            return response
        try:
            body = json.loads(response.content)
        except ValueError:
            return response
        if request.url.path == "/graphql" and (
            not isinstance(body, dict) or body.get("data") is None
        ):
            return response
        headers = dict(response.headers)
        if request.url.path == "/graphql":
            units = split_graphql(json.loads(request.content), body, headers, self._recorded_at)
        else:
            units = split_rest(str(request.url), body, headers, self._recorded_at)
        self.units.extend(units)
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


async def record(
    token: str,
    *,
    inner: httpx.AsyncBaseTransport | None = None,
    scenarios: tuple[Scenario, ...] = SCENARIOS,
    users: tuple[str, ...] = USERS,
    clock: Clock | None = None,
) -> list[Unit]:
    """Record ``scenarios`` and ``users``; refuse a private repository before reading more of it.

    The resolve response is the first one for a repository, so a private one raises after that
    one request and before ``main`` writes anything.
    """
    recorded_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    recording = RecordingTransport(inner or httpx.AsyncHTTPTransport(), recorded_at)
    async with GitHubSource(token, http_transport=recording, clock=clock) as source:
        for scenario in scenarios:
            full_name = f"{scenario.owner}/{scenario.name}"
            if full_name in PRIVATE:
                raise PrivateRepository(full_name)
            repository = await source.resolve(scenario.owner, scenario.name)
            if repository.is_private:
                raise PrivateRepository(repository.full_name)
            if scenario.index:
                await source.index_pull_requests(repository)
            if scenario.numbers:
                async for outcomes in source.hydrate_pull_requests(repository, scenario.numbers):
                    failed = [outcome for outcome in outcomes if isinstance(outcome, Failed)]
                    if failed:
                        raise RecordingFailed(f"{full_name}: {failed}")
        if users:
            async for lookup in source.lookup_users(users):
                if lookup.failed:
                    raise RecordingFailed(f"user lookup: {lookup.failed}")
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
            shutil.rmtree(staging, ignore_errors=True)
            return 1
        path.write_text(text)
    shutil.rmtree(RECORDED, ignore_errors=True)
    staging.rename(RECORDED)
    print(f"wrote {len(units)} units under {RECORDED}", file=sys.stderr)
    return 0
