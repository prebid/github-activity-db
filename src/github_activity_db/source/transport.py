"""The one module in ``source`` that sends requests to GitHub (redesign plan 5.3 step 4, 5.7).

Every request goes through ``Transport._send``, which keeps one request in flight, waits on the
budget and on any rate-limit pause, classifies each failure, and retries server errors. Callers
see only results and ``source.errors`` exceptions.

Failure classes:

- rate limited: GraphQL ``RATE_LIMITED`` or a secondary-limit message (HTTP 200 or 403); REST
  403/429 with ``retry-after``, ``x-ratelimit-remaining: 0``, or a secondary-limit message.
  Wait for ``retry-after``; else, when the primary limit is exhausted, until the reset; else 60 s,
  doubling while it repeats. The pause holds every request on this transport and is never
  counted as an attempt.
- timeout or resource limit: HTTP 502/504, or a GraphQL timeout or resource-limit error.
  ``RequestTooHeavy`` at once for a splittable request; retried like a server error otherwise.
- server or transport error: other 5xx, connection errors, client timeouts, malformed bodies.
  Retried after 2, 4, and 8 s (with jitter), then ``ServerError``.
- 401, a failed credential request, or 403 without a rate-limit signal: ``Unauthorized`` or
  ``Forbidden``, both fatal.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Literal, TypeVar

from githubkit.exception import (
    AuthCredentialError,
    AuthExpiredError,
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


@dataclass
class _Pause:
    """The rate-limit pause every request on the transport honours."""

    until: datetime | None = None
    fallback_waits: int = 0


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
        self.graphql_budget = BudgetPool(GRAPHQL_RESERVE_POINTS)
        self.rest_budget = BudgetPool(REST_RESERVE_CALLS)
        self._in_flight = asyncio.Semaphore(MAX_IN_FLIGHT)
        self._pause = _Pause()

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
        while True:
            self._check_deadline()
            async with self._in_flight:
                # Checked while holding the slot, so a request queued behind one that hit a
                # rate limit sees the pause it set.
                hold = self._hold(budget)
                if hold is None:
                    outcome = await self._attempt(call, read, budget)
            if hold is not None:
                await self._wait(hold)
                continue
            if not isinstance(outcome, _Failure):
                self._pause.fallback_waits = 0
                return outcome
            if outcome.kind == "rate_limited":
                self._pause_for(outcome)
                continue
            if outcome.kind == "timeout" and splittable:
                raise RequestTooHeavy(outcome.reason)
            if retries == len(SERVER_RETRY_DELAYS):
                if outcome.kind == "timeout":
                    raise RequestTooHeavy(outcome.reason)
                raise ServerError(outcome.reason)
            await self._wait(self._clock.jitter(SERVER_RETRY_DELAYS[retries]))
            retries += 1

    def _hold(self, budget: BudgetPool) -> float | None:
        """Seconds to wait before the next request, or None when it may go now."""
        now = self._clock.now()
        if self._pause.until is not None and self._pause.until > now:
            return (self._pause.until - now).total_seconds()
        blocked = budget.blocked_until(now)
        if blocked is not None:
            return (blocked - now).total_seconds() + RESET_MARGIN
        return None

    def _pause_for(self, failure: _Failure) -> None:
        """Hold every request until the rate limit is expected to have lifted."""
        wait = failure.wait
        if wait is None:
            wait = RATE_LIMIT_FALLBACK_WAIT * 2**self._pause.fallback_waits
            self._pause.fallback_waits += 1
        until = self._clock.now() + timedelta(seconds=wait)
        if self._pause.until is None or until > self._pause.until:
            self._pause.until = until

    async def _attempt(
        self,
        call: Callable[[], Awaitable[Response[Any]]],
        read: Callable[[Response[Any]], ResultT | _Failure],
        budget: BudgetPool,
    ) -> ResultT | RestRefused | _Failure:
        try:
            response = await call()
        except RateLimitExceeded as e:
            return self._rate_limited(e.response.headers, budget, primary=False, reason=repr(e))
        except RequestFailed as e:
            return self._classify_status(e.response, budget)
        except RequestTimeout as e:
            return _Failure("server", f"client timeout: {e!r}")
        except RequestError as e:
            return self._classify_wrapped(e, budget)
        except ValueError as e:
            # githubkit parses retry-after with int() inside arequest; a value GitHub's docs
            # do not allow (an HTTP date) surfaces here.
            return _Failure("rate_limited", f"unparseable rate-limit header: {e}")
        return read(response)

    def _classify_wrapped(self, error: RequestError[Any], budget: BudgetPool) -> _Failure:
        """A failure githubkit wrapped: raised inside the HTTP client, such as the auth flow.

        An installation-token mint runs inside the client and can fail with its own response,
        so a credential failure arrives here wrapped rather than as a status.
        """
        inner = error.exc
        if isinstance(inner, RateLimitExceeded):
            return self._rate_limited(
                inner.response.headers, budget, primary=False, reason=repr(inner)
            )
        if isinstance(inner, RequestFailed) and inner.response.status_code < 500:
            raise Unauthorized(f"credential request failed: {inner!r}")
        if isinstance(inner, AuthCredentialError | AuthExpiredError):
            raise Unauthorized(f"credentials rejected: {inner!r}")
        return _Failure("server", f"transport error: {error!r}")

    def _classify_status(
        self, response: Response[Any], budget: BudgetPool
    ) -> RestRefused | _Failure:
        status = response.status_code
        body = _ErrorBody.read(response)
        if status == 401:
            raise Unauthorized(f"GitHub answered 401: {body.summary}")
        if status in (403, 429):
            if status == 429 or body.mentions_secondary_limit or body.rate_limited_type:
                return self._rate_limited(
                    response.headers,
                    budget,
                    primary=body.rate_limited_type,
                    reason=f"HTTP {status}: {body.summary}",
                )
            raise Forbidden(f"GitHub answered 403: {body.summary}")
        if status in _TIMEOUT_STATUSES:
            return _Failure("timeout", f"HTTP {status}: {body.summary}")
        if status >= 500:
            return _Failure("server", f"HTTP {status}: {body.summary}")
        if status in (404, 422):
            return RestRefused(status)
        raise UnexpectedResponse(f"GitHub answered {status}: {body.summary}", status=status)

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
                response.headers,
                self.graphql_budget,
                primary=primary,
                reason=f"GraphQL: {limited[0].message}",
            )
        data = body.get("data")
        request_level = [error for error in errors if not error.path]
        if request_level or not isinstance(data, dict):
            failure = _request_level_failure(request_level or list(errors))
            if failure is not None:
                return failure
            detail = "; ".join(error.message for error in errors) or "no data"
            raise UnexpectedResponse(f"GraphQL request rejected: {detail}")
        reading = data.get("rateLimit")
        if reading is None:
            # Every document selects rateLimit; without it points go uncounted (plan 5.3 step 4).
            raise UnexpectedResponse("GraphQL response has no rateLimit")
        self._record_graphql_budget(reading)
        return GraphQLResult(data=data, errors=errors, date=date)

    def _read_rest(self, response: Response[Any]) -> RestResult | _Failure:
        date = _response_date(response)
        try:
            body = response.json()
        except ValueError as e:
            return _Failure("server", f"REST body is not JSON: {e}")
        if date is None:
            return _Failure("server", "REST response without a Date header")
        remaining = _int_header(response.headers, "x-ratelimit-remaining")
        reset = _reset_header(response.headers)
        if remaining is not None and reset is not None:
            self.rest_budget.record(remaining, reset)
        return RestResult(body=body, date=date)

    def _record_graphql_budget(self, reading: object) -> None:
        try:
            parsed = RateLimitReading.model_validate(reading)
        except ValidationError as e:
            raise UnexpectedResponse(f"GraphQL rateLimit is malformed: {e}") from e
        self.usage.graphql_points += parsed.cost
        self.graphql_budget.record(parsed.remaining, parsed.reset_at.astimezone(UTC))

    # ----------------------------------------------------------------------------------------
    # Waiting
    # ----------------------------------------------------------------------------------------
    def _rate_limited(
        self, headers: httpx.Headers, budget: BudgetPool, *, primary: bool, reason: str
    ) -> _Failure:
        """A rate-limit failure carrying how long GitHub asks to wait (plan 5.3 step 4)."""
        now = self._clock.now()
        retry_after = _retry_after(headers, now)
        if retry_after is not None:
            return _Failure("rate_limited", reason, retry_after)
        if primary or _int_header(headers, "x-ratelimit-remaining") == 0:
            until = _reset_header(headers) or budget.latest_reset(now)
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


@dataclass(frozen=True)
class _ErrorBody:
    """What an error response's JSON body says, read once."""

    messages: tuple[str, ...]
    rate_limited_type: bool

    @classmethod
    def read(cls, response: Response[Any]) -> _ErrorBody:
        try:
            body = response.json()
        except ValueError:
            return cls(messages=(response.text[:200],), rate_limited_type=False)
        if not isinstance(body, dict):
            return cls(messages=(str(body)[:200],), rate_limited_type=False)
        messages = [body["message"]] if isinstance(body.get("message"), str) else []
        errors = [entry for entry in body.get("errors") or () if isinstance(entry, dict)]
        messages.extend(str(entry.get("message", "")) for entry in errors)
        rate_limited = any(entry.get("type") == "RATE_LIMITED" for entry in errors)
        return cls(messages=tuple(messages), rate_limited_type=rate_limited)

    @property
    def mentions_secondary_limit(self) -> bool:
        return any(_SECONDARY_LIMIT in message.lower() for message in self.messages)

    @property
    def summary(self) -> str:
        return "; ".join(self.messages) or "no message"


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


def _int_header(headers: httpx.Headers, name: str) -> int | None:
    value = headers.get(name)
    if not isinstance(value, str):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _reset_header(headers: httpx.Headers) -> datetime | None:
    reset = _int_header(headers, "x-ratelimit-reset")
    return datetime.fromtimestamp(reset, tz=UTC) if reset is not None else None


def _retry_after(headers: httpx.Headers, now: datetime) -> float | None:
    """``retry-after`` in seconds; GitHub sends seconds, HTTP also allows a date."""
    value = headers.get("retry-after")
    if not isinstance(value, str):
        return None
    seconds = _int_header(headers, "retry-after")
    if seconds is not None:
        return float(max(seconds, 0))
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max((when.astimezone(UTC) - now).total_seconds(), 0.0)


def _response_date(response: Response[Any]) -> datetime | None:
    value = response.headers.get("date")
    if not isinstance(value, str):
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return parsed.astimezone(UTC)
