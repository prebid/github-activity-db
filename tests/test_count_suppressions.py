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


def _package(tmp_path: Path, *sources: str) -> Path:
    root = tmp_path / "pkg"
    (root / "sub").mkdir(parents=True)
    for i, source in enumerate(sources):
        (root / ("sub" if i % 2 else ".") / f"module_{i}.py").write_text(source, encoding="utf-8")
    return root


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
    "# isort: skip_file\nimport sys\nimport os\n",
    "# isort: off\nimport sys\nimport os\n",
    "import sys  # isort: skip\nimport os\n",
    "# ruff: isort: skip_file\nimport sys\nimport os\n",
    "# ruff: isort: off\nimport sys\nimport os\n",
    # Range suppressions, which ruff 0.14.11 honors only in preview.
    "# ruff: disable[F401]\nimport os\n",
    "#ruff:disable[F401, E501]\nimport os\n",
    "# ruff: disable [F401]\nimport os\n",
    "# ruff : disable[F401]\nimport os\n",
    "#\truff:\tdisable\t[F401]\nimport os\n",
    "#\u3000ruff:\u3000disable[F401]\nimport os\n",
    "# ruff: disable[ F401, ]  # reason\nimport os\n",
    "# ruff: disable[F401] reason\nimport os\n",
    "def f() -> None:\n    # ruff: disable[F401]\n    import os\n",
    "# ruff: disable[F401]\nimport os\n# ruff: enable[F401]\n",
    # Not honored by ruff (uppercase), counted so that no honored spelling escapes.
    "# RUFF: DISABLE[F401]\nimport os\n",
]

TYPE_IGNORE_SPELLINGS = [
    'x: int = "a"  # type: ignore[assignment]\n',
    'x: int = "a"  # type:ignore[assignment]\n',
    'x: int = "a"  #type:ignore\n',
    'x: int = "a"  # type: ignore\n',
    '# type: ignore\nx: int = "a"\n',
    '# mypy: ignore-errors\nx: int = "a"\n',
    '# mypy: disable-error-code="assignment"\nx: int = "a"\n',
    "from typing import no_type_check\n\n\n@no_type_check\ndef f() -> int:\n    return 'x'\n",
    "import typing\n\n\n@typing.no_type_check\nclass C:\n    x: int = 'x'\n",
    "from typing import no_type_check\n\n\n@no_type_check\nasync def f() -> int:\n    return 'x'\n",
    "from typing import no_type_check as ntc\n\n\n@ntc\ndef f() -> int:\n    return 'x'\n",
    "from typing_extensions import no_type_check as ntc\n\n\n"
    "@ntc\ndef f() -> int:\n    return 'x'\n",
    # A re-export of typing's no_type_check, as mypy follows it.
    "from pkg.compat import no_type_check as skip\n\n\n@skip\ndef f() -> int:\n    return 'x'\n",
]

REFUSED = [
    pytest.param(
        "module.py",
        "# coding: latin-1\n# ruff:\u3000disable[F401]\nimport os\n".encode(),
        False,
        id="latin-1 declaration",
    ),
    pytest.param(
        "module.py",
        b'# coding: unicode_escape\n# \\x0a"""\n# ruff: noqa: F401\n# \\x0a"""\n',
        False,
        id="unicode_escape declaration",
    ),
    pytest.param("module.py", b"# coding: no-such-codec\nimport os\n", False, id="unknown codec"),
    pytest.param("module.py", b'"""Doc."""\r# ruff: noqa: F401\rimport os\r', False, id="lone CR"),
    pytest.param("module.PY", b"import os  # noqa: F401\n", False, id="uppercase suffix"),
    pytest.param(
        "stub.PYI", b"x: int = undefined_name  # noqa: F821\n", False, id="uppercase stub suffix"
    ),
    pytest.param(
        "tool", b"#!/usr/bin/env python3\nimport os  # noqa: F401\n", True, id="python shebang"
    ),
    pytest.param("app.wsgi", b"import os  # noqa: F401\n", False, id="wsgi suffix"),
    pytest.param(
        "tool",
        b"#!/usr/bin/env nix-shell\n#! nix-shell -i python3\nimport os  # noqa: F401\n",
        True,
        id="nix-shell shebang",
    ),
]

ACCEPTED = [
    pytest.param("module.py", b"# -*- coding: utf-8 -*-\nimport os\n", False, id="utf-8"),
    pytest.param("module.py", b"\xef\xbb\xbfimport os\n", False, id="byte-order mark"),
    pytest.param("module.py", b"import os\r\nimport sys\r\n", False, id="CRLF"),
    pytest.param(
        "tool", b"#!/usr/bin/env python3\nimport os\n", False, id="shebang, not executable"
    ),
    pytest.param("tool.sh", b"#!/bin/sh\necho python\n", True, id="shell script"),
    pytest.param(
        "tool.sh", b"#!/usr/bin/env python3\nimport os\n", True, id="python shebang, shell suffix"
    ),
]


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


@pytest.mark.parametrize("source", NOQA_SPELLINGS)
def test_counts_every_noqa_spelling(tmp_path: Path, source: str) -> None:
    assert _counts(_run(_package(tmp_path, source))) == (0, 1)


@pytest.mark.parametrize("source", TYPE_IGNORE_SPELLINGS)
def test_counts_every_type_ignore_spelling(tmp_path: Path, source: str) -> None:
    assert _counts(_run(_package(tmp_path, source))) == (1, 0)


def test_counts_stub_files(tmp_path: Path) -> None:
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "stub.pyi").write_text("x: int  # type: ignore[misc]\ny: int  # noqa: E501\n")
    result = _run(root)
    assert result.stdout.startswith("scanned 1 files")
    assert _counts(result) == (1, 1)


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


@pytest.mark.parametrize(("name", "content", "executable"), REFUSED)
def test_refuses_a_file_it_cannot_read_as_ruff_does(
    tmp_path: Path, name: str, content: bytes, executable: bool
) -> None:
    root = _package(tmp_path, "import os\n")
    path = root / name
    path.write_bytes(content)
    if executable:
        path.chmod(0o755)
    result = _run(root)
    assert result.returncode == 1
    assert f"{path}: refused:" in result.stderr
    assert "comments:" not in result.stdout


@pytest.mark.parametrize(("name", "content", "executable"), ACCEPTED)
def test_accepts_files_it_reads_as_ruff_does(
    tmp_path: Path, name: str, content: bytes, executable: bool
) -> None:
    root = _package(tmp_path, "import os\n")
    path = root / name
    path.write_bytes(content)
    if executable:
        path.chmod(0o755)
    result = _run(root)
    assert result.returncode == 0, result.stderr
    assert "refused" not in result.stderr


def test_skips_entries_named_py_that_are_not_files(tmp_path: Path) -> None:
    root = _package(tmp_path, "import os  # noqa: F401\n")
    (root / "pkg.py").mkdir()
    (root / "pkg.py" / "inner.py").write_text("import sys\n")
    (root / "gone.py").symlink_to(root / "missing.py")
    result = _run(root)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("scanned 2 files")
    assert _counts(result) == (0, 1)
