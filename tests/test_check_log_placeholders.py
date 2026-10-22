"""scripts/check_log_placeholders.py: %-style messages in loguru calls (redesign plan gap 80)."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_log_placeholders.py"
SRC = ROOT / "src"


def _run(*paths: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *map(str, paths)],
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", "")},
        timeout=60,
    )


def _python_file_count(root: Path) -> int:
    """The .py and .pyi files under ``root``, counted with os.walk rather than the script's glob."""
    return sum(
        Path(name).suffix in {".py", ".pyi"} for _, _, names in os.walk(root) for name in names
    )


def test_no_python_files_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    result = _run(tmp_path / "empty")
    assert result.returncode == 1
    # A run that finds a site also exits 1, but only after reporting what it scanned.
    assert result.stdout == ""


def test_src_has_no_percent_style_messages() -> None:
    result = _run(SRC)
    assert result.returncode == 0, result.stdout
    scanned = re.search(r"^scanned (\d+) files", result.stdout, re.MULTILINE)
    found = re.search(r"^%-style loguru messages: (\d+)$", result.stdout, re.MULTILINE)
    assert scanned is not None and found is not None, result.stdout
    assert int(scanned.group(1)) == _python_file_count(SRC)
    assert int(found.group(1)) == 0
