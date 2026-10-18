"""Fail when a pull request removes or rewrites an entry of a version lock the base commit has.

Two locks, each with its own rule:

- tests/query_versions.json maps each ``<kind>:<version>`` to the hash of that kind's GraphQL
  documents (redesign plan Step 2). A test ties the current documents to the current version's
  entry; this check keeps every entry at the base fixed, so a document change cannot reuse a
  version by editing its hash.
- tests/schema_versions.json maps each schema version to its consumer (table or view, column)
  pairs, and from version 2 to the SHA-256 of each consumer view's frozen SQL (plan 5.1): an
  entry is a list of pairs, or an object with ``columns`` (that list) and ``views`` (view name to
  digest, 64 lowercase hex characters). A test ties the current entry to the data dictionary,
  and from Step 3b-1 to the views at head; this check keeps every entry at the base, every pair
  in it, and every view digest in it, so removing or renaming a consumer column, or changing or
  removing a view, needs a new schema version. Pairs and views may be added to the newest entry
  only: an older version describes databases that never had them.

Usage: check_query_lock.py --base <git ref> [--lock <one of the locks above>]

Without --lock, checks both. Run anywhere inside the repository; paths are taken from its root.
Uses only the standard library, so it runs with ``python -I -S``.

Exit 0 when every entry at <ref> is kept (new entries are fine, and so is a lock file that does
not exist at <ref>); 1 when an entry was changed or removed (deleting the lock removes them all);
2 when the check cannot run: <ref> is not a commit, git is not on PATH, --lock is not one of the
locks, git fails, or a lock file cannot be read as the JSON its rule expects (a schema version is
a positive integer of at most nine digits without leading zeros, and no object lists a key
twice). A checkout without the base therefore never passes by default.
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


class RepeatedKey(ValueError):
    """A JSON object lists a key twice, which JSON allows and ``json.loads`` reads as the last."""


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """A JSON object whose keys are distinct."""
    keys = [key for key, _ in pairs]
    if repeated := sorted({key for key in keys if keys.count(key) > 1}):
        raise RepeatedKey(f"key listed twice: {', '.join(repeated)}")
    return dict(pairs)


def _object(text: str, where: str) -> dict[str, Any]:
    try:
        entries = json.loads(text, object_pairs_hook=_unique_keys)
    except RepeatedKey as e:
        raise CannotCheck(f"{where}: {e}") from e
    except ValueError as e:  # JSONDecodeError, and an integer past the digit limit
        raise CannotCheck(f"{where} is not JSON: {e}") from e
    if not isinstance(entries, dict):
        raise CannotCheck(f"{where} is not a JSON object")
    return entries


def digests(text: str, where: str) -> dict[str, str]:
    entries = _object(text, where)
    if not all(isinstance(value, str) for value in entries.values()):
        raise CannotCheck(f"{where} is not a JSON object of strings")
    return entries


SchemaEntry = tuple[set[tuple[str, str]], dict[str, str]]


def _pairs(value: object, key: str, where: str) -> set[tuple[str, str]]:
    if not isinstance(value, list) or not all(
        isinstance(pair, list) and len(pair) == 2 and all(isinstance(p, str) for p in pair)
        for pair in value
    ):
        raise CannotCheck(f"{where}: {key} is not a list of [table, column] pairs")
    return {(table, column) for table, column in value}


def _views(value: object, key: str, where: str) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        isinstance(name, str) and isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)
        for name, digest in value.items()
    ):
        raise CannotCheck(f"{where}: {key} views is not an object of SHA-256 digests")
    return dict(value)


def schema_entries(text: str, where: str) -> dict[str, SchemaEntry]:
    """Each version's consumer pairs and view digests; a list entry has no views."""
    entries = _object(text, where)
    read: dict[str, SchemaEntry] = {}
    for key, value in entries.items():
        if re.fullmatch(r"[1-9][0-9]{0,8}", key) is None:
            raise CannotCheck(
                f"{where}: schema version {key!r} is not a positive integer of at most nine "
                "digits without leading zeros"
            )
        if isinstance(value, dict):
            if set(value) != {"columns", "views"}:
                raise CannotCheck(f"{where}: {key} must have exactly columns and views")
            read[key] = (_pairs(value["columns"], key, where), _views(value["views"], key, where))
        else:
            read[key] = (_pairs(value, key, where), {})
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


def schema_problems(base: dict[str, SchemaEntry], current: dict[str, SchemaEntry]) -> list[str]:
    """Entries of ``base`` that ``current`` removed; pairs and views it removed from one, and
    view digests it changed; and pairs and views it added to one that is not its newest."""
    found = []
    newest = max(current, key=int, default=None)
    for key, (listed, views) in sorted(base.items(), key=lambda entry: int(entry[0])):
        if key not in current:
            found.append(f"schema version {key} was removed; lock entries are append-only")
            continue
        now_listed, now_views = current[key]
        for table, column in sorted(listed - now_listed):
            found.append(
                f"schema version {key} lost {table}.{column}; removing or renaming a consumer "
                "column needs a new schema version"
            )
        for name, digest in sorted(views.items()):
            if name not in now_views:
                found.append(
                    f"schema version {key} lost view {name}; removing a view needs a new "
                    "schema version"
                )
            elif now_views[name] != digest:
                found.append(
                    f"schema version {key} changed view {name}; a change to a view's rows or "
                    "derivation needs a new schema version"
                )
        if key != newest:
            for table, column in sorted(now_listed - listed):
                found.append(
                    f"schema version {key} gained {table}.{column}; only the newest version, "
                    f"{newest}, gains columns"
                )
            for name in sorted(now_views.keys() - views.keys()):
                found.append(
                    f"schema version {key} gained view {name}; only the newest version, "
                    f"{newest}, gains views"
                )
    return found


Reader = Callable[[str, str], dict[str, Any]]
Rule = Callable[[dict[str, Any], dict[str, Any]], list[str]]
LOCKS: dict[str, tuple[Reader, Rule]] = {
    QUERY_LOCK: (digests, digest_problems),
    SCHEMA_LOCK: (schema_entries, schema_problems),
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
