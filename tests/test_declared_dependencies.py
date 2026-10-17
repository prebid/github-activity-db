"""Every third-party module the package imports is a declared dependency (redesign plan gap 76).

mypy, ruff, pytest, and the wheel job's `uv pip check` all pass when src/ imports a package that
is installed only because another dependency requires it, so this test reads the imports itself.
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from importlib.metadata import packages_distributions
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIRST_PARTY = "github_activity_db"
REQUIREMENT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _declared(pyproject: Path) -> set[str]:
    declared: set[str] = set()
    for requirement in tomllib.loads(pyproject.read_text())["project"]["dependencies"]:
        match = REQUIREMENT_NAME.match(requirement)
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


def undeclared_imports(src: Path, pyproject: Path) -> tuple[int, dict[str, list[str]]]:
    """The number of files scanned, and each undeclared module with the files importing it.

    A module no installed distribution provides counts as undeclared.
    """
    declared = _declared(pyproject)
    owners = packages_distributions()
    files = sorted(src.rglob("*.py"))
    found: dict[str, list[str]] = {}
    for path in files:
        for name in sorted(_imported_top_levels(path)):
            if name in sys.stdlib_module_names or name == FIRST_PARTY:
                continue
            if not {_normalize(owner) for owner in owners.get(name, [])} & declared:
                found.setdefault(name, []).append(path.relative_to(src).as_posix())
    return len(files), found


def test_every_third_party_import_in_src_is_declared() -> None:
    scanned, found = undeclared_imports(ROOT / "src", ROOT / "pyproject.toml")
    assert scanned > 0
    assert found == {}


def test_reports_undeclared_imports_in_a_planted_package(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "planted"\ndependencies = ["rich>=13.0", "Pydantic-Settings>=2"]\n'
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
    )
    (package / "b.py").write_text(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    import rich\n"
        "    import httpx.Timeout\n"
        "def f() -> None:\n"
        "    import not_an_installed_module_xyz\n"
        'DECOY = "import sqlalchemy"\n'
    )
    scanned, found = undeclared_imports(tmp_path / "src", tmp_path / "pyproject.toml")
    assert scanned == 2
    assert found == {
        "click": ["planted/a.py"],
        "httpx": ["planted/a.py", "planted/b.py"],
        "not_an_installed_module_xyz": ["planted/b.py"],
    }
