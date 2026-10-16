"""Check an installed, non-editable build: the packaged tracking.toml loads.

Run with the interpreter of an environment where the wheel is installed, from a directory
outside the checkout:

    python -I scripts/wheel_smoke.py SOURCE_TRACKING_TOML CHECKOUT_DIR
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import github_activity_db
from github_activity_db.config import Settings


def main(argv: list[str]) -> int:
    source_toml, checkout = Path(argv[1]), Path(argv[2]).resolve()
    # Settings reads environment variables case-insensitively, so inspect the value it
    # resolved rather than one spelling of the variable name.
    settings = Settings(_env_file=None)
    if settings.tracking_config_path is not None:
        print(
            f"FAIL: tracking_config_path is set ({settings.tracking_config_path}), "
            "so the packaged file would not be read"
        )
        return 1
    package_dir = Path(github_activity_db.__file__).resolve().parent
    if package_dir.is_relative_to(checkout):
        print(f"FAIL: imported from the checkout ({package_dir}), not an installed build")
        return 1
    expected = [
        f"{repo['owner']}/{repo['name']}"
        for repo in tomllib.loads(source_toml.read_text())["repositories"]
    ]
    installed = settings.tracked_repos
    print(f"installed package: {package_dir}")
    print(f"tracked repositories from the packaged tracking.toml: {len(installed)}")
    if installed != expected:
        print("FAIL: packaged list differs from the source tracking.toml")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
