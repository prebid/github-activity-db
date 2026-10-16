"""Fail on module-level type aliases to Any; use a TYPE_CHECKING block instead.

Flagged at module level (statements inside functions, classes, or `if` blocks are not):
- `Name = Any`, `Name = typing.Any`, and unions containing Any, such as `Name = Any | None`,
  `Optional[Any]`, or `Union[int, Any]`, and `Annotated[Any, ...]`;
- `Name: TypeAlias = Any`, also with `typing.TypeAlias`;
- `type Name = Any` and `type Name[T] = Any`.
Names are resolved through the module's imports from `typing` and `typing_extensions`, so
`from typing import Any as Dynamic` followed by `Name = Dynamic` is flagged, and a `class Any`
defined in the module, or `Any` imported from another package, is not.

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


TYPING_MODULES = {"typing", "typing_extensions"}


class TypingNames:
    """The local names that refer to typing objects and modules in one module."""

    def __init__(self, tree: ast.Module) -> None:
        self.objects: dict[str, str] = {}  # local name -> typing attribute it refers to
        self.modules: set[str] = set()  # local names bound to typing or typing_extensions
        self.star = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in TYPING_MODULES:
                        self.modules.add(alias.asname or alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module in TYPING_MODULES:
                for alias in node.names:
                    if alias.name == "*":
                        self.star = True
                    else:
                        self.objects[alias.asname or alias.name] = alias.name

    def refers_to(self, node: ast.expr, name: str) -> bool:
        if isinstance(node, ast.Name):
            return self.objects.get(node.id) == name or (self.star and node.id == name)
        return (
            isinstance(node, ast.Attribute)
            and node.attr == name
            and isinstance(node.value, ast.Name)
            and node.value.id in self.modules
        )


def _is_any(node: ast.expr, names: TypingNames) -> bool:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _is_any(node.left, names) or _is_any(node.right, names)
    if isinstance(node, ast.Subscript):
        args = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
        if names.refers_to(node.value, "Annotated"):
            return bool(args) and _is_any(args[0], names)
        if names.refers_to(node.value, "Optional") or names.refers_to(node.value, "Union"):
            return any(_is_any(arg, names) for arg in args)
    return names.refers_to(node, "Any")


def any_aliases(source: bytes) -> Iterator[tuple[int, str]]:
    tree = ast.parse(source)
    names = TypingNames(tree)
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and _is_any(statement.value, names):
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    yield statement.lineno, target.id
        elif (
            isinstance(statement, ast.AnnAssign)
            and statement.value is not None
            and names.refers_to(statement.annotation, "TypeAlias")
            and _is_any(statement.value, names)
            and isinstance(statement.target, ast.Name)
        ):
            yield statement.lineno, statement.target.id
        elif isinstance(statement, ast.TypeAlias) and _is_any(statement.value, names):
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
