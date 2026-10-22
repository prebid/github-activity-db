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
import os
from pathlib import Path

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
    # Counted with os.walk rather than the glob, so the scan cannot pass on fewer files.
    assert len(files) == sum(
        Path(name).suffix == ".py" for _, _, names in os.walk(SRC) for name in names
    )
    assert SRC / "github_activity_db" / "sync" / "snapshot.py" in files
    assert server_side_cursors(files) == []
