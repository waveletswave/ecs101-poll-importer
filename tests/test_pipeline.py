"""Whole runs of the importer against an in-memory spreadsheet.

The unit tests elsewhere exercise one function at a time. The defects here
lived between functions: a match made while one command ran was saved wrongly
and undone by a later command. Only running the commands in sequence, the way
a TA does over several weeks, shows that.

Every person is invented and every address uses example.edu.
"""

from __future__ import annotations

import json
import types
from pathlib import Path
from typing import Dict, List

import pytest

from ecs101 import cli, pipeline
from ecs101.parsers import parse_poll_everywhere_export


class FakeSpreadsheet:
    """Answers the handful of Sheets API calls that SheetIO makes."""

    title = "ECS101 test sheet"

    def __init__(self) -> None:
        self.tabs: Dict[str, List[List[str]]] = {}
        self.sheet_ids: Dict[str, int] = {}
        self.value_writes = 0

    def fetch_sheet_metadata(self):
        return {"sheets": [
            {"properties": {"title": title, "sheetId": sheet_id,
                            "gridProperties": {"rowCount": 1000, "columnCount": 60}}}
            for title, sheet_id in self.sheet_ids.items()
        ]}

    def add_worksheet(self, title, rows, cols):
        self.sheet_ids[title] = len(self.sheet_ids) + 1
        self.tabs[title] = []
        return types.SimpleNamespace(id=self.sheet_ids[title])

    def values_batch_get(self, ranges):
        return {"valueRanges": [{"values": self.tabs.get(r.strip("'"), [])} for r in ranges]}

    def batch_update(self, body):
        return {}

    def values_batch_update(self, body):
        # SheetIO writes each tab as one rectangle from A1 and then clears
        # whatever lies outside it, so replacing the tab is the net effect.
        self.value_writes += 1
        for block in body["data"]:
            title = block["range"].rsplit("!", 1)[0].strip("'")
            self.tabs[title] = [list(row) for row in block["values"]]
        return {}

    def values_batch_clear(self, body=None, params=None):
        return {}

    def records(self, title: str) -> List[Dict[str, str]]:
        rows = self.tabs.get(title, [])
        if not rows:
            return []
        return [dict(zip(rows[0], row)) for row in rows[1:] if any(row)]


CANVAS_HEADER = "Student,ID,SIS User ID,SIS Login ID,Section,Assignment 1\n    Points Possible,,,,,10\n"
ROWAN = ("Rowan", "Fletcher", '"Fletcher, Rowan",100001,9000001,rowanf,ECS101-01,9\n')
NOOR = ("Noor", "Haddad", '"Haddad, Noor",100003,9000003,noorh,ECS101-01,10\n')
# Joins during drop/add. The login is a NetID, so the only automatic route to
# a match is the first.last address, as it was for the student who exposed the
# defect.
LATE = ("Marguerite", "Ellsworth", '"Ellsworth, Marguerite",100007,9000007,me318,ECS101-01,8\n')

LECTURE_HEADER = (
    "Response #,Started At (CDT),Participant First Name,Participant Last Name,"
    "Email,Custom Report ID,Screen Name,Public ID,"
    "Where is this photo taken?,Which mineral is the hardest?\n"
)


def canvas(tmp_path: Path, name: str, *students) -> Path:
    path = tmp_path / name
    path.write_text(CANVAS_HEADER + "".join(s[2] for s in students), encoding="utf-8")
    return path


def lecture(tmp_path: Path, name: str, day: str, *students) -> Path:
    """A lecture export in which each listed student answered both questions."""
    rows = [
        f"{i},{day} 12:40 PM,{first},{last},{first.lower()}.{last.lower()}@example.edu,,"
        f"{first} {last[0]},{700000 + i},Somewhere,Diamond\n"
        for i, (first, last, _) in enumerate(students, start=1)
    ]
    path = tmp_path / name
    path.write_text(LECTURE_HEADER + "".join(rows), encoding="utf-8")
    return path


def parsed(*paths: Path):
    out = []
    for path in paths:
        polls, _ = parse_poll_everywhere_export(path, "America/New_York", "CDT")
        out.extend(polls)
    return out


@pytest.fixture
def sheet(monkeypatch) -> FakeSpreadsheet:
    fake = FakeSpreadsheet()
    monkeypatch.setattr(pipeline, "open_spreadsheet", lambda config: fake)
    return fake


@pytest.fixture
def config(tmp_path: Path) -> Dict[str, str]:
    return {"spreadsheet_id": "test-sheet", "backup_dir": str(tmp_path / "backups")}


def attendance_of(sheet: FakeSpreadsheet, student: str) -> Dict[str, str]:
    for row in sheet.records("Attendance"):
        if row["Student"] == student:
            return row
    raise AssertionError(f"{student} is not on the Attendance tab")


# ---------------------------------------------------------------------------
# A student who joins late must keep the classes they attended, including the
# ones from before Canvas listed them, through every later import.
# ---------------------------------------------------------------------------

def test_a_late_addition_keeps_earlier_classes_through_an_import_they_miss(
    tmp_path, sheet, config, scripted, silent
):
    pipeline.sync_canvas_roster(
        canvas(tmp_path, "roster_v1.csv", ROWAN, NOOR), config,
        input_fn=scripted(), output_fn=silent,
    )

    # Not on the roster yet, so the only possible answer at review is S.
    pipeline.import_polls(
        parsed(lecture(tmp_path, "Lecture4.csv", "09/02/26", ROWAN, NOOR, LATE)), config,
        input_fn=scripted("S"), output_fn=silent,
    )

    # Drop/add ends and the new roster lists them. Their address now matches.
    pipeline.sync_canvas_roster(
        canvas(tmp_path, "roster_v2.csv", ROWAN, NOOR, LATE), config,
        input_fn=scripted(), output_fn=silent,
    )
    pipeline.import_polls(
        parsed(lecture(tmp_path, "Lecture5.csv", "09/09/26", ROWAN, LATE)), config,
        input_fn=scripted(), output_fn=silent,
    )

    # The first import they are absent from. This is the one that used to
    # blank every earlier match, because the Participant Map still held the
    # row saved before they were on the roster.
    pipeline.import_polls(
        parsed(lecture(tmp_path, "Lecture7.csv", "09/16/26", ROWAN, NOOR)), config,
        input_fn=scripted(), output_fn=silent,
    )

    row = attendance_of(sheet, "Marguerite Ellsworth")
    assert [row["2026-09-02"], row["2026-09-09"], row["2026-09-16"]] == ["P", "P", ""]
    assert row["Classes Attended"] == "2"

    saved = {r["Poll Key"]: r for r in sheet.records("Participant Map")}
    entry = saved["email:marguerite.ellsworth@example.edu"]
    assert (entry["Student Key"], entry["Match Type"]) == ("canvas:100007", "email-name")


# ---------------------------------------------------------------------------
# Scoring questions are asked only for exports that will be imported.
# ---------------------------------------------------------------------------

def test_imported_exports_are_skipped_before_any_scoring_question(
    tmp_path, sheet, config, scripted, silent
):
    pipeline.sync_canvas_roster(
        canvas(tmp_path, "roster.csv", ROWAN, NOOR), config,
        input_fn=scripted(), output_fn=silent,
    )
    first = lecture(tmp_path, "Lecture4.csv", "09/02/26", ROWAN, NOOR)
    second = lecture(tmp_path, "Lecture5.csv", "09/09/26", ROWAN)

    asked: List[List[str]] = []

    def configure(polls):
        asked.append(sorted({p.path.name for p in polls}))

    pipeline.import_polls(parsed(first), config, configure=configure,
                          input_fn=scripted(), output_fn=silent)
    pipeline.import_polls(parsed(first, second), config, configure=configure,
                          input_fn=scripted(), output_fn=silent)
    assert asked == [["Lecture4.csv"], ["Lecture5.csv"]]

    writes = sheet.value_writes
    backups = sorted(Path(config["backup_dir"]).iterdir())
    printed: List[str] = []
    pipeline.import_polls(parsed(first, second), config, configure=configure,
                          input_fn=scripted(), output_fn=printed.append)

    assert asked == [["Lecture4.csv"], ["Lecture5.csv"]], "nothing new, so nothing asked"
    assert sheet.value_writes == writes, "nothing new, so nothing written"
    assert sorted(Path(config["backup_dir"]).iterdir()) == backups
    assert any("Nothing new to import" in line for line in printed)


def test_an_expired_token_is_reported_before_any_scoring_question(
    tmp_path, monkeypatch, capsys
):
    """A TA once answered every scoring question for seven lectures, only to
    be told at the end that the Google authorization had expired."""
    day = tmp_path / "polls" / "2026-09-02"
    day.mkdir(parents=True)
    lecture(day, "Lecture4.csv", "09/02/26", ROWAN, NOOR)
    settings = tmp_path / "config.json"
    settings.write_text(json.dumps({
        "spreadsheet_id": "test-sheet",
        "backup_dir": str(tmp_path / "backups"),
    }), encoding="utf-8")

    def expired(config):
        raise RuntimeError("The saved Google authorization has expired or been revoked.")

    asked = []
    monkeypatch.setattr(pipeline, "open_spreadsheet", expired)
    monkeypatch.setattr(cli, "configure_daily_scoring",
                        lambda polls, flagged=None: asked.append(polls))

    assert cli.main([str(tmp_path / "polls"), "--config", str(settings)]) == 3
    assert asked == []
    assert "expired" in capsys.readouterr().err
