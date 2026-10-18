"""Fail on %-style placeholders in loguru calls; loguru formats arguments with `{}` (gap 44, 80).

loguru fills `logger.info("found {} items", count)` with str.format, so `logger.info("found %s
items", count)` logs the literal `%s` and drops the argument without an error. ruff cannot catch
this: its logging rules model the standard library, which formats with %.

Flagged: a call `<receiver>.<level>(message, argument, ...)`, where level is one of trace, debug,
info, success, warning, error, critical, or exception, or `<receiver>.log(level, message,
argument, ...)`, when the message is a string literal (implicitly concatenated literals included)
that holds a %-directive and at least one positional or keyword argument follows it (loguru
formats with both: `logger.info("found {n}", n=3)`). A literal `%%` is not a directive. Not
flagged: a message that is not a string literal (a variable or an f-string), and a call without
arguments after the message, since loguru then formats nothing.

Usage: python scripts/check_log_placeholders.py PATH...
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from collections.abc import Iterator
from pathlib import Path

LEVELS = frozenset(
    {"trace", "debug", "info", "success", "warning", "error", "critical", "exception"}
)
# printf-style conversion: flags, width, precision, length, conversion; `%%` is matched first.
DIRECTIVE = re.compile(r"%%|%(\([^)]*\))?[#0 +-]*(\*|\d+)?(\.(\*|\d+))?[hlL]?[diouxXeEfFgGcrsa]")


def python_files(paths: list[Path]) -> Iterator[Path]:
    for path in paths:
        if path.is_dir():
            yield from sorted(p for p in path.rglob("*") if p.suffix in {".py", ".pyi"})
        elif path.suffix in {".py", ".pyi"}:
            yield path


def has_directive(message: str) -> bool:
    return any(match.group() != "%%" for match in DIRECTIVE.finditer(message))


def percent_placeholders(source: bytes) -> Iterator[tuple[int, str]]:
    """The line and message of every loguru call that passes arguments to a %-style message."""
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr in LEVELS:
            message_index = 0
        elif node.func.attr == "log":
            message_index = 1
        else:
            continue
        if len(node.args) <= message_index or (
            len(node.args) == message_index + 1 and not node.keywords
        ):
            continue
        message = node.args[message_index]
        if isinstance(message, ast.Constant) and isinstance(message.value, str):
            if has_directive(message.value):
                yield node.lineno, message.value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args(argv)
    files = list(python_files(args.paths))
    if not files:
        print(f"no Python files under {' '.join(map(str, args.paths))}", file=sys.stderr)
        return 1
    found = [
        f"{path}:{line}: %-style placeholder in {message!r}"
        for path in files
        for line, message in percent_placeholders(path.read_bytes())
    ]
    print(f"scanned {len(files)} files under {' '.join(map(str, args.paths))}")
    print(f"%-style loguru messages: {len(found)}")
    for site in found:
        print(f"  {site}")
    if found:
        print("loguru formats arguments with {}: use logger.info('found {} items', count).")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
