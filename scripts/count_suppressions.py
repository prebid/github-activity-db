"""Count lint and type-check suppressions and enforce caps.

Suppressions are read from comment tokens (tokenize) and decorators (ast), so directive text
inside a string does not count. Both .py and .pyi files are scanned.

- noqa: line-level `# noqa`, `#NOQA`, `# noqa: F401`; file-level `# ruff: noqa`,
  `# ruff: noqa: F401`, `# flake8: noqa`, in any letter case; and the isort action comments
  ruff honors, `# isort: skip_file`, `# isort: off`, `# isort: skip`, also with a `# ruff:`
  prefix.
- type: ignore: `# type: ignore`, `# type:ignore[code]`, `#type:ignore`; mypy inline
  configuration such as `# mypy: ignore-errors` or `# mypy: disable-error-code=...`; and the
  `@no_type_check` decorator, also as `@typing.no_type_check` or under an imported alias.

A directive after other comment text on the same line counts. Not counted: suppressions
configured in pyproject.toml (per-file ignores, mypy overrides) and formatter comments
(`# fmt: off`).

Usage: python scripts/count_suppressions.py PATH... --max-type-ignore N --max-noqa N
"""

from __future__ import annotations

import argparse
import ast
import io
import re
import sys
import tokenize
from collections.abc import Iterator
from pathlib import Path

NOQA = re.compile(
    r"#\s*(?:(?:ruff|flake8)\s*:\s*)?noqa\b"
    r"|#\s*(?:ruff\s*:\s*)?isort\s*:\s*(?:skip_file|off|skip)\b",
    re.IGNORECASE,
)
TYPE_IGNORE = re.compile(r"#\s*(?:type:\s*ignore\b|mypy\s*:)")
NO_TYPE_CHECK = "no_type_check"
TYPING_MODULES = {"typing", "typing_extensions"}


def python_files(paths: list[Path]) -> Iterator[Path]:
    for path in paths:
        if path.is_dir():
            yield from sorted(p for p in path.rglob("*") if p.suffix in {".py", ".pyi"})
        elif path.suffix in {".py", ".pyi"}:
            yield path


def comments(source: bytes) -> Iterator[tuple[int, str]]:
    for token in tokenize.tokenize(io.BytesIO(source).readline):
        if token.type == tokenize.COMMENT:
            yield token.start[0], token.string


def no_type_check_lines(source: bytes) -> Iterator[int]:
    tree = ast.parse(source)
    # Names that refer to no_type_check, including `from typing import no_type_check as x`.
    local_names = {NO_TYPE_CHECK} | {
        alias.asname
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module in TYPING_MODULES
        for alias in node.names
        if alias.name == NO_TYPE_CHECK and alias.asname
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            for decorator in node.decorator_list:
                target = decorator.func if isinstance(decorator, ast.Call) else decorator
                if (isinstance(target, ast.Name) and target.id in local_names) or (
                    isinstance(target, ast.Attribute) and target.attr == NO_TYPE_CHECK
                ):
                    yield decorator.lineno


def count(files: list[Path]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {"type: ignore": [], "noqa": []}
    for path in files:
        source = path.read_bytes()
        for line, text in comments(source):
            if TYPE_IGNORE.search(text):
                found["type: ignore"].append(f"{path}:{line}")
            if NOQA.search(text):
                found["noqa"].append(f"{path}:{line}")
        for line in no_type_check_lines(source):
            found["type: ignore"].append(f"{path}:{line}")
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
