"""The pre-commit check that rejects module-level type aliases to Any."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_any_aliases.py"
SRC = ROOT / "src"


ALIASES = [
    "from typing import Any\n\nFoo = Any\n",
    "import typing\n\nFoo = typing.Any\n",
    "from typing import Any\n\nFoo = Any | None\n",
    "from typing import Any, TypeAlias\n\nFoo: TypeAlias = Any\n",
    "import typing\n\nFoo: typing.TypeAlias = typing.Any\n",
    "from typing import Any\n\ntype Foo = Any\n",
    "import typing\n\ntype Foo = typing.Any\n",
    "from typing import Any\n\ntype Foo[T] = Any\n",
    "from typing import Any\n\nFoo = None | Any\n",
    "from typing import Any, Optional\n\nFoo = Optional[Any]\n",
    "import typing\n\nFoo = typing.Union[int, typing.Any]\n",
    'from typing import Annotated, Any\n\nFoo = Annotated[Any, "x"]\n',
    "from typing import Any as Dynamic\n\ntype Foo = Dynamic\n",
    "from typing import Any, Optional as Maybe\n\nFoo = Maybe[Any]\n",
    "from typing import Any, Union as Either\n\nFoo = Either[int, Any]\n",
    'from typing import Annotated as Note, Any\n\nFoo = Note[Any, "x"]\n',
    "from typing import Any, TypeAlias as Alias\n\nFoo: Alias = Any\n",
    "import typing_extensions as te\n\nFoo = te.Any\n",
    "from typing import *\n\nFoo = Any\n",
    # Imports in function and class bodies bind other scopes and do not replace this one.
    "from typing import Any as Dynamic\n\n\ndef convert(value: object) -> int:\n"
    '    from typing import cast as Dynamic\n    return Dynamic("int", value)\n\n\n'
    "type Foo = Dynamic\n",
    "from typing import Any as Dynamic\n\n\nclass C:\n    from typing import cast as Dynamic\n\n\n"
    "Foo = Dynamic\n",
    # Imports in module-level blocks run in module scope.
    "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n"
    "    from typing import Any as Dynamic\n\n"
    "Foo = Dynamic\n",
    "try:\n    from typing import Any as Dynamic\nexcept ImportError:\n    pass\n\nFoo = Dynamic\n",
    # The latest module-level binding wins.
    "class Any:\n    pass\n\n\nfrom typing import Any\n\nFoo = Any\n",
    # Where branches disagree, a name that may be typing.Any counts.
    "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    from typing import Any as JSON\n"
    "else:\n    JSON = object\n\nFoo = JSON\n",
    "import sys\n\nif sys.version_info >= (3, 12):\n    from mylib import D\nelse:\n"
    "    from typing import Any as D\n\nFoo = D\n",
    "try:\n    from mylib import D\nexcept ImportError:\n"
    "    from typing import Any as D\n\nFoo = D\n",
    "from typing import Any as D\n\nfor D in []:\n    pass\n\nFoo = D\n",
    "from typing import Any as D\n\nmatch 1:\n    case 1:\n        D = int\n\nFoo = D\n",
    # String forward references where a type is expected.
    'from typing import Any, TypeAlias\n\nFoo: TypeAlias = "Any"\n',
    'from typing import Any\n\ntype Foo = "Any"\n',
    'from typing import Any, Optional\n\nFoo = Optional["Any"]\n',
    'from typing import Any, Optional, TypeAlias\n\nFoo: TypeAlias = "Optional[Any]"\n',
    # x[k] = ... and x.attr = ... read x; they do not rebind it.
    'from typing import Any\n\nHANDLERS: dict[object, str] = {}\nHANDLERS[Any] = "any"\n'
    "Foo = Any\n",
    'import typing as t\n\nt.__doc__ = "patched"\nFoo = t.Any\n',
    "from typing import Any\n\nCOUNTS: dict[object, int] = {}\nCOUNTS[Any] += 1\nFoo = Any\n",
    # Aliases in module-level blocks other than an `if TYPE_CHECKING:` body.
    "from typing import Any\n\ntry:\n    Foo = Any\nexcept ImportError:\n    pass\n",
    "import contextlib\nfrom typing import Any\n\nwith contextlib.suppress(Exception):\n"
    "    Foo = Any\n",
    "from typing import Any\n\nfor _ in range(1):\n    Foo = Any\n",
    "from typing import Any\n\nwhile True:\n    Foo = Any\n    break\n",
    "from typing import Any\n\nmatch 1:\n    case 1:\n        Foo = Any\n",
    "import sys\nfrom typing import Any\n\nif sys.version_info >= (3, 12):\n    Foo = Any\n",
    # Assignments that copy a typing module or object.
    "import typing\n\nt = typing\nFoo = t.Any\n",
    # Each binding rule.
    "from typing import *\n\n\nclass Any:\n    pass\n\n\nfrom typing import *\n\nFoo = Any\n",
    "from typing import Any as D\n\nwhile False:\n    D = int\n\nFoo = D\n",
    "try:\n    pass\nfinally:\n    from typing import Any as D\n\nFoo = D\n",
    "try:\n    from mylib import D\nexcept* ImportError:\n    from typing import Any as D\n\n"
    "Foo = D\n",
    "from mylib import D\n\nmatch 1:\n    case 1:\n        from typing import Any as D\n\n"
    "Foo = D\n",
    "import sys\nfrom typing import *\n\nif sys.version_info >= (3, 12):\n\n    class Any:\n"
    "        pass\n\n\nFoo = Any\n",
    # A type checker reads an `if TYPE_CHECKING:` body and the else of `if not TYPE_CHECKING:`.
    "from typing import TYPE_CHECKING, Any\n\nif TYPE_CHECKING:\n    Foo = Any\n",
    "from typing import TYPE_CHECKING, Any\n\nif not TYPE_CHECKING:\n    pass\nelse:\n"
    "    Foo = Any\n",
    # A quoted TypeAlias annotation, a copied typing object, and a string Annotated argument.
    'from typing import Any, TypeAlias\n\nFoo: "TypeAlias" = Any\n',
    "import typing\nfrom typing import Any\n\nO = typing.Optional\nFoo = O[Any]\n",
    'from typing import Annotated, Any\n\nFoo = Annotated["Any", "x"]\n',
    # An annotated subscript target reads its names.
    "from typing import Any\n\nCOUNTS: dict[object, int] = {}\nCOUNTS[Any]: int = 2\nFoo = Any\n",
]

NOT_ALIASES = [
    "from typing import Any\n\nFoo = dict[str, Any]\n",
    'from typing import Annotated, Any\n\nFoo = Annotated[int, Any, "x"]\n',
    "from typing import Any, Optional\n\nFoo = Optional[list[Any]]\n",
    "from typing import AnyStr\n\nFoo = AnyStr\n",
    "from typing import Any\n\nvalue: Any = 1\n",
    "from typing import Any\n\n\ndef f() -> None:\n    Foo = Any\n",
    "from typing import Any\n\n\nclass C:\n    Foo = Any\n",
    'TEXT = "Foo = Any"\n',
    "class Any:\n    pass\n\n\ntype Foo = Any\n",
    "from mylib import Any\n\nFoo = Any\n",
    "import mylib\n\nFoo = mylib.Any\n",
    # A function-local typing import does not make the module's own class typing.Any.
    "class Any:\n    pass\n\n\ndef f() -> None:\n    from typing import Any\n\n\ntype Foo = Any\n",
    # A later module-level binding replaces the typing import.
    "from typing import Any\n\n\nclass Any:\n    pass\n\n\nFoo = Any\n",
    "from typing import Any as Dynamic\n\nDynamic = int\nFoo = Dynamic\n",
    "from typing import Any\nfrom mylib import Any\n\nFoo = Any\n",
    "import typing as t\nimport mylib as t\n\nFoo = t.Any\n",
    "from typing import *\n\n\nclass Any:\n    pass\n\n\nFoo = Any\n",
    "import contextlib\nfrom typing import Any as D\n\nwith contextlib.nullcontext(1) as D:\n"
    "    pass\n\nFoo = D\n",
    # A string outside a type position is a value, and an unparsable reference is skipped.
    'from typing import Any\n\nFoo = "Any"\n',
    'from typing import Any, Literal\n\nFoo = Literal["Any"]\n',
    'from typing import TypeAlias\n\nFoo: TypeAlias = "not ( valid"\n',
    # Each binding rule.
    "from .typing import Any\n\nFoo = Any\n",
    "from typing import Any as D\n\ntype D = int\nFoo = D\n",
    "from typing import Any as D\n\nD += 1\nFoo = D\n",
    "from typing import Any as D\n\nD: int = 1\nFoo = D\n",
    "from typing import Any as D\n\ntry:\n    pass\nexcept Exception as D:\n    Foo = D\n",
    "from typing import Any\n\nfor Any in [1]:\n    Foo = Any\n",
    # The branch only the runtime executes is exempt.
    "from typing import TYPE_CHECKING, Any\n\nif TYPE_CHECKING:\n    pass\nelse:\n    Foo = Any\n",
    "from typing import TYPE_CHECKING, Any\n\nif not TYPE_CHECKING:\n    Foo = Any\n",
    # The TYPE_CHECKING pattern: a precise type for the checker, Any for the runtime.
    "from typing import TYPE_CHECKING, Any\n\nif TYPE_CHECKING:\n    from mylib import Model\n\n"
    "    Foo = Model\nelse:\n    Foo = Any\n",
    # The attribute form `typing.TYPE_CHECKING` marks the runtime-only else as well.
    "import typing\nfrom typing import Any\n\nif typing.TYPE_CHECKING:\n    pass\nelse:\n"
    "    Foo = Any\n",
]


def _run(*paths: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *map(str, paths)],
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", "")},
        timeout=60,
    )


def _run_on_source(
    tmp_path: Path, source: str, name: str = "module.py"
) -> subprocess.CompletedProcess[str]:
    """The check over a package of one module holding ``source``."""
    root = tmp_path / "pkg"
    root.mkdir()
    (root / name).write_text(source)
    return _run(root)


def _python_file_count(root: Path) -> int:
    """The .py and .pyi files under ``root``, counted with os.walk rather than the script's glob."""
    return sum(
        Path(name).suffix in {".py", ".pyi"} for _, _, names in os.walk(root) for name in names
    )


def test_src_has_no_alias_to_any() -> None:
    result = _run(SRC)
    assert result.returncode == 0, result.stdout
    scanned = re.search(r"^scanned (\d+) files", result.stdout, re.MULTILINE)
    found = re.search(r"^aliases to Any: (\d+)$", result.stdout, re.MULTILINE)
    assert scanned is not None and found is not None, result.stdout
    assert int(scanned.group(1)) == _python_file_count(SRC)
    assert int(found.group(1)) == 0


def test_no_python_files_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    result = _run(tmp_path / "empty")
    assert result.returncode == 1
    # A run that finds an alias also exits 1, but only after reporting what it scanned; an
    # uncaught exception would print a traceback instead of the one refusal line.
    assert result.stdout == ""
    assert result.stderr.splitlines() == [f"no Python files under {tmp_path / 'empty'}"]


@pytest.mark.parametrize("source", ALIASES)
def test_rejects_an_alias_to_any(tmp_path: Path, source: str) -> None:
    result = _run_on_source(tmp_path, source)
    assert result.returncode == 1
    assert "aliases to Any: 1" in result.stdout
    assert "Foo is an alias to Any" in result.stdout


@pytest.mark.parametrize("source", NOT_ALIASES)
def test_accepts_other_uses_of_any(tmp_path: Path, source: str) -> None:
    result = _run_on_source(tmp_path, source)
    assert result.returncode == 0, result.stdout
    assert "aliases to Any: 0" in result.stdout


def test_scans_stub_files(tmp_path: Path) -> None:
    result = _run_on_source(tmp_path, "from typing import Any\n\nFoo = Any\n", name="stub.pyi")
    assert result.returncode == 1
    assert result.stdout.startswith("scanned 1 files")
