"""Record the source-layer fixtures from live GitHub (run ``scripts/record_fixtures.py``).

Runs the real source layer through a recording transport and writes one file per unit (see
``tests.source.recordings``). Public repositories only: the recorder refuses a private
repository before writing anything. A failed item or user lookup stops the recording, so the
fixtures are replaced only by a complete set. Response bodies and the headers the source layer
reads are kept; request headers, which carry the token, never are.

``--only first-sync`` runs the sync engine's first sync of ``FIRST_SYNC_REPOSITORY`` into a
scratch database through the recording transport and writes every unit it asked for under
``tests/fixtures/source/first_sync/``, replacing that directory alone.

``--only rate-limits`` records the rate-limit readings alone and adds them to the existing
recordings, leaving every other unit unchanged. ``--only documents`` records every scenario and
user again for a change of the GraphQL documents and replaces those units, keeping the existing
REST compare pages and rate-limit readings, whose requests did not change; a compare page the
existing recordings lack is added.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

from github_activity_db.source import Clock, Failed, GitHubSource, RepositoryRecord, SystemClock
from github_activity_db.sync.engine import SyncOptions, sync
from github_activity_db.tracking import TrackingConfig, TrackingConfigFile, load_tracking_config
from tests.db.alembic_support import upgrade
from tests.source.recordings import FIRST_SYNC, RECORDED, Unit, split_graphql, split_rest

MAX_FILE_BYTES = 1000 * 1024  # check-added-large-files --maxkb=1000
# Lowercase: GitHub matches owner and name ignoring case.
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
    # 21: comments created by email (createdViaEmail true), a review request, an assignee.
    Scenario("prebid", "go-gdpr", index=True, numbers=(21,)),
    Scenario("prebid", "prebid-cache-java", index=True),
    Scenario(
        "prebid",
        "Prebid.js",
        numbers=(
            15724,  # dependabot as PR author (Bot) and as commit user (declared User)
            13481,  # changedFiles 2, files 3: one path listed twice
            12975,  # null PR author; one of two comments with a null author
            13769,  # merged with null mergedBy
            11155,  # 101 commits: commit overflow through node(id:)
            8858,  # 254 commits: compare fallback, 3 pages
            15771,  # closed without merge; a Bot and a User assigned; an editor
            15753,  # a user-linked closing issue; reviews with inline comments
            15735,  # an edited review
            999999,  # no such PR: NOT_FOUND alias
        ),
    ),
    Scenario("prebid", "prebid-server-java", numbers=(4637,)),  # a dismissed verdict
    Scenario("prebid", "prebid-mobile-ios", numbers=(851,)),  # 101 files: file overflow
    Scenario(
        "prebid",
        "salesagent",
        numbers=(
            1699,  # 106 reviews: review overflow
            1649,  # in a milestone
            2360,  # open, a review requested as code owner
        ),
    ),
    Scenario("prebid", "prebid-server", numbers=(4343,)),  # 109 comments: comment overflow
    Scenario("prebid", "prebid-cache", numbers=(129,)),  # GitHub returns no head repository
    # 144 labels: a second label page. No tracked repository has more than 100 labels.
    Scenario("golang", "go"),
)

USERS = (
    "MDM6Qm90NDk2OTkzMzM=",  # dependabot, as GitActor.user returns it
    "MDQ6VXNlcjE2ODMxNzU=",  # patmmccann
    "MDQ6VXNlcjY0NzcyNw==",  # muuki88: a profile with company, location, and website
    "MDEyOk9yZ2FuaXphdGlvbjEzMjQyNzYw",  # prebid: an Organization profile
    "MDQ6VXNlcjk5OTk5OTk5OTk=",  # no such account: NOT_FOUND
)

# The repository whose first sync is recorded: small (one batch), with commit-linked accounts no
# actor sighting covers, so the sync looks them up.
FIRST_SYNC_REPOSITORY = ("prebid", "go-gdpr")

# Units --only documents keeps: their REST and RateLimit requests did not change.
KEPT_BY_DOCUMENTS = frozenset({"compare", "rate_limit", "rest_rate_limit"})


class PrivateRepository(Exception):
    pass


def _refuse_listed(owner: str, name: str) -> None:
    """Raise ``PrivateRepository`` for a repository ``PRIVATE`` lists, before any request."""
    if f"{owner}/{name}".lower() in PRIVATE:
        raise PrivateRepository(f"{owner}/{name}")


class PublicOnlySource(GitHubSource):
    """The source every recording reads through: a resolve that answers a private repository
    raises ``PrivateRepository`` once the resolve's own requests (its first response, then any
    further label pages) are answered, so nothing else of it is requested (D33). ``refused``
    names it."""

    refused: str | None = None

    async def resolve(self, owner: str, name: str) -> RepositoryRecord:
        repository = await super().resolve(owner, name)
        if repository.is_private:
            self.refused = repository.full_name
            raise PrivateRepository(repository.full_name)
        return repository


class RecordingFailed(Exception):
    """An item or a user lookup failed, so the recording would be incomplete."""


class RecordingTransport(httpx.AsyncBaseTransport):
    """Passes requests to GitHub and splits each response that carries data into units.

    A unit answered again (a batch alias that failed, then its item's solo retry) replaces the
    earlier one, since both would be written to one file. A 200 without data (a GraphQL error
    page, a body that is not JSON) is passed on unrecorded, and the source layer classifies it:
    when it retries or waits, the next answer is recorded; when it fails the request or the run,
    the recording stops as a live run would.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport, recorded_at: str) -> None:
        self._inner = inner
        self._recorded_at = recorded_at
        self._units: dict[Path, Unit] = {}

    @property
    def units(self) -> list[Unit]:
        return list(self._units.values())

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
        self._units.update((unit.path(), unit) for unit in units)
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


async def record(
    token: str,
    *,
    inner: httpx.AsyncBaseTransport | None = None,
    scenarios: tuple[Scenario, ...] = SCENARIOS,
    users: tuple[str, ...] = USERS,
    rate_limits: bool = True,
    clock: Clock | None = None,
) -> list[Unit]:
    """Record ``scenarios``, ``users``, and the rate-limit readings; refuse a private repository
    before reading more of it.

    The resolve's requests (its first response, then any further label pages) are the first
    ones for a repository, so a private one raises after them and before ``main`` writes
    anything.
    """
    recorded_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    recording = RecordingTransport(inner or httpx.AsyncHTTPTransport(), recorded_at)
    async with PublicOnlySource(token, http_transport=recording, clock=clock) as source:
        for scenario in scenarios:
            full_name = f"{scenario.owner}/{scenario.name}"
            _refuse_listed(scenario.owner, scenario.name)
            repository = await source.resolve(scenario.owner, scenario.name)
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
        if rate_limits:
            await source.rate_limits()
        print(f"usage: {source.usage}", file=sys.stderr)
    return recording.units


def first_sync_config(owner: str, name: str) -> TrackingConfig:
    """The packaged tracking.toml with only ``owner/name`` tracked: its floor and settings."""
    packaged = load_tracking_config()
    entry = next(r for r in packaged.repositories if (r.owner, r.name) == (owner, name))
    return packaged.model_copy(update={"repositories": (entry,), "sync": packaged.sync})


async def record_first_sync(
    token: str, owner: str, name: str, *, inner: httpx.AsyncBaseTransport | None = None
) -> list[Unit]:
    """Run a first sync of ``owner/name`` into a scratch database through the recording
    transport; every unit it asked for. A private repository is refused after the sync's resolve
    (its label pages included) and before any other request; a failed item or a run that does not
    exit 0 stops it too."""
    _refuse_listed(owner, name)
    recorded_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    recording = RecordingTransport(inner or httpx.AsyncHTTPTransport(), recorded_at)
    opened: list[PublicOnlySource] = []

    def open_source(deadline: datetime | None) -> PublicOnlySource:
        opened.append(PublicOnlySource(token, http_transport=recording, deadline=deadline))
        return opened[-1]

    config = first_sync_config(owner, name)
    with tempfile.TemporaryDirectory() as directory:
        database = Path(directory) / "first-sync.db"
        upgrade(database)
        report = await sync(
            SyncOptions(
                database_url=f"sqlite+aiosqlite:///{database}",
                config=TrackingConfigFile(config, "recorder", "0" * 64),
                trigger="manual",
                code_version="recorder",
            ),
            open_source=open_source,
            clock=SystemClock(),
        )
    # The engine fails the run on the refusal, before the index.
    refused = [source.refused for source in opened if source.refused is not None]
    if refused:
        raise PrivateRepository(refused[0])
    if report.exit_code != 0 or report.failures:
        raise RecordingFailed(f"first sync exited {report.exit_code}: {report.failures}")
    print(
        f"first sync: {report.items_hydrated} items, {report.graphql_points} GraphQL points",
        file=sys.stderr,
    )
    return recording.units


def write_units(units: list[Unit], directory: Path) -> int:
    """Write ``units`` under a fresh ``directory``, replacing it only when every unit fits."""
    staging = directory.with_name(directory.name + ".new")
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
    shutil.rmtree(directory, ignore_errors=True)
    staging.rename(directory)
    print(f"wrote {len(units)} units under {directory}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record the source-layer test fixtures.")
    parser.add_argument(
        "--only",
        choices=("rate-limits", "documents", "first-sync"),
        help="rate-limits: record the readings alone; documents: record every GraphQL unit "
        "again, keeping the compare pages and rate-limit readings; first-sync: record the "
        "first sync of FIRST_SYNC_REPOSITORY under tests/fixtures/source/first_sync",
    )
    args = parser.parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print(
            'GITHUB_TOKEN is required (for example GITHUB_TOKEN="$(gh auth token)")',
            file=sys.stderr,
        )
        return 2
    if args.only == "first-sync":
        owner, name = FIRST_SYNC_REPOSITORY
        return write_units(asyncio.run(record_first_sync(token, owner, name)), FIRST_SYNC)
    if args.only == "rate-limits":
        units = asyncio.run(record(token, scenarios=(), users=()))
    elif args.only == "documents":
        units = asyncio.run(record(token, rate_limits=False))
    else:
        units = asyncio.run(record(token))
    staging = RECORDED.with_name("recorded.new")
    shutil.rmtree(staging, ignore_errors=True)
    if args.only is not None:
        shutil.copytree(RECORDED, staging)
    if args.only == "documents":
        for path in list(staging.rglob("*.json")):
            if Unit.load(path).kind not in KEPT_BY_DOCUMENTS:
                path.unlink()
        for directory in sorted(staging.rglob("*"), reverse=True):
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
        units = [
            unit
            for unit in units
            if unit.kind not in KEPT_BY_DOCUMENTS or not (staging / unit.path()).exists()
        ]
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
