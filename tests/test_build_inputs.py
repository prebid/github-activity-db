"""Build inputs pinned in more than one file must agree."""

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _pins(lines: list[str]) -> dict[str, str]:
    pins = {}
    for line in lines:
        match = re.match(r"^([A-Za-z0-9_.-]+)==(\S+)", line.strip())
        if match:
            pins[match.group(1).lower()] = match.group(2)
    return pins


def test_build_dependency_pins_agree() -> None:
    # [build-system] pins the backend for every build; [tool.uv] build-constraint-dependencies
    # pins it and its dependencies for every uv build, including `uv sync`; and
    # build-constraints.txt pins the same versions with hashes for the built-wheel CI job.
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    constraints_text = (REPO_ROOT / "build-constraints.txt").read_text()

    uv_pins = _pins(pyproject["tool"]["uv"]["build-constraint-dependencies"])
    hashed_pins = _pins(constraints_text.splitlines())
    backend_pins = _pins(pyproject["build-system"]["requires"])

    assert backend_pins.keys() == {"hatchling"}
    assert uv_pins == hashed_pins
    assert backend_pins["hatchling"] == uv_pins["hatchling"]
    assert constraints_text.count("--hash=sha256:") >= len(hashed_pins)
