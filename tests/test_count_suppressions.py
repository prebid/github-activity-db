"""The suppression-comment caps that pre-commit enforces on src/."""

import importlib.util
import io
import os
import re
import subprocess
import sys
import tokenize
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "count_suppressions.py"
SRC = ROOT / "src"
COUNT_LINE = re.compile(r"^(type: ignore|noqa) comments: (\d+) \(max \d+\)$", re.MULTILINE)
SCANNED_LINE = re.compile(r"^scanned (\d+) files", re.MULTILINE)


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
        env={"PATH": os.environ.get("PATH", "")},
        timeout=60,
    )


def _counts(result: subprocess.CompletedProcess[str]) -> tuple[int, int]:
    found = dict(COUNT_LINE.findall(result.stdout))
    return int(found["type: ignore"]), int(found["noqa"])


def _scanned(result: subprocess.CompletedProcess[str]) -> int:
    match = SCANNED_LINE.search(result.stdout)
    assert match is not None, result.stdout
    return int(match.group(1))


def _load() -> ModuleType:
    """The script as a module, so its functions can be called in process."""
    spec = importlib.util.spec_from_file_location("count_suppressions", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _walk(root: Path) -> list[Path]:
    """Every regular file at or under ``root``, listed with os.walk rather than a glob."""
    if root.is_file():
        return [root]
    return sorted(
        Path(directory, name)
        for directory, _, names in os.walk(root)
        for name in names
        if Path(directory, name).is_file()
    )


def _python_files(root: Path) -> list[Path]:
    return [path for path in _walk(root) if path.suffix in {".py", ".pyi"}]


def _second_reading(root: Path) -> dict[str, list[str]]:
    """The suppression sites under ``root``, as the script lists them, read a second way: comment
    tokens whose text, without whitespace and in lowercase, holds a directive, and decorator lines
    naming no_type_check."""
    found: dict[str, list[str]] = {"type: ignore": [], "noqa": []}
    for path in _python_files(root):
        source = path.read_bytes()
        for token in tokenize.tokenize(io.BytesIO(source).readline):
            if token.type != tokenize.COMMENT:
                continue
            text = "".join(token.string.lower().split())
            if "type:ignore" in text or "mypy:" in text:
                found["type: ignore"].append(f"{path}:{token.start[0]}")
            if any(word in text for word in ("noqa", "ruff:disable", "isort:skip", "isort:off")):
                found["noqa"].append(f"{path}:{token.start[0]}")
        found["type: ignore"] += [
            f"{path}:{number}"
            for number, line in enumerate(source.decode().splitlines(), start=1)
            if line.lstrip().startswith("@") and "no_type_check" in line
        ]
    return found


def test_src_is_within_the_hook_caps() -> None:
    result = _run(SRC)
    assert result.returncode == 0, result.stdout
    assert result.stderr == ""
    assert _scanned(result) == len(_python_files(SRC))
    sites = _second_reading(SRC)
    assert _counts(result) == (len(sites["type: ignore"]), len(sites["noqa"]))


@pytest.mark.parametrize(
    ("kind", "cap"), [("type: ignore", "max_type_ignore"), ("noqa", "max_noqa")]
)
def test_at_the_cap_passes_and_one_below_fails(kind: str, cap: str) -> None:
    sites = _second_reading(SRC)
    at_cap = {"max_type_ignore": len(sites["type: ignore"]), "max_noqa": len(sites["noqa"])}
    passing = _run(SRC, **at_cap)
    assert passing.returncode == 0, passing.stdout

    # One below the count; -1 where src holds no site of the kind.
    over_cap = _run(SRC, **{**at_cap, cap: at_cap[cap] - 1})
    assert over_cap.returncode == 1
    listed = [line.strip() for line in over_cap.stdout.splitlines() if line.startswith("  ")]
    assert sorted(listed) == sorted(sites[kind])


def test_directive_text_outside_a_directive_comment_is_not_counted() -> None:
    # The script's docstring spells the directives it counts; text in a string is not a comment.
    strings = [
        token.string
        for token in tokenize.tokenize(io.BytesIO(SCRIPT.read_bytes()).readline)
        if token.type == tokenize.STRING
    ]
    assert any("# noqa" in text and "# type: ignore" in text for text in strings)
    result = _run(SCRIPT)
    sites = _second_reading(SCRIPT)
    assert result.returncode == 0, result.stdout
    assert _scanned(result) == 1
    assert _counts(result) == (len(sites["type: ignore"]), len(sites["noqa"]))


def test_reads_every_file_under_src_as_ruff_does() -> None:
    module = _load()
    files = _walk(SRC)
    assert list(module.all_files([SRC])) == files
    assert [module.refusal(path) for path in files] == [None] * len(files)


def test_refuses_a_file_identify_cannot_read(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load()
    typed = SRC / "github_activity_db" / "py.typed"
    others = [str(path) for path in _walk(SRC) if path.suffix not in {".py", ".pyi"}]
    arguments = [str(SRC), "--max-type-ignore", "5", "--max-noqa", "3"]
    assert module.refusal(typed) is None
    assert module.main(arguments) == 0
    capsys.readouterr()

    def unreadable(path: str) -> set[str]:
        # The response the operating system gives a read of a file without read permission.
        raise PermissionError(13, "Permission denied", path)

    monkeypatch.setattr(module, "tags_from_path", unreadable)
    assert module.refusal(typed) is not None
    assert module.main(arguments) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert [line.partition(": refused: ")[0] for line in err.splitlines()] == others


def test_no_python_files_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    result = _run(tmp_path / "empty")
    assert result.returncode == 1
    # A run over files reports what it scanned before any cap fails it.
    assert result.stdout == ""
