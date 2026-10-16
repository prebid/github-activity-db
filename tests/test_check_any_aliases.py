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
]
NOT_ALIASES = [
    "from typing import Any\n\nFoo = dict[str, Any]\n",
    "from typing import AnyStr\n\nFoo = AnyStr\n",
    "from typing import Any\n\nvalue: Any = 1\n",
    "from typing import Any\n\n\ndef f() -> None:\n    Foo = Any\n",
    "from typing import Any\n\n\nclass C:\n    Foo = Any\n",
    'TEXT = "Foo = Any"\n',
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
