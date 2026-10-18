"""Fail when a pull request removes or rewrites an entry of a version lock the base commit has.

Two locks, each with its own rule:

- tests/query_versions.json maps each ``<kind>:<version>`` to the hash of that kind's GraphQL
  documents (redesign plan Step 2). A test ties the current documents to the current version's
  entry; this check keeps every entry at the base fixed, so a document change cannot reuse a
  version by editing its hash.
- tests/schema_versions.json maps each schema version to its consumer (table or view, column)
  pairs (plan 5.1). A test ties the current entry to the data dictionary; this check keeps every
  entry at the base and every pair in it, so removing or renaming a consumer column needs a new
  schema version. Pairs may be added to the newest entry only: an older version describes
  databases that never had the added column.

Usage: check_query_lock.py --base <git ref> [--lock <one of the locks above>]

Without --lock, checks both. Run anywhere inside the repository; paths are taken from its root.
Uses only the standard library, so it runs with ``python -I -S``.

Exit 0 when every entry at <ref> is kept (new entries are fine, and so is a lock file that does
not exist at <ref>); 1 when an entry was changed or removed (deleting the lock removes them all);
2 when the check cannot run: <ref> is not a commit, git is not on PATH, --lock is not one of the
locks, git fails, or a lock file cannot be read as the JSON its rule expects (a schema version is
a positive integer without leading zeros). A checkout without the base therefore never passes by
default.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

QUERY_LOCK = "tests/query_versions.json"
SCHEMA_LOCK = "tests/schema_versions.json"


class CannotCheck(Exception):
    """The check cannot run (exit 2)."""


def _object(text: str, where: str) -> dict[str, Any]:
    try:
        entries = json.loads(text)
    except ValueError as e:  # JSONDecodeError, and an integer past Python's digit limit
        raise CannotCheck(f"{where} is not JSON: {e}") from e
    if not isinstance(entries, dict):
        raise CannotCheck(f"{where} is not a JSON object")
    return entries


def digests(text: str, where: str) -> dict[str, str]:
    entries = _object(text, where)
    if not all(isinstance(value, str) for value in entries.values()):
        raise CannotCheck(f"{where} is not a JSON object of strings")
    return entries


def pairs(text: str, where: str) -> dict[str, set[tuple[str, str]]]:
    entries = _object(text, where)
    read: dict[str, set[tuple[str, str]]] = {}
    for key, value in entries.items():
        if re.fullmatch(r"[1-9][0-9]*", key) is None:
            raise CannotCheck(f"{where}: schema version {key!r} is not a positive integer")
        if not isinstance(value, list) or not all(
            isinstance(pair, list) and len(pair) == 2 and all(isinstance(p, str) for p in pair)
            for pair in value
        ):
            raise CannotCheck(f"{where}: {key} is not a list of [table, column] pairs")
        read[key] = {(table, column) for table, column in value}
    return read


def digest_problems(base: dict[str, str], current: dict[str, str]) -> list[str]:
    """Entries of ``base`` that ``current`` changed or removed."""
    found = []
    for key, digest in sorted(base.items()):
        if key not in current:
            found.append(f"{key} was removed; lock entries are append-only")
        elif current[key] != digest:
            found.append(f"{key} changed from {digest} to {current[key]}; add a version instead")
    return found


def pair_problems(
    base: dict[str, set[tuple[str, str]]], current: dict[str, set[tuple[str, str]]]
) -> list[str]:
    """Entries of ``base`` that ``current`` removed, pairs it removed from one, and pairs it
    added to one that is not its newest."""
    found = []
    newest = max(current, key=int, default=None)
    for key, listed in sorted(base.items(), key=lambda entry: int(entry[0])):
        if key not in current:
            found.append(f"schema version {key} was removed; lock entries are append-only")
            continue
        for table, column in sorted(listed - current[key]):
            found.append(
                f"schema version {key} lost {table}.{column}; removing or renaming a consumer "
                "column needs a new schema version"
            )
        if key != newest:
            for table, column in sorted(current[key] - listed):
                found.append(
                    f"schema version {key} gained {table}.{column}; only the newest version, "
                    f"{newest}, gains columns"
                )
    return found


Reader = Callable[[str, str], dict[str, Any]]
Rule = Callable[[dict[str, Any], dict[str, Any]], list[str]]
LOCKS: dict[str, tuple[Reader, Rule]] = {
    QUERY_LOCK: (digests, digest_problems),
    SCHEMA_LOCK: (pairs, pair_problems),
}


def _git(git: str, *args: str, cwd: Path | None = None) -> str:
    result = subprocess.run([git, *args], capture_output=True, text=True, check=False, cwd=cwd)
    if result.returncode != 0:
        raise CannotCheck(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def check(base_ref: str, locks: list[str]) -> int:
    git = shutil.which("git")
    if git is None:
        raise CannotCheck("git is not on PATH")
    # Every path is taken from the repository root, wherever the check runs from.
    root = Path(_git(git, "rev-parse", "--show-toplevel").strip())
    try:
        _git(git, "rev-parse", "--verify", "--quiet", f"{base_ref}^{{commit}}", cwd=root)
    except CannotCheck as e:
        raise CannotCheck(f"{base_ref} is not a commit; fetch the base branch first") from e
    failed = False
    for lock in locks:
        read, rule = LOCKS[lock]
        if not _git(git, "ls-tree", "--name-only", base_ref, "--", lock, cwd=root).strip():
            print(f"{lock} does not exist at {base_ref}; nothing to compare")
            continue
        base = read(_git(git, "show", f"{base_ref}:{lock}", cwd=root), f"{lock} at {base_ref}")
        path = root / lock
        current = read(path.read_text(), lock) if path.exists() else {}
        found = rule(base, current)
        for problem in found:
            print(f"{lock}: {problem}", file=sys.stderr)
        if found:
            failed = True
        else:
            print(f"{lock}: {len(base)} entries at {base_ref} kept; {len(current)} entries now")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True, help="git ref of the pull request's base")
    parser.add_argument("--lock", help=f"one lock file to check: {' or '.join(LOCKS)}")
    args = parser.parse_args(argv)
    try:
        if args.lock is not None and args.lock not in LOCKS:
            raise CannotCheck(f"--lock {args.lock} is not one of {', '.join(LOCKS)}")
        return check(args.base, [args.lock] if args.lock else list(LOCKS))
    except (CannotCheck, OSError, UnicodeDecodeError, RecursionError) as e:
        # UnicodeDecodeError: a lock that is not UTF-8; RecursionError: one nested too deeply.
        print(f"cannot check: {e!r}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
