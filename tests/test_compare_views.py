"""The upgrade-verification helper in tools/compare_views.py."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import compare_views  # noqa: E402


def _write(path: Path, rows) -> Path:
    import csv
    with path.open("w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(rows)
    return path


BEFORE = [
    ["Student", "2026-08-24", "2026-08-26", "Classes Attended"],
    ["Alina Ashworth", "P", "P", "2"],
    ["Bruno Beck", "", "P", "1"],
]


def test_identical_tables_report_no_difference(tmp_path, capsys):
    a = _write(tmp_path / "a.csv", BEFORE)
    b = _write(tmp_path / "b.csv", BEFORE)
    assert compare_views.compare(a, b) == 0
    assert "No differences" in capsys.readouterr().out


def test_a_backfilled_student_is_pinpointed(tmp_path, capsys):
    after = [r[:] for r in BEFORE]
    after[2] = ["Bruno Beck", "P", "P", "2"]      # week 1 backfilled
    a = _write(tmp_path / "a.csv", BEFORE)
    b = _write(tmp_path / "b.csv", after)

    assert compare_views.compare(a, b) == 1
    out = capsys.readouterr().out
    assert "CHANGED  Bruno Beck" in out
    assert "2026-08-24: '' -> 'P'" in out
    assert "Alina Ashworth" not in out          # unchanged rows stay quiet


def test_added_and_removed_students_are_listed(tmp_path, capsys):
    after = [BEFORE[0], BEFORE[1], ["Cleo Cardoso", "P", "", "1"]]
    a = _write(tmp_path / "a.csv", BEFORE)
    b = _write(tmp_path / "b.csv", after)

    assert compare_views.compare(a, b) == 1
    out = capsys.readouterr().out
    assert "MISSING FROM AFTER   Bruno Beck" in out
    assert "NEW IN AFTER         Cleo Cardoso" in out


def test_renamed_columns_are_reported_and_skipped(tmp_path, capsys):
    after = [
        ["Student", "2026-08-24", "2026-08-31", "Classes Attended"],
        ["Alina Ashworth", "P", "P", "2"],
        ["Bruno Beck", "", "P", "1"],
    ]
    a = _write(tmp_path / "a.csv", BEFORE)
    b = _write(tmp_path / "b.csv", after)

    compare_views.compare(a, b)
    out = capsys.readouterr().out
    assert "COLUMN DIFFERENCES" in out
    assert "only in before: '2026-08-26'" in out
    assert "only in after : '2026-08-31'" in out


def test_empty_file_is_an_error(tmp_path):
    empty = tmp_path / "empty.csv"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ValueError):
        compare_views.read_table(empty)
