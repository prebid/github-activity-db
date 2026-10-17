"""Fail unless each database file has the pinned SHA-256 of the frozen github_activity.db (D26).

From Step 3 until Step 6c removes it, the tracked github_activity.db keeps its pre-Step-3
content (redesign plan D26, evidence "Tracked database"): the application's default
DATABASE_URL names the git-ignored github_activity.local.db instead, and alembic/env.py refuses
to migrate the tracked file. Runs as a pre-commit hook on the file, so CI's --all-files run
checks it.

Usage: python scripts/check_frozen_db.py DATABASE...
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

PINNED_SHA256 = "c18b2bc50a7e60f822cff185723cabc2846da31bcd8814aa9df46dca0d1b4751"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("databases", nargs="+", type=Path)
    args = parser.parse_args(argv)
    failed = False
    for path in args.databases:
        try:
            found = sha256(path)
        except OSError as e:
            print(f"{path}: cannot read: {e}")
            failed = True
            continue
        if found == PINNED_SHA256:
            print(f"{path}: sha256 {found}, the pinned frozen content")
        else:
            print(
                f"{path}: sha256 {found} differs from the pinned {PINNED_SHA256}; the tracked "
                f"database stays frozen until Step 6c (D26): git checkout -- {path}"
            )
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
