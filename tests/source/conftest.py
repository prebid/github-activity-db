"""Fixtures for the source-layer tests: a replayed GitHub and a clock that never sleeps."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest

from github_activity_db.source import GitHubSource
from tests.source.replay import Replay

START = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


class FakeClock:
    """Time that moves only when the code under test sleeps; jitter is zero."""

    def __init__(self, start: datetime = START) -> None:
        self._now = start
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self._now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self._now += timedelta(seconds=seconds)

    def jitter(self, delay: float) -> float:
        return delay


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def replay() -> Iterator[Replay]:
    replay = Replay.from_fixtures()
    yield replay
    assert replay.problems == [], "\n".join(replay.problems)


@pytest.fixture
def open_source(replay: Replay, clock: FakeClock):
    """``async with open_source(deadline=...) as source`` over the replay and the fake clock."""

    @asynccontextmanager
    async def opened(deadline: datetime | None = None) -> AsyncIterator[GitHubSource]:
        source = GitHubSource(
            "test-token", http_transport=replay.transport(), clock=clock, deadline=deadline
        )
        async with source:
            yield source

    return opened
