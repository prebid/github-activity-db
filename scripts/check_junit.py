"""Fail unless a pytest JUnit report shows an executed, passing run.

pytest exits 0 when every collected test is skipped. This gate reads the report instead:
at least one test executed, none failed or errored, and no more skips than allowed.
With ``--same-as``, it also fails unless a second report holds the same tests, each with the
same outcome, so two runs of the suite can be shown to agree (redesign plan D40). A test pytest
reports in more than one <testcase> (it writes a second one for a teardown error after a failure)
has the outcomes of all of them, sorted and joined: ``error+failed``.

Usage: python scripts/check_junit.py REPORT.xml [--max-skipped N] [--same-as OTHER.xml]
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

COUNTED = ("tests", "failures", "errors", "skipped")


def read_counts(report: Path) -> dict[str, int]:
    """Sum the counters over every <testsuite> in the report."""
    root = ET.parse(report).getroot()
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    if not suites:
        raise ValueError(f"no <testsuite> element in {report}")
    return {key: sum(int(suite.get(key, "0")) for suite in suites) for key in COUNTED}


def read_outcomes(report: Path) -> dict[str, str]:
    """Each test's id (classname::name) and its outcome: passed, failed, error, or skipped, or
    the outcomes of each <testcase> that holds the test, joined: ``error+failed``."""
    root = ET.parse(report).getroot()
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    if not suites:
        raise ValueError(f"no <testsuite> element in {report}")
    outcomes: dict[str, set[str]] = {}
    for suite in suites:
        for case in suite.iter("testcase"):
            test = f"{case.get('classname', '')}::{case.get('name', '')}"
            outcome = "passed"
            for kind in ("failure", "error", "skipped"):
                if case.find(kind) is not None:
                    outcome = "failed" if kind == "failure" else kind
                    break
            outcomes.setdefault(test, set()).add(outcome)
    return {test: "+".join(sorted(found)) for test, found in outcomes.items()}


def differences(first: dict[str, str], second: dict[str, str]) -> list[str]:
    """Every test the two reports do not agree on, sorted."""
    return [
        f"{test}: {first.get(test, 'absent')} vs {second.get(test, 'absent')}"
        for test in sorted(first.keys() | second.keys())
        if first.get(test) != second.get(test)
    ]


def problems(counts: dict[str, int], max_skipped: int) -> list[str]:
    executed = counts["tests"] - counts["skipped"]
    found = []
    if executed <= 0:
        found.append(f"no test executed ({counts['tests']} collected, {counts['skipped']} skipped)")
    if counts["failures"]:
        found.append(f"{counts['failures']} failed")
    if counts["errors"]:
        found.append(f"{counts['errors']} errored")
    if counts["skipped"] > max_skipped:
        found.append(f"{counts['skipped']} skipped, more than the allowed {max_skipped}")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--max-skipped", type=int, default=0)
    parser.add_argument("--same-as", type=Path, help="a second report that must agree test by test")
    args = parser.parse_args(argv)
    if args.max_skipped < 0:
        parser.error("--max-skipped must be 0 or more")
    try:
        counts = read_counts(args.report)
        compared = (
            differences(read_outcomes(args.report), read_outcomes(args.same_as))
            if args.same_as is not None
            else []
        )
    except (OSError, ET.ParseError, ValueError) as e:
        print(f"cannot read JUnit report: {e}", file=sys.stderr)
        return 1
    print(counts)
    found = problems(counts, args.max_skipped) + [f"differs: {line}" for line in compared]
    for problem in found:
        print(f"FAIL: {problem}", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
