"""scripts/check_log_placeholders.py: %-style messages in loguru calls (redesign plan gap 80)."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.support import files_under

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_log_placeholders.py"
SRC = ROOT / "src"


FLAGGED = [
    'logger.info("found %s items", count)\n',
    'logger.warning("retry %d of %d", attempt, total)\n',
    'log.error("failed: %r", error)\n',
    'logger.opt(depth=1).debug("value %(name)s", values)\n',
    'logger.exception("lost %.2f%%", share)\n',
    'logger.log("INFO", "found %s", count)\n',
    'logger.success("done " "in %ss", seconds)\n',
    'logger.trace("%5d", n)\n',
    "def f():\n    logger.critical('%s', x)\n",
    'logger.info("found %(n)s items", n=3)\n',  # loguru formats keyword arguments too
    'logger.warning("retry %s", attempt=2)\n',
    'logger.log("INFO", "found %s", count=3)\n',
]

ACCEPTED = [
    'logger.info("found {} items", count)\n',
    'logger.info("found %s items")\n',  # no arguments: loguru formats nothing
    'logger.info("100%% done", count)\n',  # a literal percent sign, no directive
    "logger.info(template, count)\n",  # not a literal
    'logger.info(f"found {count} items")\n',
    'console.print("found %s", count)\n',  # not a log level
    'logger.log("INFO", "found {}", count)\n',
    "DECOY = \"logger.info('found %s', count)\"\n",
    'logger.info("a % b", count)\n',  # `% b` is a space flag without a conversion
    'logger.info("found {n} items", n=3)\n',
    "logger.info(**fields)\n",  # no message at all
]


def _run(*paths: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *map(str, paths)],
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", "")},
        timeout=60,
    )


def _run_on_source(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
    """The check over a package of one module holding ``source``."""
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "module.py").write_text(source)
    return _run(root)


def test_no_python_files_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    result = _run(tmp_path / "empty")
    assert result.returncode == 1
    # A run that finds a site also exits 1, but only after reporting what it scanned; an
    # uncaught exception would print a traceback instead of the one refusal line.
    assert result.stdout == ""
    assert result.stderr.splitlines() == [f"no Python files under {tmp_path / 'empty'}"]


def test_src_has_no_percent_style_messages() -> None:
    result = _run(SRC)
    assert result.returncode == 0, result.stdout
    scanned = re.search(r"^scanned (\d+) files", result.stdout, re.MULTILINE)
    found = re.search(r"^%-style loguru messages: (\d+)$", result.stdout, re.MULTILINE)
    assert scanned is not None and found is not None, result.stdout
    assert int(scanned.group(1)) == len(files_under(SRC, {".py", ".pyi"}))
    assert int(found.group(1)) == 0


@pytest.mark.parametrize("source", FLAGGED)
def test_flags_a_percent_style_message(tmp_path: Path, source: str) -> None:
    result = _run_on_source(tmp_path, source)
    assert result.returncode == 1, result.stdout
    assert "%-style loguru messages: 1" in result.stdout


@pytest.mark.parametrize("source", ACCEPTED)
def test_accepts_brace_style_and_other_calls(tmp_path: Path, source: str) -> None:
    result = _run_on_source(tmp_path, source)
    assert result.returncode == 0, result.stdout
    assert "%-style loguru messages: 0" in result.stdout


def test_reports_each_site(tmp_path: Path) -> None:
    result = _run_on_source(
        tmp_path, 'logger.info("a %s", x)\nlogger.debug("b {}", y)\nlogger.error("c %d", z)\n'
    )
    assert result.returncode == 1
    assert "%-style loguru messages: 2" in result.stdout
    assert "module.py:1:" in result.stdout
    assert "module.py:3:" in result.stdout
