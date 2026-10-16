"""Fail on module-level type aliases to Any; use a TYPE_CHECKING block instead.

Flagged: aliases anywhere a type checker reads them in module scope, including an
`if TYPE_CHECKING:` body. Not flagged: the branch only the runtime executes (the `else` of
`if TYPE_CHECKING:`, the body of `if not TYPE_CHECKING:`), so `if TYPE_CHECKING: X = Precise` /
`else: X = Any` is allowed, and aliases inside functions and classes. The forms:
- `Name = Any`, `Name = typing.Any`, unions containing Any (`Any | None`, `Optional[Any]`,
  `Union[int, Any]`), and `Annotated[Any, ...]`;
- `Name: TypeAlias = Any`, also with `typing.TypeAlias` or a quoted `"TypeAlias"`;
- `type Name = Any` and `type Name[T] = Any`.
String forward references such as `Name: TypeAlias = "Any"` or `Optional["Any"]` are parsed where
a type is expected; a plain `Name = "Any"` is a string value.

Names are resolved through the module's own bindings, in source order: imports from `typing` and
`typing_extensions`, and assignments that copy them (`t = typing`, `D = typing.Any`). Imports
inside functions and classes bind other scopes and do not count. A later module-level class,
function, `type` statement, assignment to the name, `with`/`for`/`except` target, or non-typing
import replaces a typing binding; `x[k] = ...` and `x.attr = ...` only read `x`. Where branches
disagree (`if`/`else`, `try`/`except`, a loop or `match` that may not run), a name counts as
typing if any branch binds it so.

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

TYPING_MODULES = {"typing", "typing_extensions"}
NEW_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def python_files(paths: list[Path]) -> Iterator[Path]:
    for path in paths:
        if path.is_dir():
            yield from sorted(p for p in path.rglob("*") if p.suffix in {".py", ".pyi"})
        elif path.suffix in {".py", ".pyi"}:
            yield path


def _stored_names(target: ast.AST | None) -> list[str]:
    """Names a target binds: `x` and `a, *b` do; `x[k]` and `x.attr` only read `x`."""
    if target is None:
        return []
    return [
        node.id
        for node in ast.walk(target)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
    ]


class TypingNames:
    """Module-scope names that may refer to typing objects and modules at one point."""

    def __init__(self) -> None:
        self.objects: dict[str, set[str]] = {}  # local name -> typing attributes it may be
        self.modules: set[str] = set()  # local names that may be typing or typing_extensions
        self.star = False  # `from typing import *` may have bound every typing name
        self.shadowed: set[str] = set()  # names rebound since that star import

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

    def resolve(self, node: ast.expr) -> tuple[set[str], bool]:
        """The typing attributes node may be, and whether it may be a typing module."""
        if isinstance(node, ast.Name):
            attrs = set(self.objects.get(node.id, ()))
            if self.star and node.id not in self.shadowed:
                attrs.add(node.id)
            return attrs, node.id in self.modules
        if isinstance(node, ast.Attribute) and self.resolve(node.value)[1]:
            return {node.attr}, False
        return set(), False

    def refers_to(self, node: ast.expr, name: str) -> bool:
        return name in self.resolve(node)[0]

    def unbind(self, *names: str) -> None:
        for name in names:
            self.objects.pop(name, None)
            self.modules.discard(name)
            self.shadowed.add(name)

    def _bind_copy(self, target: ast.expr, value: ast.expr) -> None:
        """After `target = value`, a plain name target refers to whatever value may be."""
        attrs, is_module = self.resolve(value)
        self.unbind(*_stored_names(target))
        if isinstance(target, ast.Name):
            if attrs:
                self.objects[target.id] = attrs
            if is_module:
                self.modules.add(target.id)

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
        elif isinstance(statement, NEW_SCOPES):
            self.unbind(statement.name)
        elif isinstance(statement, ast.TypeAlias):
            self.unbind(statement.name.id)
        elif isinstance(statement, ast.Assign):
            for target in statement.targets:
                self._bind_copy(target, statement.value)
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            self._bind_copy(statement.target, statement.value)
        elif isinstance(statement, ast.AugAssign):
            self.unbind(*_stored_names(statement.target))


def _is_any(node: ast.expr, names: TypingNames, type_context: bool) -> bool:
    """Whether node is Any, or a union, Optional, or Annotated form of it.

    In a type context (a TypeAlias value, a `type` statement, or a typing subscript argument)
    a string is a forward reference and is parsed; elsewhere a string is a value.
    """
    if type_context and isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            parsed = ast.parse(node.value.strip(), mode="eval").body
        except SyntaxError:
            return False
        return _is_any(parsed, names, type_context=True)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _is_any(node.left, names, type_context) or _is_any(node.right, names, type_context)
    if isinstance(node, ast.Subscript):
        args = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
        if names.refers_to(node.value, "Annotated"):
            return bool(args) and _is_any(args[0], names, type_context=True)
        if names.refers_to(node.value, "Optional") or names.refers_to(node.value, "Union"):
            return any(_is_any(arg, names, type_context=True) for arg in args)
    return names.refers_to(node, "Any")


def _annotation(node: ast.expr) -> ast.expr:
    """An annotation, with a string annotation (`x: "TypeAlias"`) parsed."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            return ast.parse(node.value.strip(), mode="eval").body
        except SyntaxError:
            return node
    return node


def _aliases_in(statement: ast.stmt, names: TypingNames) -> Iterator[str]:
    if isinstance(statement, ast.Assign) and _is_any(statement.value, names, type_context=False):
        for target in statement.targets:
            if isinstance(target, ast.Name):
                yield target.id
    elif (
        isinstance(statement, ast.AnnAssign)
        and statement.value is not None
        and names.refers_to(_annotation(statement.annotation), "TypeAlias")
        and _is_any(statement.value, names, type_context=True)
        and isinstance(statement.target, ast.Name)
    ):
        yield statement.target.id
    elif isinstance(statement, ast.TypeAlias) and _is_any(
        statement.value, names, type_context=True
    ):
        yield statement.name.id


def _runtime_only_branch(test: ast.expr) -> str | None:
    """The branch of an `if` that only the runtime executes: "orelse", "body", or None.

    Type checkers read the `TYPE_CHECKING` side and treat the other as unreachable.
    """

    def is_type_checking(node: ast.expr) -> bool:
        return (isinstance(node, ast.Name) and node.id == "TYPE_CHECKING") or (
            isinstance(node, ast.Attribute) and node.attr == "TYPE_CHECKING"
        )

    if is_type_checking(test):
        return "orelse"
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return "body" if is_type_checking(test.operand) else None
    return None


def _walk(
    statements: list[ast.stmt],
    names: TypingNames,
    flag: bool,
    found: list[tuple[int, str]],
) -> TypingNames:
    """Follow module-scope bindings through statements; record aliases to Any where flagged."""
    for statement in statements:
        if flag:
            found.extend((statement.lineno, alias) for alias in _aliases_in(statement, names))
        if isinstance(statement, ast.If):
            runtime_only = _runtime_only_branch(statement.test)
            body = _walk(statement.body, names.copy(), flag and runtime_only != "body", found)
            orelse = _walk(statement.orelse, names.copy(), flag and runtime_only != "orelse", found)
            names = TypingNames.merge(body, orelse)
        elif isinstance(statement, ast.For | ast.AsyncFor | ast.While):
            entered = names.copy()
            if not isinstance(statement, ast.While):
                entered.unbind(*_stored_names(statement.target))
            body = _walk(statement.body, entered, flag, found)
            names = _walk(statement.orelse, TypingNames.merge(names, body), flag, found)
        elif isinstance(statement, ast.With | ast.AsyncWith):
            for item in statement.items:
                names.unbind(*_stored_names(item.optional_vars))
            names = _walk(statement.body, names, flag, found)
        elif isinstance(statement, ast.Try | ast.TryStar):
            body = _walk(statement.body, names.copy(), flag, found)
            branches = [_walk(statement.orelse, body.copy(), flag, found)]
            for handler in statement.handlers:
                entered = TypingNames.merge(names, body)
                if handler.name:
                    entered.unbind(handler.name)
                branches.append(_walk(handler.body, entered, flag, found))
            names = _walk(statement.finalbody, TypingNames.merge(*branches), flag, found)
        elif isinstance(statement, ast.Match):
            cases = [_walk(case.body, names.copy(), flag, found) for case in statement.cases]
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
