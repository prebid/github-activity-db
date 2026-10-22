"""tests/fixtures/history: each copy is the git blob its README row records.

The README's table gives each copy's path under the directory, its source commit, and its git blob
id. The copies compared are the files git tracks under the directory, so a cache a tool writes
there, which git ignores, is not one of them.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HISTORY = Path(__file__).resolve().parent / "fixtures" / "history"
README = "README.md"
GIT = shutil.which("git")
HEADER = "| File | Source commit | Blob | Read by |"
SEPARATOR = re.compile(r"\|(?:-+\|){4}")
ROW = re.compile(
    r"\| `(?P<file>[^`|]+)` \| `[0-9a-f]{40}`[^|]* \| `(?P<blob>[0-9a-f]{40})` \| [^|]+ \|"
)
# The README's rows, one per tracked copy.
COPIES = 11


class TableError(ValueError):
    """A README line the table reader does not model; ``line`` is its number, or None when the
    README holds no table or more than one."""

    def __init__(self, line: int | None, reason: str) -> None:
        super().__init__(reason if line is None else f"README line {line}: {reason}")
        self.line = line


def read_rows(text: str) -> list[tuple[str, str]]:
    """Each row's file and blob id, in order. Every line starting with ``|`` is the header, the
    separator under it, or a row."""
    lines = text.splitlines()
    if lines.count(HEADER) != 1:
        raise TableError(None, f"{lines.count(HEADER)} tables, not 1")
    header = lines.index(HEADER)
    rows = []
    for number, line in enumerate(lines, start=1):
        if not line.startswith("|") or number == header + 1:
            continue
        if number == header + 2 and SEPARATOR.fullmatch(line):
            continue
        match = ROW.fullmatch(line)
        if match is None:
            raise TableError(number, "not a `| file | commit | blob | read by |` row")
        rows.append((match["file"], match["blob"]))
    return rows


def blob_id(content: bytes) -> str:
    """The id git gives a file holding ``content``: the SHA-1 of ``blob <size>``, a NUL byte, and
    the bytes."""
    return hashlib.sha1(b"blob %d\0" % len(content) + content, usedforsecurity=False).hexdigest()


def copy_problems(directory: Path, files: list[str], rows: list[tuple[str, str]]) -> list[str]:
    """Each of ``files`` (paths under ``directory``) without exactly one row, each row that names
    none of them, and each file whose content is not the blob its row records."""
    recorded: dict[str, list[str]] = {}
    for file, blob in rows:
        recorded.setdefault(file, []).append(blob)
    problems = []
    for file in sorted(set(files) | recorded.keys()):
        blobs = recorded.get(file, [])
        if file not in files:
            problems.append(f"{file}: a row but no file")
        elif len(blobs) != 1:
            problems.append(f"{file}: {len(blobs)} rows")
        elif (found := blob_id((directory / file).read_bytes())) != blobs[0]:
            problems.append(f"{file}: blob {found}, the row records {blobs[0]}")
    return problems


def tracked_files(directory: Path, home: Path) -> list[str]:
    """The files git tracks under ``directory``, relative to it, other than the README; git runs
    with a home of the test's own and no global or system configuration."""
    assert GIT is not None
    result = subprocess.run(
        [GIT, "ls-files", "-z", "--", "."],
        cwd=directory,
        capture_output=True,
        check=True,
        env={
            "PATH": os.environ["PATH"],
            "HOME": str(home),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        },
        timeout=60,
    )
    names = result.stdout.decode().split("\0")
    return sorted(name for name in names if name and name != README)


def test_each_copy_is_the_blob_its_row_records(tmp_path: Path) -> None:
    rows = read_rows((HISTORY / README).read_text())
    files = tracked_files(HISTORY, tmp_path)
    assert len(files) == len(rows) == COPIES
    assert copy_problems(HISTORY, files, rows) == []


# Sample copies the tests below write, with the ids `git hash-object` gives their content.
X, X_BLOB = b'{"a": 1}\n', "cb5b2f69babc53f9145b45ad20b9a075d48717ef"
X_EDITED_BLOB = "3c27e19a80d4780c181bc216b4456841d86d74f3"  # b'{"a": 2}\n'
Y, Y_BLOB = b"# y\n", "29dfab31929d7957cdd6997e17ed38642047d7ed"


def _readme(rows: list[tuple[str, str]]) -> str:
    lines = [HEADER, "|------|---------------|------|---------|"]
    lines += [f"| `{file}` | `{'1' * 40}` | `{blob}` | `tests/test_x.py` |" for file, blob in rows]
    return "# Earlier versions\n\nText.\n\n" + "\n".join(lines) + "\n"


@pytest.fixture
def sample(tmp_path: Path) -> Path:
    directory = tmp_path / "history"
    (directory / "a").mkdir(parents=True)
    (directory / "b").mkdir()
    (directory / "a" / "x.json").write_bytes(X)
    (directory / "b" / "y.md").write_bytes(Y)
    return directory


def test_blob_id_is_gits() -> None:
    assert blob_id(b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
    assert [blob_id(X), blob_id(b'{"a": 2}\n'), blob_id(Y)] == [X_BLOB, X_EDITED_BLOB, Y_BLOB]


def test_copies_their_rows_record_pass(sample: Path) -> None:
    rows = read_rows(_readme([("a/x.json", X_BLOB), ("b/y.md", Y_BLOB)]))
    assert rows == [("a/x.json", X_BLOB), ("b/y.md", Y_BLOB)]
    assert copy_problems(sample, ["a/x.json", "b/y.md"], rows) == []


@pytest.mark.parametrize(
    ("rows", "files", "problems"),
    [
        pytest.param(
            [("a/x.json", X_EDITED_BLOB), ("b/y.md", Y_BLOB)],
            ["a/x.json", "b/y.md"],
            [f"a/x.json: blob {X_BLOB}, the row records {X_EDITED_BLOB}"],
            id="content other than the row's blob",
        ),
        pytest.param(
            [("a/x.json", X_BLOB)],
            ["a/x.json", "b/y.md"],
            ["b/y.md: 0 rows"],
            id="a file without a row",
        ),
        pytest.param(
            [("a/x.json", X_BLOB), ("a/x.json", X_BLOB), ("b/y.md", Y_BLOB)],
            ["a/x.json", "b/y.md"],
            ["a/x.json: 2 rows"],
            id="a file with two rows",
        ),
        pytest.param(
            [("a/x.json", X_BLOB), ("b/y.md", Y_BLOB), ("c/z.json", X_BLOB)],
            ["a/x.json", "b/y.md"],
            ["c/z.json: a row but no file"],
            id="a row naming no file",
        ),
    ],
)
def test_a_copy_without_exactly_one_matching_row_is_reported(
    sample: Path, rows: list[tuple[str, str]], files: list[str], problems: list[str]
) -> None:
    assert copy_problems(sample, files, read_rows(_readme(rows))) == problems


@pytest.mark.parametrize(
    ("text", "line"),
    [
        pytest.param(_readme([("a/x.json", X_BLOB[:39])]), 7, id="short blob id"),
        pytest.param(
            _readme([("a/x.json", X_BLOB)]).replace(" | `tests/test_x.py` |", " |"),
            7,
            id="no reader column",
        ),
        pytest.param(_readme([("a/x.json", X_BLOB)]) + "\n| x |\n", 9, id="a line after it"),
        pytest.param(
            _readme([("a/x.json", X_BLOB)]).replace("|------|", "|------|------|"),
            6,
            id="a separator of five columns",
        ),
        pytest.param("# Earlier versions\n\nNo table.\n", None, id="no table"),
        pytest.param(_readme([]) + _readme([]), None, id="two tables"),
    ],
)
def test_a_table_line_it_does_not_model_is_refused(text: str, line: int | None) -> None:
    with pytest.raises(TableError) as raised:
        read_rows(text)
    assert raised.value.line == line


def test_only_the_files_git_tracks_are_compared(tmp_path: Path) -> None:
    """A repository the test builds: the README and one copy committed, one file never added, and
    one git ignores. Only the committed copy is compared."""
    assert GIT is not None
    repo, home = tmp_path / "repo", tmp_path / "home"
    copies = repo / "history"
    (copies / "abc1234").mkdir(parents=True)
    home.mkdir()
    (copies / README).write_text("rows\n")
    (copies / "abc1234" / "kept.toml").write_text("kept = 1\n")
    (copies / ".gitignore").write_text("ignored.txt\n")
    environment = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    for args in (
        ["init", "-q"],
        ["add", "history/README.md", "history/abc1234/kept.toml"],
        ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "copies"],
    ):
        subprocess.run([GIT, *args], cwd=repo, env=environment, check=True, timeout=60)
    (copies / "untracked.toml").write_text("untracked = 1\n")
    (copies / "ignored.txt").write_text("ignored\n")
    assert tracked_files(copies, home) == ["abc1234/kept.toml"]
