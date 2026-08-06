"""Fail a CI job whose test suite silently skipped the work that matters.

A green build that tested nothing is worse than a red one, and that is exactly what
happened here: a Windows job reported success while every conversion test skipped
for want of a toolchain, and nobody noticed because the total looked fine.

So rather than counting skips against a magic threshold (which needs re-tuning every
time the suite grows), this asserts the specific suites that convert real
repositories actually ran. Skips elsewhere are legitimate and platform-dependent:
the native-vs-git-svn comparison cannot run without git-svn, and that is the whole
point of the platforms that omit it.

Usage:
    pytest --junit-xml=results.xml
    python tests/assert_suite_ran.py results.xml
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Tuple

# Test modules that exercise a real conversion. If any of these skip, the machine
# cannot do the thing this project exists to do, whatever the totals say.
MUST_RUN = ("test_e2e", "test_push_integration", "test_native_engine")

# Individual tests allowed to skip even inside those modules, with the reason they
# are exempt. Keep this list short and justified.
ALLOWED_SKIPS = {
    "test_native_trees_are_identical_to_git_svn":
        "needs git-svn, which some platforms deliberately do not install",
    "test_pipeline_runs_with_the_git_svn_engine_explicitly":
        "needs git-svn, which some platforms deliberately do not install",
}


def load(path: Path) -> Tuple[int, int, List[Tuple[str, str, str]]]:
    tree = ET.parse(path)
    root = tree.getroot()
    suites = root.findall("testsuite") or [root]

    total = skipped = 0
    details: List[Tuple[str, str, str]] = []
    for suite in suites:
        total += int(suite.get("tests", 0))
        skipped += int(suite.get("skipped", 0))
        for case in suite.iter("testcase"):
            skip = case.find("skipped")
            if skip is not None:
                details.append((case.get("classname", ""), case.get("name", ""),
                                skip.get("message", "")))
    return total, skipped, details


def main(argv: List[str]) -> int:
    if len(argv) < 2:
        print("usage: assert_suite_ran.py <junit-xml>", file=sys.stderr)
        return 2
    report = Path(argv[1])
    if not report.is_file():
        print(f"junit report not found: {report}", file=sys.stderr)
        return 2

    total, skipped, details = load(report)
    print(f"ran {total - skipped} of {total} tests ({skipped} skipped)")

    if details:
        print("\nSkipped:")
        for classname, name, message in details:
            print(f"  {classname}::{name}\n      {message}")

    offenders = [
        (classname, name)
        for classname, name, _message in details
        if any(module in classname for module in MUST_RUN)
        and name.split("[")[0] not in ALLOWED_SKIPS
    ]

    if offenders:
        print("\nERROR: the conversion suite did not run:", file=sys.stderr)
        for classname, name in offenders:
            print(f"  {classname}::{name}", file=sys.stderr)
        print("\nThis platform cannot convert a repository, so a passing build here "
              "would be meaningless. Check the toolchain.", file=sys.stderr)
        return 1

    print("\nConversion suite ran.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
