"""Rules the test suite holds itself to (docs/testing.md, Principles).

Each wait on a subprocess has an explicit timeout (redesign plan Step 3): ``subprocess.run``,
``call``, ``check_call``, and ``check_output``, called as attributes of the module or by the
names ``from subprocess import ...`` binds. Out of scope: ``Popen`` objects, whose waits are
method calls on a value the scan cannot type.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
WAITING = frozenset({"run", "call", "check_call", "check_output"})


def untimed_subprocess_waits(*paths: Path) -> list[str]:
    found = []
    for path in paths:
        tree = ast.parse(path.read_bytes())
        names = {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "subprocess"
            for alias in node.names
            if alias.name in WAITING
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            waits = (
                isinstance(function, ast.Attribute)
                and function.attr in WAITING
                and isinstance(function.value, ast.Name)
                and function.value.id == "subprocess"
            ) or (isinstance(function, ast.Name) and function.id in names)
            if waits and not any(keyword.arg == "timeout" for keyword in node.keywords):
                found.append(f"{path.relative_to(path.parents[1])}:{node.lineno}")
    return found


def test_every_subprocess_wait_in_the_suite_has_a_timeout() -> None:
    files = sorted(TESTS.rglob("*.py"))
    assert len(files) > 30, f"scanned {len(files)} files"
    assert untimed_subprocess_waits(*files) == []


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ("import subprocess\nsubprocess.run(['true'])\n", True),
        ("import subprocess\nsubprocess.check_output(['true'], text=True)\n", True),
        ("from subprocess import run\nrun(['true'])\n", True),
        ("from subprocess import run as go\ngo(['true'])\n", True),
        ("import subprocess\nsubprocess.run(['true'], timeout=60)\n", False),
        ("def run(argv):\n    return argv\nrun(['true'])\n", False),
        ("import subprocess\nsubprocess.Popen(['true'])\n", False),
    ],
)
def test_flags_an_untimed_wait_and_nothing_else(tmp_path: Path, source: str, flagged: bool) -> None:
    (tmp_path / "pkg").mkdir()
    path = tmp_path / "pkg" / "module.py"
    path.write_text(source)
    assert bool(untimed_subprocess_waits(path)) is flagged
