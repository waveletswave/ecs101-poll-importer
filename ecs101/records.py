"""Canonical Questions / Responses / Import Log / Roster record building."""

from __future__ import annotations

from typing import Dict, List, Sequence, Tuple

from .identity import lookup_mapping
from .models import (
    ROSTER_HEADERS,
    CanvasStudent,
    PollFile,
    effective_poll_responses,
)
from .normalize import (
    clean_space,
    normalize_answer_text,
    normalize_email,
    poll_identity_key,
    short_hash,
    timestamp_sort_key,
)

__all__ = [
    "normalize_response_schema",
    "collapse_canonical_responses",
    "poll_to_records",
    "remap_existing_responses",
    "build_updated_roster",
    "roster_by_key",
    "active_roster_rows",
    "count_blank_active",
]


# ---------------------------------------------------------------------------
# Roster helpers
# ---------------------------------------------------------------------------

def roster_by_key(roster_rows: Sequence[Dict[str, str]]) -> Dict[str, Dict[str, str]]:
    return {
        clean_space(r.get("Student Key")): r
        for r in roster_rows
        if clean_space(r.get("Student Key"))
    }


def active_roster_rows(roster_rows: Sequence[Dict[str, str]]) -> List[Dict[str, str]]:
    """Rows not explicitly marked inactive.

    A blank Active cell counts as active, which is the safe direction (a
    student is never dropped by a formatting accident), but callers should
    report how many blanks they saw via count_blank_active.
    """
    return [
        r for r in roster_rows
        if str(r.get("Active", "")).strip().upper() != "FALSE"
        and clean_space(r.get("Student Key"))
    ]


def count_blank_active(roster_rows: Sequence[Dict[str, str]]) -> int:
    return sum(
        1 for r in roster_rows
        if clean_space(r.get("Student Key")) and not clean_space(r.get("Active"))
    )


def build_updated_roster(
    canvas_students: Sequence[CanvasStudent],
    existing_roster: Sequence[Dict[str, str]],
    synced_at: str,
) -> Tuple[List[Dict[str, str]], Dict[str, int]]:
    existing_canvas = {
        clean_space(r.get("Student Key")): r
        for r in existing_roster
        if clean_space(r.get("Student Key")).startswith("canvas:")
    }

    current_keys = {s.student_key for s in canvas_students}
    updated: Dict[str, Dict[str, str]] = {}
    stats: Dict[str, int] = {}

    def bump(name: str) -> None:
        stats[name] = stats.get(name, 0) + 1

    for student in canvas_students:
        old = existing_canvas.get(student.student_key)
        if old:
            bump("retained")
            first_seen = clean_space(old.get("First Seen")) or synced_at
            notes = clean_space(old.get("Notes"))
        else:
            bump("added")
            first_seen = synced_at
            notes = ""

        updated[student.student_key] = {
            "Student Key": student.student_key,
            "Student Name": student.student_name,
            "Canvas Name": student.canvas_name,
            "Section": student.section,
            "Login": student.login,
            "Active": "TRUE",
            "First Seen": first_seen,
            "Roster Updated At": synced_at,
            "Notes": notes,
        }

    for key, old in existing_canvas.items():
        if key in current_keys:
            continue
        bump("inactivated")
        updated[key] = {
            "Student Key": key,
            "Student Name": clean_space(old.get("Student Name")) or key,
            "Canvas Name": clean_space(old.get("Canvas Name")),
            "Section": clean_space(old.get("Section")),
            "Login": clean_space(old.get("Login")),
            "Active": "FALSE",
            "First Seen": clean_space(old.get("First Seen")),
            "Roster Updated At": synced_at,
            "Notes": clean_space(old.get("Notes")),
        }

    rows = sorted(
        updated.values(),
        key=lambda r: (
            str(r.get("Active", "")).upper() == "FALSE",
            r.get("Student Name", "").casefold(),
        ),
    )
    return [{h: clean_space(r.get(h)) for h in ROSTER_HEADERS} for r in rows], stats


# ---------------------------------------------------------------------------
# Response schema
# ---------------------------------------------------------------------------

def normalize_response_schema(row: Dict[str, str]) -> Dict[str, str]:
    """Upgrade a v1 / v2 / v2.3 response row into the v3 schema.

    Reading is deliberately tolerant: v1 stored the Poll participant under
    'Student', v2 under 'Poll Participant', and neither carried a Poll Key or
    an e-mail. Columns the v3 schema dropped ('Question', 'Source File') are
    simply not carried forward; both are reachable from Questions via
    'Question ID'.
    """
    participant = clean_space(
        row.get("Poll Participant")
        or row.get("Student Name")
        or row.get("Student")
    )
    email = normalize_email(row.get("Poll Email"))
    poll_key = clean_space(row.get("Poll Key"))
    if not poll_key or ":" not in poll_key:
        poll_key = poll_identity_key(participant, email)

    return {
        "Date": clean_space(row.get("Date")),
        "Question ID": clean_space(row.get("Question ID")),
        "Student Key": clean_space(row.get("Student Key")),
        "Student Name": clean_space(row.get("Student Name")) if clean_space(row.get("Student Key")) else "",
        "Poll Key": poll_key,
        "Poll Participant": participant,
        "Poll Email": email,
        "Screen Name": clean_space(row.get("Screen Name")),
        "Response": normalize_answer_text(row.get("Response")),
        "Correct": clean_space(row.get("Correct")),
        "Timestamp": clean_space(row.get("Timestamp")),
        "File Hash": short_hash(row.get("File Hash")),
        "Match Status": clean_space(row.get("Match Status")) or "unresolved",
    }


def collapse_canonical_responses(rows: Sequence[Dict[str, str]]) -> List[Dict[str, str]]:
    """One latest effective response per question and canonical identity.

    Matched students collapse by Canvas student key, so a student who answered
    from two Poll identities counts once. Unmatched identities collapse by Poll
    Key, so two different unresolved people never merge.

    Ordering uses parsed instants rather than raw strings, because a sheet can
    hold both legacy ('2026-08-24 12:39:10') and v3
    ('2026-08-26T13:27:00-04:00') formats at once.
    """
    latest: Dict[Tuple[str, str], Dict[str, str]] = {}
    for row in rows:
        qid = clean_space(row.get("Question ID"))
        skey = clean_space(row.get("Student Key"))
        pkey = clean_space(row.get("Poll Key")) or poll_identity_key(
            row.get("Poll Participant") or row.get("Student Name"),
            row.get("Poll Email"),
        )
        identity = skey or pkey
        if not qid or not identity:
            continue
        key = (qid, identity)
        old = latest.get(key)
        if old is None or timestamp_sort_key(row.get("Timestamp")) >= timestamp_sort_key(
            old.get("Timestamp")
        ):
            latest[key] = dict(row)

    return sorted(
        latest.values(),
        key=lambda r: (
            r.get("Date", ""),
            r.get("Question ID", ""),
            (r.get("Student Name", "") or r.get("Poll Participant", "")).casefold(),
        ),
    )


# ---------------------------------------------------------------------------
# Building records from a parsed poll
# ---------------------------------------------------------------------------

def poll_to_records(
    poll: PollFile,
    imported_at: str,
    mapping: Dict[str, Dict[str, str]],
) -> Tuple[Dict[str, str], List[Dict[str, str]], Dict[str, str]]:
    correct_text = (
        "" if poll.correct_answers is None
        else "; ".join(sorted(poll.correct_answers))
    )

    question = {
        "Question ID": poll.question_id,
        "Date": poll.class_date,
        "Question": poll.question_name,
        "Question Order": str(poll.question_order),
        "Source Format": poll.source_format,
        "Source File": poll.path.name,
        "First Response At": poll.first_response_at,
        "Options": " | ".join(answer for answer, _ in poll.summary),
        "Correct Answer": correct_text,
        "Scored": "TRUE" if poll.scored else "FALSE",
        "File Hash": poll.file_hash,
        "Imported At": imported_at,
    }

    responses: List[Dict[str, str]] = []
    for r in effective_poll_responses(poll):
        map_row = lookup_mapping(mapping, r.student_name, r.normalized_email)
        match_type = clean_space(map_row.get("Match Type")) or "unresolved"
        student_key = clean_space(map_row.get("Student Key"))
        student_name = clean_space(map_row.get("Student Name")) if student_key else ""

        correct = ""
        if poll.correct_answers is not None:
            correct = "1" if r.response in poll.correct_answers else "0"

        responses.append({
            "Date": poll.class_date,
            "Question ID": poll.question_id,
            "Student Key": student_key,
            "Student Name": student_name,
            "Poll Key": r.poll_key,
            "Poll Participant": r.student_name,
            "Poll Email": r.normalized_email,
            "Screen Name": r.screen_name,
            "Response": r.response,
            "Correct": correct,
            "Timestamp": r.created_at,
            "File Hash": short_hash(poll.file_hash),
            "Match Status": match_type,
        })

    responses = collapse_canonical_responses(responses)
    matched_students = len({r["Student Key"] for r in responses if r.get("Student Key")})
    unmatched = sum(1 for r in responses if not r.get("Student Key"))

    import_log = {
        "Import ID": f"{short_hash(poll.file_hash)}::q{poll.question_order:02d}",
        "Date": poll.class_date,
        "Source File": poll.path.name,
        "File Hash": poll.file_hash,
        "Question ID": poll.question_id,
        "Effective Responses": str(len(responses)),
        "Matched Students": str(matched_students),
        "Unmatched / Non-student": str(unmatched),
        "Imported At": imported_at,
    }

    return question, responses, import_log


def remap_existing_responses(
    response_rows: Sequence[Dict[str, str]],
    mapping: Dict[str, Dict[str, str]],
) -> List[Dict[str, str]]:
    """Re-apply the Participant Map to stored responses.

    This is what backfills a student's earlier attendance once their identity
    is confirmed. v2.3.1 built the final mapping and then never applied it to
    history, so a student confirmed in week 2 stayed absent for week 1.
    """
    remapped: List[Dict[str, str]] = []
    for original in response_rows:
        row = normalize_response_schema(original)
        map_row = lookup_mapping(mapping, row["Poll Participant"], row["Poll Email"])
        match_type = clean_space(map_row.get("Match Type")) or "unresolved"
        student_key = clean_space(map_row.get("Student Key"))

        row["Student Key"] = student_key
        row["Student Name"] = clean_space(map_row.get("Student Name")) if student_key else ""
        row["Match Status"] = match_type
        remapped.append(row)

    return collapse_canonical_responses(remapped)
