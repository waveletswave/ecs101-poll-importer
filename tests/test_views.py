"""Derived views: Attendance, Scores, Leaderboard, Attendance Review."""

from __future__ import annotations

from ecs101.records import collapse_canonical_responses, normalize_response_schema
from ecs101.views import build_attendance_review, build_derived_tables

ROSTER = [
    {"Student Key": "canvas:1", "Student Name": "Alina Ashworth", "Active": "TRUE"},
    {"Student Key": "canvas:2", "Student Name": "Bruno Beck", "Active": "TRUE"},
    {"Student Key": "canvas:3", "Student Name": "Cleo Cardoso", "Active": "FALSE"},
]

QUESTIONS = [
    {"Date": "2026-08-24", "Question ID": "2026-08-24::Q1", "Question": "Q1",
     "Question Order": "1", "Scored": "TRUE"},
    {"Date": "2026-08-24", "Question ID": "2026-08-24::Q2", "Question": "Q2",
     "Question Order": "2", "Scored": "FALSE"},
    {"Date": "2026-08-26", "Question ID": "2026-08-26::Q1", "Question": "Q1",
     "Question Order": "1", "Scored": "TRUE"},
]


def _resp(date, qid, skey, correct, name="", pkey="", at="2026-08-24T13:30:00-04:00",
          status="confirmed"):
    return {
        "Date": date, "Question ID": qid, "Student Key": skey, "Student Name": name,
        "Poll Key": pkey or f"email:{(name or skey).lower().replace(' ', '')}@x.edu",
        "Poll Participant": name or skey, "Poll Email": "", "Screen Name": "",
        "Response": "A", "Correct": correct, "Timestamp": at,
        "File Hash": "abc123def456", "Match Status": status,
    }


def test_attendance_marks_any_answered_question():
    responses = [
        _resp("2026-08-24", "2026-08-24::Q2", "canvas:1", "", "Alina Ashworth"),
        _resp("2026-08-26", "2026-08-26::Q1", "canvas:2", "1", "Bruno Beck"),
    ]
    attendance, _, _ = build_derived_tables(QUESTIONS, responses, ROSTER)
    assert attendance[0] == ["Student", "2026-08-24", "2026-08-26", "Classes Attended"]
    assert attendance[1] == ["Alina Ashworth", "P", "", "1"]
    assert attendance[2] == ["Bruno Beck", "", "P", "1"]


def test_inactive_students_are_excluded_from_every_view():
    responses = [_resp("2026-08-24", "2026-08-24::Q1", "canvas:3", "1", "Cleo Cardoso")]
    attendance, scores, leaderboard = build_derived_tables(QUESTIONS, responses, ROSTER)
    names = {row[0] for row in attendance[1:]}
    assert "Cleo Cardoso" not in names
    assert "Cleo Cardoso" not in {row[0] for row in scores[1:]}
    assert "Cleo Cardoso" not in {row[1] for row in leaderboard[1:]}


def test_only_scored_questions_appear_in_scores():
    _, scores, _ = build_derived_tables(QUESTIONS, [], ROSTER)
    assert scores[0][1:3] == ["2026-08-24 | Q1", "2026-08-26 | Q1"]
    assert len(scores[0]) == 6   # Student + 2 questions + 3 totals


def test_unanswered_scored_question_stays_blank_not_zero():
    responses = [_resp("2026-08-24", "2026-08-24::Q1", "canvas:1", "0", "Alina Ashworth")]
    _, scores, _ = build_derived_tables(QUESTIONS, responses, ROSTER)
    alice = next(r for r in scores[1:] if r[0] == "Alina Ashworth")
    assert alice[1] == "0"    # answered, wrong
    assert alice[2] == ""     # never answered
    assert alice[3] == "0"    # total correct
    assert alice[4] == "1"    # scored questions answered
    assert alice[5] == "0.000"


def test_accuracy_is_blank_when_nothing_was_answered():
    _, scores, _ = build_derived_tables(QUESTIONS, [], ROSTER)
    assert scores[1][5] == ""


def test_leaderboard_ties_share_a_rank():
    responses = [
        _resp("2026-08-24", "2026-08-24::Q1", "canvas:1", "1", "Alina Ashworth"),
        _resp("2026-08-24", "2026-08-24::Q1", "canvas:2", "1", "Bruno Beck"),
    ]
    _, _, leaderboard = build_derived_tables(QUESTIONS, responses, ROSTER)
    assert leaderboard[1][0] == "1"
    assert leaderboard[2][0] == "1"


def test_two_poll_identities_for_one_student_count_once():
    responses = collapse_canonical_responses([
        _resp("2026-08-24", "2026-08-24::Q1", "canvas:1", "0", "Alina A.",
              pkey="email:a1@x.edu", at="2026-08-24T13:30:00-04:00"),
        _resp("2026-08-24", "2026-08-24::Q1", "canvas:1", "1", "Alina Ashworth",
              pkey="email:a2@x.edu", at="2026-08-24T13:35:00-04:00"),
    ])
    assert len(responses) == 1
    _, scores, _ = build_derived_tables(QUESTIONS, responses, ROSTER)
    alice = next(r for r in scores[1:] if r[0] == "Alina Ashworth")
    assert alice[1] == "1", "the later response should win"


def test_unresolved_identities_appear_in_the_review_sheet():
    responses = [
        {**_resp("2026-08-24", "2026-08-24::Q1", "", "1", "Ghost Person",
                 status="unresolved"),
         "Poll Email": "ghost@gmail.com"},
    ]
    review = build_attendance_review(QUESTIONS, responses, ROSTER)
    flat = [" | ".join(r) for r in review]
    assert any("Ghost Person <ghost@gmail.com>" in line and "Unresolved" in line
               for line in flat)


def test_review_says_no_matched_response_not_absent():
    review = build_attendance_review(QUESTIONS, [], ROSTER)
    flat = "\n".join(" | ".join(r) for r in review)
    assert "No matched Poll response" in flat
    assert "Absent" not in flat


def test_review_omits_present_students_by_default():
    """Listing every present student every date is thousands of rows of nothing.

    A 90-student, 28-lecture semester emits ~2,500 'Present' rows that repeat
    what the Attendance tab already shows, and every import rewrites all of
    them. Sized here at 90 students over the 2 dates in QUESTIONS.
    """
    roster = [
        {"Student Key": f"canvas:{i}", "Student Name": f"Student {i:03d}", "Active": "TRUE"}
        for i in range(90)
    ]
    responses = [
        _resp(date, f"{date}::Q1", f"canvas:{i}", "1", f"Student {i:03d}")
        for date in ("2026-08-24", "2026-08-26")
        for i in range(88)          # two students absent each day
    ]
    compact = build_attendance_review(QUESTIONS, responses, roster)
    full = build_attendance_review(QUESTIONS, responses, roster, include_present_rows=True)

    assert len(full) - len(compact) == 88 * 2 - 1   # minus the one explanatory line
    assert len(compact) < 20

    # The four actionable rows are still there; no per-student "Present" row is.
    def status_rows(matrix):
        return [r for r in matrix if len(r) == 5 and r[1].startswith("canvas:")]

    assert all(r[3] == "No matched Poll response" for r in status_rows(compact))
    assert len(status_rows(compact)) == 4
    assert any(r[3] == "Present" for r in status_rows(full))


def test_review_summary_counts_are_right():
    responses = [_resp("2026-08-24", "2026-08-24::Q1", "canvas:1", "1", "Alina Ashworth")]
    review = build_attendance_review(QUESTIONS, responses, ROSTER)
    header = review.index(["Class Summary"])
    row = review[header + 2]
    assert row[0] == "2026-08-24"
    assert row[1] == "2"    # active roster
    assert row[2] == "1"    # present
    assert row[3] == "1"    # no matched response


def test_old_v2_response_rows_still_read():
    """Columns v3 dropped must not break a sheet written by v2.3.1."""
    old = {
        "Date": "2026-08-24", "Question ID": "2026-08-24::Q1", "Question": "Q1",
        "Student Key": "canvas:1", "Student Name": "Alina Ashworth",
        "Poll Participant": "Alina A.", "Screen Name": "alice",
        "Response": "A", "Correct": "1", "Timestamp": "2026-08-24 12:37:00",
        "Source File": "L1.csv", "File Hash": "f" * 64, "Match Status": "confirmed",
    }
    row = normalize_response_schema(old)
    assert row["Student Key"] == "canvas:1"
    assert row["Poll Key"] == "name:alina a"
    assert row["File Hash"] == "f" * 12
    assert "Question" not in row and "Source File" not in row


def test_v1_response_rows_still_read():
    old = {
        "Date": "2026-08-24", "Question ID": "2026-08-24::Q1",
        "Student": "Alina Ashworth", "Response": "A", "Correct": "1",
        "Timestamp": "2026-08-24 12:37:00",
    }
    row = normalize_response_schema(old)
    assert row["Poll Participant"] == "Alina Ashworth"
    assert row["Student Key"] == ""
    assert row["Match Status"] == "unresolved"
