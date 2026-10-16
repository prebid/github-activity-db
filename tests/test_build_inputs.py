"""Build inputs pinned in more than one file must agree."""

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_build_backend_pin_matches_the_hashed_constraints() -> None:
    # pyproject.toml pins the backend for every build; build-constraints.txt (used by the
    # built-wheel CI job with --require-hashes) pins it and its dependencies with hashes.
    requires = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["build-system"]["requires"]
    constraints = (REPO_ROOT / "build-constraints.txt").read_text()
    pinned = re.search(r"^hatchling==(\S+)", constraints, re.MULTILINE)
    assert pinned is not None
    assert requires == [f"hatchling=={pinned.group(1)}"]
    assert "--hash=sha256:" in constraints
