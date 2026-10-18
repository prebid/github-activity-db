"""The CI gate over pytest's JUnit report."""

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_junit.py"


def _report(tmp_path: Path, *suites: tuple[int, int, int, int]) -> Path:
    body = "".join(
        f'<testsuite name="pytest" tests="{t}" failures="{f}" errors="{e}" skipped="{s}"/>'
        for t, f, e, s in suites
    )
    path = tmp_path / "junit.xml"
    path.write_text(f'<?xml version="1.0"?><testsuites>{body}</testsuites>')
    return path


def _gate(report: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(report), *extra],
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize(
    ("counts", "reason"),
    [
        ((0, 0, 0, 0), "no test executed"),
        ((5, 0, 0, 5), "no test executed"),
        ((5, 1, 0, 0), "1 failed"),
        ((5, 0, 1, 0), "1 errored"),
        ((5, 0, 0, 1), "more than the allowed 0"),
    ],
)
def test_rejects(tmp_path: Path, counts: tuple[int, int, int, int], reason: str) -> None:
    result = _gate(_report(tmp_path, counts))
    assert result.returncode == 1
    assert reason in result.stderr


def test_accepts_an_executed_passing_run(tmp_path: Path) -> None:
    assert _gate(_report(tmp_path, (5, 0, 0, 0))).returncode == 0


def test_partial_skips_need_an_explicit_allowance(tmp_path: Path) -> None:
    report = _report(tmp_path, (5, 0, 0, 2))
    assert _gate(report).returncode == 1
    assert _gate(report, "--max-skipped", "2").returncode == 0


def test_sums_every_testsuite(tmp_path: Path) -> None:
    result = _gate(_report(tmp_path, (3, 0, 0, 0), (2, 1, 0, 0)))
    assert result.returncode == 1
    assert "'tests': 5" in result.stdout
    assert "1 failed" in result.stderr


@pytest.mark.parametrize("content", [None, "<not xml", '<?xml version="1.0"?><testsuites/>'])
def test_unreadable_report_fails(tmp_path: Path, content: str | None) -> None:
    path = tmp_path / "junit.xml"
    if content is not None:
        path.write_text(content)
    result = _gate(path)
    assert result.returncode == 1
    assert "cannot read JUnit report" in result.stderr


def test_negative_allowance_is_refused(tmp_path: Path) -> None:
    result = _gate(_report(tmp_path, (5, 0, 0, 0)), "--max-skipped", "-1")
    assert result.returncode == 2
    assert "--max-skipped must be 0 or more" in result.stderr


@pytest.mark.parametrize(
    ("test_body", "gate_exit"),
    [
        ("@pytest.mark.skip\ndef test_a():\n    pass\n", 1),
        ("def test_a():\n    pass\n", 0),
    ],
)
def test_report_written_by_pytest(tmp_path: Path, test_body: str, gate_exit: int) -> None:
    (tmp_path / "test_sample.py").write_text(f"import pytest\n\n\n{test_body}")
    report = tmp_path / "junit.xml"
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={report}"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    # pytest exits 0 in both cases, including when every test was skipped.
    assert run.returncode == 0, run.stdout
    assert _gate(report).returncode == gate_exit
