"""Record the GitHub source-layer test fixtures from live GitHub.

Usage (from the repository root, with a token that can read the public tracked repositories):

    GITHUB_TOKEN="$(gh auth token)" uv run --no-sync python scripts/record_fixtures.py

Writes tests/fixtures/source/recorded/, replacing it only after every scenario recorded without a
failed item or user lookup. See tests/source/recorder.py for the scenarios.

With ``--only rate-limits`` it records the rate-limit readings alone and adds them to the
existing recordings, leaving every other unit unchanged. With ``--only documents``, for a change
of the GraphQL documents, it records every scenario and user again and replaces those units,
keeping the recorded REST compare pages and rate-limit readings.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.source.recorder import main

if __name__ == "__main__":
    raise SystemExit(main())
