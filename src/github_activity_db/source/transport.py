"""The one place the source layer sends requests to GitHub (redesign plan 5.3 step 4, 5.7).

Every request goes through ``Transport._send``, which keeps one request in flight, waits on the
budget, classifies each failure, waits on rate limits, and retries server errors. Callers see
only results and ``source.errors`` exceptions.

Failure classes:

- rate limited: GraphQL ``RATE_LIMITED`` or a secondary-limit message (HTTP 200 or 403); REST
  403/429 with ``retry-after``, ``x-ratelimit-remaining: 0``, or a secondary-limit message.
  Wait for ``retry-after``; else, when the primary limit is exhausted, until the reset; else 60 s,
  doubling while it repeats. Never counted as an attempt.
- timeout or resource limit: HTTP 502/504, or a GraphQL timeout or resource-limit error.
  ``RequestTooHeavy`` at once for a splittable request; retried like a server error otherwise.
- server or transport error: other 5xx, connection errors, client timeouts, malformed bodies.
  Retried after 2, 4, and 8 s (with jitter), then ``ServerError``.
- 401: ``Unauthorized``. 403 without a rate-limit signal: ``Forbidden``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Literal, TypeVar

from githubkit.exception import (
    RateLimitExceeded,
    RequestError,
    RequestFailed,
    RequestTimeout,
)
from pydantic import ValidationError

from .budget import BudgetPool, Usage
from .clock import Clock, SystemClock
from .errors import (
    DeadlineReached,
    Forbidden,
    RateLimitWaitTooLong,
    RequestTooHeavy,
    ServerError,
    Unauthorized,
    UnexpectedResponse,
)
from .limits import (
    GRAPHQL_RESERVE_POINTS,
    MAX_IN_FLIGHT,
    MAX_RATE_LIMIT_WAIT,
    RATE_LIMIT_FALLBACK_WAIT,
    RESET_MARGIN,
    REST_RESERVE_CALLS,
    SERVER_RETRY_DELAYS,
)
from .models import RateLimitReading

if TYPE_CHECKING:
    import httpx
    from githubkit import GitHub
    from githubkit.response import Response

# GitHub's wording for secondary limits ("You have exceeded a secondary rate limit") and for
# GraphQL requests stopped at its 10 s limit ("We couldn't respond to your request in time";
# "This may be the result of a timeout").
_SECONDARY_LIMIT = "secondary rate limit"
_TIMEOUT_MESSAGES = ("in time", "timeout", "timed out")
_RESOURCE_LIMIT_TYPES = frozenset({"MAX_NODE_LIMIT_EXCEEDED", "RESOURCE_LIMITS_EXCEEDED"})
_TIMEOUT_STATUSES = frozenset({502, 504})
# "Something went wrong while executing your query. Please include ... when reporting this issue."
_SERVER_FAULT = "something went wrong"


@dataclass(frozen=True)
class GraphQLError:
    """One entry of a GraphQL response's ``errors``."""

    type: str | None
    message: str
    path: tuple[str | int, ...]


@dataclass(frozen=True)
class GraphQLResult:
    """A GraphQL response GitHub answered: data and errors together (plan 5.7)."""

    data: Mapping[str, Any]
    errors: tuple[GraphQLError, ...]
    date: datetime

    def errors_under(self, *path: str | int) -> tuple[GraphQLError, ...]:
        """Errors whose path starts with ``path``."""
        size = len(path)
        return tuple(error for error in self.errors if error.path[:size] == path)


@dataclass(frozen=True)
class RestResult:
    """A REST response with a 2xx status."""

    body: Any
    date: datetime


@dataclass(frozen=True)
class RestRefused:
    """A REST request GitHub answered with 404 or 422."""

    status: int


@dataclass(frozen=True)
class _Failure:
    """A failed attempt the loop may wait on, retry, or turn into an exception."""

    kind: Literal["rate_limited", "timeout", "server"]
    reason: str
    # Rate limited only: seconds GitHub asks for; None means the doubling fallback.
    wait: float | None = None


ResultT = TypeVar("ResultT", GraphQLResult, RestResult)


class Transport:
    """Sends GraphQL and REST requests through one githubkit client.

    ``github`` must be opened with ``async with`` by the caller, and every task using this
    transport created inside that block: githubkit keeps the open HTTP client in a context
    variable, so a task created earlier builds a new client per request.
    """

    def __init__(
        self,
        github: GitHub[Any],
        *,
        clock: Clock | None = None,
        deadline: datetime | None = None,
    ) -> None:
        self._github = github
        self._clock: Clock = clock or SystemClock()
        self.deadline = deadline
        self.usage = Usage()
        self.graphql_budget = BudgetPool("graphql", GRAPHQL_RESERVE_POINTS)
        self.rest_budget = BudgetPool("rest", REST_RESERVE_CALLS)
        self._in_flight = asyncio.Semaphore(MAX_IN_FLIGHT)

    @property
    def clock(self) -> Clock:
        return self._clock

    # ----------------------------------------------------------------------------------------
    # Public
    # ----------------------------------------------------------------------------------------
    async def graphql(
        self, document: str, variables: Mapping[str, object], *, splittable: bool
    ) -> GraphQLResult:
        """Send a GraphQL query; path-level errors come back in the result."""
        payload = {"query": document, "variables": dict(variables)}

        async def call() -> Response[Any]:
            self.usage.graphql_requests += 1
            return await self._github.arequest("POST", "/graphql", json=payload)

        result = await self._send(call, self._read_graphql, self.graphql_budget, splittable)
        if isinstance(result, RestRefused):
            raise UnexpectedResponse(
                f"GraphQL endpoint answered {result.status}", status=result.status
            )
        return result

    async def rest_get(
        self, path: str, params: Mapping[str, str | int]
    ) -> RestResult | RestRefused:
        """GET a REST resource; 404 and 422 come back as ``RestRefused``."""

        async def call() -> Response[Any]:
            self.usage.rest_calls += 1
            return await self._github.arequest("GET", path, params=dict(params))

        return await self._send(call, self._read_rest, self.rest_budget, splittable=False)

    # ----------------------------------------------------------------------------------------
    # The loop
    # ----------------------------------------------------------------------------------------
    async def _send(
        self,
        call: Callable[[], Awaitable[Response[Any]]],
        read: Callable[[Response[Any]], ResultT | _Failure],
        budget: BudgetPool,
        splittable: bool,
    ) -> ResultT | RestRefused:
        retries = 0
        fallback_waits = 0
        while True:
            self._check_deadline()
            blocked = budget.blocked_until(self._clock.now())
            if blocked is not None:
                await self._wait((blocked - self._clock.now()).total_seconds() + RESET_MARGIN)
                continue
            async with self._in_flight:
                outcome = await self._attempt(call, read)
            if not isinstance(outcome, _Failure):
                return outcome
            if outcome.kind == "rate_limited":
                wait = outcome.wait
                if wait is None:
                    wait = RATE_LIMIT_FALLBACK_WAIT * 2**fallback_waits
                    fallback_waits += 1
                await self._wait(wait)
                continue
            if outcome.kind == "timeout" and splittable:
                raise RequestTooHeavy(outcome.reason)
            if retries == len(SERVER_RETRY_DELAYS):
                if outcome.kind == "timeout":
                    raise RequestTooHeavy(outcome.reason)
                raise ServerError(outcome.reason)
            await self._wait(self._clock.jitter(SERVER_RETRY_DELAYS[retries]))
            retries += 1

    async def _attempt(
        self,
        call: Callable[[], Awaitable[Response[Any]]],
        read: Callable[[Response[Any]], ResultT | _Failure],
    ) -> ResultT | RestRefused | _Failure:
        try:
            response = await call()
        except RateLimitExceeded as e:
            return self._rate_limited(e.response.headers, primary=False, reason=repr(e))
        except RequestFailed as e:
            return self._classify_status(e.response)
        except RequestTimeout as e:
            return _Failure("server", f"client timeout: {e!r}")
        except RequestError as e:
            return _Failure("server", f"transport error: {e!r}")
        return read(response)

    def _classify_status(self, response: Response[Any]) -> RestRefused | _Failure:
        status = response.status_code
        message = _body_message(response)
        if status == 401:
            raise Unauthorized(f"GitHub answered 401: {message}")
        if status in (403, 429):
            if status == 429 or _SECONDARY_LIMIT in message.lower():
                return self._rate_limited(
                    response.headers, primary=False, reason=f"HTTP {status}: {message}"
                )
            raise Forbidden(f"GitHub answered 403: {message}")
        if status in _TIMEOUT_STATUSES:
            return _Failure("timeout", f"HTTP {status}: {message}")
        if status >= 500:
            return _Failure("server", f"HTTP {status}: {message}")
        if status in (404, 422):
            return RestRefused(status)
        raise UnexpectedResponse(f"GitHub answered {status}: {message}", status=status)

    # ----------------------------------------------------------------------------------------
    # Reading successful responses
    # ----------------------------------------------------------------------------------------
    def _read_graphql(self, response: Response[Any]) -> GraphQLResult | _Failure:
        date = _response_date(response)
        try:
            body = response.json()
        except ValueError as e:
            return _Failure("server", f"GraphQL body is not JSON: {e}")
        if date is None or not isinstance(body, dict):
            return _Failure("server", "GraphQL response without a Date header or a JSON object")
        errors = tuple(_graphql_error(entry) for entry in body.get("errors") or ())
        limited = [
            error
            for error in errors
            if error.type == "RATE_LIMITED" or _SECONDARY_LIMIT in error.message.lower()
        ]
        if limited:
            primary = any(error.type == "RATE_LIMITED" for error in limited)
            return self._rate_limited(
                response.headers, primary=primary, reason=f"GraphQL: {limited[0].message}"
            )
        data = body.get("data")
        request_level = [error for error in errors if not error.path]
        if request_level or not isinstance(data, dict):
            failure = _request_level_failure(request_level or list(errors))
            if failure is not None:
                return failure
            detail = "; ".join(error.message for error in errors) or "no data"
            raise UnexpectedResponse(f"GraphQL request rejected: {detail}")
        self._record_graphql_budget(data.get("rateLimit"))
        return GraphQLResult(data=data, errors=errors, date=date)

    def _read_rest(self, response: Response[Any]) -> RestResult | _Failure:
        date = _response_date(response)
        try:
            body = response.json()
        except ValueError as e:
            return _Failure("server", f"REST body is not JSON: {e}")
        if date is None:
            return _Failure("server", "REST response without a Date header")
        self._record_rest_budget(response.headers)
        return RestResult(body=body, date=date)

    def _record_graphql_budget(self, reading: object) -> None:
        if reading is None:
            return
        try:
            parsed = RateLimitReading.model_validate(reading)
        except ValidationError as e:
            raise UnexpectedResponse(f"GraphQL rateLimit is malformed: {e}") from e
        self.usage.graphql_points += parsed.cost
        self.graphql_budget.record(parsed.remaining, parsed.reset_at.astimezone(UTC))

    def _record_rest_budget(self, headers: httpx.Headers) -> None:
        remaining = headers.get("x-ratelimit-remaining")
        reset = headers.get("x-ratelimit-reset")
        if remaining is None or reset is None:
            return
        self.rest_budget.record(int(remaining), datetime.fromtimestamp(int(reset), tz=UTC))

    # ----------------------------------------------------------------------------------------
    # Waiting
    # ----------------------------------------------------------------------------------------
    def _rate_limited(self, headers: httpx.Headers, *, primary: bool, reason: str) -> _Failure:
        """A rate-limit failure carrying how long GitHub asks to wait (plan 5.3 step 4)."""
        retry_after = headers.get("retry-after")
        if retry_after is not None:
            return _Failure("rate_limited", reason, float(int(retry_after)))
        if primary or headers.get("x-ratelimit-remaining") == "0":
            now = self._clock.now()
            reset = headers.get("x-ratelimit-reset")
            until = (
                datetime.fromtimestamp(int(reset), tz=UTC)
                if reset is not None
                else self.graphql_budget.latest_reset(now)
            )
            if until is not None:
                seconds = max((until - now).total_seconds(), 0.0)
                return _Failure("rate_limited", reason, seconds + RESET_MARGIN)
        return _Failure("rate_limited", reason)

    async def _wait(self, seconds: float) -> None:
        seconds = max(seconds, 0.0)
        if self.deadline is not None:
            if self._clock.now() + timedelta(seconds=seconds) > self.deadline:
                raise DeadlineReached(self.deadline)
        elif seconds > MAX_RATE_LIMIT_WAIT:
            raise RateLimitWaitTooLong(seconds, MAX_RATE_LIMIT_WAIT)
        await self._clock.sleep(seconds)

    def _check_deadline(self) -> None:
        if self.deadline is not None and self._clock.now() >= self.deadline:
            raise DeadlineReached(self.deadline)


def _graphql_error(entry: object) -> GraphQLError:
    if not isinstance(entry, dict):
        return GraphQLError(type=None, message=str(entry), path=())
    path = entry.get("path") or ()
    error_type = entry.get("type")
    return GraphQLError(
        type=error_type if isinstance(error_type, str) else None,
        message=str(entry.get("message", "")),
        path=tuple(part for part in path if isinstance(part, str | int)),
    )


def _request_level_failure(errors: list[GraphQLError]) -> _Failure | None:
    """A timeout or server failure among request-level errors; None for a rejected query."""
    for error in errors:
        message = error.message.lower()
        if error.type in _RESOURCE_LIMIT_TYPES or any(
            fragment in message for fragment in _TIMEOUT_MESSAGES
        ):
            return _Failure("timeout", f"GraphQL: {error.message}")
    for error in errors:
        if _SERVER_FAULT in error.message.lower():
            return _Failure("server", f"GraphQL: {error.message}")
    return None


def _response_date(response: Response[Any]) -> datetime | None:
    value = response.headers.get("date")
    if not isinstance(value, str):
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return parsed.astimezone(UTC)


def _body_message(response: Response[Any]) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict):
        message = body.get("message")
        if isinstance(message, str):
            return message
        errors = body.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            return str(errors[0].get("message", ""))
    return str(body)[:200]
