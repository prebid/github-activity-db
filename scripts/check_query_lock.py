"""Fail when a pull request changes or removes an entry of the hydration-version lock.

tests/query_versions.json maps each ``<kind>:<version>`` to the hash of that kind's GraphQL
documents (redesign plan Step 2). A test ties the current documents to the current version's
entry; this check keeps every entry at the base fixed, so a document change cannot reuse a
version by editing its hash.

Usage: check_query_lock.py --base <git ref> [--lock tests/query_versions.json]

Run anywhere inside the repository; paths are taken from its root. Uses only the standard
library, so it runs with ``python -I -S``.

Exit 0 when every entry at <ref> is unchanged (new entries are fine, and so is a lock file that
does not exist at <ref>); 1 when an entry was changed or removed (deleting the lock removes them
all); 2 when the check cannot run: <ref> is not a commit, git is not on PATH, --lock is not a
relative path, git fails, or a lock file cannot be read as a JSON object of strings. A checkout
without the base therefore never passes by default.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

DEFAULT_LOCK = "tests/query_versions.json"


class CannotCheck(Exception):
    """The check cannot run (exit 2)."""


def problems(base: dict[str, str], current: dict[str, str]) -> list[str]:
    """Entries of ``base`` that ``current`` changed or removed."""
    found = []
    for key, digest in sorted(base.items()):
        if key not in current:
            found.append(f"{key} was removed; lock entries are append-only")
        elif current[key] != digest:
            found.append(f"{key} changed from {digest} to {current[key]}; add a version instead")
    return found


def _git(git: str, *args: str, cwd: Path | None = None) -> str:
    result = subprocess.run([git, *args], capture_output=True, text=True, check=False, cwd=cwd)
    if result.returncode != 0:
        raise CannotCheck(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _entries(text: str, where: str) -> dict[str, str]:
    try:
        entries = json.loads(text)
    except ValueError as e:  # JSONDecodeError, and an integer past Python's digit limit
        raise CannotCheck(f"{where} is not JSON: {e}") from e
    if not isinstance(entries, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in entries.items()
    ):
        raise CannotCheck(f"{where} is not a JSON object of strings")
    return entries


def check(base_ref: str, lock: str) -> int:
    git = shutil.which("git")
    if git is None:
        raise CannotCheck("git is not on PATH")
    if PurePosixPath(lock).is_absolute() or ".." in PurePosixPath(lock).parts:
        raise CannotCheck(f"--lock {lock} must be a path relative to the repository root")
    # Every path is taken from the repository root, wherever the check runs from.
    root = Path(_git(git, "rev-parse", "--show-toplevel").strip())
    try:
        _git(git, "rev-parse", "--verify", "--quiet", f"{base_ref}^{{commit}}", cwd=root)
    except CannotCheck as e:
        raise CannotCheck(f"{base_ref} is not a commit; fetch the base branch first") from e
    if not _git(git, "ls-tree", "--name-only", base_ref, "--", lock, cwd=root).strip():
        print(f"{lock} does not exist at {base_ref}; nothing to compare")
        return 0
    base = _entries(_git(git, "show", f"{base_ref}:{lock}", cwd=root), f"{lock} at {base_ref}")
    path = root / lock
    current = _entries(path.read_text(), lock) if path.exists() else {}
    found = problems(base, current)
    for problem in found:
        print(problem, file=sys.stderr)
    if found:
        return 1
    print(f"{len(base)} lock entries at {base_ref} unchanged; {len(current)} entries now")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True, help="git ref of the pull request's base")
    parser.add_argument("--lock", default=DEFAULT_LOCK, help="lock file path in the repository")
    args = parser.parse_args(argv)
    try:
        return check(args.base, args.lock)
    except (CannotCheck, OSError, UnicodeDecodeError, RecursionError) as e:
        # UnicodeDecodeError: a lock that is not UTF-8; RecursionError: one nested too deeply.
        print(f"cannot check: {e!r}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
