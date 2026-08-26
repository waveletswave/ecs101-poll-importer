#!/usr/bin/env python3
"""Compare two Attendance / Scores / Leaderboard tables and report what changed.

Built for verifying a v2.x to v3 upgrade without touching the live sheet:

    # 1. Download the current Attendance tab from Google Sheets as CSV.
    # 2. Produce the v3 view locally, writing nothing to Google:
    python ecs101_poll_importer.py polls/ --dry-run \
        --roster-csv rosters/canvas.csv --output-dir preview
    # 3. See exactly which students the two disagree about:
    python tools/compare_views.py ~/Downloads/Attendance.csv preview/attendance_preview.csv

Any student listed is one whose record changes. On an upgrade the expected
finding is students gaining attendance or points they had been missing, which
is the v2.3.1 backfill defect showing itself.

Exit status is 0 when the tables agree and 1 when they differ, so this can be
dropped into a check script.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Dict, List, Tuple


def read_table(path: Path) -> Tuple[List[str], Dict[str, Dict[str, str]]]:
    """Read a view CSV keyed by the first column (the student name)."""
    with Path(path).open("r", encoding="utf-8-sig", newline="") as f:
        rows = [r for r in csv.reader(f) if any(c.strip() for c in r)]
    if not rows:
        raise ValueError(f"{path} is empty")

    header = [c.strip() for c in rows[0]]
    table: Dict[str, Dict[str, str]] = {}
    for row in rows[1:]:
        padded = list(row) + [""] * (len(header) - len(row))
        key = padded[0].strip()
        if not key:
            continue
        table[key] = {
            header[i]: padded[i].strip()
            for i in range(1, len(header))
            if header[i]
        }
    return header, table


def compare(before: Path, after: Path) -> int:
    head_a, table_a = read_table(before)
    head_b, table_b = read_table(after)

    print(f"before : {before}  ({len(table_a)} rows, {len(head_a)} columns)")
    print(f"after  : {after}  ({len(table_b)} rows, {len(head_b)} columns)")
    print()

    differences = 0

    cols_a, cols_b = set(head_a[1:]), set(head_b[1:])
    only_a, only_b = sorted(cols_a - cols_b), sorted(cols_b - cols_a)
    if only_a or only_b:
        print("COLUMN DIFFERENCES")
        for c in only_a:
            print(f"  - only in before: {c!r}")
        for c in only_b:
            print(f"  + only in after : {c!r}")
        print("  (column names differ, e.g. a renamed question. Only shared "
              "columns are compared below.)")
        print()

    names_a, names_b = set(table_a), set(table_b)
    for name in sorted(names_a - names_b):
        print(f"MISSING FROM AFTER   {name}")
        differences += 1
    for name in sorted(names_b - names_a):
        print(f"NEW IN AFTER         {name}")
        differences += 1
    if names_a - names_b or names_b - names_a:
        print()

    shared_cols = [c for c in head_b[1:] if c in cols_a]
    changed = 0
    for name in sorted(names_a & names_b, key=str.casefold):
        deltas = [
            (col, table_a[name].get(col, ""), table_b[name].get(col, ""))
            for col in shared_cols
            if table_a[name].get(col, "") != table_b[name].get(col, "")
        ]
        if not deltas:
            continue
        changed += 1
        differences += len(deltas)
        print(f"CHANGED  {name}")
        for col, was, now in deltas:
            print(f"           {col}: {was!r} -> {now!r}")

    print()
    print("=" * 60)
    if differences == 0:
        print(f"No differences. {len(names_a & names_b)} rows match on "
              f"{len(shared_cols)} shared column(s).")
        return 0
    print(f"{differences} difference(s) across {changed} student(s).")
    print("On a v2.x to v3 upgrade, students gaining attendance or points is "
          "the expected direction: those are records the old backfill defect "
          "had left unmatched.")
    return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare two Attendance / Scores / Leaderboard CSV exports."
    )
    parser.add_argument("before", type=Path, help="Baseline CSV, e.g. the current Google Sheet tab")
    parser.add_argument("after", type=Path, help="New CSV, e.g. a v3 dry-run preview")
    args = parser.parse_args(argv)
    try:
        return compare(args.before, args.after)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
