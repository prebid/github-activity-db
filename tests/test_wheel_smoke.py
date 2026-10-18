"""The built-wheel smoke check rejects every way it could read a file other than the packaged one.

The CI job runs the script against an installed wheel. These tests run it in this environment
with a separate directory as the checkout argument, so the import-location check passes, and
use a copy of the package without tracking.toml for the missing-resource cases.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "wheel_smoke.py"
SOURCE_TOML = REPO_ROOT / "src" / "github_activity_db" / "tracking.toml"
SPELLINGS = ["TRACKING_CONFIG_PATH", "tracking_config_path", "Tracking_Config_Path"]


def _run(
    checkout: Path,
    overrides: dict[str, str] | None = None,
    package_root: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k.lower() != "tracking_config_path"}
    env.update(overrides or {})
    if package_root is not None:
        env["PYTHONPATH"] = str(package_root)
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(SOURCE_TOML), str(checkout)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    path = tmp_path / "checkout"
    path.mkdir()
    return path


@pytest.fixture
def package_without_resource(tmp_path: Path) -> Path:
    """A copy of the package with tracking.toml removed, importable through PYTHONPATH."""
    root = tmp_path / "site"
    shutil.copytree(REPO_ROOT / "src" / "github_activity_db", root / "github_activity_db")
    (root / "github_activity_db" / "tracking.toml").unlink()
    return root


def test_passes_when_the_packaged_file_is_read(checkout: Path) -> None:
    result = _run(checkout)
    assert result.returncode == 0, result.stdout
    assert "tracked repositories from the packaged tracking.toml: 29" in result.stdout


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_rejects_an_override_in_any_letter_case(checkout: Path, spelling: str) -> None:
    result = _run(checkout, {spelling: str(SOURCE_TOML)})
    assert result.returncode == 1
    assert "tracking_config_path is set" in result.stdout


def test_an_empty_override_means_the_packaged_file(checkout: Path) -> None:
    result = _run(checkout, {"TRACKING_CONFIG_PATH": ""})
    assert result.returncode == 0, result.stdout


def test_fails_when_the_packaged_file_is_missing(
    checkout: Path, package_without_resource: Path
) -> None:
    result = _run(checkout, package_root=package_without_resource)
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "tracking config not found" in output
    # The error names the copy, so the resource-less package is the one that was imported.
    assert str(package_without_resource) in output


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_an_override_cannot_hide_a_missing_packaged_file(
    checkout: Path, package_without_resource: Path, spelling: str
) -> None:
    result = _run(checkout, {spelling: str(SOURCE_TOML)}, package_without_resource)
    assert result.returncode == 1
    assert "tracking_config_path is set" in result.stdout


def test_rejects_an_import_from_the_checkout() -> None:
    result = _run(REPO_ROOT)
    assert result.returncode == 1
    assert "imported from the checkout" in result.stdout
