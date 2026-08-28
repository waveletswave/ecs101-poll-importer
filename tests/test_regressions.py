"""One test per defect found in the v2.3.1 review.

Every test in this file fails against v2.3.1 and passes against v3. They are
the reason the package was split: each of these exercises a pure function with
plain dicts, no Google account and no network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ecs101.normalize import now_iso
from ecs101.parsers import (
    lecture_question_columns,
    parse_poll_everywhere_export,
    suspicious_question_columns,
)
from ecs101.records import (
    collapse_canonical_responses,
    poll_to_records,
    remap_existing_responses,
)
from ecs101.scoring import count_answer_hits, prompt_correct_answer
from ecs101.views import build_derived_tables


# ---------------------------------------------------------------------------
# Finding 01 - a student confirmed in a later week must get their earlier
# attendance and score back.
# ---------------------------------------------------------------------------

def test_retroactive_identification_backfills_history():
    week1 = [{
        "Date": "2026-08-24",
        "Question ID": "2026-08-24::Q1",
        "Student Key": "",
        "Student Name": "",
        "Poll Key": "email:alice@example.edu",
        "Poll Participant": "Alina A.",
        "Poll Email": "alice@example.edu",
        "Screen Name": "alice",
        "Response": "Yellowstone",
        "Correct": "1",
        "Timestamp": "2026-08-24T13:37:00-04:00",
        "File Hash": "aaaaaaaaaaaa",
        "Match Status": "unresolved",
    }]

    # Week 2: the TA confirms this identity is Alina Ashworth.
    mapping = {
        "email:alice@example.edu": {
            "Poll Key": "email:alice@example.edu",
            "Poll Participant": "Alina A.",
            "Poll Email": "alice@example.edu",
            "Student Key": "canvas:123",
            "Student Name": "Alina Ashworth",
            "Match Type": "confirmed",
        }
    }

    backfilled = remap_existing_responses(week1, mapping)
    assert backfilled[0]["Student Key"] == "canvas:123"
    assert backfilled[0]["Match Status"] == "confirmed"

    week2 = [{
        "Date": "2026-08-26",
        "Question ID": "2026-08-26::Q1",
        "Student Key": "canvas:123",
        "Student Name": "Alina Ashworth",
        "Poll Key": "email:alice@example.edu",
        "Poll Participant": "Alina A.",
        "Poll Email": "alice@example.edu",
        "Screen Name": "alice",
        "Response": "Kamchatka",
        "Correct": "1",
        "Timestamp": "2026-08-26T13:27:00-04:00",
        "File Hash": "bbbbbbbbbbbb",
        "Match Status": "confirmed",
    }]

    roster = [{"Student Key": "canvas:123", "Student Name": "Alina Ashworth", "Active": "TRUE"}]
    questions = [
        {"Date": "2026-08-24", "Question ID": "2026-08-24::Q1", "Question": "Q1",
         "Scored": "TRUE", "Question Order": "1"},
        {"Date": "2026-08-26", "Question ID": "2026-08-26::Q1", "Question": "Q1",
         "Scored": "TRUE", "Question Order": "1"},
    ]
    responses = collapse_canonical_responses([*backfilled, *week2])
    attendance, scores, _ = build_derived_tables(questions, responses, roster)

    # ['Alina Ashworth', '2026-08-24', '2026-08-26', 'Classes Attended']
    assert attendance[1] == ["Alina Ashworth", "P", "P", "2"]
    # Both scored questions answered correctly.
    assert scores[1][1:3] == ["1", "1"]
    assert scores[1][3] == "2"


# ---------------------------------------------------------------------------
# Finding 03 - the summary block and the individual-results block must clean
# answer text identically, or the whole class is scored zero.
# ---------------------------------------------------------------------------

def test_legacy_summary_and_individual_answers_use_the_same_normalization(tmp_path: Path):
    """Irregular internal whitespace must not split the two blocks apart."""
    path = tmp_path / "legacy.csv"
    path.write_text(
        "Response,Count\n"
        '&quot;Yellowstone  caldera&quot;,3\n'   # two spaces, HTML-escaped quotes
        "Total,3\n"
        "\n"
        "Individual Results\n"
        "Response,Via,Screen name,Registered participant,Created At\n"
        "Yellowstone caldera,web,a,Alina Ashworth,2026-08-24 12:37:00\n"
        "Yellowstone caldera,web,b,Bruno Beck,2026-08-24 12:37:10\n",
        encoding="utf-8",
    )

    polls, _ = parse_poll_everywhere_export(path)
    poll = polls[0]

    summary_answers = {a for a, _ in poll.summary}
    response_answers = {r.response for r in poll.responses}
    assert summary_answers == response_answers, (
        "summary and individual blocks disagree; scoring by exact equality "
        "would give every student 0"
    )

    poll.correct_answers = {poll.summary[0][0]}
    _, rows, _ = poll_to_records(poll, now_iso(), {})
    assert [r["Correct"] for r in rows] == ["1", "1"]


def test_prompt_rejects_a_correct_answer_that_matches_nothing(legacy_csv, silent, scripted):
    """The zero-hit guard is the general defence, not just for the bug above."""
    polls, _ = parse_poll_everywhere_export(legacy_csv)
    poll = polls[0]

    # Type an answer that no student gave, decline to force it, then pick 1.
    answers = scripted("T", "Kilauea, Hawaii", "n", "1")
    prompt_correct_answer(poll, input_fn=answers, output_fn=silent)

    assert poll.correct_answers == {"Snake River Canyon, Oregon"}
    assert count_answer_hits(poll, poll.correct_answers) == 2


def test_zero_hit_answer_can_still_be_forced(legacy_csv, silent, scripted):
    answers = scripted("T", "Kilauea, Hawaii", "y")
    polls, _ = parse_poll_everywhere_export(legacy_csv)
    poll = polls[0]
    prompt_correct_answer(poll, input_fn=answers, output_fn=silent)
    assert poll.correct_answers == {"Kilauea, Hawaii"}


# ---------------------------------------------------------------------------
# Finding 04 - a metadata column after 'Public ID' must not become question 1.
# ---------------------------------------------------------------------------

def test_metadata_column_after_public_id_is_not_treated_as_a_question():
    headers = [
        "Response #", "Started At (CDT)", "Participant First Name",
        "Participant Last Name", "Email", "Screen Name", "Public ID",
        "Response Time (s)",                      # new metadata column
        "Which volcano is a supervolcano?",
        "What year was the last eruption?",
    ]
    questions = lecture_question_columns(headers)
    assert questions[0] == "Which volcano is a supervolcano?"
    assert "Response Time (s)" not in questions


def test_unrecognised_extra_column_is_flagged_as_suspicious():
    """A column the name filter cannot know about still has to be caught."""
    rows = [{"Device Fingerprint": f"fp-{i:04d}", "Q1": "Kamchatka"} for i in range(20)]
    flagged = suspicious_question_columns(rows, ["Device Fingerprint", "Q1"])
    assert "Device Fingerprint" in flagged
    assert "Q1" not in flagged


# ---------------------------------------------------------------------------
# Finding 05 - the reviewer must be able to reach a student outside the top
# candidates without marking a real student as a non-student.
# ---------------------------------------------------------------------------

def test_reviewer_can_search_for_a_student_not_in_the_candidate_list(
    roster_rows, scripted, silent
):
    from ecs101.identity import RosterIndex, prompt_participant_match

    index = RosterIndex(roster_rows)
    # 'Thi Lan Pham Mai' vs Canvas 'Mai Thi Lan Pham': word order differs.
    answers = scripted("F pham", "1")
    row = prompt_participant_match(
        "Thi Lan Pham Mai", "mailanpham@example.com", index, input_fn=answers
    )
    assert row["Student Key"] == "canvas:100005"
    assert row["Match Type"] == "confirmed"


def test_marking_a_participant_non_student_requires_confirmation(roster_rows, scripted):
    from ecs101.identity import RosterIndex, prompt_participant_match

    index = RosterIndex(roster_rows)
    # Decline the confirmation, then leave unresolved instead.
    answers = scripted("N", "n", "S")
    row = prompt_participant_match("Prof Marlowe", "", index, input_fn=answers)
    assert row["Match Type"] == "unresolved"


# ---------------------------------------------------------------------------
# Finding 10 - --replace must not delete the Import Log rows of the other
# questions that share a lecture-wide CSV's file hash.
# ---------------------------------------------------------------------------

def test_replace_only_drops_log_rows_for_the_replaced_question():
    from ecs101.normalize import clean_space

    existing_log = [
        {"Question ID": "2026-08-26::Q1", "File Hash": "h" * 64, "Source File": "L2.csv"},
        {"Question ID": "2026-08-26::Q2", "File Hash": "h" * 64, "Source File": "L2.csv"},
        {"Question ID": "2026-08-26::Q3", "File Hash": "h" * 64, "Source File": "L2.csv"},
    ]
    replace_qids = {"2026-08-26::Q1"}

    kept = [r for r in existing_log if clean_space(r.get("Question ID")) not in replace_qids]
    assert [r["Question ID"] for r in kept] == ["2026-08-26::Q2", "2026-08-26::Q3"], (
        "Q2 and Q3 share Q1's file hash but were not replaced; their import "
        "records must survive"
    )


# ---------------------------------------------------------------------------
# Finding 02 - a failure after writing began must be distinguishable from one
# before it.
# ---------------------------------------------------------------------------

def test_partial_write_is_reported_as_a_partial_write():
    from ecs101.sheets import SheetIO

    class FakeSheet:
        title = "ECS101"

        def fetch_sheet_metadata(self):
            return {"sheets": [{"properties": {
                "title": "Responses", "sheetId": 1,
                "gridProperties": {"rowCount": 100, "columnCount": 20},
            }}]}

        def batch_update(self, body):
            return {}

        def values_batch_update(self, body):
            raise RuntimeError("quota exceeded")

    io = SheetIO(FakeSheet())
    io.stage("Responses", [["a"], ["b"]])
    with pytest.raises(RuntimeError):
        io.commit()
    assert io.write_started is True, (
        "the CLI relies on this flag to stop claiming 'no data were written'"
    )


def test_values_are_written_before_anything_is_cleared():
    """clear() must never run before the replacement data is in place."""
    from ecs101.sheets import SheetIO

    order = []

    class FakeSheet:
        title = "ECS101"

        def fetch_sheet_metadata(self):
            return {"sheets": [{"properties": {
                "title": "Responses", "sheetId": 1,
                "gridProperties": {"rowCount": 500, "columnCount": 20},
            }}]}

        def batch_update(self, body):
            order.append("grow")
            return {}

        def values_batch_update(self, body):
            order.append("write")
            return {}

        def values_batch_clear(self, params=None, body=None):
            order.append("clear")
            return {}

    io = SheetIO(FakeSheet())
    io.stage("Responses", [["h"], ["1"], ["2"]])
    summary = io.commit()
    assert order.index("write") < order.index("clear")
    assert summary["api_calls"] <= 3


def test_one_import_costs_a_handful_of_api_calls_not_forty():
    from ecs101.sheets import SheetIO

    calls = {"n": 0}
    titles = [
        "Roster", "Participant Map", "Questions", "Responses", "Import Log",
        "Attendance", "Attendance Review", "Scores", "Leaderboard",
    ]

    class FakeSheet:
        title = "ECS101"

        def fetch_sheet_metadata(self):
            calls["n"] += 1
            return {"sheets": [
                {"properties": {"title": t, "sheetId": i,
                                "gridProperties": {"rowCount": 1000, "columnCount": 30}}}
                for i, t in enumerate(titles)
            ]}

        def values_batch_get(self, ranges):
            calls["n"] += 1
            return {"valueRanges": [{"values": []} for _ in ranges]}

        def batch_update(self, body):
            calls["n"] += 1
            return {}

        def values_batch_update(self, body):
            calls["n"] += 1
            return {}

        def values_batch_clear(self, params=None, body=None):
            calls["n"] += 1
            return {}

    io = SheetIO(FakeSheet())
    io.ensure_worksheets(titles)
    io.read_all(titles)
    for t in titles:
        io.stage(t, [["h"], ["v"]])
    io.commit()

    # v2.3.1 needed roughly 54 for the same work, against a 60/minute quota.
    assert calls["n"] <= 6, f"expected a handful of API calls, made {calls['n']}"


# ---------------------------------------------------------------------------
# A ragged matrix must be written as a full rectangle. values.update only
# touches the cells it is given, so short rows used to leave the previous
# import's text sitting to their right and empty rows were skipped entirely.
# ---------------------------------------------------------------------------

def _fake_sheet(recorder, rows=200, cols=12):
    class FakeSheet:
        title = "ECS101"

        def fetch_sheet_metadata(self):
            return {"sheets": [{"properties": {
                "title": "Attendance Review", "sheetId": 1,
                "gridProperties": {"rowCount": rows, "columnCount": cols},
            }}]}

        def batch_update(self, body):
            return {}

        def values_batch_update(self, body):
            recorder["written"] = body["data"][0]["values"]
            return {}

        def values_batch_clear(self, params=None, body=None):
            recorder["cleared"] = (params or {}).get("ranges", [])
            return {}

    return FakeSheet()


def test_a_ragged_matrix_is_written_as_a_rectangle():
    from ecs101.sheets import SheetIO

    rec = {}
    io = SheetIO(_fake_sheet(rec))
    io.stage("Attendance Review", [
        ["Date", "Roster", "Present", "Missing", "Unresolved", "Non-student"],
        ["2026-08-26", "89", "82", "7", "4", "2"],
        [],                                   # the blank separator row
        ["Unmatched Poll Identities"],        # a one-cell section header
        ["Date", "Poll Participant", "Status", "Notes"],
    ])
    io.commit()

    written = rec["written"]
    widths = {len(r) for r in written}
    assert widths == {6}, f"rows must all be 6 wide, got {sorted(widths)}"
    assert written[2] == [""] * 6, "the separator row must be written, not skipped"
    assert written[3] == ["Unmatched Poll Identities", "", "", "", "", ""], (
        "a short header row must blank the cells to its right"
    )


def test_columns_beyond_the_new_width_are_cleared():
    """A shrinking table must not leave its old right-hand columns behind."""
    from ecs101.sheets import SheetIO

    rec = {}
    io = SheetIO(_fake_sheet(rec, rows=200, cols=12))
    io.stage("Attendance Review", [["a", "b"], ["c", "d"]])
    io.commit()

    cleared = rec["cleared"]
    assert any("!C1:L" in r for r in cleared), f"columns C..L should be cleared, got {cleared}"
    assert any("!A3:L200" in r for r in cleared), f"rows 3..200 should be cleared, got {cleared}"


def test_column_letters():
    from ecs101.sheets import _column_letter

    assert [_column_letter(i) for i in (1, 2, 26, 27, 28, 52, 53)] == [
        "A", "B", "Z", "AA", "AB", "AZ", "BA",
    ]
