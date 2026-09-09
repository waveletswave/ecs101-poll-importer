"""The Excused tab: the one tab the instructor owns and the importer only reads.

Every other tab is rebuilt from scratch on each import, so an annotation made
directly on Attendance would be silently overwritten. Keeping the source of
truth in its own tab is what makes the annotation durable.
"""

from __future__ import annotations

from ecs101.records import parse_excused_rows
from ecs101.views import build_attendance_review, build_derived_tables

ROSTER = [
    {"Student Key": "canvas:1", "Student Name": "Alina Ashworth", "Active": "TRUE"},
    {"Student Key": "canvas:2", "Student Name": "Bruno Beck", "Active": "TRUE"},
    {"Student Key": "canvas:3", "Student Name": "Cleo Cardoso", "Active": "TRUE"},
]

QUESTIONS = [
    {"Date": "2026-08-24", "Question ID": "2026-08-24::Q1", "Question": "Q1",
     "Question Order": "1", "Scored": "TRUE"},
    {"Date": "2026-08-26", "Question ID": "2026-08-26::Q1", "Question": "Q1",
     "Question Order": "1", "Scored": "TRUE"},
]

DATES = ["2026-08-24", "2026-08-26"]


def _resp(date, qid, skey, name, correct="1"):
    return {
        "Date": date, "Question ID": qid, "Student Key": skey, "Student Name": name,
        "Poll Key": f"email:{skey}@x.edu", "Poll Participant": name, "Poll Email": "",
        "Screen Name": "", "Response": "A", "Correct": correct,
        "Timestamp": f"{date}T13:30:00-04:00", "File Hash": "abc123def456",
        "Match Status": "confirmed",
    }


# ---------------------------------------------------------------------------
# Resolving the instructor's rows
# ---------------------------------------------------------------------------

def test_a_row_resolves_by_student_key():
    excused, problems = parse_excused_rows(
        [{"Date": "2026-08-24", "Student Key": "canvas:2", "Student Name": "",
          "Reason": "Varsity travel"}],
        ROSTER, DATES,
    )
    assert excused == {("canvas:2", "2026-08-24"): "Varsity travel"}
    assert problems == []


def test_a_row_resolves_by_name_alone():
    excused, problems = parse_excused_rows(
        [{"Date": "2026-08-26", "Student Key": "", "Student Name": "cleo  cardoso",
          "Reason": "Illness"}],
        ROSTER, DATES,
    )
    assert excused == {("canvas:3", "2026-08-26"): "Illness"}
    assert problems == []


def test_blank_rows_are_ignored():
    excused, problems = parse_excused_rows(
        [{"Date": "", "Student Key": "", "Student Name": "", "Reason": ""}],
        ROSTER, DATES,
    )
    assert excused == {} and problems == []


def test_an_unknown_name_is_reported_not_dropped():
    _, problems = parse_excused_rows(
        [{"Date": "2026-08-24", "Student Key": "", "Student Name": "Nobody Here",
          "Reason": ""}],
        ROSTER, DATES,
    )
    assert len(problems) == 1
    assert "Nobody Here" in problems[0] and "row 2" in problems[0]


def test_an_ambiguous_name_asks_for_the_key():
    roster = ROSTER + [
        {"Student Key": "canvas:4", "Student Name": "Alina Ashworth", "Active": "TRUE"},
    ]
    _, problems = parse_excused_rows(
        [{"Date": "2026-08-24", "Student Key": "", "Student Name": "Alina Ashworth",
          "Reason": ""}],
        roster, DATES,
    )
    assert "more than one student" in problems[0]
    assert "Student Key" in problems[0]


def test_a_date_with_no_imported_questions_is_reported():
    _, problems = parse_excused_rows(
        [{"Date": "2026-09-14", "Student Key": "canvas:1", "Student Name": "", "Reason": ""}],
        ROSTER, DATES,
    )
    assert "not a class date" in problems[0]


def test_a_key_and_name_that_disagree_are_flagged_and_the_key_wins():
    excused, problems = parse_excused_rows(
        [{"Date": "2026-08-24", "Student Key": "canvas:1", "Student Name": "Bruno Beck",
          "Reason": "Conference"}],
        ROSTER, DATES,
    )
    assert excused == {("canvas:1", "2026-08-24"): "Conference"}
    assert "Using the key" in problems[0]


# ---------------------------------------------------------------------------
# Rendering into the views
# ---------------------------------------------------------------------------

def test_attendance_shows_e_and_counts_it_separately():
    responses = [_resp("2026-08-24", "2026-08-24::Q1", "canvas:1", "Alina Ashworth")]
    excused = {("canvas:1", "2026-08-26"): "Illness"}

    attendance, _, _ = build_derived_tables(QUESTIONS, responses, ROSTER, excused)
    alina = next(r for r in attendance[1:] if r[0] == "Alina Ashworth")

    assert alina == ["Alina Ashworth", "P", "E", "1", "1"]


def test_classes_attended_does_not_count_an_excused_absence():
    """Settled with the instructor: the plain total stays a count of classes attended."""
    excused = {("canvas:2", "2026-08-24"): "", ("canvas:2", "2026-08-26"): ""}
    attendance, _, _ = build_derived_tables(QUESTIONS, [], ROSTER, excused)
    bruno = next(r for r in attendance[1:] if r[0] == "Bruno Beck")
    assert bruno[3] == "0"     # Classes Attended
    assert bruno[4] == "2"     # Excused


def test_a_matched_response_beats_an_excused_row():
    """If the student actually answered, they were there, whatever the tab says."""
    responses = [_resp("2026-08-24", "2026-08-24::Q1", "canvas:1", "Alina Ashworth")]
    excused = {("canvas:1", "2026-08-24"): "Illness"}
    attendance, _, _ = build_derived_tables(QUESTIONS, responses, ROSTER, excused)
    alina = next(r for r in attendance[1:] if r[0] == "Alina Ashworth")
    assert alina[1] == "P"
    assert alina[4] == "0"


def test_scores_are_untouched_by_an_excused_absence():
    """Also settled: an excused student did not answer, and Scores says so."""
    excused = {("canvas:1", "2026-08-24"): "Illness"}
    _, with_excused, _ = build_derived_tables(QUESTIONS, [], ROSTER, excused)
    _, without, _ = build_derived_tables(QUESTIONS, [], ROSTER, None)
    assert with_excused == without


def test_review_marks_the_student_excused_with_the_reason():
    excused = {("canvas:2", "2026-08-24"): "Varsity travel"}
    review = build_attendance_review(QUESTIONS, [], ROSTER, excused=excused)

    rows = [r for r in review if len(r) == 5 and r[1] == "canvas:2" and r[0] == "2026-08-24"]
    assert rows and rows[0][3] == "Excused"
    assert rows[0][4] == "Varsity travel"


def test_review_summary_separates_excused_from_missing():
    responses = [_resp("2026-08-24", "2026-08-24::Q1", "canvas:1", "Alina Ashworth")]
    excused = {("canvas:2", "2026-08-24"): "Varsity travel"}
    review = build_attendance_review(QUESTIONS, responses, ROSTER, excused=excused)

    header = review.index(["Class Summary"])
    assert review[header + 1][2:5] == ["Present", "Excused", "No matched Poll response"]
    row = review[header + 2]
    assert row[0] == "2026-08-24"
    assert row[2] == "1"    # present
    assert row[3] == "1"    # excused
    assert row[4] == "1"    # genuinely unaccounted for


def test_views_are_unchanged_when_the_tab_is_empty():
    responses = [_resp("2026-08-24", "2026-08-24::Q1", "canvas:1", "Alina Ashworth")]
    a1, s1, l1 = build_derived_tables(QUESTIONS, responses, ROSTER, {})
    a2, s2, l2 = build_derived_tables(QUESTIONS, responses, ROSTER, None)
    assert (a1, s1, l1) == (a2, s2, l2)
    assert all(cell != "E" for row in a1[1:] for cell in row)


# ---------------------------------------------------------------------------
# The tab is created once and then left alone. This is the whole point: any
# annotation the importer could overwrite is an annotation that will be lost.
# ---------------------------------------------------------------------------

def _fake_sheet(existing, recorder):
    class FakeSheet:
        title = "ECS101"

        def fetch_sheet_metadata(self):
            return {"sheets": [{"properties": {
                "title": t, "sheetId": i,
                "gridProperties": {"rowCount": 200, "columnCount": 12},
            }} for i, t in enumerate(existing)]}

        def add_worksheet(self, title, rows, cols):
            recorder.setdefault("created", []).append(title)
            existing.append(title)
            return type("WS", (), {"id": 99})()

        def values_batch_get(self, ranges):
            return {"valueRanges": [{"values": []} for _ in ranges]}

        def batch_update(self, body):
            return {}

        def values_batch_update(self, body):
            recorder.setdefault("written", []).extend(
                d["range"].split("!")[0].strip("'") for d in body["data"]
            )
            recorder["rows"] = {
                d["range"].split("!")[0].strip("'"): d["values"] for d in body["data"]
            }
            return {}

        def values_batch_clear(self, params=None, body=None):
            return {}

    return FakeSheet()


def test_the_tab_is_created_with_headers_and_a_worked_example():
    from ecs101.models import EXCUSED_HEADERS
    from ecs101.sheets import SheetIO, WORKSHEET_TITLES

    tabs = [t for t in WORKSHEET_TITLES.values() if t != "Excused"]
    rec = {}
    io = SheetIO(_fake_sheet(tabs, rec))
    io.ensure_worksheets()
    io.commit()

    assert rec["created"] == ["Excused"]
    seeded = rec["rows"]["Excused"]
    assert seeded[0][:len(EXCUSED_HEADERS)] == EXCUSED_HEADERS
    # An example the instructor can copy the shape from, in every column.
    assert all(cell for cell in seeded[1][:4])
    assert seeded[1][0].startswith("#")


def test_the_seeded_example_never_becomes_a_warning():
    """It sits in the tab for the whole semester, so it must stay silent."""
    from ecs101.models import EXCUSED_SEED_ROWS, EXCUSED_HEADERS

    rows = [dict(zip(EXCUSED_HEADERS, list(r) + [""] * 4)) for r in EXCUSED_SEED_ROWS]
    excused, problems = parse_excused_rows(rows, ROSTER, DATES)
    assert excused == {}
    assert problems == []


def test_removing_the_hash_activates_a_row():
    from ecs101.models import EXCUSED_HEADERS

    row = dict(zip(EXCUSED_HEADERS, ["2026-08-26", "canvas:2", "Bruno Beck", "Travel"]))
    excused, problems = parse_excused_rows([row], ROSTER, DATES)
    assert excused == {("canvas:2", "2026-08-26"): "Travel"}
    assert problems == []


def test_a_hand_written_comment_is_also_ignored():
    excused, problems = parse_excused_rows(
        [{"Date": "# waiting to hear back from the registrar", "Student Key": "",
          "Student Name": "", "Reason": ""}],
        ROSTER, DATES,
    )
    assert excused == {} and problems == []


def test_an_existing_tab_is_never_written_to_again():
    from ecs101.sheets import SheetIO, WORKSHEET_TITLES

    tabs = list(WORKSHEET_TITLES.values())          # Excused already there
    rec = {}
    io = SheetIO(_fake_sheet(tabs, rec))
    io.ensure_worksheets()
    for title in tabs:
        if title != "Excused":
            io.stage(title, [["h"], ["v"]])
    io.commit()

    assert "created" not in rec
    assert "Excused" not in rec.get("written", [])


def test_the_tab_is_read_on_every_run():
    """Editing it and running --refresh-views is how the instructor's work lands."""
    from ecs101.pipeline import _readable_titles

    assert "Excused" in _readable_titles()


def test_the_name_column_is_called_student_name():
    """Matches the Roster tab, so the three tabs line up column for column."""
    from ecs101.models import EXCUSED_HEADERS

    assert EXCUSED_HEADERS == ["Date", "Student Key", "Student Name", "Reason"]

    review = build_attendance_review(QUESTIONS, [], ROSTER)
    detail_header = next(
        r for r in review if r[:2] == ["Date", "Student Key"]
    )
    assert detail_header[:3] == EXCUSED_HEADERS[:3], (
        "the first three columns must match so rows can be pasted straight across"
    )


def test_a_tab_created_before_the_rename_still_works():
    """Tolerate the old 'Student' header rather than silently ignoring the column."""
    excused, problems = parse_excused_rows(
        [{"Date": "2026-08-24", "Student Key": "", "Student": "Bruno Beck",
          "Reason": "Illness"}],
        ROSTER, DATES,
    )
    assert excused == {("canvas:2", "2026-08-24"): "Illness"}
    assert problems == []
