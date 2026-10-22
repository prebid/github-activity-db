"""Every third-party module the package imports is a declared dependency (redesign plan gap 76).

mypy, ruff, pytest, and the wheel job's `uv pip check` all pass when src/ imports a package that
is installed only because another dependency requires it, so this test reads the imports itself.
Installed metadata maps only top-level names to distributions, so an import is accepted only when
every distribution that installs its top-level name is declared: in a namespace package that
several distributions share, the metadata cannot tell which one provides a member, and a declared
sibling must not stand in for an undeclared provider.
"""

from __future__ import annotations

import ast
import os
import re
import sys
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import packages_distributions
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
# pyproject.toml as it was before httpx was declared (tests/fixtures/history/README.md).
BEFORE_HTTPX = ROOT / "tests" / "fixtures" / "history" / "ea7d12e" / "pyproject.toml"
FIRST_PARTY = "github_activity_db"
# sys.stdlib_module_names leaves out `__main__` and typeshed's type-checking-only `_typeshed`.
STANDARD_LIBRARY = sys.stdlib_module_names | {"__main__", "_typeshed"}
REQUIREMENT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


@dataclass(frozen=True, order=True)
class Undeclared:
    """A top-level name imported in `path` that no installed distribution provides, or whose
    installing distributions are not all declared."""

    path: str
    name: str
    providers: tuple[str, ...]


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _declared(pyproject: Path) -> set[str]:
    declared: set[str] = set()
    for requirement in tomllib.loads(pyproject.read_text())["project"]["dependencies"]:
        match = REQUIREMENT_NAME.match(requirement.strip())
        assert match is not None, requirement
        declared.add(_normalize(match.group()))
    return declared


def _imported_top_levels(path: Path) -> set[str]:
    """Absolute imports at any depth, `TYPE_CHECKING` blocks included."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_bytes())):
        if isinstance(node, ast.Import):
            names.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.partition(".")[0])
    return names


def undeclared_imports(
    src: Path, pyproject: Path, owners: Mapping[str, Sequence[str]] | None = None
) -> tuple[int, list[Undeclared]]:
    """The number of files scanned, and every import no declared dependency provides.

    ``owners`` maps top-level names to the distributions installing them; by default, the
    installed environment's answer.
    """
    declared = _declared(pyproject)
    if owners is None:
        owners = packages_distributions()
    files = sorted(src.rglob("*.py"))
    found: list[Undeclared] = []
    for path in files:
        for name in sorted(_imported_top_levels(path)):
            if name in STANDARD_LIBRARY or name == FIRST_PARTY:
                continue
            providers = tuple(sorted({_normalize(owner) for owner in owners.get(name, [])}))
            if not providers or not set(providers) <= declared:
                found.append(Undeclared(path.relative_to(src).as_posix(), name, providers))
    return len(files), found


def _explain(found: list[Undeclared]) -> str:
    return "\n".join(
        f"{item.path}: {item.name} is installed by "
        f"{', '.join(item.providers) or 'no installed distribution'}; declare "
        f"{'every one of them' if len(item.providers) > 1 else 'it'} in [project].dependencies"
        for item in found
    )


def _python_file_count(root: Path) -> int:
    """The .py files under ``root``, counted with os.walk rather than the glob."""
    return sum(Path(name).suffix == ".py" for _, _, names in os.walk(root) for name in names)


def _importers(name: str) -> list[str]:
    """The files under src/ with a statement importing ``name``, found in their text."""
    statement = re.compile(rf"^[ \t]*(?:from|import)[ \t]+{re.escape(name)}\b", re.MULTILINE)
    return sorted(
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if statement.search(path.read_text())
    )


def _requirement_names(pyproject: Path) -> set[str]:
    """The [project].dependencies names, read with packaging rather than this test's pattern."""
    return {
        canonicalize_name(Requirement(requirement).name)
        for requirement in tomllib.loads(pyproject.read_text())["project"]["dependencies"]
    }


def test_every_third_party_import_in_src_is_declared() -> None:
    scanned, found = undeclared_imports(SRC, ROOT / "pyproject.toml")
    assert scanned == _python_file_count(SRC)
    assert found == [], _explain(found)


def test_reports_what_an_earlier_pyproject_did_not_declare() -> None:
    """The dependencies before httpx was declared, read against src/: each file importing a
    distribution declared since then is reported."""
    added = _requirement_names(ROOT / "pyproject.toml") - _requirement_names(BEFORE_HTTPX)
    assert "httpx" in added
    expected = [
        Undeclared(path, name.replace("-", "_"), (name,))
        for name in added
        for path in _importers(name.replace("-", "_"))
    ]
    scanned, found = undeclared_imports(SRC, BEFORE_HTTPX)
    assert scanned == _python_file_count(SRC)
    assert sorted(found) == sorted(expected)


@pytest.mark.parametrize(
    ("installers", "reported"),
    [
        pytest.param(
            ["SQLAlchemy", "identify"],
            ("identify", "sqlalchemy"),
            id="a declared sibling does not stand in for an undeclared provider",
        ),
        pytest.param(["SQLAlchemy", "alembic"], None, id="every installing distribution declared"),
        pytest.param(None, (), id="no installed distribution"),
    ],
)
def test_a_name_several_distributions_install_needs_all_of_them_declared(
    installers: list[str] | None, reported: tuple[str, ...] | None
) -> None:
    # The installed environment's answer, with sqlalchemy installed by the distributions given,
    # as in a namespace several distributions share, or by none.
    owners = dict(packages_distributions())
    if installers is None:
        del owners["sqlalchemy"]
    else:
        owners["sqlalchemy"] = installers
    scanned, found = undeclared_imports(SRC, ROOT / "pyproject.toml", owners)
    assert scanned == _python_file_count(SRC)
    importers = _importers("sqlalchemy")
    assert importers
    assert sorted(found) == (
        [] if reported is None else [Undeclared(path, "sqlalchemy", reported) for path in importers]
    )
