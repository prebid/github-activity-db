"""Record the GitHub source-layer test fixtures from live GitHub.

Usage (from the repository root, with a token that can read the public tracked repositories):

    GITHUB_TOKEN="$(gh auth token)" uv run --no-sync python scripts/record_fixtures.py

Writes tests/fixtures/source/recorded/, replacing it only after every scenario recorded without a
failed item or user lookup. See tests/source/recorder.py for the scenarios.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.source.recorder import main

if __name__ == "__main__":
    raise SystemExit(main())
