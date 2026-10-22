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
def test_src_s_imports_of_a_name_several_distributions_install_need_all_of_them_declared(
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


def test_reports_undeclared_imports_in_a_planted_package(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "planted"\ndependencies = [" rich>=13.0", "Pydantic-Settings>=2"]\n'
    )
    package = tmp_path / "src" / "planted"
    package.mkdir(parents=True)
    (package / "a.py").write_text(
        "from __future__ import annotations\n"
        "import json\n"
        "import httpx\n"
        "from click.exceptions import UsageError\n"
        "from pydantic_settings import BaseSettings\n"
        "from . import b\n"
        "from github_activity_db import config\n"
        "import __main__\n"
    )
    (package / "b.py").write_text(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    import rich\n"
        "    import httpx.Timeout\n"
        "    from _typeshed import StrPath\n"
        "def f() -> None:\n"
        "    import not_an_installed_module_xyz\n"
        'DECOY = "import sqlalchemy"\n'
    )
    scanned, found = undeclared_imports(tmp_path / "src", tmp_path / "pyproject.toml")
    assert scanned == 2
    assert found == [
        Undeclared("planted/a.py", "click", ("click",)),
        Undeclared("planted/a.py", "httpx", ("httpx",)),
        Undeclared("planted/b.py", "httpx", ("httpx",)),
        Undeclared("planted/b.py", "not_an_installed_module_xyz", ()),
    ]


def _install(site: Path, name: str, files: dict[str, str], *, record: bool = True) -> None:
    """A distribution as an installer leaves it: its files, METADATA, and RECORD (or, without
    RECORD, top_level.txt)."""
    for relative, text in files.items():
        (site / relative).parent.mkdir(parents=True, exist_ok=True)
        (site / relative).write_text(text)
    info = site / f"{name.replace('-', '_')}-1.0.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n")
    if record:
        rows = [*files, f"{info.name}/METADATA", f"{info.name}/RECORD"]
        (info / "RECORD").write_text("".join(f"{row},,\n" for row in rows))
    else:
        (info / "top_level.txt").write_text(f"{name.replace('-', '_')}\n")


@pytest.mark.parametrize(
    ("source", "declared", "expected"),
    [
        pytest.param(
            "from review_namespace.beta import PROVIDER\n",
            '["review-alpha"]',
            [Undeclared("app/m.py", "review_namespace", ("review-alpha", "review-beta"))],
            id="a declared sibling does not stand in for the provider",
        ),
        pytest.param(
            "from review_namespace.beta import PROVIDER\n",
            '["review-beta"]',
            [Undeclared("app/m.py", "review_namespace", ("review-alpha", "review-beta"))],
            id="a namespace shared with an undeclared distribution is ambiguous",
        ),
        pytest.param(
            "from review_namespace import beta\nimport review_namespace.alpha\n",
            '["review-alpha", "review-beta"]',
            [],
            id="every installing distribution declared",
        ),
        pytest.param(
            "import legacy_thing.sub\n",
            '["review-alpha"]',
            [Undeclared("app/m.py", "legacy_thing", ("legacy-thing",))],
            id="no RECORD, top_level.txt",
        ),
        pytest.param("import legacy_thing\n", '["legacy-thing"]', [], id="no RECORD, declared"),
    ],
)
def test_a_name_several_distributions_install_needs_all_of_them_declared(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    declared: str,
    expected: list[Undeclared],
) -> None:
    site = tmp_path / "site"
    _install(site, "review-alpha", {"review_namespace/alpha/__init__.py": 'PROVIDER = "a"\n'})
    _install(site, "review-beta", {"review_namespace/beta/__init__.py": 'PROVIDER = "b"\n'})
    _install(site, "legacy-thing", {"legacy_thing/__init__.py": ""}, record=False)
    monkeypatch.syspath_prepend(str(site))
    (tmp_path / "pyproject.toml").write_text(f'[project]\nname = "x"\ndependencies = {declared}\n')
    package = tmp_path / "src" / "app"
    package.mkdir(parents=True)
    (package / "m.py").write_text(source)
    assert undeclared_imports(tmp_path / "src", tmp_path / "pyproject.toml") == (1, expected)
