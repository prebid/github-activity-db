"""The built-wheel smoke check rejects every way it could read a file other than the packaged one.

The CI job runs the script isolated (`-I`) against an installed wheel. These tests run it the same
way in this environment, with only the environment variables each test sets, and with a separate
directory as the checkout argument, so the import-location check passes.
"""

import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import github_activity_db

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "wheel_smoke.py"
SOURCE_TOML = REPO_ROOT / "src" / "github_activity_db" / "tracking.toml"
SPELLINGS = ["TRACKING_CONFIG_PATH", "tracking_config_path", "Tracking_Config_Path"]
COUNT_LINE = re.compile(r"^tracked repositories from the packaged tracking\.toml: (\d+)$", re.M)


def _run(
    checkout: Path, cwd: Path, overrides: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), str(SOURCE_TOML), str(checkout)],
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", ""), **(overrides or {})},
        cwd=cwd,
        timeout=60,
    )


def _tracked_count(result: subprocess.CompletedProcess[str]) -> int:
    match = COUNT_LINE.search(result.stdout)
    assert match is not None, result.stdout
    return int(match.group(1))


def _refused_value(result: subprocess.CompletedProcess[str]) -> Path:
    """The value a refusal names in parentheses; a refusal prints one line and nothing else."""
    (line,) = result.stdout.splitlines()
    return Path(line[line.index("(") + 1 : line.rindex(")")])


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    path = tmp_path / "checkout"
    path.mkdir()
    return path


@pytest.fixture
def source_count() -> int:
    """The repositories the source tracking.toml lists, read with tomllib."""
    return len(tomllib.loads(SOURCE_TOML.read_text())["repositories"])


def test_passes_when_the_packaged_file_is_read(checkout: Path, source_count: int) -> None:
    result = _run(checkout, checkout.parent)
    assert result.returncode == 0, result.stdout
    assert _tracked_count(result) == source_count


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_rejects_an_override_in_any_letter_case(checkout: Path, spelling: str) -> None:
    result = _run(checkout, checkout.parent, {spelling: str(SOURCE_TOML)})
    assert result.returncode == 1
    assert _refused_value(result) == SOURCE_TOML
    assert result.stderr == ""


def test_rejects_an_override_before_reading_it(checkout: Path) -> None:
    missing = checkout.parent / "missing.toml"
    result = _run(checkout, checkout.parent, {"TRACKING_CONFIG_PATH": str(missing)})
    # Reading a missing file raises; the refusal comes first, and nothing else is printed.
    assert result.returncode == 1
    assert _refused_value(result) == missing
    assert result.stderr == ""


def test_an_empty_override_means_the_packaged_file(checkout: Path, source_count: int) -> None:
    result = _run(checkout, checkout.parent, {"TRACKING_CONFIG_PATH": ""})
    assert result.returncode == 0, result.stdout
    assert _tracked_count(result) == source_count


def test_rejects_an_import_from_the_checkout(tmp_path: Path) -> None:
    # The editable install imports the package from the checkout's src/.
    package_dir = Path(github_activity_db.__file__).resolve().parent
    assert package_dir.is_relative_to(REPO_ROOT)
    result = _run(REPO_ROOT, tmp_path)
    assert result.returncode == 1
    assert _refused_value(result) == package_dir
    assert result.stderr == ""
