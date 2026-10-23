"""pytest run over test files a test writes, and scripts/check_junit.py loaded to read its report.

Shared by the tests of check_junit.py and of the suite's own rules, so the two run pytest the same
way: its own configuration, only the environment variables it needs, and a bounded wait.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
CHECK_JUNIT = ROOT / "scripts" / "check_junit.py"


def load_check_junit() -> ModuleType:
    """scripts/check_junit.py, loaded from its file: CI runs it standalone, not as a package."""
    spec = importlib.util.spec_from_file_location("check_junit", CHECK_JUNIT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_pytest(
    root: Path, files: dict[str, str], *options: str, importable: bool = False
) -> subprocess.CompletedProcess[str]:
    """pytest over ``files``, written to ``root`` beside a ``pytest.ini`` of their own, each name a
    path under ``root`` whose directories are made; the report is ``root/junit.xml``. With
    ``importable``, this repository's package and tests import."""
    for name, text in {"pytest.ini": "[pytest]\n", **files}.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONHASHSEED": "0",
    }
    if importable:
        environment["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), str(ROOT)])
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            f"--junitxml={root / 'junit.xml'}",
            *options,
        ],
        cwd=root,
        capture_output=True,
        text=True,
        env=environment,
        timeout=120,
    )
