"""Count lint and type-check suppression comments and enforce caps.

Only comments count; they are read with tokenize, so directive text inside a string does not.

- noqa: line-level `# noqa`, `#NOQA`, `# noqa: F401`, and file-level `# ruff: noqa`,
  `# ruff: noqa: F401`, `# flake8: noqa` in any letter case.
- type: ignore: `# type: ignore`, `# type:ignore[code]`, `#type:ignore`, and mypy inline
  configuration such as `# mypy: ignore-errors` or `# mypy: disable-error-code=...`.

A directive after other comment text on the same line counts. Suppressions configured in
pyproject.toml (per-file ignores, mypy overrides) are not comments and are not counted.

Usage: python scripts/count_suppressions.py PATH... --max-type-ignore N --max-noqa N
"""

from __future__ import annotations

import argparse
import re
import sys
import tokenize
from collections.abc import Iterator
from pathlib import Path

NOQA = re.compile(r"#\s*(?:(?:ruff|flake8)\s*:\s*)?noqa\b", re.IGNORECASE)
TYPE_IGNORE = re.compile(r"#\s*(?:type:\s*ignore\b|mypy\s*:)")


def python_files(paths: list[Path]) -> Iterator[Path]:
    for path in paths:
        if path.is_dir():
            yield from sorted(path.rglob("*.py"))
        elif path.suffix == ".py":
            yield path


def comments(path: Path) -> Iterator[tuple[int, str]]:
    with path.open("rb") as handle:
        for token in tokenize.tokenize(handle.readline):
            if token.type == tokenize.COMMENT:
                yield token.start[0], token.string


def count(files: list[Path]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {"type: ignore": [], "noqa": []}
    for path in files:
        for line, text in comments(path):
            if TYPE_IGNORE.search(text):
                found["type: ignore"].append(f"{path}:{line}")
            if NOQA.search(text):
                found["noqa"].append(f"{path}:{line}")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--max-type-ignore", type=int, required=True)
    parser.add_argument("--max-noqa", type=int, required=True)
    args = parser.parse_args(argv)
    files = list(python_files(args.paths))
    if not files:
        print(f"no Python files under {' '.join(map(str, args.paths))}", file=sys.stderr)
        return 1
    print(f"scanned {len(files)} files under {' '.join(map(str, args.paths))}")
    found = count(files)
    caps = {"type: ignore": args.max_type_ignore, "noqa": args.max_noqa}
    failed = False
    for kind, sites in found.items():
        print(f"{kind} comments: {len(sites)} (max {caps[kind]})")
        if len(sites) > caps[kind]:
            failed = True
            for site in sites:
                print(f"  {site}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
