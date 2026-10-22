"""No code in ``src/`` reads through a server-side cursor (redesign plan gap 77).

In rollback-journal mode a read left half-consumed on one connection blocks another
connection's commit until the busy timeout; through the aiosqlite dialect only a server-side
cursor leaves a result half-read. The scan flags, in every ``.py`` file under ``src/``: a call
of an attribute named ``stream``, ``stream_scalars``, or ``yield_per``; a ``stream_results``
keyword whose value is not the constant ``False``; a ``yield_per`` keyword; and a dict literal
with a ``"stream_results"`` or ``"yield_per"`` key. Out of scope: ``getattr`` with a string, a
method rebound to another name, options built at run time, and reads through a raw DBAPI cursor
(``sqlite3``, ``.connection.cursor()``, ``driver_connection``), whose half-read results block a
commit as well.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.support import files_under

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
CALLS = {"stream", "stream_scalars", "yield_per"}
OPTIONS = {"stream_results", "yield_per"}


def server_side_cursors(files: list[Path]) -> list[str]:
    found: list[str] = []
    for path in files:
        for node in ast.walk(ast.parse(path.read_bytes())):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute) and node.func.attr in CALLS:
                    found.append(f"{path}:{node.lineno}: .{node.func.attr}()")
                for keyword in node.keywords:
                    if keyword.arg == "yield_per" or (
                        keyword.arg == "stream_results"
                        and not (
                            isinstance(keyword.value, ast.Constant) and keyword.value.value is False
                        )
                    ):
                        found.append(f"{path}:{node.lineno}: {keyword.arg}=")
            elif isinstance(node, ast.Dict):
                for key in node.keys:
                    if isinstance(key, ast.Constant) and key.value in OPTIONS:
                        found.append(f"{path}:{node.lineno}: {key.value!r} key")
    return found


def test_no_server_side_cursor_in_src() -> None:
    files = sorted(SRC.rglob("*.py"))
    assert len(files) == len(files_under(SRC, {".py"}))
    assert SRC / "github_activity_db" / "sync" / "snapshot.py" in files
    assert server_side_cursors(files) == []


@pytest.mark.parametrize(
    "source",
    [
        "async def f(c, q):\n    return await c.stream(q)\n",
        "async def f(s, q):\n    return await s.stream_scalars(q)\n",
        "def f(r):\n    return r.yield_per(100)\n",
        "def f(q):\n    return q.execution_options(yield_per=100)\n",
        "def f(q):\n    return q.execution_options(stream_results=True)\n",
        "def f(q, flag):\n    return q.execution_options(stream_results=flag)\n",
        "def f(q):\n    return q.execution_options(**{'stream_results': True})\n",
        "OPTIONS = {'yield_per': 10}\n",
        "class Reader:\n    def read(self, c, q):\n        return c.stream(q)\n",
    ],
    ids=[
        "stream",
        "stream-scalars",
        "yield-per-method",
        "yield-per-option",
        "stream-results-true",
        "stream-results-variable",
        "stream-results-dict",
        "yield-per-dict",
        "in-a-method",
    ],
)
def test_flags_each_spelling(tmp_path: Path, source: str) -> None:
    (tmp_path / "module.py").write_text(source)
    assert len(server_side_cursors([tmp_path / "module.py"])) == 1


@pytest.mark.parametrize(
    "source",
    [
        "DECOY = 'connection.stream(query)'\n",
        "# rows = await connection.stream(query)\n",
        "def f(q):\n    return q.execution_options(stream_results=False)\n",
        "def stream(x):\n    return x\nstream(1)\n",
        "async def f(c, q):\n    return (await c.execute(q)).all()\n",
        "OPTIONS = {'stream': True}\n",
    ],
    ids=["string", "comment", "stream-results-false", "a-function-named-stream", "execute", "key"],
)
def test_does_not_flag_other_code(tmp_path: Path, source: str) -> None:
    (tmp_path / "module.py").write_text(source)
    assert server_side_cursors([tmp_path / "module.py"]) == []
