"""The transport's failure classes, waits, retries, budget, and accounting (plan 5.3 step 4)."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import httpx
import pytest

from github_activity_db.source.errors import (
    DeadlineReached,
    Forbidden,
    RateLimitWaitTooLong,
    RepositoryRequestFailed,
    RequestTooHeavy,
    ServerError,
    Unauthorized,
    UnexpectedResponse,
)
from github_activity_db.source.limits import (
    MAX_RATE_LIMIT_WAIT,
    RATE_LIMIT_FALLBACK_WAIT,
    RESET_MARGIN,
    SERVER_RETRY_DELAYS,
)
from github_activity_db.source.transport import RestRefused
from tests.source.conftest import FakeClock
from tests.source.replay import Replay, Seen, synthetic

RESOLVE = {"owner": "prebid", "name": "go-gdpr"}


def resolves(seen: Seen) -> bool:
    return seen.operation == "Resolve"


def compare_unit(replay: Replay) -> tuple[str, dict[str, int]]:
    unit = next(unit for unit in replay.units.values() if unit.kind == "compare")
    key = unit.key
    path = f"/repos/{key['owner']}/{key['name']}/compare/{key['base']}...{key['head']}"
    return path, {"per_page": 100, "page": key["page"]}


def reset_in(clock: FakeClock, seconds: int) -> str:
    return str(int((clock.now() + timedelta(seconds=seconds)).timestamp()))


class TestRateLimited:
    async def test_graphql_primary_limit_waits_until_the_reset(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        replay.fault(
            lambda request: synthetic(
                "rate_limited", headers={"x-ratelimit-reset": reset_in(clock, 600)}
            ),
            when=resolves,
        )
        async with open_source() as source:
            repository = await source.resolve(**RESOLVE)
        assert repository.full_name == "prebid/go-gdpr"
        assert clock.sleeps == [600 + RESET_MARGIN]

    async def test_graphql_secondary_limit_on_200_waits_and_doubles(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        replay.fault(lambda request: synthetic("secondary_limit_graphql"), when=resolves, times=2)
        async with open_source() as source:
            await source.resolve(**RESOLVE)
        assert clock.sleeps == [RATE_LIMIT_FALLBACK_WAIT, 2 * RATE_LIMIT_FALLBACK_WAIT]

    async def test_secondary_limit_403_with_retry_after(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        replay.fault(
            lambda request: synthetic("secondary_limit_rest", headers={"retry-after": "30"}),
            when=resolves,
        )
        async with open_source() as source:
            await source.resolve(**RESOLVE)
        assert clock.sleeps == [30.0]

    async def test_secondary_limit_403_without_headers_falls_back(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        replay.fault(lambda request: synthetic("secondary_limit_rest"), when=resolves)
        async with open_source() as source:
            await source.resolve(**RESOLVE)
        assert clock.sleeps == [RATE_LIMIT_FALLBACK_WAIT]

    async def test_rest_429_falls_back(self, open_source, replay: Replay, clock: FakeClock) -> None:
        path, params = compare_unit(replay)
        replay.fault(
            lambda request: synthetic("too_many_requests"), when=lambda s: s.operation == "rest"
        )
        async with open_source() as source:
            result = await source.transport.rest_get(path, params)
        assert not isinstance(result, RestRefused)
        assert clock.sleeps == [RATE_LIMIT_FALLBACK_WAIT]

    async def test_rest_primary_limit_waits_until_the_reset(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        path, params = compare_unit(replay)
        replay.fault(
            lambda request: synthetic(
                "primary_limit_rest", headers={"x-ratelimit-reset": reset_in(clock, 120)}
            ),
            when=lambda s: s.operation == "rest",
        )
        async with open_source() as source:
            await source.transport.rest_get(path, params)
        assert clock.sleeps == [120 + RESET_MARGIN]

    async def test_a_wait_past_the_deadline_stops(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        replay.fault(
            lambda request: synthetic(
                "rate_limited", headers={"x-ratelimit-reset": reset_in(clock, 600)}
            ),
            when=resolves,
        )
        async with open_source(deadline=clock.now() + timedelta(minutes=5)) as source:
            with pytest.raises(DeadlineReached):
                await source.resolve(**RESOLVE)
        assert clock.sleeps == []

    async def test_without_a_deadline_an_overlong_wait_is_fatal(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        wait = int(MAX_RATE_LIMIT_WAIT) + 600
        replay.fault(
            lambda request: synthetic(
                "rate_limited", headers={"x-ratelimit-reset": reset_in(clock, wait)}
            ),
            when=resolves,
        )
        async with open_source() as source:
            with pytest.raises(RateLimitWaitTooLong):
                await source.resolve(**RESOLVE)


class TestFatal:
    async def test_401_is_unauthorized(self, open_source, replay: Replay) -> None:
        replay.fault(lambda request: synthetic("unauthorized"), when=resolves)
        async with open_source() as source:
            with pytest.raises(Unauthorized):
                await source.resolve(**RESOLVE)

    async def test_403_without_a_rate_limit_signal_is_forbidden(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        replay.fault(lambda request: synthetic("forbidden"), when=resolves)
        async with open_source() as source:
            with pytest.raises(Forbidden):
                await source.resolve(**RESOLVE)
        assert clock.sleeps == []

    async def test_a_rejected_query_is_unexpected(self, open_source, replay: Replay) -> None:
        replay.fault(lambda request: synthetic("query_rejected"), when=resolves)
        async with open_source() as source:
            with pytest.raises(UnexpectedResponse, match="noSuchField"):
                await source.resolve(**RESOLVE)

    async def test_an_unhandled_status_is_unexpected(self, open_source, replay: Replay) -> None:
        replay.fault(
            lambda request: httpx.Response(409, json={"message": "conflict"}), when=resolves
        )
        async with open_source() as source:
            with pytest.raises(UnexpectedResponse) as raised:
                await source.resolve(**RESOLVE)
        assert raised.value.status == 409


class TestServerErrors:
    """An unsplittable request (resolve) retries after 2, 4, and 8 s, then fails its repository."""

    @pytest.mark.parametrize(
        ("respond", "cause"),
        [
            (lambda request: synthetic("server_error"), ServerError),
            (lambda request: synthetic("bad_gateway"), RequestTooHeavy),
            (lambda request: synthetic("graphql_timeout"), RequestTooHeavy),
            (lambda request: synthetic("graphql_server_fault"), ServerError),
            (lambda request: httpx.Response(200, text="<html>proxy</html>"), ServerError),
        ],
        ids=["500", "502", "graphql-timeout", "graphql-server-fault", "not-json"],
    )
    async def test_retried_three_times_then_the_repository_fails(
        self, open_source, replay: Replay, clock: FakeClock, respond, cause
    ) -> None:
        replay.fault(respond, when=resolves, times=None)
        async with open_source() as source:
            with pytest.raises(RepositoryRequestFailed) as raised:
                await source.resolve(**RESOLVE)
        assert isinstance(raised.value.cause, cause)
        assert clock.sleeps == list(SERVER_RETRY_DELAYS)
        assert sum(1 for seen in replay.seen if seen.operation == "Resolve") == 4

    @pytest.mark.parametrize(
        "error", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")], ids=["connect", "read"]
    )
    async def test_transport_errors_are_retried(
        self, open_source, replay: Replay, clock: FakeClock, error: Exception
    ) -> None:
        def fail(request: httpx.Request) -> httpx.Response:
            raise error

        replay.fault(fail, when=resolves, times=2)
        async with open_source() as source:
            await source.resolve(**RESOLVE)
        assert clock.sleeps == list(SERVER_RETRY_DELAYS[:2])

    async def test_a_recovered_request_succeeds(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        replay.fault(lambda request: synthetic("server_error"), when=resolves, times=1)
        async with open_source() as source:
            repository = await source.resolve(**RESOLVE)
        assert repository.github_id > 0
        assert clock.sleeps == [SERVER_RETRY_DELAYS[0]]

    async def test_a_splittable_timeout_is_not_retried(self, open_source, replay: Replay) -> None:
        replay.fault(lambda request: synthetic("bad_gateway"), times=None)
        async with open_source() as source:
            with pytest.raises(RequestTooHeavy):
                await source.transport.graphql("query PullRequestBatch { x }", {}, splittable=True)
        assert len(replay.seen) == 1


class TestBudget:
    async def test_waits_until_the_reset_when_below_the_reserve(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        reset_at = clock.now() + timedelta(minutes=20)

        def low(body: dict) -> None:
            body["data"]["rateLimit"] = {
                "cost": 1,
                "remaining": 10,
                "resetAt": reset_at.isoformat().replace("+00:00", "Z"),
            }

        replay.modify(low, when=resolves)
        async with open_source() as source:
            await source.resolve(**RESOLVE)
            assert clock.sleeps == []
            await source.resolve(**RESOLVE)
        assert clock.sleeps == [20 * 60 + RESET_MARGIN]

    async def test_accounts_points_requests_and_rest_calls(
        self, open_source, replay: Replay
    ) -> None:
        path, params = compare_unit(replay)
        async with open_source() as source:
            await source.resolve(**RESOLVE)
            await source.transport.rest_get(path, params)
            usage = source.usage
        assert (usage.graphql_points, usage.graphql_requests, usage.rest_calls) == (1, 1, 1)

    async def test_rest_refusals_come_back_as_values(self, open_source, replay: Replay) -> None:
        path, params = compare_unit(replay)
        replay.fault(
            lambda request: httpx.Response(404, json={"message": "Not Found"}),
            when=lambda s: s.operation == "rest",
        )
        async with open_source() as source:
            result = await source.transport.rest_get(path, params)
        assert result == RestRefused(404)

    async def test_a_request_at_the_deadline_is_not_sent(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        async with open_source(deadline=clock.now()) as source:
            with pytest.raises(DeadlineReached):
                await source.resolve(**RESOLVE)
        assert replay.seen == []


async def test_one_request_in_flight(open_source, replay: Replay) -> None:
    in_flight = 0
    most = 0

    def wrap(handle):
        async def counting(request: httpx.Request) -> httpx.Response:
            nonlocal in_flight, most
            in_flight += 1
            most = max(most, in_flight)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            in_flight -= 1
            return handle(request)

        return counting

    replay.wrap = wrap
    async with open_source() as source:
        await asyncio.gather(*(source.resolve(**RESOLVE) for _ in range(4)))
    assert most == 1
    assert len(replay.seen) == 4
