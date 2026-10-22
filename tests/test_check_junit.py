"""The CI gate over pytest's JUnit report.

Every report is one pytest wrote, in a run over a test file written for it, or is built in memory
from one with its counters changed into a shape a run can leave: failures, errors, skips, no test
executed, several suites merged, a file cut short, or no suite at all.
"""

import ast
import copy
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_junit.py"
COUNTED = ("tests", "failures", "errors", "skipped")


def _pytest(root: Path, test_body: str) -> subprocess.CompletedProcess[str]:
    """Run pytest over one test file in ``root``, with its own configuration and only the
    environment variables it needs; the report is ``root/junit.xml``."""
    (root / "pytest.ini").write_text("[pytest]\n")
    (root / "test_sample.py").write_text(f"import pytest\n\n\n{test_body}")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            f"--junitxml={root / 'junit.xml'}",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", ""), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        timeout=60,
    )


@pytest.fixture(scope="module")
def written(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    """The report pytest wrote for one passing test."""
    root = tmp_path_factory.mktemp("pytest-run")
    run = _pytest(root, "def test_a():\n    pass\n")
    assert run.returncode == 0, run.stdout
    return (root / "junit.xml").read_bytes()


def _report(tmp_path: Path, written: bytes, *suites: tuple[int, int, int, int]) -> Path:
    """The written report with its <testsuite> once per ``suites``, each with those counters."""
    # The report pytest wrote in this session.
    root = ET.fromstring(written)  # noqa: S314
    (template,) = root.findall("testsuite")
    root.remove(template)
    for counts in suites:
        suite = copy.deepcopy(template)
        suite.attrib.update(zip(COUNTED, map(str, counts), strict=True))
        root.append(suite)
    path = tmp_path / "junit.xml"
    path.write_bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))
    return path


def _gate(report: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(report), *extra],
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", "")},
        timeout=60,
    )


def _printed(result: subprocess.CompletedProcess[str]) -> object:
    """The counters the gate read, which it prints before judging them."""
    return ast.literal_eval(result.stdout.strip())


@pytest.mark.parametrize(
    ("counts", "problems"),
    [
        ((0, 0, 0, 0), 1),  # no test executed
        ((5, 0, 0, 5), 2),  # no test executed, and more skips than allowed
        ((5, 1, 0, 0), 1),
        ((5, 0, 1, 0), 1),
        ((5, 0, 0, 1), 1),
    ],
)
def test_rejects(
    tmp_path: Path, written: bytes, counts: tuple[int, int, int, int], problems: int
) -> None:
    result = _gate(_report(tmp_path, written, counts))
    assert result.returncode == 1
    assert _printed(result) == dict(zip(COUNTED, counts, strict=True))
    assert len(result.stderr.splitlines()) == problems


def test_accepts_an_executed_passing_run(tmp_path: Path, written: bytes) -> None:
    report = tmp_path / "junit.xml"
    report.write_bytes(written)
    result = _gate(report)
    assert result.returncode == 0, result.stderr
    assert _printed(result) == {"tests": 1, "failures": 0, "errors": 0, "skipped": 0}
    assert result.stderr == ""


def test_partial_skips_need_an_explicit_allowance(tmp_path: Path, written: bytes) -> None:
    report = _report(tmp_path, written, (5, 0, 0, 2))
    assert _gate(report).returncode == 1
    assert _gate(report, "--max-skipped", "1").returncode == 1
    assert _gate(report, "--max-skipped", "2").returncode == 0


def test_sums_every_testsuite(tmp_path: Path, written: bytes) -> None:
    result = _gate(_report(tmp_path, written, (3, 0, 0, 0), (2, 1, 0, 0)))
    assert result.returncode == 1
    assert _printed(result) == {"tests": 5, "failures": 1, "errors": 0, "skipped": 0}
    assert len(result.stderr.splitlines()) == 1


@pytest.mark.parametrize("shape", ["missing", "cut short", "no testsuite"])
def test_unreadable_report_fails(tmp_path: Path, written: bytes, shape: str) -> None:
    path = tmp_path / "junit.xml"
    if shape == "cut short":
        path.write_bytes(written[: len(written) // 2])
    elif shape == "no testsuite":
        path = _report(tmp_path, written)
    result = _gate(path)
    assert result.returncode == 1
    # The counters are printed only after the report is read.
    assert result.stdout == ""


def test_negative_allowance_is_refused(tmp_path: Path, written: bytes) -> None:
    report = tmp_path / "junit.xml"
    report.write_bytes(written)
    result = _gate(report, "--max-skipped", "-1")
    assert result.returncode == 2
    assert result.stdout == ""


@pytest.mark.parametrize(
    ("test_body", "skipped", "gate_exit"),
    [
        ("@pytest.mark.skip\ndef test_a():\n    pass\n", 1, 1),
        ("def test_a():\n    pass\n", 0, 0),
    ],
)
def test_report_written_by_pytest(
    tmp_path: Path, test_body: str, skipped: int, gate_exit: int
) -> None:
    run = _pytest(tmp_path, test_body)
    # pytest exits 0 in both cases, including when every test was skipped.
    assert run.returncode == 0, run.stdout
    result = _gate(tmp_path / "junit.xml")
    assert result.returncode == gate_exit
    assert _printed(result) == {"tests": 1, "failures": 0, "errors": 0, "skipped": skipped}
