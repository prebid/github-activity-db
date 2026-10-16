"""Fail on module-level type aliases to Any; use a TYPE_CHECKING block instead.

Flagged at module level (statements inside functions, classes, or `if` blocks are not):
- `Name = Any`, `Name = typing.Any`, and unions containing Any, such as `Name = Any | None`,
  `Optional[Any]`, or `Union[int, Any]`, and `Annotated[Any, ...]`;
- `Name: TypeAlias = Any`, also with `typing.TypeAlias`;
- `type Name = Any` and `type Name[T] = Any`.
`Any` matches the name `Any` and any attribute named `Any` (`typing.Any`, `t.Any`).

Usage: python scripts/check_any_aliases.py PATH...
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections.abc import Iterator
from pathlib import Path


def python_files(paths: list[Path]) -> Iterator[Path]:
    for path in paths:
        if path.is_dir():
            yield from sorted(p for p in path.rglob("*") if p.suffix in {".py", ".pyi"})
        elif path.suffix in {".py", ".pyi"}:
            yield path


def _is_named(node: ast.expr, name: str) -> bool:
    return (isinstance(node, ast.Name) and node.id == name) or (
        isinstance(node, ast.Attribute) and node.attr == name
    )


def _is_any(node: ast.expr) -> bool:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _is_any(node.left) or _is_any(node.right)
    if isinstance(node, ast.Subscript):
        args = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
        if _is_named(node.value, "Annotated"):
            return bool(args) and _is_any(args[0])
        if _is_named(node.value, "Optional") or _is_named(node.value, "Union"):
            return any(_is_any(arg) for arg in args)
    return _is_named(node, "Any")


def any_aliases(source: bytes) -> Iterator[tuple[int, str]]:
    for statement in ast.parse(source).body:
        if isinstance(statement, ast.Assign) and _is_any(statement.value):
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    yield statement.lineno, target.id
        elif (
            isinstance(statement, ast.AnnAssign)
            and statement.value is not None
            and _is_named(statement.annotation, "TypeAlias")
            and _is_any(statement.value)
            and isinstance(statement.target, ast.Name)
        ):
            yield statement.lineno, statement.target.id
        elif isinstance(statement, ast.TypeAlias) and _is_any(statement.value):
            yield statement.lineno, statement.name.id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args(argv)
    files = list(python_files(args.paths))
    if not files:
        print(f"no Python files under {' '.join(map(str, args.paths))}", file=sys.stderr)
        return 1
    found = [
        f"{path}:{line}: {name} is an alias to Any"
        for path in files
        for line, name in any_aliases(path.read_bytes())
    ]
    print(f"scanned {len(files)} files under {' '.join(map(str, args.paths))}")
    print(f"aliases to Any: {len(found)}")
    for site in found:
        print(f"  {site}")
    if found:
        print("Use a TYPE_CHECKING block instead of an alias to Any.")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
