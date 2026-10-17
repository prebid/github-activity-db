"""Request mechanics, kept in code rather than configuration (redesign plan 5.5).

A measured value names the evidence row or GitHub rule it rests on; a value chosen without a
measurement says so, and plan section 8 lists it for Step 5 to measure. Changing a measured
value can invalidate a measured cost or time, so a change re-measures it.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

# GraphQL connections accept `first` from 1 to 100 (GitHub GraphQL node limits).
PAGE_SIZE: Final = 100

# Evidence "Hydration cost": 25 of the heaviest salesagent PRs cost 2 points and about 9 s,
# under GitHub's 10 s request limit.
PR_BATCH_SIZE: Final = 25

# Overflow continuations per request. Chosen, not measured (plan section 8): 25 aliases of 100
# entries each stay at one point by GitHub's cost formula.
OVERFLOW_ALIASES_PER_REQUEST: Final = 25

# `nodes(ids:)` accepts at most 100 ids.
USER_LOOKUP_BATCH: Final = 100

# Evidence "Commit cap": GraphQL `commits` and REST PR commits stop at 250.
GRAPHQL_COMMIT_CAP: Final = 250

# REST compare page size; without per_page/page the list stops at 250 commits.
COMPARE_PAGE_SIZE: Final = 100

# Closing-issue links read by the index (plan 5.8, D23).
INDEX_CLOSING_LINKS: Final = 10

# GitHub's `Date` header has one-second resolution and is set after the page is read, so the
# index boundary sits this far before it (plan 5.3 step 2).
INDEX_BOUNDARY_MARGIN: Final = timedelta(seconds=60)

# Retries after the first attempt for 5xx, transport errors, and unsplittable timeouts
# (plan 5.3 step 4). Jitter adds up to JITTER_FRACTION of each delay (chosen, not measured).
SERVER_RETRY_DELAYS: Final = (2.0, 4.0, 8.0)
JITTER_FRACTION: Final = 0.25

# GitHub: without retry-after or an exhausted primary limit, "wait for at least one minute before
# retrying"; while a limit persists, "wait for an exponentially increasing amount of time between
# retries". The transport doubles this per repeated limit.
RATE_LIMIT_FALLBACK_WAIT: Final = 60.0

# Without --max-minutes, a rate-limit wait longer than this fails the run (plan 5.3 step 4).
# Chosen, not measured: a primary window resets within an hour, plus 5 minutes.
MAX_RATE_LIMIT_WAIT: Final = 65 * 60.0

# Budget reserves. Chosen, not measured: the largest measured request costs 2 points (Evidence
# "Hydration cost") and one request is in flight, so 100 leaves room for other use of the token.
GRAPHQL_RESERVE_POINTS: Final = 100
REST_RESERVE_CALLS: Final = 100

# Seconds added to a reset time before the next request, for clock differences with GitHub.
# Chosen, not measured.
RESET_MARGIN: Final = 1.0

# GitHub limits GraphQL to 60 s of CPU time per minute and advises against concurrent requests;
# parallel requests were never measured (plan 5.3 step 2). Step 5 measures before raising it.
MAX_IN_FLIGHT: Final = 1

# HTTP client timeouts, chosen, not measured. The read timeout is above GitHub's 10 s request
# limit, so it fires only on a stalled connection; the connect timeout bounds connection setup.
READ_TIMEOUT: Final = 30.0
CONNECT_TIMEOUT: Final = 10.0
