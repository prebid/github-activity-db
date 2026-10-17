"""Fail when a pull request changes or removes an entry of the hydration-version lock.

tests/query_versions.json maps each ``<kind>:<version>`` to the hash of that kind's GraphQL
documents (redesign plan Step 2). A test ties the current documents to the current version; this
check keeps old entries fixed, so a document change cannot reuse a version by editing its hash.

Usage: check_query_lock.py --base <git ref> [--lock tests/query_versions.json]

Exit 0 when every entry at <ref> is unchanged (new entries are fine, and so is a lock file that
does not exist at <ref>); 1 when an entry was changed or removed; 2 when <ref> is not a commit,
so a checkout without the base never passes by default.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_LOCK = "tests/query_versions.json"


def problems(base: dict[str, str], current: dict[str, str]) -> list[str]:
    """Entries of ``base`` that ``current`` changed or removed."""
    found = []
    for key, digest in sorted(base.items()):
        if key not in current:
            found.append(f"{key} was removed; lock entries are append-only")
        elif current[key] != digest:
            found.append(f"{key} changed from {digest} to {current[key]}; add a version instead")
    return found


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    git = shutil.which("git")
    if git is None:
        raise SystemExit("git is not on PATH")
    return subprocess.run([git, *args], capture_output=True, text=True, check=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True, help="git ref of the pull request's base")
    parser.add_argument("--lock", default=DEFAULT_LOCK, help="lock file path in the repository")
    args = parser.parse_args(argv)

    if _git("rev-parse", "--verify", "--quiet", f"{args.base}^{{commit}}").returncode != 0:
        print(f"{args.base} is not a commit; fetch the base branch first", file=sys.stderr)
        return 2
    if _git("cat-file", "-e", f"{args.base}:{args.lock}").returncode != 0:
        print(f"{args.lock} does not exist at {args.base}; nothing to compare")
        return 0
    base = json.loads(_git("show", f"{args.base}:{args.lock}").stdout)
    current = json.loads(Path(args.lock).read_text())
    found = problems(base, current)
    for problem in found:
        print(problem, file=sys.stderr)
    if found:
        return 1
    print(f"{len(base)} lock entries at {args.base} unchanged; {len(current)} entries now")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
