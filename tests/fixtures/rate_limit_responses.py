"""Mock GitHub rate limit API response fixtures.

These fixtures represent realistic GitHub REST API rate limit responses
for testing schema parsing and rate limit monitoring logic.

See: https://docs.github.com/en/rest/rate-limit/rate-limit
"""

import time

# -----------------------------------------------------------------------------
# Helper to generate reset timestamps
# -----------------------------------------------------------------------------


def future_reset_timestamp(seconds_from_now: int = 3600) -> int:
    """Generate a Unix timestamp for reset time in the future."""
    return int(time.time()) + seconds_from_now


# -----------------------------------------------------------------------------
# Full Rate Limit API Response (GET /rate_limit)
# -----------------------------------------------------------------------------

# Authenticated PAT with healthy quota
RATE_LIMIT_RESPONSE_HEALTHY = {
    "resources": {
        "core": {
            "limit": 5000,
            "remaining": 4500,
            "used": 500,
            "reset": future_reset_timestamp(3600),
        },
        "search": {
            "limit": 30,
            "remaining": 28,
            "used": 2,
            "reset": future_reset_timestamp(60),
        },
        "graphql": {
            "limit": 5000,
            "remaining": 4800,
            "used": 200,
            "reset": future_reset_timestamp(3600),
        },
        "code_search": {
            "limit": 10,
            "remaining": 10,
            "used": 0,
            "reset": future_reset_timestamp(60),
        },
        "integration_manifest": {
            "limit": 5000,
            "remaining": 5000,
            "used": 0,
            "reset": future_reset_timestamp(3600),
        },
    },
    "rate": {
        "limit": 5000,
        "remaining": 4500,
        "used": 500,
        "reset": future_reset_timestamp(3600),
    },
}

# Authenticated PAT with warning-level quota (20-50% remaining)
RATE_LIMIT_RESPONSE_WARNING = {
    "resources": {
        "core": {
            "limit": 5000,
            "remaining": 1500,  # 30% remaining
            "used": 3500,
            "reset": future_reset_timestamp(1800),
        },
        "search": {
            "limit": 30,
            "remaining": 10,
            "used": 20,
            "reset": future_reset_timestamp(60),
        },
        "graphql": {
            "limit": 5000,
            "remaining": 1200,
            "used": 3800,
            "reset": future_reset_timestamp(1800),
        },
        "code_search": {
            "limit": 10,
            "remaining": 3,
            "used": 7,
            "reset": future_reset_timestamp(60),
        },
        "integration_manifest": {
            "limit": 5000,
            "remaining": 5000,
            "used": 0,
            "reset": future_reset_timestamp(3600),
        },
    },
    "rate": {
        "limit": 5000,
        "remaining": 1500,
        "used": 3500,
        "reset": future_reset_timestamp(1800),
    },
}

# Authenticated PAT with critical quota (< 20% remaining)
RATE_LIMIT_RESPONSE_CRITICAL = {
    "resources": {
        "core": {
            "limit": 5000,
            "remaining": 250,  # 5% remaining
            "used": 4750,
            "reset": future_reset_timestamp(600),
        },
        "search": {
            "limit": 30,
            "remaining": 2,
            "used": 28,
            "reset": future_reset_timestamp(60),
        },
        "graphql": {
            "limit": 5000,
            "remaining": 100,
            "used": 4900,
            "reset": future_reset_timestamp(600),
        },
        "code_search": {
            "limit": 10,
            "remaining": 0,
            "used": 10,
            "reset": future_reset_timestamp(60),
        },
        "integration_manifest": {
            "limit": 5000,
            "remaining": 5000,
            "used": 0,
            "reset": future_reset_timestamp(3600),
        },
    },
    "rate": {
        "limit": 5000,
        "remaining": 250,
        "used": 4750,
        "reset": future_reset_timestamp(600),
    },
}

# Exhausted rate limit (0 remaining)
RATE_LIMIT_RESPONSE_EXHAUSTED = {
    "resources": {
        "core": {
            "limit": 5000,
            "remaining": 0,
            "used": 5000,
            "reset": future_reset_timestamp(300),
        },
        "search": {
            "limit": 30,
            "remaining": 0,
            "used": 30,
            "reset": future_reset_timestamp(60),
        },
        "graphql": {
            "limit": 5000,
            "remaining": 0,
            "used": 5000,
            "reset": future_reset_timestamp(300),
        },
        "code_search": {
            "limit": 10,
            "remaining": 0,
            "used": 10,
            "reset": future_reset_timestamp(60),
        },
        "integration_manifest": {
            "limit": 5000,
            "remaining": 5000,
            "used": 0,
            "reset": future_reset_timestamp(3600),
        },
    },
    "rate": {
        "limit": 5000,
        "remaining": 0,
        "used": 5000,
        "reset": future_reset_timestamp(300),
    },
}

# Unauthenticated (60 requests/hour limit)
RATE_LIMIT_RESPONSE_UNAUTHENTICATED = {
    "resources": {
        "core": {
            "limit": 60,
            "remaining": 55,
            "used": 5,
            "reset": future_reset_timestamp(3600),
        },
        "search": {
            "limit": 10,
            "remaining": 10,
            "used": 0,
            "reset": future_reset_timestamp(60),
        },
        "graphql": {
            "limit": 0,
            "remaining": 0,
            "used": 0,
            "reset": future_reset_timestamp(3600),
        },
        "code_search": {
            "limit": 10,
            "remaining": 10,
            "used": 0,
            "reset": future_reset_timestamp(60),
        },
        "integration_manifest": {
            "limit": 5000,
            "remaining": 5000,
            "used": 0,
            "reset": future_reset_timestamp(3600),
        },
    },
    "rate": {
        "limit": 60,
        "remaining": 55,
        "used": 5,
        "reset": future_reset_timestamp(3600),
    },
}


# -----------------------------------------------------------------------------
# Response Headers (returned on every API call)
# -----------------------------------------------------------------------------


def make_rate_limit_headers(
    remaining: int = 4999,
    limit: int = 5000,
    used: int = 1,
    reset_in_seconds: int = 3600,
    resource: str = "core",
) -> dict[str, str]:
    """Create rate limit headers as returned by GitHub API.

    Args:
        remaining: Requests remaining in window
        limit: Maximum requests allowed
        used: Requests used in window
        reset_in_seconds: Seconds until reset
        resource: Rate limit resource pool

    Returns:
        Dict of header name -> value (all strings)
    """
    return {
        "x-ratelimit-limit": str(limit),
        "x-ratelimit-remaining": str(remaining),
        "x-ratelimit-used": str(used),
        "x-ratelimit-reset": str(int(time.time()) + reset_in_seconds),
        "x-ratelimit-resource": resource,
    }


# Pre-built header fixtures for common scenarios
HEADERS_HEALTHY = make_rate_limit_headers(
    remaining=4500, limit=5000, used=500, reset_in_seconds=3600
)

HEADERS_WARNING = make_rate_limit_headers(
    remaining=1500, limit=5000, used=3500, reset_in_seconds=1800
)

HEADERS_CRITICAL = make_rate_limit_headers(
    remaining=250, limit=5000, used=4750, reset_in_seconds=600
)

HEADERS_EXHAUSTED = make_rate_limit_headers(
    remaining=0, limit=5000, used=5000, reset_in_seconds=300
)

HEADERS_UNAUTHENTICATED = make_rate_limit_headers(
    remaining=55, limit=60, used=5, reset_in_seconds=3600
)

HEADERS_SEARCH_POOL = make_rate_limit_headers(
    remaining=28, limit=30, used=2, reset_in_seconds=60, resource="search"
)

HEADERS_GRAPHQL_POOL = make_rate_limit_headers(
    remaining=4800, limit=5000, used=200, reset_in_seconds=3600, resource="graphql"
)


# -----------------------------------------------------------------------------
# Edge Cases
# -----------------------------------------------------------------------------

# Headers with missing fields (partial response)
HEADERS_PARTIAL = {
    "x-ratelimit-remaining": "100",
    "x-ratelimit-limit": "5000",
    # Missing: used, reset, resource
}

# Headers with zero limit (shouldn't happen but handle gracefully)
HEADERS_ZERO_LIMIT = make_rate_limit_headers(
    remaining=0, limit=0, used=0, reset_in_seconds=3600
)

# Response with only core pool (minimal response)
RATE_LIMIT_RESPONSE_MINIMAL = {
    "resources": {
        "core": {
            "limit": 5000,
            "remaining": 4000,
            "used": 1000,
            "reset": future_reset_timestamp(3600),
        },
    },
    "rate": {
        "limit": 5000,
        "remaining": 4000,
        "used": 1000,
        "reset": future_reset_timestamp(3600),
    },
}
