"""Fail on module-level type aliases to Any; use a TYPE_CHECKING block instead.

Flagged at module level (statements inside functions, classes, or `if` blocks are not):
- `Name = Any`, `Name = typing.Any`, and unions containing Any, such as `Name = Any | None`,
  `Optional[Any]`, or `Union[int, Any]`, and `Annotated[Any, ...]`;
- `Name: TypeAlias = Any`, also with `typing.TypeAlias`;
- `type Name = Any` and `type Name[T] = Any`.
Names are resolved through the module's own imports from `typing` and `typing_extensions`,
in source order: `from typing import Any as Dynamic` followed by `Name = Dynamic` is flagged,
and a `class Any` defined in the module, or `Any` imported from another package, is not.
Imports inside functions and classes do not count, since they bind names in another scope;
imports inside module-level `if`, `try`, `with`, `match`, and loop blocks do. A later
module-level class, function, assignment, `with`/`for`/`except` target, or non-typing import of
the same name replaces the typing binding. Where branches disagree (`if TYPE_CHECKING:` with an
`else`, `try`/`except`, loops that may not run), a name counts as typing if any branch binds it so.
Not tracked: `del`, `global`, walrus targets, and `match` captures. A rebinding by one of them
leaves the typing binding in place (over-flagging), and `(Name := typing.Any)` is not recognized.

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
NEW_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _stored_names(target: ast.AST | None) -> list[str]:
    if target is None:
        return []
    return [node.id for node in ast.walk(target) if isinstance(node, ast.Name)]


class TypingNames:
    """Module-scope names that may refer to typing objects and modules at one point."""

    def __init__(self) -> None:
        self.objects: dict[str, set[str]] = {}  # local name -> typing attributes it may be
        self.modules: set[str] = set()  # local names that may be typing or typing_extensions
        self.star = False  # `from typing import *` may have bound every typing name
        self.shadowed: set[str] = set()  # names rebound after a star import

    def copy(self) -> TypingNames:
        other = TypingNames()
        other.objects = {name: set(attrs) for name, attrs in self.objects.items()}
        other.modules = set(self.modules)
        other.star = self.star
        other.shadowed = set(self.shadowed)
        return other

    @staticmethod
    def merge(*branches: TypingNames) -> TypingNames:
        """A name may be typing after the branches if it may be in any of them."""
        merged = TypingNames()
        for branch in branches:
            for name, attrs in branch.objects.items():
                merged.objects.setdefault(name, set()).update(attrs)
            merged.modules |= branch.modules
            merged.star = merged.star or branch.star
        merged.shadowed = set.intersection(*(branch.shadowed for branch in branches))
        return merged

    def unbind(self, *names: str) -> None:
        for name in names:
            self.objects.pop(name, None)
            self.modules.discard(name)
            self.shadowed.add(name)

    def update(self, statement: ast.stmt) -> None:
        """Apply the bindings a simple module-scope statement makes."""
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                if alias.asname:
                    local, is_typing = alias.asname, alias.name in TYPING_MODULES
                else:
                    local = alias.name.split(".")[0]
                    is_typing = local in TYPING_MODULES
                self.unbind(local)
                if is_typing:
                    self.modules.add(local)
        elif isinstance(statement, ast.ImportFrom):
            from_typing = statement.level == 0 and statement.module in TYPING_MODULES
            for alias in statement.names:
                if alias.name == "*":
                    if from_typing:
                        self.star = True
                        self.shadowed.clear()
                    continue
                local = alias.asname or alias.name
                self.unbind(local)
                if from_typing:
                    self.objects[local] = {alias.name}
                    self.shadowed.discard(local)
        elif isinstance(statement, NEW_SCOPES):
            self.unbind(statement.name)
        elif isinstance(statement, ast.TypeAlias):
            self.unbind(statement.name.id)
        elif isinstance(statement, ast.Assign):
            for target in statement.targets:
                self.unbind(*_stored_names(target))
        elif isinstance(statement, ast.AugAssign) or (
            isinstance(statement, ast.AnnAssign) and statement.value is not None
        ):
            self.unbind(*_stored_names(statement.target))

    def refers_to(self, node: ast.expr, name: str) -> bool:
        if isinstance(node, ast.Name):
            if node.id in self.objects:
                return name in self.objects[node.id]
            return self.star and node.id == name and node.id not in self.shadowed
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


def _aliases_in(statement: ast.stmt, names: TypingNames) -> Iterator[str]:
    if isinstance(statement, ast.Assign) and _is_any(statement.value, names):
        for target in statement.targets:
            if isinstance(target, ast.Name):
                yield target.id
    elif (
        isinstance(statement, ast.AnnAssign)
        and statement.value is not None
        and names.refers_to(statement.annotation, "TypeAlias")
        and _is_any(statement.value, names)
        and isinstance(statement.target, ast.Name)
    ):
        yield statement.target.id
    elif isinstance(statement, ast.TypeAlias) and _is_any(statement.value, names):
        yield statement.name.id


def _walk(
    statements: list[ast.stmt],
    names: TypingNames,
    top_level: bool,
    found: list[tuple[int, str]],
) -> TypingNames:
    """Follow module-scope bindings through statements; record top-level aliases to Any."""
    for statement in statements:
        if top_level:
            found.extend((statement.lineno, alias) for alias in _aliases_in(statement, names))
        if isinstance(statement, ast.If):
            body = _walk(statement.body, names.copy(), False, found)
            orelse = _walk(statement.orelse, names.copy(), False, found)
            names = TypingNames.merge(body, orelse)
        elif isinstance(statement, ast.For | ast.AsyncFor | ast.While):
            entered = names.copy()
            if not isinstance(statement, ast.While):
                entered.unbind(*_stored_names(statement.target))
            body = _walk(statement.body, entered, False, found)
            names = _walk(statement.orelse, TypingNames.merge(names, body), False, found)
        elif isinstance(statement, ast.With | ast.AsyncWith):
            for item in statement.items:
                names.unbind(*_stored_names(item.optional_vars))
            names = _walk(statement.body, names, False, found)
        elif isinstance(statement, ast.Try | ast.TryStar):
            body = _walk(statement.body, names.copy(), False, found)
            branches = [_walk(statement.orelse, body.copy(), False, found)]
            for handler in statement.handlers:
                entered = TypingNames.merge(names, body)
                if handler.name:
                    entered.unbind(handler.name)
                branches.append(_walk(handler.body, entered, False, found))
            names = _walk(statement.finalbody, TypingNames.merge(*branches), False, found)
        elif isinstance(statement, ast.Match):
            cases = [_walk(case.body, names.copy(), False, found) for case in statement.cases]
            names = TypingNames.merge(names, *cases)
        else:
            names.update(statement)
    return names


def any_aliases(source: bytes) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    _walk(ast.parse(source).body, TypingNames(), True, found)
    return found


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
