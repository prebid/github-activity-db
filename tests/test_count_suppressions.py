"""The suppression-comment caps that pre-commit enforces on src/."""

import re
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "count_suppressions.py"
COUNT_LINE = re.compile(r"^(type: ignore|noqa) comments: (\d+) \(max \d+\)$", re.MULTILINE)


def _run(
    root: Path, *, max_type_ignore: int = 5, max_noqa: int = 3
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            str(root),
            "--max-type-ignore",
            str(max_type_ignore),
            "--max-noqa",
            str(max_noqa),
        ],
        capture_output=True,
        text=True,
    )


def _counts(result: subprocess.CompletedProcess[str]) -> tuple[int, int]:
    found = dict(COUNT_LINE.findall(result.stdout))
    return int(found["type: ignore"]), int(found["noqa"])


def _package(tmp_path: Path, *sources: str) -> Path:
    root = tmp_path / "pkg"
    (root / "sub").mkdir(parents=True)
    for i, source in enumerate(sources):
        (root / ("sub" if i % 2 else ".") / f"module_{i}.py").write_text(source)
    return root


# Spellings that ruff 0.14 and mypy 1.19 honor as suppressions.
NOQA_SPELLINGS = [
    "import os  # noqa\n",
    "import os  # NOQA\n",
    "import os  #noqa\n",
    "import os  # noqa:F401\n",
    "import os  # NoQa: F401\n",
    "import os  # unused on purpose  # noqa: F401\n",
    "# ruff: noqa\nimport os\n",
    "# ruff: noqa: F401\nimport os\n",
    "#ruff:noqa\nimport os\n",
    "# flake8: noqa\nimport os\n",
    "# flake8: noqa: F401\nimport os\n",
]
TYPE_IGNORE_SPELLINGS = [
    'x: int = "a"  # type: ignore[assignment]\n',
    'x: int = "a"  # type:ignore[assignment]\n',
    'x: int = "a"  #type:ignore\n',
    'x: int = "a"  # type: ignore\n',
    '# type: ignore\nx: int = "a"\n',
    '# mypy: ignore-errors\nx: int = "a"\n',
    '# mypy: disable-error-code="assignment"\nx: int = "a"\n',
]
NOT_SUPPRESSIONS = [
    'TEXT = "# noqa: F401, # type: ignore[misc], # ruff: noqa, # mypy: ignore-errors"\n',
    '"""Docstring.\n\n# noqa\n# type: ignore\n"""\n',
    "# this module needs no noqa and no type checking exceptions\nimport os\n",
]


@pytest.mark.parametrize("source", NOQA_SPELLINGS)
def test_counts_every_noqa_spelling(tmp_path: Path, source: str) -> None:
    assert _counts(_run(_package(tmp_path, source))) == (0, 1)


@pytest.mark.parametrize("source", TYPE_IGNORE_SPELLINGS)
def test_counts_every_type_ignore_spelling(tmp_path: Path, source: str) -> None:
    assert _counts(_run(_package(tmp_path, source))) == (1, 0)


@pytest.mark.parametrize("source", NOT_SUPPRESSIONS)
def test_directive_text_outside_a_directive_comment_is_not_counted(
    tmp_path: Path, source: str
) -> None:
    assert _counts(_run(_package(tmp_path, source))) == (0, 0)


def test_counts_across_files_and_subdirectories(tmp_path: Path) -> None:
    root = _package(tmp_path, *NOQA_SPELLINGS[:3], *TYPE_IGNORE_SPELLINGS[:2])
    result = _run(root)
    assert _counts(result) == (2, 3)
    assert result.stdout.startswith("scanned 5 files")


@pytest.mark.parametrize(
    ("sources", "caps", "kind"),
    [
        (NOQA_SPELLINGS[:3], {"max_noqa": 3}, "noqa"),
        (TYPE_IGNORE_SPELLINGS[:5], {"max_type_ignore": 5}, "type: ignore"),
    ],
)
def test_at_the_cap_passes_and_one_more_fails(
    tmp_path: Path, sources: list[str], caps: dict[str, int], kind: str
) -> None:
    at_cap = _run(_package(tmp_path, *sources), **caps)
    assert at_cap.returncode == 0, at_cap.stdout

    over = {name: value - 1 for name, value in caps.items()}
    over_cap = _run(tmp_path / "pkg", **over)
    assert over_cap.returncode == 1
    listed = [line.strip() for line in over_cap.stdout.splitlines() if line.startswith("  ")]
    assert len(listed) == len(sources)
    assert all(re.fullmatch(r".+\.py:\d+", site) for site in listed)
    assert f"{kind} comments: {len(sources)}" in over_cap.stdout


def test_no_python_files_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    result = _run(tmp_path / "empty")
    assert result.returncode == 1
    assert "no Python files" in result.stderr
