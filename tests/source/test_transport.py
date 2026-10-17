"""The transport's failure classes, waits, retries, budget, and accounting (plan 5.3 step 4)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any, cast

import httpx
import pytest
from githubkit.exception import (
    AuthCredentialError,
    AuthExpiredError,
    RequestFailed,
    SecondaryRateLimitExceeded,
)
from githubkit.response import Response

import github_activity_db.source as source_package
from github_activity_db.source import GitHubSource, documents
from github_activity_db.source.client import build_github
from github_activity_db.source.clock import SystemClock
from github_activity_db.source.errors import (
    ClientFailure,
    DeadlineReached,
    Forbidden,
    RateLimitWaitTooLong,
    RepositoryNotFound,
    RepositoryRequestFailed,
    RepositoryShapeError,
    RequestTooHeavy,
    ServerError,
    Unauthorized,
    UnexpectedResponse,
)
from github_activity_db.source.limits import (
    CONNECT_TIMEOUT,
    JITTER_FRACTION,
    MAX_RATE_LIMIT_WAIT,
    RATE_LIMIT_FALLBACK_WAIT,
    READ_TIMEOUT,
    RESET_MARGIN,
    SERVER_RETRY_DELAYS,
)
from github_activity_db.source.transport import RestRefused, Transport, _Failure
from tests.source.conftest import START, FakeClock
from tests.source.replay import RATE_LIMIT, Replay, Seen, synthetic

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


def iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def rate_limit(cost: int, remaining: int, reset_at: datetime) -> Callable[[dict[str, Any]], None]:
    """A ``modify`` change setting the response's ``rateLimit`` reading."""

    def change(body: dict[str, Any]) -> None:
        body["data"]["rateLimit"] = {"cost": cost, "remaining": remaining, "resetAt": iso(reset_at)}

    return change


def date_header(clock: FakeClock) -> dict[str, str]:
    return {"date": format_datetime(clock.now(), usegmt=True)}


def resolve_body(replay: Replay, **extra: Any) -> dict[str, Any]:
    """The recorded go-gdpr resolve answer as a response body, with ``extra`` added."""
    data = {"repository": replay.unit("resolve", **RESOLVE).data, "rateLimit": RATE_LIMIT}
    return {"data": data, **extra}


HUGE = "9" * 20
REST_DATE = {"date": "Sat, 17 Oct 2026 12:00:00 GMT"}


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

    async def test_graphql_primary_limit_without_a_reset_waits_for_the_last_reading(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        replay.modify(rate_limit(1, 4000, clock.now() + timedelta(seconds=300)), when=resolves)
        replay.fault(lambda request: synthetic("rate_limited"), when=resolves)
        async with open_source() as source:
            await source.resolve(**RESOLVE)
            await source.resolve(**RESOLVE)
        assert clock.sleeps == [300 + RESET_MARGIN]

    @pytest.mark.parametrize("status", [200, 403])
    async def test_rate_limited_type_waits_for_the_last_reading(
        self, open_source, replay: Replay, clock: FakeClock, status: int
    ) -> None:
        replay.modify(rate_limit(1, 4000, clock.now() + timedelta(seconds=300)), when=resolves)
        body = {"errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}]}
        replay.fault(
            lambda request: httpx.Response(status, json=body, headers=date_header(clock)),
            when=resolves,
        )
        async with open_source() as source:
            await source.resolve(**RESOLVE)
            await source.resolve(**RESOLVE)
        assert clock.sleeps == [300 + RESET_MARGIN]

    @pytest.mark.parametrize("status", [200, 403])
    async def test_a_secondary_limit_in_any_error_falls_back(
        self, open_source, replay: Replay, clock: FakeClock, status: int
    ) -> None:
        body = {
            "errors": [
                {"type": "FORBIDDEN", "message": "Resource not accessible"},
                {"message": "You have exceeded a secondary rate limit."},
            ]
        }
        replay.fault(
            lambda request: httpx.Response(status, json=body, headers=date_header(clock)),
            when=resolves,
        )
        async with open_source() as source:
            await source.resolve(**RESOLVE)
        assert clock.sleeps == [RATE_LIMIT_FALLBACK_WAIT]

    async def test_rest_primary_limit_without_a_reset_waits_for_the_last_reading(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        path, params = compare_unit(replay)
        unit = next(u for u in replay.units.values() if u.kind == "compare")
        headers = {
            **unit.headers,
            "x-ratelimit-remaining": "4000",
            "x-ratelimit-reset": reset_in(clock, 200),
        }
        replay.fault(
            lambda request: httpx.Response(200, json=unit.data, headers=headers),
            when=lambda s: s.operation == "rest",
        )
        replay.fault(
            lambda request: synthetic("primary_limit_rest"), when=lambda s: s.operation == "rest"
        )
        async with open_source() as source:
            await source.transport.rest_get(path, params)
            await source.transport.rest_get(path, params)
        assert clock.sleeps == [200 + RESET_MARGIN]

    async def test_a_success_resets_the_doubling(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        replay.fault(lambda request: synthetic("secondary_limit_graphql"), when=resolves)
        async with open_source() as source:
            await source.resolve(**RESOLVE)
            replay.fault(lambda request: synthetic("secondary_limit_graphql"), when=resolves)
            await source.resolve(**RESOLVE)
        assert clock.sleeps == [RATE_LIMIT_FALLBACK_WAIT, RATE_LIMIT_FALLBACK_WAIT]

    async def test_wrapped_rate_limit_waits_for_retry_after(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        def limited(request: httpx.Request) -> httpx.Response:
            response = httpx.Response(403, headers={"retry-after": "45"}, request=request)
            raise SecondaryRateLimitExceeded(Response(response, Any), timedelta(seconds=45))

        replay.fault(limited, when=resolves)
        async with open_source() as source:
            await source.resolve(**RESOLVE)
        assert clock.sleeps == [45.0]


class TestPersistentLimits:
    """GitHub: while a limit persists, "wait for an exponentially increasing amount of time
    between retries, and throw an error after a specific number of retries"."""

    async def test_a_repeated_limit_backs_off_from_retry_after(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        replay.fault(
            lambda request: synthetic("secondary_limit_rest", headers={"retry-after": "30"}),
            when=resolves,
            times=3,
        )
        async with open_source() as source:
            await source.resolve(**RESOLVE)
        assert clock.sleeps == [30.0, 2 * RATE_LIMIT_FALLBACK_WAIT, 4 * RATE_LIMIT_FALLBACK_WAIT]

    async def test_a_zero_retry_after_resends_once_then_backs_off(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        replay.fault(
            lambda request: synthetic("secondary_limit_rest", headers={"retry-after": "0"}),
            when=resolves,
            times=2,
        )
        async with open_source() as source:
            await source.resolve(**RESOLVE)
        assert clock.sleeps == [2 * RATE_LIMIT_FALLBACK_WAIT]
        assert sum(1 for seen in replay.seen if seen.operation == "Resolve") == 3

    async def test_a_limit_that_never_lifts_fails_the_run(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        # Eight limits: seven waits, and the eighth wait is too long. A transport that kept
        # waiting would get the recorded answer on the ninth request instead of looping.
        replay.fault(
            lambda request: synthetic("secondary_limit_rest", headers={"retry-after": "60"}),
            when=resolves,
            times=8,
        )
        async with open_source() as source:
            with pytest.raises(RateLimitWaitTooLong):
                await source.resolve(**RESOLVE)
        waits = [RATE_LIMIT_FALLBACK_WAIT * 2**repeat for repeat in range(7)]
        assert clock.sleeps == waits
        assert waits[-1] <= MAX_RATE_LIMIT_WAIT < 2 * waits[-1]

    async def test_a_limit_with_an_unreadable_reading_still_waits(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        body = {
            "data": {"rateLimit": {"cost": 1, "remaining": 0, "resetAt": None}},
            "errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}],
        }
        replay.fault(
            lambda request: httpx.Response(200, json=body, headers=date_header(clock)),
            when=resolves,
        )
        async with open_source() as source:
            await source.resolve(**RESOLVE)
        assert clock.sleeps == [RATE_LIMIT_FALLBACK_WAIT]

    async def test_a_primary_limit_without_any_reset_falls_back(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        replay.fault(lambda request: synthetic("rate_limited"), when=resolves)
        async with open_source() as source:
            await source.resolve(**RESOLVE)
        assert clock.sleeps == [RATE_LIMIT_FALLBACK_WAIT]

    def test_a_shorter_limit_keeps_the_longer_pause(self, clock: FakeClock) -> None:
        # Reachable only with more than one request in flight.
        transport = Transport(build_github("test-token"), clock=clock)
        transport._pause_for(_Failure("rate_limited", "first", 300.0))
        transport._pause_for(_Failure("rate_limited", "second", 30.0))
        assert transport._pause.until == clock.now() + timedelta(seconds=300)


class TestOutOfRangeValues:
    """Values GitHub's docs do not allow end in a classified failure, never another exception."""

    @pytest.mark.parametrize(
        "headers",
        [
            {"retry-after": HUGE},
            {"x-ratelimit-remaining": "0", "x-ratelimit-reset": HUGE},
            {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "100000000000000000"},
        ],
        ids=["retry-after", "reset", "reset-past-the-c-library"],
    )
    async def test_a_403_githubkit_cannot_read_falls_back(
        self, open_source, replay: Replay, clock: FakeClock, headers: dict[str, str]
    ) -> None:
        replay.fault(
            lambda request: synthetic("secondary_limit_rest", headers=headers), when=resolves
        )
        async with open_source() as source:
            await source.resolve(**RESOLVE)
        assert clock.sleeps == [RATE_LIMIT_FALLBACK_WAIT]

    async def test_a_403_with_a_null_message_falls_back(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        replay.fault(
            lambda request: httpx.Response(
                403,
                json={"message": None},
                headers={**date_header(clock), "x-ratelimit-remaining": "0"},
            ),
            when=resolves,
        )
        async with open_source() as source:
            await source.resolve(**RESOLVE)
        assert clock.sleeps == [RATE_LIMIT_FALLBACK_WAIT]

    async def test_a_retry_after_too_long_for_a_float_is_too_long(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        replay.fault(
            lambda request: synthetic(
                "secondary_limit_graphql", headers={"retry-after": "9" * 400}
            ),
            when=resolves,
        )
        async with open_source() as source:
            with pytest.raises(RateLimitWaitTooLong):
                await source.resolve(**RESOLVE)

    async def test_a_reset_centuries_away_meets_the_deadline(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        path, params = compare_unit(replay)
        unit = next(u for u in replay.units.values() if u.kind == "compare")
        headers = {
            **unit.headers,
            "x-ratelimit-remaining": "5",
            "x-ratelimit-reset": "253402300799",
        }
        replay.fault(
            lambda request: httpx.Response(200, json=unit.data, headers=headers),
            when=lambda s: s.operation == "rest",
        )
        async with open_source(deadline=clock.now() + timedelta(minutes=10)) as source:
            await source.transport.rest_get(path, params)
            with pytest.raises(DeadlineReached):
                await source.transport.rest_get(path, params)

    async def test_a_huge_retry_after_on_200_is_too_long(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        replay.fault(
            lambda request: synthetic("secondary_limit_graphql", headers={"retry-after": HUGE}),
            when=resolves,
        )
        async with open_source() as source:
            with pytest.raises(RateLimitWaitTooLong):
                await source.resolve(**RESOLVE)
        assert clock.sleeps == []


class TestPauseIsShared:
    async def test_a_queued_request_waits_out_the_pause(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        sent_at: list[datetime] = []

        def wrap(handle):
            async def timed(request: httpx.Request) -> httpx.Response:
                sent_at.append(clock.now())
                await asyncio.sleep(0)
                return handle(request)

            return timed

        replay.wrap = wrap
        replay.fault(
            lambda request: synthetic("secondary_limit_rest", headers={"retry-after": "30"}),
            when=resolves,
        )
        start = clock.now()
        async with open_source() as source:
            await asyncio.gather(source.resolve(**RESOLVE), source.resolve(**RESOLVE))
        assert len(sent_at) == 3
        assert sent_at[0] == start
        assert all(moment >= start + timedelta(seconds=30) for moment in sent_at[1:])


class TestMalformedHeaders:
    async def test_an_http_date_retry_after_on_403_falls_back(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        # githubkit parses retry-after on 403/429 with int() before the transport sees it.
        when = format_datetime(clock.now() + timedelta(seconds=45), usegmt=True)
        replay.fault(
            lambda request: synthetic("secondary_limit_rest", headers={"retry-after": when}),
            when=resolves,
        )
        async with open_source() as source:
            await source.resolve(**RESOLVE)
        assert clock.sleeps == [RATE_LIMIT_FALLBACK_WAIT]

    async def test_an_http_date_retry_after_on_200_is_honoured(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        when = format_datetime(clock.now() + timedelta(seconds=45), usegmt=True)
        replay.fault(
            lambda request: synthetic("secondary_limit_graphql", headers={"retry-after": when}),
            when=resolves,
        )
        async with open_source() as source:
            await source.resolve(**RESOLVE)
        assert clock.sleeps == [45.0]

    async def test_an_unreadable_retry_after_on_200_falls_back(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        replay.fault(
            lambda request: synthetic("secondary_limit_graphql", headers={"retry-after": "soon"}),
            when=resolves,
        )
        async with open_source() as source:
            await source.resolve(**RESOLVE)
        assert clock.sleeps == [RATE_LIMIT_FALLBACK_WAIT]

    async def test_an_unparseable_reset_on_403_falls_back(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        path, params = compare_unit(replay)
        replay.fault(
            lambda request: synthetic("primary_limit_rest", headers={"x-ratelimit-reset": "soon"}),
            when=lambda s: s.operation == "rest",
        )
        async with open_source() as source:
            await source.transport.rest_get(path, params)
        assert clock.sleeps == [RATE_LIMIT_FALLBACK_WAIT]

    @pytest.mark.parametrize(
        ("remaining", "reset"),
        [
            ("5.0", None),
            ("5", "soon"),
            ("", None),
            ("5", "999999999999"),
            ("5", "100000000000000000"),
            ("5", HUGE),
        ],
        ids=[
            "remaining-not-integer",
            "reset-not-integer",
            "remaining-empty",
            "reset-past-year-9999",
            "reset-past-the-c-library",
            "reset-past-a-c-long",
        ],
    )
    async def test_unparseable_rest_rate_headers_are_ignored(
        self,
        open_source,
        replay: Replay,
        clock: FakeClock,
        remaining: str,
        reset: str | None,
    ) -> None:
        path, params = compare_unit(replay)
        unit = next(u for u in replay.units.values() if u.kind == "compare")
        headers = {
            **unit.headers,
            "x-ratelimit-remaining": remaining,
            "x-ratelimit-reset": reset or reset_in(clock, 900),
        }
        replay.fault(
            lambda request: httpx.Response(200, json=unit.data, headers=headers),
            when=lambda s: s.operation == "rest",
        )
        async with open_source() as source:
            await source.transport.rest_get(path, params)
            await source.transport.rest_get(path, params)
            assert source.transport.rest_budget.blocked_until(clock.now()) is None
        assert clock.sleeps == []


class TestCredentials:
    """Failures raised inside the HTTP client, where githubkit's auth flows raise them."""

    @staticmethod
    def raising(error: Exception | int):
        def fail(request: httpx.Request) -> httpx.Response:
            if isinstance(error, int):
                raise RequestFailed(Response(httpx.Response(error, request=request), Any))
            raise error

        return fail

    @pytest.mark.parametrize(
        "error",
        [401, 404, AuthCredentialError("bad key"), AuthExpiredError("refresh token expired")],
        ids=["mint-401", "mint-404", "credential", "expired"],
    )
    async def test_a_credential_failure_is_unauthorized(
        self, open_source, replay: Replay, clock: FakeClock, error: Exception | int
    ) -> None:
        replay.fault(self.raising(error), when=resolves, times=None)
        async with open_source() as source:
            with pytest.raises(Unauthorized):
                await source.resolve(**RESOLVE)
        assert clock.sleeps == []
        assert len(replay.seen) == 1

    async def test_a_server_error_minting_a_token_is_retried(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        replay.fault(self.raising(502), when=resolves)
        async with open_source() as source:
            await source.resolve(**RESOLVE)
        assert clock.sleeps == [SERVER_RETRY_DELAYS[0]]

    @pytest.mark.parametrize(
        ("status", "message"),
        [
            (429, "Too many requests"),
            (403, "You have exceeded a secondary rate limit. Please wait."),
        ],
        ids=["429", "403-secondary"],
    )
    async def test_a_rate_limited_token_mint_waits(
        self, open_source, replay: Replay, clock: FakeClock, status: int, message: str
    ) -> None:
        # githubkit raises a rate limit only with retry-after or remaining 0; without them the
        # mint fails as a plain RequestFailed, which still carries the limit.
        def limited(request: httpx.Request) -> httpx.Response:
            response = httpx.Response(status, json={"message": message}, request=request)
            raise RequestFailed(Response(response, Any))

        replay.fault(limited, when=resolves)
        async with open_source() as source:
            await source.resolve(**RESOLVE)
        assert clock.sleeps == [RATE_LIMIT_FALLBACK_WAIT]

    async def test_a_token_mint_githubkit_cannot_read_waits(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        # The auth flow checks the mint's response with githubkit's own _check, as here.
        when = format_datetime(clock.now() + timedelta(seconds=45), usegmt=True)

        def limited(request: httpx.Request) -> httpx.Response:
            response = httpx.Response(
                403,
                json={"message": "You have exceeded a secondary rate limit."},
                headers={"retry-after": when},
                request=request,
            )
            return build_github("test-token")._check(response).raw_response

        replay.fault(limited, when=resolves)
        async with open_source() as source:
            await source.resolve(**RESOLVE)
        assert clock.sleeps == [RATE_LIMIT_FALLBACK_WAIT]

    async def test_a_token_mint_rate_limited_by_type_waits_for_the_last_reading(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        replay.modify(rate_limit(1, 4000, clock.now() + timedelta(seconds=300)), when=resolves)

        def limited(request: httpx.Request) -> httpx.Response:
            body = {"errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}]}
            raise RequestFailed(Response(httpx.Response(403, json=body, request=request), Any))

        replay.fault(limited, when=resolves)
        async with open_source() as source:
            await source.resolve(**RESOLVE)
            await source.resolve(**RESOLVE)
        assert clock.sleeps == [300 + RESET_MARGIN]

    @pytest.mark.parametrize(
        "error",
        [TypeError("not from githubkit's rate-limit readers"), ValueError("bad key data")],
        ids=["type-error", "value-error"],
    )
    async def test_a_defect_inside_the_client_is_fatal(
        self, open_source, replay: Replay, clock: FakeClock, error: Exception
    ) -> None:
        replay.fault(self.raising(error), when=resolves)
        async with open_source() as source:
            with pytest.raises(ClientFailure):
                await source.resolve(**RESOLVE)
        assert clock.sleeps == []

    async def test_a_client_failure_outside_the_network_is_fatal(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        replay.fault(
            self.raising(RuntimeError("JWT support for GitHub APP should be installed")),
            when=resolves,
        )
        async with open_source() as source:
            with pytest.raises(ClientFailure, match="JWT support"):
                await source.resolve(**RESOLVE)
        assert clock.sleeps == []
        assert len(replay.seen) == 1


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

    async def test_a_not_found_naming_a_timeout_is_not_a_timeout(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        # A NOT_FOUND message repeats the requested name, which may contain "timeout".
        message = "Could not resolve to a Repository with the name 'prebid/timeout-tools'."

        def missing(body: dict[str, Any]) -> None:
            body["data"]["repository"] = None
            body["errors"] = [{"type": "NOT_FOUND", "path": ["repository"], "message": message}]

        replay.modify(missing, when=resolves)
        async with open_source() as source:
            with pytest.raises(RepositoryNotFound):
                await source.resolve(**RESOLVE)
        assert clock.sleeps == []

    async def test_a_403_with_malformed_errors_is_forbidden(
        self, open_source, replay: Replay
    ) -> None:
        replay.fault(
            lambda request: httpx.Response(
                403, json={"message": "Resource not accessible by integration", "errors": 5}
            ),
            when=resolves,
        )
        async with open_source() as source:
            with pytest.raises(Forbidden, match="Resource not accessible"):
                await source.resolve(**RESOLVE)

    async def test_a_defect_in_the_request_path_is_not_a_rate_limit(self, clock: FakeClock) -> None:
        # A client whose arequest no longer accepts the transport's arguments: the TypeError is
        # not raised by githubkit's rate-limit readers, so it propagates instead of waiting.
        class Incompatible:
            async def arequest(self, *args: object, **kwargs: object) -> object:
                raise TypeError("arequest() got an unexpected keyword argument 'content'")

        transport = Transport(cast("Any", Incompatible()), clock=clock)
        with pytest.raises(TypeError, match="unexpected keyword"):
            await transport.graphql("query Resolve { x }", {}, splittable=False)
        assert clock.sleeps == []

    async def test_a_403_with_an_html_body_is_forbidden(self, open_source, replay: Replay) -> None:
        replay.fault(
            lambda request: httpx.Response(403, text="<html>blocked</html>"), when=resolves
        )
        async with open_source() as source:
            with pytest.raises(Forbidden, match="<html>blocked</html>"):
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
            (lambda request: synthetic("gateway_timeout"), RequestTooHeavy),
            (lambda request: synthetic("graphql_timeout"), RequestTooHeavy),
            (lambda request: synthetic("resource_limit"), RequestTooHeavy),
            (lambda request: synthetic("graphql_server_fault"), ServerError),
            (lambda request: synthetic("graphql_generic_timeout"), RequestTooHeavy),
            (lambda request: httpx.Response(200, text="<html>proxy</html>"), ServerError),
            (lambda request: httpx.Response(502, text="<html>bad gateway</html>"), RequestTooHeavy),
            (lambda request: httpx.Response(503, json=["unavailable"]), ServerError),
        ],
        ids=[
            "500",
            "502",
            "504",
            "graphql-timeout",
            "graphql-resource-limit",
            "graphql-server-fault",
            "graphql-generic-timeout",
            "not-json",
            "502-html",
            "503-json-list",
        ],
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

    @pytest.mark.parametrize(
        "name", ["bad_gateway", "gateway_timeout", "graphql_timeout", "resource_limit"]
    )
    async def test_a_splittable_timeout_is_not_retried(
        self, open_source, replay: Replay, clock: FakeClock, name: str
    ) -> None:
        replay.fault(lambda request: synthetic(name), times=None)
        async with open_source() as source:
            with pytest.raises(RequestTooHeavy):
                await source.transport.graphql("query PullRequestBatch { x }", {}, splittable=True)
        assert len(replay.seen) == 1
        assert clock.sleeps == []

    async def test_a_splittable_server_error_is_retried(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        replay.fault(lambda request: synthetic("server_error"), times=None)
        async with open_source() as source:
            with pytest.raises(ServerError):
                await source.transport.graphql("query PullRequestBatch { x }", {}, splittable=True)
        assert clock.sleeps == list(SERVER_RETRY_DELAYS)


class TestBudget:
    async def test_waits_until_the_reset_when_below_the_reserve(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        replay.modify(rate_limit(1, 10, clock.now() + timedelta(minutes=20)), when=resolves)
        async with open_source() as source:
            await source.resolve(**RESOLVE)
            assert clock.sleeps == []
            await source.resolve(**RESOLVE)
        assert clock.sleeps == [20 * 60 + RESET_MARGIN]

    async def test_rest_waits_until_the_reset_when_below_the_reserve(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        path, params = compare_unit(replay)
        reset = reset_in(clock, 900)

        def low(request: httpx.Request) -> httpx.Response:
            unit = next(u for u in replay.units.values() if u.kind == "compare")
            headers = {**unit.headers, "x-ratelimit-remaining": "5", "x-ratelimit-reset": reset}
            return httpx.Response(200, json=unit.data, headers=headers)

        replay.fault(low, when=lambda s: s.operation == "rest")
        async with open_source() as source:
            await source.transport.rest_get(path, params)
            assert clock.sleeps == []
            await source.transport.rest_get(path, params)
        assert clock.sleeps == [900 + RESET_MARGIN]

    async def test_accounts_points_requests_and_rest_calls(
        self, open_source, replay: Replay
    ) -> None:
        path, params = compare_unit(replay)
        async with open_source() as source:
            await source.resolve(**RESOLVE)
            await source.transport.rest_get(path, params)
            usage = source.usage
        assert (usage.graphql_points, usage.graphql_requests, usage.rest_calls) == (1, 1, 1)

    @pytest.mark.parametrize(
        "respond",
        [
            lambda request: httpx.Response(200, text="<html>proxy</html>", headers=REST_DATE),
            lambda request: httpx.Response(200, json={"total_commits": 0}),
        ],
        ids=["not-json", "no-date"],
    )
    async def test_a_malformed_rest_response_is_retried(
        self, open_source, replay: Replay, clock: FakeClock, respond
    ) -> None:
        path, params = compare_unit(replay)
        replay.fault(respond, when=lambda s: s.operation == "rest")
        async with open_source() as source:
            result = await source.transport.rest_get(path, params)
        assert not isinstance(result, RestRefused)
        assert clock.sleeps == [SERVER_RETRY_DELAYS[0]]

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

    async def test_a_request_queued_past_the_deadline_is_not_sent(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        def wrap(handle):
            async def slow(request: httpx.Request) -> httpx.Response:
                await asyncio.sleep(0)  # the second request queues on the slot meanwhile
                await clock.sleep(25)  # and the first runs past the deadline
                return handle(request)

            return slow

        replay.wrap = wrap
        async with open_source(deadline=clock.now() + timedelta(seconds=20)) as source:
            results: list[object] = await asyncio.gather(
                source.resolve(**RESOLVE), source.resolve(**RESOLVE), return_exceptions=True
            )
        first, second = results
        assert not isinstance(first, BaseException)
        assert isinstance(second, DeadlineReached)
        assert len(replay.seen) == 1

    async def test_a_retry_that_ends_before_the_deadline_is_waited(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        replay.fault(lambda request: synthetic("server_error"), when=resolves)
        async with open_source(deadline=clock.now() + timedelta(minutes=10)) as source:
            await source.resolve(**RESOLVE)
        assert clock.sleeps == [SERVER_RETRY_DELAYS[0]]

    async def test_points_are_the_sum_of_reported_costs(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        reset_at = clock.now() + timedelta(hours=1)
        replay.modify(rate_limit(7, 4000, reset_at), when=resolves)
        replay.modify(rate_limit(3, 3993, reset_at), when=resolves)
        async with open_source() as source:
            await source.resolve(**RESOLVE)
            await source.resolve(**RESOLVE)
            usage = source.usage
        assert (usage.graphql_points, usage.graphql_requests) == (10, 2)


class TestGraphQLReading:
    async def test_a_response_without_rate_limit_is_unexpected(
        self, open_source, replay: Replay
    ) -> None:
        replay.modify(lambda body: body["data"].pop("rateLimit"), when=resolves)
        async with open_source() as source:
            with pytest.raises(UnexpectedResponse, match="no rateLimit"):
                await source.resolve(**RESOLVE)

    @pytest.mark.parametrize(
        "reading",
        [
            {"cost": "1", "remaining": 4000, "resetAt": "2099-01-01T00:00:00Z"},
            {"cost": 1, "remaining": 4000, "resetAt": "2099-01-01"},
            {"cost": 1, "remaining": 4000},
        ],
        ids=["cost-string", "reset-without-time", "reset-missing"],
    )
    async def test_a_malformed_rate_limit_is_unexpected(
        self, open_source, replay: Replay, reading: dict[str, Any]
    ) -> None:
        def change(body: dict[str, Any]) -> None:
            body["data"]["rateLimit"] = reading

        replay.modify(change, when=resolves)
        async with open_source() as source:
            with pytest.raises(UnexpectedResponse, match="rateLimit is malformed"):
                await source.resolve(**RESOLVE)

    async def test_a_response_without_a_date_is_retried(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        replay.fault(lambda request: httpx.Response(200, json=resolve_body(replay)), when=resolves)
        async with open_source() as source:
            await source.resolve(**RESOLVE)
        assert clock.sleeps == [SERVER_RETRY_DELAYS[0]]

    async def test_an_unreadable_date_is_retried(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        replay.fault(
            lambda request: httpx.Response(
                200, json=resolve_body(replay), headers={"date": "yesterday"}
            ),
            when=resolves,
        )
        async with open_source() as source:
            await source.resolve(**RESOLVE)
        assert clock.sleeps == [SERVER_RETRY_DELAYS[0]]

    @pytest.mark.parametrize(
        "body",
        [{}, {"data": None}, {"data": []}, [], {"data": {}, "errors": 5}],
        ids=["empty", "null-data", "list-data", "list-body", "errors-not-a-list"],
    )
    async def test_a_malformed_body_is_retried(
        self, open_source, replay: Replay, clock: FakeClock, body: Any
    ) -> None:
        replay.fault(
            lambda request: httpx.Response(200, json=body, headers=date_header(clock)),
            when=resolves,
        )
        async with open_source() as source:
            await source.resolve(**RESOLVE)
        assert clock.sleeps == [SERVER_RETRY_DELAYS[0]]

    @pytest.mark.parametrize(
        "entry", ["boom", {"message": "boom", "path": 5}], ids=["string", "path"]
    )
    async def test_a_malformed_error_entry_is_a_rejection(
        self, open_source, replay: Replay, clock: FakeClock, entry: Any
    ) -> None:
        body = resolve_body(replay, errors=[entry])
        replay.fault(
            lambda request: httpx.Response(200, json=body, headers=date_header(clock)),
            when=resolves,
        )
        async with open_source() as source:
            with pytest.raises(UnexpectedResponse, match="rejected: boom"):
                await source.resolve(**RESOLVE)

    @pytest.mark.parametrize("status", [404, 422])
    async def test_the_graphql_endpoint_refusing_is_unexpected(
        self, open_source, replay: Replay, status: int
    ) -> None:
        replay.fault(
            lambda request: httpx.Response(status, json={"message": "refused"}), when=resolves
        )
        async with open_source() as source:
            with pytest.raises(UnexpectedResponse) as raised:
                await source.resolve(**RESOLVE)
        assert raised.value.status == status

    @pytest.mark.parametrize(
        ("path", "message", "cause"),
        [
            ([], "We couldn't respond to your request in time.", RequestTooHeavy),
            (["repository"], "Resource limits for this query exceeded.", RequestTooHeavy),
            (["repository"], "Something went wrong while executing your query.", ServerError),
        ],
        ids=["pathless-timeout-with-data", "resource-limit-at-a-path", "server-fault-at-a-path"],
    )
    async def test_an_error_about_the_request_fails_it_at_any_path(
        self,
        open_source,
        replay: Replay,
        clock: FakeClock,
        path: list[str],
        message: str,
        cause: type[Exception],
    ) -> None:
        # GitHub documents partial results "along with an error indicating that resource limits
        # were exceeded"; the paths here are constructed.
        body = resolve_body(replay, errors=[{"message": message, "path": path}])
        replay.fault(
            lambda request: httpx.Response(200, json=body, headers=date_header(clock)),
            when=resolves,
            times=None,
        )
        async with open_source() as source:
            with pytest.raises(RepositoryRequestFailed) as raised:
                await source.resolve(**RESOLVE)
        assert isinstance(raised.value.cause, cause)
        assert clock.sleeps == list(SERVER_RETRY_DELAYS)

    async def test_points_in_a_failed_response_are_counted(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        reading = {**RATE_LIMIT, "cost": 3}
        body = {
            "data": {"repository": None, "rateLimit": reading},
            "errors": [{"message": "We couldn't respond to your request in time.", "path": []}],
        }
        replay.fault(
            lambda request: httpx.Response(200, json=body, headers=date_header(clock)),
            when=resolves,
        )
        async with open_source() as source:
            await source.resolve(**RESOLVE)
            usage = source.usage
        assert (usage.graphql_points, usage.graphql_requests) == (3 + 1, 2)

    async def test_unencodable_variables_raise_before_any_request(
        self, open_source, replay: Replay
    ) -> None:
        async with open_source() as source:
            with pytest.raises(ValueError, match="JSON"):
                await source.transport.graphql(
                    "query Resolve { x }", {"n": float("nan")}, splittable=False
                )
            usage = source.usage
        assert replay.seen == []
        assert usage.graphql_requests == 0


def test_the_client_leaves_retries_caching_and_timeouts_to_the_transport() -> None:
    config = build_github("test-token").config
    assert config.auto_retry is None
    assert config.http_cache is False
    assert config.timeout == httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT)


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


def test_the_system_clock_jitters_within_its_fraction() -> None:
    clock = SystemClock()
    samples = [clock.jitter(8.0) for _ in range(200)]
    assert all(8.0 <= sample <= 8.0 * (1 + JITTER_FRACTION) for sample in samples)
    assert len(set(samples)) > 1


async def test_retry_delays_go_through_the_clock_jitter(replay: Replay) -> None:
    class Jittery(FakeClock):
        def jitter(self, delay: float) -> float:
            return delay + 0.5

    clock = Jittery()
    replay.fault(lambda request: synthetic("server_error"), when=resolves, times=3)
    async with GitHubSource("test-token", http_transport=replay.transport(), clock=clock) as source:
        await source.resolve(**RESOLVE)
    assert clock.sleeps == [delay + 0.5 for delay in SERVER_RETRY_DELAYS]


class TestHttpDates:
    """HTTP-dates are GMT (RFC 9110), whatever the host's time zone."""

    @pytest.fixture
    def tokyo(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        monkeypatch.setenv("TZ", "Asia/Tokyo")
        time.tzset()
        yield
        monkeypatch.undo()
        time.tzset()

    async def test_a_date_without_a_zone_is_gmt(
        self, open_source, replay: Replay, clock: FakeClock, tokyo: None
    ) -> None:
        replay.fault(
            lambda request: httpx.Response(
                200, json=resolve_body(replay), headers={"date": "Sat Oct 17 12:00:00 2026"}
            ),
            when=resolves,
        )
        async with open_source() as source:
            result = await source.transport.graphql(documents.RESOLVE, RESOLVE, splittable=False)
        assert result.date == datetime(2026, 10, 17, 12, 0, tzinfo=UTC)

    async def test_a_retry_after_date_without_a_zone_is_gmt(
        self, open_source, replay: Replay, clock: FakeClock, tokyo: None
    ) -> None:
        when = (clock.now() + timedelta(seconds=45)).strftime("%a, %d %b %Y %H:%M:%S -0000")
        replay.fault(
            lambda request: synthetic("secondary_limit_graphql", headers={"retry-after": when}),
            when=resolves,
        )
        async with open_source() as source:
            await source.resolve(**RESOLVE)
        assert clock.sleeps == [45.0]

    @pytest.mark.parametrize(
        "headers",
        [
            {"date": "Sat, 17 Oct 99999999999999999999 12:00:00 GMT"},
            {"date": "Fri, 31 Dec 9999 23:59:59 -2359"},
        ],
        ids=["huge-year", "past-year-9999-in-utc"],
    )
    async def test_an_unreadable_date_is_retried(
        self, open_source, replay: Replay, clock: FakeClock, headers: dict[str, str]
    ) -> None:
        replay.fault(
            lambda request: httpx.Response(200, json=resolve_body(replay), headers=headers),
            when=resolves,
        )
        async with open_source() as source:
            await source.resolve(**RESOLVE)
        assert clock.sleeps == [SERVER_RETRY_DELAYS[0]]

    async def test_an_unreadable_retry_after_date_falls_back(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        replay.fault(
            lambda request: synthetic(
                "secondary_limit_graphql",
                headers={"retry-after": "Fri, 31 Dec 9999 23:59:59 -2359"},
            ),
            when=resolves,
        )
        async with open_source() as source:
            await source.resolve(**RESOLVE)
        assert clock.sleeps == [RATE_LIMIT_FALLBACK_WAIT]

    async def test_a_reset_past_year_9999_is_a_malformed_reading(
        self, open_source, replay: Replay
    ) -> None:
        def change(body: dict[str, Any]) -> None:
            body["data"]["rateLimit"] = {**RATE_LIMIT, "resetAt": "9999-12-31T23:59:59-23:59"}

        replay.modify(change, when=resolves)
        async with open_source() as source:
            with pytest.raises(UnexpectedResponse, match="rateLimit is malformed"):
                await source.resolve(**RESOLVE)


class TestReadingOrder:
    """A rate limit is read before the Date header, and points before either."""

    async def test_a_rate_limit_without_a_date_still_waits(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        body = {"data": None, "errors": [{"message": "You have exceeded a secondary rate limit."}]}
        replay.fault(lambda request: httpx.Response(200, json=body), when=resolves)
        async with open_source() as source:
            await source.resolve(**RESOLVE)
        assert clock.sleeps == [RATE_LIMIT_FALLBACK_WAIT]

    async def test_points_in_a_response_without_a_date_are_counted(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        reading = {**RATE_LIMIT, "cost": 5}
        body = {"data": {**resolve_body(replay)["data"], "rateLimit": reading}}
        replay.fault(lambda request: httpx.Response(200, json=body), when=resolves)
        async with open_source() as source:
            await source.resolve(**RESOLVE)
            usage = source.usage
        assert (usage.graphql_points, usage.graphql_requests) == (5 + 1, 2)

    async def test_a_limit_without_a_reset_waits_for_the_replayed_window(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        # Every replayed response reads a window resetting an hour after the clock's start.
        replay.fault(lambda request: synthetic("rate_limited"), when=resolves)
        async with open_source() as source:
            await source.resolve(**RESOLVE)
            replay.fault(lambda request: synthetic("rate_limited"), when=resolves)
            await source.resolve(**RESOLVE)
        reset = datetime.fromisoformat(str(RATE_LIMIT["resetAt"]))
        assert clock.sleeps == [
            RATE_LIMIT_FALLBACK_WAIT,
            (reset - (START + timedelta(seconds=RATE_LIMIT_FALLBACK_WAIT))).total_seconds()
            + RESET_MARGIN,
        ]

    async def test_alternating_windows_gate_through_the_transport(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        late = clock.now() + timedelta(minutes=50)
        soon = clock.now() + timedelta(minutes=10)
        replay.modify(rate_limit(1, 4000, late), when=resolves)
        replay.modify(rate_limit(1, 50, soon), when=resolves)
        async with open_source() as source:
            await source.resolve(**RESOLVE)
            await source.resolve(**RESOLVE)
            assert clock.sleeps == []
            await source.resolve(**RESOLVE)
        assert clock.sleeps == [10 * 60 + RESET_MARGIN]


class TestInputGuards:
    """Odd values in an answer are read as absent, never raised."""

    async def test_a_403_with_a_null_message_and_no_limit_is_forbidden(
        self, open_source, replay: Replay
    ) -> None:
        replay.fault(
            lambda request: httpx.Response(
                403, json={"message": None}, headers={"x-ratelimit-remaining": "4000"}
            ),
            when=resolves,
        )
        async with open_source() as source:
            with pytest.raises(Forbidden, match="no message"):
                await source.resolve(**RESOLVE)

    async def test_a_403_with_error_entries_that_are_not_objects_is_forbidden(
        self, open_source, replay: Replay
    ) -> None:
        replay.fault(
            lambda request: httpx.Response(403, json={"message": "denied", "errors": ["x", 1]}),
            when=resolves,
        )
        async with open_source() as source:
            with pytest.raises(Forbidden, match="denied"):
                await source.resolve(**RESOLVE)

    @pytest.mark.parametrize(
        "error",
        [
            {"type": ["NOT_FOUND"], "message": "m", "path": ["repository"]},
            {"type": "SERVICE_UNAVAILABLE", "message": "m", "path": ["repository", None]},
        ],
        ids=["type-not-a-string", "path-part-null"],
    )
    async def test_odd_error_fields_are_read_as_absent(
        self, open_source, replay: Replay, error: dict[str, Any]
    ) -> None:
        # The error still sits under the repository, which fails it; nothing raises TypeError.
        body = resolve_body(replay, errors=[error])
        replay.fault(
            lambda request: httpx.Response(200, json=body, headers=REST_DATE), when=resolves
        )
        async with open_source() as source:
            with pytest.raises(RepositoryShapeError, match="errors under repository"):
                await source.resolve(**RESOLVE)

    async def test_a_reset_in_the_past_waits_only_the_margin(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        replay.fault(
            lambda request: synthetic(
                "rate_limited", headers={"x-ratelimit-reset": reset_in(clock, -100)}
            ),
            when=resolves,
        )
        async with open_source() as source:
            await source.resolve(**RESOLVE)
        assert clock.sleeps == [RESET_MARGIN]

    async def test_a_negative_retry_after_resends_at_once(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        replay.fault(
            lambda request: synthetic("secondary_limit_graphql", headers={"retry-after": "-5"}),
            when=resolves,
        )
        async with open_source() as source:
            await source.resolve(**RESOLVE)
        assert clock.sleeps == []
        assert sum(1 for seen in replay.seen if seen.operation == "Resolve") == 2


@pytest.mark.parametrize(
    ("error", "retried"),
    [
        (httpx.RemoteProtocolError("server hung up"), True),
        (httpx.DecodingError("bad gzip"), True),
        (httpx.TooManyRedirects("loop"), False),
        (httpx.UnsupportedProtocol("ftp"), False),
        (httpx.LocalProtocolError("bad header"), False),
        (httpx.ProxyError("proxy down"), False),
    ],
    ids=[
        "remote-protocol",
        "decoding",
        "redirect-loop",
        "unsupported-protocol",
        "local-protocol",
        "proxy",
    ],
)
async def test_only_network_side_client_errors_are_retried(
    open_source, replay: Replay, clock: FakeClock, error: Exception, retried: bool
) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise error

    replay.fault(fail, when=resolves)
    async with open_source() as source:
        if retried:
            await source.resolve(**RESOLVE)
        else:
            with pytest.raises(ClientFailure):
                await source.resolve(**RESOLVE)
    assert clock.sleeps == ([SERVER_RETRY_DELAYS[0]] if retried else [])


async def test_a_transport_failure_is_caught_by_the_packages_error(
    open_source, replay: Replay
) -> None:
    # Callers of GitHubSource.transport catch it through the exported base class.
    replay.fault(lambda request: synthetic("server_error"), times=None)
    async with open_source() as source:
        with pytest.raises(source_package.RequestFailedError):
            await source.transport.graphql(documents.RESOLVE, RESOLVE, splittable=False)
