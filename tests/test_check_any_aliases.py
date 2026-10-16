"""The pre-commit check that rejects module-level type aliases to Any."""

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_any_aliases.py"


def _run(tmp_path: Path, source: str, name: str = "module.py") -> subprocess.CompletedProcess[str]:
    root = tmp_path / "pkg"
    root.mkdir()
    (root / name).write_text(source)
    return subprocess.run([sys.executable, str(SCRIPT), str(root)], capture_output=True, text=True)


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
    # An alias inside a TYPE_CHECKING block is the recommended form.
    "from typing import TYPE_CHECKING, Any\n\nif TYPE_CHECKING:\n    Foo = Any\n",
]


@pytest.mark.parametrize("source", ALIASES)
def test_rejects_an_alias_to_any(tmp_path: Path, source: str) -> None:
    result = _run(tmp_path, source)
    assert result.returncode == 1
    assert "aliases to Any: 1" in result.stdout
    assert "Foo is an alias to Any" in result.stdout


@pytest.mark.parametrize("source", NOT_ALIASES)
def test_accepts_other_uses_of_any(tmp_path: Path, source: str) -> None:
    result = _run(tmp_path, source)
    assert result.returncode == 0, result.stdout
    assert "aliases to Any: 0" in result.stdout


def test_scans_stub_files(tmp_path: Path) -> None:
    result = _run(tmp_path, "from typing import Any\n\nFoo = Any\n", name="stub.pyi")
    assert result.returncode == 1
    assert result.stdout.startswith("scanned 1 files")


def test_no_python_files_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path / "empty")], capture_output=True, text=True
    )
    assert result.returncode == 1
    assert "no Python files" in result.stderr
