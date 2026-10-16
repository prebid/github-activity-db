"""The built-wheel smoke check rejects every way it could read a file other than the packaged one.

The CI job runs the script against an installed wheel; these tests run it against this
environment, passing a scratch directory as the checkout so the import-location check passes.
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


def _run(checkout: Path, **overrides: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k.lower() != "tracking_config_path"}
    env.update(overrides)
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(SOURCE_TOML), str(checkout)],
        capture_output=True,
        text=True,
        env=env,
    )


def test_passes_when_the_packaged_file_is_read(tmp_path: Path) -> None:
    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout
    assert "tracked repositories from the packaged tracking.toml: 18" in result.stdout


@pytest.mark.parametrize(
    "spelling", ["TRACKING_CONFIG_PATH", "tracking_config_path", "Tracking_Config_Path"]
)
def test_rejects_an_override_in_any_letter_case(tmp_path: Path, spelling: str) -> None:
    other = tmp_path / "other.toml"
    shutil.copy(SOURCE_TOML, other)
    result = _run(tmp_path, **{spelling: str(other)})
    assert result.returncode == 1
    assert "tracking_config_path is set" in result.stdout


def test_an_empty_override_means_the_packaged_file(tmp_path: Path) -> None:
    result = _run(tmp_path, TRACKING_CONFIG_PATH="")
    assert result.returncode == 0, result.stdout


def test_rejects_an_import_from_the_checkout() -> None:
    result = _run(REPO_ROOT)
    assert result.returncode == 1
    assert "imported from the checkout" in result.stdout
