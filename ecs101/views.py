"""Rebuilt views: Attendance, Attendance Review, Scores, Leaderboard."""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from .normalize import clean_space, timestamp_sort_key
from .records import active_roster_rows

__all__ = [
    "roster_display_map",
    "latest_canonical_responses",
    "build_derived_tables",
    "build_attendance_review",
]


def roster_display_map(
    roster_rows: Sequence[Dict[str, str]],
) -> Tuple[List[str], Dict[str, str]]:
    """Return ACTIVE Canvas roster only, in alphabetical order."""
    active = active_roster_rows(roster_rows)
    active.sort(key=lambda r: r.get("Student Name", "").casefold())
    ordered = [clean_space(r["Student Key"]) for r in active]
    display = {
        clean_space(r["Student Key"]): clean_space(r.get("Student Name")) or clean_space(r["Student Key"])
        for r in active
    }
    return ordered, display


def latest_canonical_responses(
    rows: Sequence[Dict[str, str]],
) -> Dict[Tuple[str, str], Dict[str, str]]:
    latest: Dict[Tuple[str, str], Dict[str, str]] = {}
    for row in rows:
        qid = clean_space(row.get("Question ID"))
        skey = clean_space(row.get("Student Key"))
        if not qid or not skey:
            continue
        key = (qid, skey)
        old = latest.get(key)
        if old is None or timestamp_sort_key(row.get("Timestamp")) >= timestamp_sort_key(
            old.get("Timestamp")
        ):
            latest[key] = row
    return latest


def _question_sort_key(q: Dict[str, str]):
    try:
        order = int(clean_space(q.get("Question Order")))
    except ValueError:
        order = 10 ** 6
    return (clean_space(q.get("Date")), order, clean_space(q.get("Question ID")))


def build_derived_tables(
    questions: Sequence[Dict[str, str]],
    responses: Sequence[Dict[str, str]],
    roster_rows: Sequence[Dict[str, str]],
) -> Tuple[List[List[str]], List[List[str]], List[List[str]]]:
    students, display = roster_display_map(roster_rows)

    dates = sorted({clean_space(q.get("Date")) for q in questions if clean_space(q.get("Date"))})
    scored_questions = [
        q for q in questions
        if str(q.get("Scored", "")).strip().upper() == "TRUE"
    ]
    scored_questions.sort(key=_question_sort_key)

    active_keys = set(students)

    attended = {
        (clean_space(r.get("Student Key")), clean_space(r.get("Date")))
        for r in responses
        if clean_space(r.get("Student Key")) in active_keys and clean_space(r.get("Date"))
    }

    attendance = [["Student", *dates, "Classes Attended"]]
    for skey in students:
        vals = ["P" if (skey, d) in attended else "" for d in dates]
        attendance.append([
            display.get(skey, skey),
            *vals,
            str(sum(v == "P" for v in vals)),
        ])

    latest = latest_canonical_responses(responses)
    q_labels = [
        f'{clean_space(q.get("Date"))} | {clean_space(q.get("Question")) or clean_space(q.get("Question ID"))}'
        for q in scored_questions
    ]

    scores = [[
        "Student", *q_labels,
        "Total Correct", "Scored Questions Answered", "Accuracy",
    ]]

    leaderboard_data: List[Tuple[str, int, int, Optional[float]]] = []

    for skey in students:
        vals: List[str] = []
        total_correct = 0
        answered = 0

        for q in scored_questions:
            row = latest.get((clean_space(q.get("Question ID")), skey))
            if row is None:
                vals.append("")
                continue
            correct = clean_space(row.get("Correct"))
            if correct in {"0", "1"}:
                vals.append(correct)
                answered += 1
                total_correct += int(correct)
            else:
                vals.append("")

        accuracy = total_correct / answered if answered else None
        scores.append([
            display.get(skey, skey),
            *vals,
            str(total_correct),
            str(answered),
            "" if accuracy is None else f"{accuracy:.3f}",
        ])
        leaderboard_data.append((display.get(skey, skey), total_correct, answered, accuracy))

    leaderboard_data.sort(key=lambda x: (-x[1], -x[2], x[0].casefold()))

    leaderboard = [[
        "Rank", "Student", "Total Correct", "Scored Questions Answered", "Accuracy",
    ]]
    last_score: Optional[int] = None
    rank = 0
    for i, (name, correct, answered, accuracy) in enumerate(leaderboard_data, start=1):
        if correct != last_score:
            rank = i
            last_score = correct
        leaderboard.append([
            str(rank), name, str(correct), str(answered),
            "" if accuracy is None else f"{accuracy:.3f}",
        ])

    return attendance, scores, leaderboard


def build_attendance_review(
    questions: Sequence[Dict[str, str]],
    responses: Sequence[Dict[str, str]],
    roster_rows: Sequence[Dict[str, str]],
    include_present_rows: bool = False,
) -> List[List[str]]:
    """Build a review-oriented attendance sheet from canonical data.

    The view deliberately says "No matched Poll response" rather than "Absent".
    The importer can prove only that an active Canvas student did not have a
    matched response in the imported Poll Everywhere data. A student may have
    been present but not answered, had a technical problem, or still have an
    unresolved Poll identity.

    By default only the actionable rows are listed. v2.3.1 emitted one row per
    active student per date whether or not anything needed attention, which by
    the end of a semester is a few thousand rows rewritten on every import for
    no added information: the present students are already in Attendance.
    """
    active = active_roster_rows(roster_rows)
    active.sort(key=lambda r: r.get("Student Name", "").casefold())
    active_keys = {clean_space(r.get("Student Key")) for r in active}

    dates = sorted({clean_space(q.get("Date")) for q in questions if clean_space(q.get("Date"))})

    present_by_date: Dict[str, set] = {d: set() for d in dates}
    unresolved_by_date: Dict[str, set] = {d: set() for d in dates}
    nonstudent_by_date: Dict[str, set] = {d: set() for d in dates}

    for row in responses:
        date = clean_space(row.get("Date"))
        if not date:
            continue
        present_by_date.setdefault(date, set())
        unresolved_by_date.setdefault(date, set())
        nonstudent_by_date.setdefault(date, set())

        skey = clean_space(row.get("Student Key"))
        if skey in active_keys:
            present_by_date[date].add(skey)
            continue

        poll_name = clean_space(row.get("Poll Participant"))
        if not poll_name:
            continue
        label = poll_name
        email = clean_space(row.get("Poll Email"))
        if email:
            label = f"{poll_name} <{email}>"
        if clean_space(row.get("Match Status")).casefold() == "non-student":
            nonstudent_by_date[date].add(label)
        else:
            unresolved_by_date[date].add(label)

    matrix: List[List[str]] = []

    matrix.append(["Class Summary"])
    matrix.append([
        "Date", "Active Canvas Roster", "Present",
        "No matched Poll response", "Unresolved Poll participants",
        "Non-student participants",
    ])
    for date in dates:
        present = len(present_by_date.get(date, set()))
        missing = max(len(active) - present, 0)
        matrix.append([
            date, str(len(active)), str(present), str(missing),
            str(len(unresolved_by_date.get(date, set()))),
            str(len(nonstudent_by_date.get(date, set()))),
        ])

    matrix.append([])
    heading = (
        "Student Attendance Review"
        if include_present_rows
        else "Students With No Matched Poll Response"
    )
    matrix.append([heading])
    if not include_present_rows:
        matrix.append(["Present students are listed in the Attendance tab."])
    matrix.append(["Date", "Student Key", "Student", "Status", "Notes"])

    for date in dates:
        present_keys = present_by_date.get(date, set())
        detail_rows = []
        for student in active:
            skey = clean_space(student.get("Student Key"))
            name = clean_space(student.get("Student Name"))
            if skey in present_keys:
                if not include_present_rows:
                    continue
                detail_rows.append((1, name.casefold(), [
                    date, skey, name, "Present",
                    "Matched response to at least one imported poll",
                ]))
            else:
                detail_rows.append((0, name.casefold(), [
                    date, skey, name, "No matched Poll response",
                    "Review if needed; unresolved Poll identities may later "
                    "change this status",
                ]))
        for _, _, row in sorted(detail_rows):
            matrix.append(row)

    matrix.append([])
    matrix.append(["Unmatched Poll Identities"])
    matrix.append(["Date", "Poll Participant", "Status", "Notes"])
    for date in dates:
        for name in sorted(unresolved_by_date.get(date, set()), key=str.casefold):
            matrix.append([
                date, name, "Unresolved",
                "Not currently linked to an active Canvas student",
            ])
        for name in sorted(nonstudent_by_date.get(date, set()), key=str.casefold):
            matrix.append([
                date, name, "Non-student",
                "Excluded from student attendance and scores",
            ])

    return matrix
