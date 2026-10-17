"""Every third-party module the package imports is a declared dependency (redesign plan gap 76).

mypy, ruff, pytest, and the wheel job's `uv pip check` all pass when src/ imports a package that
is installed only because another dependency requires it, so this test reads the imports itself.
Each import resolves to the distributions that install that module, read from the files each
installed distribution records, so a declared distribution that shares a namespace package with
the real provider does not stand in for it.
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from importlib.metadata import Distribution, distributions
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIRST_PARTY = "github_activity_db"
# sys.stdlib_module_names leaves out `__main__` and typeshed's type-checking-only `_typeshed`.
STANDARD_LIBRARY = sys.stdlib_module_names | {"__main__", "_typeshed"}
REQUIREMENT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
MODULE_SUFFIXES = (".py", ".pyi", ".so", ".pyd")


@dataclass(frozen=True, order=True)
class Undeclared:
    """An import no declared dependency provides: no installed distribution provides the module,
    or not every distribution that provides it is declared."""

    path: str
    module: str
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


def _provided_modules(distribution: Distribution) -> Iterator[str]:
    """The packages and modules a distribution installs, as dotted names."""
    files = distribution.files
    if files is None:
        # No record of installed files: only the top-level names it declares are known.
        top_level = distribution.read_text("top_level.txt") or ""
        yield from (line.strip() for line in top_level.splitlines() if line.strip())
        return
    for file in files:
        parts = file.parts
        if not parts or parts[0] == ".." or parts[0].endswith((".dist-info", ".egg-info", ".data")):
            continue
        packages: list[str] = []
        for part in parts[:-1]:
            if part == "__pycache__" or not part.isidentifier():
                break
            packages.append(part)
            yield ".".join(packages)
        else:
            stem = parts[-1].partition(".")[0]
            if parts[-1].endswith(MODULE_SUFFIXES) and stem.isidentifier() and stem != "__init__":
                yield ".".join([*packages, stem])


def _module_owners(search_path: list[str] | None) -> dict[str, set[str]]:
    """Each dotted package or module name, with the distributions that install it."""
    found = distributions() if search_path is None else distributions(path=search_path)
    owners: dict[str, set[str]] = {}
    for distribution in found:
        name = distribution.name  # parses METADATA on every access
        if not name:
            continue
        for module in _provided_modules(distribution):
            owners.setdefault(module, set()).add(_normalize(name))
    return owners


def _imported_modules(path: Path, owners: dict[str, set[str]]) -> Iterator[str]:
    """Absolute imports at any depth, `TYPE_CHECKING` blocks included, as the module each names.

    `from package import name` names `package.name` when that is a module someone installs.
    """
    for node in ast.walk(ast.parse(path.read_bytes())):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for alias in node.names:
                member = f"{node.module}.{alias.name}"
                yield member if member in owners else node.module


def _providers(module: str, owners: dict[str, set[str]]) -> tuple[str, ...]:
    """The distributions installing the longest installed prefix of the module's dotted name."""
    parts = module.split(".")
    for end in range(len(parts), 0, -1):
        provided_by = owners.get(".".join(parts[:end]))
        if provided_by:
            return tuple(sorted(provided_by))
    return ()


def undeclared_imports(
    src: Path, pyproject: Path, search_path: list[str] | None = None
) -> tuple[int, list[Undeclared]]:
    """The number of files scanned, and every import no declared dependency provides.

    `search_path` replaces `sys.path` as the place installed distributions are found.
    """
    declared = _declared(pyproject)
    owners = _module_owners(search_path)
    files = sorted(src.rglob("*.py"))
    found: set[Undeclared] = set()
    for path in files:
        for module in _imported_modules(path, owners):
            top_level = module.partition(".")[0]
            if top_level in STANDARD_LIBRARY or top_level == FIRST_PARTY:
                continue
            providers = _providers(module, owners)
            if not providers or not set(providers) <= declared:
                found.add(Undeclared(path.relative_to(src).as_posix(), module, providers))
    return len(files), sorted(found)


def _explain(found: list[Undeclared]) -> str:
    return "\n".join(
        f"{item.path}: {item.module} is installed by "
        f"{', '.join(item.providers) or 'no installed distribution'}; declare its provider in "
        "[project].dependencies, or import the member from its own module"
        for item in found
    )


def test_every_third_party_import_in_src_is_declared() -> None:
    scanned, found = undeclared_imports(ROOT / "src", ROOT / "pyproject.toml")
    assert scanned > 0
    assert found == [], _explain(found)


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
        Undeclared("planted/a.py", "click.exceptions", ("click",)),
        Undeclared("planted/a.py", "httpx", ("httpx",)),
        Undeclared("planted/b.py", "httpx.Timeout", ("httpx",)),
        Undeclared("planted/b.py", "not_an_installed_module_xyz", ()),
    ]


def _install(site: Path, name: str, files: dict[str, str], *, record: bool = True) -> None:
    """A distribution as an installer leaves it: its files, METADATA, and RECORD."""
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


def _shared_namespace(tmp_path: Path, source: str, declared: str) -> list[Undeclared]:
    """Two distributions that each install one member of the namespace package review_namespace."""
    site = tmp_path / "site"
    _install(site, "review-alpha", {"review_namespace/alpha/__init__.py": 'PROVIDER = "a"\n'})
    _install(site, "review-beta", {"review_namespace/beta/__init__.py": 'PROVIDER = "b"\n'})
    _install(site, "legacy-thing", {"legacy_thing/__init__.py": ""}, record=False)
    (tmp_path / "pyproject.toml").write_text(f'[project]\nname = "x"\ndependencies = {declared}\n')
    package = tmp_path / "src" / "app"
    package.mkdir(parents=True)
    (package / "m.py").write_text(source)
    scanned, found = undeclared_imports(
        tmp_path / "src", tmp_path / "pyproject.toml", search_path=[str(site)]
    )
    assert scanned == 1
    return found


@pytest.mark.parametrize(
    ("source", "declared", "expected"),
    [
        pytest.param(
            "from review_namespace.beta import PROVIDER\n",
            '["review-alpha"]',
            [Undeclared("app/m.py", "review_namespace.beta", ("review-beta",))],
            id="a declared sibling does not stand in for the provider",
        ),
        pytest.param(
            "from review_namespace.beta import PROVIDER\n", '["review-beta"]', [], id="provider"
        ),
        pytest.param(
            "from review_namespace import beta\n", '["review-beta"]', [], id="member import"
        ),
        pytest.param(
            "import review_namespace\n",
            '["review-alpha"]',
            [Undeclared("app/m.py", "review_namespace", ("review-alpha", "review-beta"))],
            id="the bare namespace is ambiguous",
        ),
        pytest.param(
            "import review_namespace\n", '["review-alpha", "review-beta"]', [], id="both declared"
        ),
        pytest.param(
            "import legacy_thing.sub\n",
            '["review-alpha"]',
            [Undeclared("app/m.py", "legacy_thing.sub", ("legacy-thing",))],
            id="no RECORD, top_level.txt",
        ),
    ],
)
def test_resolves_imports_to_the_distribution_that_installs_the_module(
    tmp_path: Path, source: str, declared: str, expected: list[Undeclared]
) -> None:
    assert _shared_namespace(tmp_path, source, declared) == expected
