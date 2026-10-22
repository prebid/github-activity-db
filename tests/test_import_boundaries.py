"""Import boundaries (redesign plan 5.7).

Only ``source/`` imports githubkit: the scan covers ``src/`` outside ``source/``, ``scripts/``,
and ``alembic/``, imports at any scope (``TYPE_CHECKING`` blocks and function bodies included),
and ``import_module`` and ``__import__`` called with a string literal, as an attribute
(``importlib.import_module``, ``builtins.__import__``), by name, or under the alias a
``from importlib import import_module as load`` gives it, with the name as the first argument
or ``name=``. Out of scope: a callable rebound by assignment, a name built at run time,
``runpy``, and ``exec``/``eval``. Tests are exempt: they build
githubkit's own errors.

``source/`` imports neither SQLAlchemy nor ``github_activity_db.db``, relative imports included,
so fetchers cannot touch the database.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.support import files_under

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "github_activity_db"
GITHUBKIT_STATEMENT = re.compile(r"^[ \t]*(?:from|import)[ \t]+githubkit\b", re.MULTILINE)
DATABASE_STATEMENT = re.compile(
    r"^[ \t]*(?:from|import)[ \t]+(?:sqlalchemy|github_activity_db\.db)\b", re.MULTILINE
)


def _python_files(*roots: Path, excluding: Path | None = None) -> list[Path]:
    files = []
    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.suffix not in {".py", ".pyi"}:
                continue
            if excluding is not None and path.is_relative_to(excluding):
                continue
            files.append(path)
    return files


def _package_of(path: Path, src: Path) -> list[str]:
    """The dotted package a module under ``src`` belongs to, as its parts.

    Relative imports in a package's ``__init__`` resolve against the package itself, and in any
    other module against its package: in both cases the module's directory.
    """
    return list(path.relative_to(src).with_suffix("").parts[:-1])


def imported_modules(path: Path, src: Path | None = None) -> Iterator[tuple[int, str]]:
    """Every module the file imports, as an absolute dotted name, with its line.

    Relative imports resolve against the file's package when ``src`` (the directory above the
    top-level package) is given; otherwise they are skipped.
    """
    tree = ast.parse(path.read_bytes())
    # Names the dynamic importers are bound to in this file, aliases included.
    importers = {"import_module", "__import__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in ("importlib", "builtins"):
            importers |= {
                alias.asname or alias.name
                for alias in node.names
                if alias.name in ("import_module", "__import__")
            }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module or ""
            elif src is not None:
                package = _package_of(path, src)
                anchor = package[: len(package) - node.level + 1]
                base = ".".join([*anchor, *([node.module] if node.module else [])])
            else:
                continue
            yield node.lineno, base
            for alias in node.names:
                yield node.lineno, f"{base}.{alias.name}"
        elif isinstance(node, ast.Call):
            named = [keyword.value for keyword in node.keywords if keyword.arg == "name"]
            target = node.args[0] if node.args else (named[0] if named else None)
            is_importer = (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in ("import_module", "__import__")
            ) or (isinstance(node.func, ast.Name) and node.func.id in importers)
            if is_importer and isinstance(target, ast.Constant) and isinstance(target.value, str):
                yield node.lineno, target.value


def _names(module: str, prefixes: tuple[str, ...]) -> bool:
    return any(module == prefix or module.startswith(f"{prefix}.") for prefix in prefixes)


def githubkit_imports(files: list[Path]) -> list[str]:
    return [
        f"{path}:{line}: {module}"
        for path in files
        for line, module in imported_modules(path)
        if _names(module, ("githubkit",))
    ]


def database_imports(files: list[Path], src: Path) -> list[str]:
    return [
        f"{path}:{line}: {module}"
        for path in files
        for line, module in imported_modules(path, src)
        if _names(module, ("sqlalchemy", "github_activity_db.db"))
    ]


def _file_count(root: Path) -> int:
    return len(files_under(root, {".py", ".pyi"}))


def _import_lines(path: Path, *, relative: bool) -> set[int]:
    """The first line of each import in ``path``, read with tokenize rather than ast: a statement
    holds one ``import`` keyword, ``from`` followed by ``.`` starts a relative one, and
    ``import_module`` or ``__import__`` is called with a string literal first or as ``name=``."""
    tokens = [
        token
        for token in tokenize.tokenize(io.BytesIO(path.read_bytes()).readline)
        if token.type not in (tokenize.NL, tokenize.COMMENT)
    ]
    separators = (tokenize.ENCODING, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT)
    lines: set[int] = set()
    start = 0
    for index, token in enumerate(tokens):
        if token.type in separators or token.string in (";", ":"):
            start = index + 1
        elif token.type == tokenize.NAME and token.string == "import":
            words = [word.string for word in tokens[start:index]]
            if relative or not (words[:1] == ["from"] and words[1].startswith(".")):
                lines.add(tokens[start].start[0])
        elif token.type == tokenize.NAME and token.string in ("import_module", "__import__"):
            call = [
                "literal" if word.type == tokenize.STRING else word.string
                for word in tokens[index + 1 : index + 5]
            ]
            if call[:2] == ["(", "literal"] or call == ["(", "name", "=", "literal"]:
                lines.add(token.start[0])
    return lines


def _scanned_lines(files: list[Path], src: Path | None = None) -> set[tuple[Path, int]]:
    return {(path, line) for path in files for line, _ in imported_modules(path, src)}


def _second_reading(files: list[Path], *, relative: bool) -> set[tuple[Path, int]]:
    return {(path, line) for path in files for line in _import_lines(path, relative=relative)}


def _sites(found: list[str]) -> set[tuple[Path, int]]:
    """The file and line of each finding, ``path:line: module``."""
    sites = set()
    for finding in found:
        path, _, line = finding.rpartition(": ")[0].rpartition(":")
        sites.add((Path(path), int(line)))
    return sites


def _statement_sites(files: list[Path], statement: re.Pattern[str]) -> set[tuple[Path, int]]:
    """The file and line of each statement the pattern matches in the text of ``files``."""
    sites = set()
    for path in files:
        text = path.read_text()
        sites |= {
            (path, text.count("\n", 0, match.start()) + 1) for match in statement.finditer(text)
        }
    return sites


def test_only_source_imports_githubkit() -> None:
    roots = (PACKAGE, ROOT / "scripts", ROOT / "alembic")
    files = _python_files(*roots, excluding=PACKAGE / "source")
    assert len(files) == sum(map(_file_count, roots)) - _file_count(PACKAGE / "source")
    assert _scanned_lines(files) == _second_reading(files, relative=False)
    assert githubkit_imports(files) == []


def test_source_does_not_import_the_database() -> None:
    files = _python_files(PACKAGE / "source")
    assert len(files) == _file_count(PACKAGE / "source")
    assert _scanned_lines(files, ROOT / "src") == _second_reading(files, relative=True)
    assert database_imports(files, ROOT / "src") == []


def test_the_scan_finds_each_githubkit_import_in_source() -> None:
    """source/ is allowed githubkit: the matcher finds each statement that imports it, and
    source/ holds 5, so the comparison cannot pass on none."""
    files = _python_files(PACKAGE / "source")
    sites = _sites(githubkit_imports(files))
    assert sites == _statement_sites(files, GITHUBKIT_STATEMENT)
    assert len(sites) == 5


def test_the_scan_finds_each_database_import_in_sync() -> None:
    """sync/ is allowed SQLAlchemy and db/: the matcher finds each statement that imports them,
    and sync/ holds 20, so the comparison cannot pass on none."""
    files = _python_files(PACKAGE / "sync")
    sites = _sites(database_imports(files, ROOT / "src"))
    assert sites == _statement_sites(files, DATABASE_STATEMENT)
    assert len(sites) == 20


def test_the_scan_sees_source_importing_githubkit() -> None:
    """Positive control: the same matcher finds the imports source/ is allowed to make."""
    assert githubkit_imports(_python_files(PACKAGE / "source"))


@pytest.mark.parametrize(
    "source",
    [
        "import githubkit\n",
        "import githubkit.versions as versions\n",
        "from githubkit import GitHub\n",
        "from githubkit.exception import RequestFailed\n",
        "def f():\n    from githubkit import GitHub\n",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from githubkit import GitHub\n",
        "import importlib\nimportlib.import_module('githubkit')\n",
        "from importlib import import_module\nimport_module('githubkit')\n",
        "from importlib import import_module as load\nload('githubkit')\n",
        "import builtins\nbuiltins.__import__('githubkit')\n",
        "from builtins import __import__ as imp\nimp('githubkit')\n",
        "import importlib\nimportlib.import_module(name='githubkit')\n",
        "__import__(name='githubkit')\n",
        "__import__('githubkit.versions')\n",
        "class C:\n    import githubkit\n",
    ],
)
def test_flags_every_githubkit_import_spelling(tmp_path: Path, source: str) -> None:
    (tmp_path / "module.py").write_text(source)
    assert len(githubkit_imports(_python_files(tmp_path))) >= 1


@pytest.mark.parametrize(
    "source",
    [
        'DECOY = "import githubkit"\n',
        "import githubkit_extensions\n",
        "# import githubkit\n",
        "import importlib\nname = 'githubkit'\nimportlib.import_module(name)\n",
        "def load(name):\n    return name\nload('githubkit')\n",
    ],
)
def test_does_not_flag_other_text(tmp_path: Path, source: str) -> None:
    (tmp_path / "module.py").write_text(source)
    assert githubkit_imports(_python_files(tmp_path)) == []


@pytest.mark.parametrize(
    ("name", "source", "flagged"),
    [
        ("transport.py", "import sqlalchemy\n", True),
        ("transport.py", "from sqlalchemy.orm import Session\n", True),
        ("transport.py", "from github_activity_db.db import models\n", True),
        ("transport.py", "from github_activity_db.db.engine import get_session\n", True),
        ("transport.py", "from ..db import engine\n", True),
        ("transport.py", "from .. import db\n", True),
        ("transport.py", "from .records import Account\n", False),
        ("transport.py", "from ..tracking import load_tracking_config\n", False),
        ("transport.py", "import sqlalchemy_utils\n", False),
    ],
)
def test_flags_database_imports_from_source(
    tmp_path: Path, name: str, source: str, flagged: bool
) -> None:
    package = tmp_path / "github_activity_db" / "source"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / name).write_text(source)
    found = database_imports(_python_files(package), tmp_path)
    assert bool(found) is flagged, found
