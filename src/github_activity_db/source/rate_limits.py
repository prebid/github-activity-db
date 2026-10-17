"""Rate-limit readings for ``ghactivity github rate-limit`` (redesign plan 5.7, 5.8)."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import ValidationError

from .documents import RATE_LIMIT_QUERY
from .errors import UnexpectedResponse
from .models import RateLimitReading, RestRateLimits
from .records import RateLimits, RestRateLimit
from .transport import RestRefused, Transport


async def read_rate_limits(transport: Transport) -> RateLimits:
    """The GraphQL ``rateLimit`` reading, then every pool of REST ``/rate_limit``.

    Raises:
        UnexpectedResponse: ``/rate_limit`` answered 404 or 422, or in a shape the models reject.
        RequestFailedError: a request still failed after the transport's retries.
    """
    graphql = await transport.graphql(RATE_LIMIT_QUERY, {}, splittable=False)
    # The transport has already read and checked `rateLimit` for the budget.
    reading = RateLimitReading.model_validate(graphql.data["rateLimit"])
    rest = await transport.rest_get("/rate_limit", {})
    if isinstance(rest, RestRefused):
        raise UnexpectedResponse(f"/rate_limit answered {rest.status}", status=rest.status)
    try:
        pools = RestRateLimits.model_validate(rest.body)
    except ValidationError as e:
        raise UnexpectedResponse(f"unexpected /rate_limit shape: {e}", status=200) from e
    return RateLimits(
        graphql_remaining=reading.remaining,
        graphql_reset_at=reading.reset_at.astimezone(UTC),
        rest=tuple(
            RestRateLimit(
                resource=name,
                limit=pool.limit,
                used=pool.used,
                remaining=pool.remaining,
                reset_at=datetime.fromtimestamp(pool.reset, UTC),
            )
            for name, pool in sorted(pools.resources.items())
        ),
    )
