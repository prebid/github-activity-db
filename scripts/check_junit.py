"""Fail unless a pytest JUnit report shows an executed, passing run.

pytest exits 0 when every collected test is skipped. This gate reads the report instead:
at least one test executed, none failed or errored, and no more skips than allowed.

Usage: python scripts/check_junit.py REPORT.xml [--max-skipped N]
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
    args = parser.parse_args(argv)
    if args.max_skipped < 0:
        parser.error("--max-skipped must be 0 or more")
    try:
        counts = read_counts(args.report)
    except (OSError, ET.ParseError, ValueError) as e:
        print(f"cannot read JUnit report: {e}", file=sys.stderr)
        return 1
    print(counts)
    found = problems(counts, args.max_skipped)
    for problem in found:
        print(f"FAIL: {problem}", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
