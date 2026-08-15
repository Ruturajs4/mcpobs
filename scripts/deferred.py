"""Print the open deferral register.

Deferrals are decisions to accept unknown risk for a while. That is often right;
losing track of WHICH risks you accepted is not. On 15 Aug 2026 three deferrals
phrased as "test that in staging" were concealing two real defects in the
pipeline's deduplication, neither of which errored.

    python scripts/deferred.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REGISTER = Path(__file__).resolve().parent.parent / "docs" / "deferred.md"
ROW = re.compile(r"^\|\s*(DF-\d+)\s*\|\s*(.+?)\s*\|")


def strip_markdown(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    return text.replace("`", "")


def main() -> int:
    if not REGISTER.exists():
        print(f"missing {REGISTER}", file=sys.stderr)
        return 1

    # DF-C* entries live in the Closed section and are excluded by the pattern,
    # which matches digits only.
    rows = [
        (match.group(1), strip_markdown(match.group(2)))
        for line in REGISTER.read_text(encoding="utf-8").splitlines()
        if (match := ROW.match(line))
    ]

    print("OPEN DEFERRALS")
    print("=" * 74)
    for ident, summary in rows:
        # Split on a sentence boundary, not any period: "min.insync.replicas"
        # and "db.operation" are not sentence ends.
        first = re.split(r"\.\s+(?=[A-Z(])", summary)[0].rstrip(".")
        print(f"  {ident:<7} {first[:66]}")
    print()
    print(f"  {len(rows)} open -> docs/deferred.md for risk and trigger")
    print("  Reviewed at the end of each engineering day.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
