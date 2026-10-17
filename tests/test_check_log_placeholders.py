"""scripts/check_log_placeholders.py: %-style messages in loguru calls (redesign plan gap 80)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_log_placeholders.py"


def _run(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "module.py").write_text(source)
    return subprocess.run([sys.executable, str(SCRIPT), str(root)], capture_output=True, text=True)


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
]


@pytest.mark.parametrize("source", FLAGGED)
def test_flags_a_percent_style_message(tmp_path: Path, source: str) -> None:
    result = _run(tmp_path, source)
    assert result.returncode == 1, result.stdout
    assert "%-style loguru messages: 1" in result.stdout


@pytest.mark.parametrize("source", ACCEPTED)
def test_accepts_brace_style_and_other_calls(tmp_path: Path, source: str) -> None:
    result = _run(tmp_path, source)
    assert result.returncode == 0, result.stdout
    assert "%-style loguru messages: 0" in result.stdout


def test_reports_each_site(tmp_path: Path) -> None:
    result = _run(
        tmp_path, 'logger.info("a %s", x)\nlogger.debug("b {}", y)\nlogger.error("c %d", z)\n'
    )
    assert result.returncode == 1
    assert "%-style loguru messages: 2" in result.stdout
    assert "module.py:1:" in result.stdout
    assert "module.py:3:" in result.stdout


def test_no_python_files_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path / "empty")], capture_output=True, text=True
    )
    assert result.returncode == 1
    assert "no Python files" in result.stderr


def test_src_has_no_percent_style_messages() -> None:
    src = Path(__file__).resolve().parents[1] / "src"
    result = subprocess.run([sys.executable, str(SCRIPT), str(src)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout
    assert "%-style loguru messages: 0" in result.stdout
