"""Rules the test suite holds itself to (docs/testing.md, Principles).

Each wait on a subprocess has an explicit timeout (redesign plan Step 3). The scan models the
``subprocess`` module imported under its own name: ``subprocess.run``, ``call``,
``check_call``, and ``check_output``, called as attributes of the module or by the names
``from subprocess import ...`` binds, pass a ``timeout`` that is not ``None``, and its exception
and constant names start nothing. Every other use of the module is refused rather than read: an
alias, a star import, ``Popen``, ``getoutput``, or the module or a waiting function passed as a
value. Out of scope: the module reached without an import statement (``importlib.import_module``,
``__import__``, ``sys.modules``), and process APIs outside ``subprocess`` (``os.system``,
``os.popen``, ``asyncio``'s subprocesses, ``multiprocessing``).
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

TESTS = Path(__file__).resolve().parent
WAITING = frozenset({"run", "call", "check_call", "check_output"})
# subprocess names that start no process.
INERT = frozenset(
    {
        "CalledProcessError",
        "CompletedProcess",
        "DEVNULL",
        "PIPE",
        "STDOUT",
        "SubprocessError",
        "TimeoutExpired",
    }
)


def _timed(call: ast.Call) -> bool:
    return any(
        keyword.arg == "timeout"
        and not (isinstance(keyword.value, ast.Constant) and keyword.value.value is None)
        for keyword in call.keywords
    )


def subprocess_problems(*paths: Path) -> list[str]:
    found = []
    for path in paths:
        tree = ast.parse(path.read_bytes())
        nodes = list(ast.walk(tree))
        where = path.relative_to(path.parents[1])
        waits: set[str] = set()
        for node in nodes:
            if isinstance(node, ast.Import):
                found += [
                    f"{where}:{node.lineno}: subprocess imported as {alias.asname}"
                    for alias in node.names
                    if alias.name == "subprocess" and alias.asname not in (None, "subprocess")
                ]
            elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
                for alias in node.names:
                    if alias.name in WAITING:
                        waits.add(alias.asname or alias.name)
                    elif alias.name not in INERT:
                        found.append(f"{where}:{node.lineno}: imports subprocess.{alias.name}")
        called = {id(node.func): node for node in nodes if isinstance(node, ast.Call)}
        bases = {id(node.value) for node in nodes if isinstance(node, ast.Attribute)}
        for node in nodes:
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "subprocess"
            ):
                if node.attr in INERT:
                    continue
                if node.attr not in WAITING or id(node) not in called:
                    found.append(f"{where}:{node.lineno}: subprocess.{node.attr}")
                elif not _timed(called[id(node)]):
                    found.append(f"{where}:{node.lineno}: no timeout")
            elif isinstance(node, ast.Name) and node.id == "subprocess" and id(node) not in bases:
                found.append(f"{where}:{node.lineno}: the subprocess module used as a value")
            elif isinstance(node, ast.Name) and node.id in waits:
                if id(node) not in called:
                    found.append(f"{where}:{node.lineno}: {node.id} used as a value")
                elif not _timed(called[id(node)]):
                    found.append(f"{where}:{node.lineno}: no timeout")
    return found


def test_every_subprocess_wait_in_the_suite_has_a_timeout() -> None:
    files = sorted(TESTS.rglob("*.py"))
    # Counted with os.walk rather than the glob, so the scan cannot pass on fewer files.
    assert len(files) == sum(
        Path(name).suffix == ".py" for _, _, names in os.walk(TESTS) for name in names
    )
    assert subprocess_problems(*files) == []
