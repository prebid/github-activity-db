"""Fixtures for the source-layer tests: a replayed GitHub and a clock that never sleeps."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Protocol

import pytest

from github_activity_db.source import (
    Account,
    GitHubSource,
    LookedUpAccount,
    PullRequestOutcome,
    RepositoryRecord,
    UserLookupFailed,
)
from tests.source.replay import Replay, unanswered

START = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


class FakeClock:
    """Time that moves only when the code under test sleeps or a test advances it; jitter is
    zero."""

    def __init__(self, start: datetime = START) -> None:
        self._now = start
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        """Time passing between a test's steps, which ``sleeps`` does not record."""
        self._now += timedelta(seconds=seconds)

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self._now += timedelta(seconds=seconds)

    def jitter(self, delay: float) -> float:
        return delay


class OpenSource(Protocol):
    def __call__(
        self, deadline: datetime | None = None
    ) -> AbstractAsyncContextManager[GitHubSource]: ...


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def replay() -> Iterator[Replay]:
    replay = Replay.from_fixtures()
    yield replay
    assert unanswered(replay) == []


def opened_over(replay: Replay, clock: FakeClock) -> OpenSource:
    """``async with opened(deadline=...) as source``: the real source over ``replay``."""

    @asynccontextmanager
    async def opened(deadline: datetime | None = None) -> AsyncIterator[GitHubSource]:
        source = GitHubSource(
            "test-token", http_transport=replay.transport(), clock=clock, deadline=deadline
        )
        async with source:
            yield source

    return opened


@pytest.fixture
def open_source(replay: Replay, clock: FakeClock) -> OpenSource:
    """``async with open_source(deadline=...) as source`` over the replay and the fake clock."""
    return opened_over(replay, clock)


async def all_outcomes(
    source: GitHubSource, repository: RepositoryRecord, numbers: Sequence[int]
) -> list[PullRequestOutcome]:
    """Every batch's outcomes, in order."""
    outcomes: list[PullRequestOutcome] = []
    async for batch in source.hydrate_pull_requests(repository, numbers):
        outcomes.extend(batch)
    return outcomes


async def all_lookups(
    source: GitHubSource, node_ids: Sequence[str]
) -> tuple[dict[str, LookedUpAccount | None], dict[str, UserLookupFailed]]:
    """Every batch's looked-up accounts and failed ids, merged."""
    accounts: dict[str, LookedUpAccount | None] = {}
    failed: dict[str, UserLookupFailed] = {}
    async for lookup in source.lookup_users(node_ids):
        accounts.update(lookup.accounts)
        failed.update(lookup.failed)
    return accounts, failed


async def all_accounts(source: GitHubSource, node_ids: Sequence[str]) -> dict[str, Account | None]:
    """Every account, for a lookup that must not fail."""
    accounts, failed = await all_lookups(source, node_ids)
    assert failed == {}, failed
    return {
        node_id: looked_up.account if looked_up is not None else None
        for node_id, looked_up in accounts.items()
    }
