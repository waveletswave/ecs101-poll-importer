#!/usr/bin/env python3
"""ECS101 Poll Everywhere -> Google Sheets importer (v2.1.1).

Architecture
------------
Canvas roster CSV
        ↓
Authoritative Roster + persistent Participant Map

Poll Everywhere CSVs
        ↓
Python cleaning / identity matching / deduplication
        ↓
Canonical Google Sheets tables:
    Roster
    Participant Map
    Questions
    Responses
    Import Log
        ↓
Automatically rebuilt views:
    Attendance
    Attendance Review
    Scores
    Leaderboard

Key rules
---------
* Canvas is the authoritative source for enrolled students.
* A Poll Everywhere participant is linked to a Canvas student by:
    1) a previously confirmed Participant Map entry,
    2) an exact normalized-name match, or
    3) a human-reviewed match for ambiguous names.
* Fuzzy matching only suggests candidates; it NEVER confirms automatically.
* Staff/guests can be persistently marked as non-students.
* Attendance: an ACTIVE Canvas student is present on a date if they answered
  ANY imported Poll Everywhere question that day.
* Scored questions: 1 = correct, 0 = incorrect, blank = unanswered.
* Unscored questions still count toward attendance.
* Responses contains one effective (latest) response per identity per question.
  Original Poll Everywhere CSVs remain the raw archive.
* Exact duplicate CSVs are detected by SHA-256 file hash, even if renamed.
* Re-importing a newer Canvas roster marks missing prior students inactive
  rather than deleting their historical records.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import hashlib
import html
import json
import re
import sys
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


VERSION = "2.1.1"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ResponseRow:
    response: str
    via: str
    screen_name: str
    registered_participant: str
    created_at: str

    @property
    def student_name(self) -> str:
        """Raw Poll Everywhere participant label used for identity matching."""
        name = self.registered_participant.strip()
        if name:
            return name
        screen = self.screen_name.strip() or "Unknown participant"
        return f"[UNREGISTERED] {screen}"

    @property
    def student_key(self) -> str:
        # Legacy / within-question dedup key only. Production student keys
        # come from Canvas IDs after participant matching.
        return normalize_person_name(self.student_name)


@dataclass
class PollFile:
    path: Path
    question_name: str
    class_date: str
    summary: List[Tuple[str, int]]
    responses: List[ResponseRow]
    file_hash: str
    correct_answers: Optional[set[str]] = None

    @property
    def question_id(self) -> str:
        return f"{self.class_date}::{self.question_name}"

    @property
    def scored(self) -> bool:
        return self.correct_answers is not None


@dataclass
class CanvasStudent:
    student_key: str
    student_name: str
    canvas_name: str
    section: str


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

def clean_space(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def strip_diacritics(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def normalize_person_name(name: str) -> str:
    """Conservative normalized name for exact matching and map lookup."""
    text = strip_diacritics(clean_space(name)).casefold()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def make_student_key(name: str) -> str:
    """Legacy name key retained for v1/v2 migration only."""
    return normalize_person_name(name)


def canvas_student_key(canvas_id: str) -> str:
    return f"canvas:{clean_space(canvas_id)}"


def canvas_display_name(canvas_name: str) -> str:
    """Convert Canvas 'Last, First Middle' to 'First Middle Last'."""
    raw = clean_space(canvas_name)
    if "," not in raw:
        return raw
    last, given = raw.split(",", 1)
    return clean_space(f"{given} {last}")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Canvas roster parser
# ---------------------------------------------------------------------------

def parse_canvas_roster(path: Path) -> List[CanvasStudent]:
    """Read only roster-identifying fields from a Canvas gradebook export.

    Assignment / grade columns are intentionally ignored.
    """
    if not path.exists():
        raise FileNotFoundError(f"Canvas roster file not found: {path}")

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        required = {"Student", "ID"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(
                f"{path.name}: missing Canvas column(s): {', '.join(sorted(missing))}"
            )

        students: List[CanvasStudent] = []
        seen_keys = set()

        for row in reader:
            raw_canvas_name = clean_space(row.get("Student") or "")
            canvas_id = clean_space(row.get("ID") or "")
            section = clean_space(row.get("Section") or "")

            # Canvas gradebook exports include a "Points Possible" pseudo-row.
            if not raw_canvas_name or normalize_person_name(raw_canvas_name) == "points possible":
                continue

            display = canvas_display_name(raw_canvas_name)

            # Ignore Canvas's synthetic Test Student.
            if normalize_person_name(display) == "test student":
                continue

            if not canvas_id:
                # A real roster row should have a stable Canvas ID.
                continue

            key = canvas_student_key(canvas_id)
            if key in seen_keys:
                continue
            seen_keys.add(key)

            students.append(
                CanvasStudent(
                    student_key=key,
                    student_name=display,
                    canvas_name=raw_canvas_name,
                    section=section,
                )
            )

    if not students:
        raise ValueError(f"No student roster rows found in {path.name}.")

    return sorted(students, key=lambda s: s.student_name.casefold())


def name_similarity(a: str, b: str) -> float:
    """Ranking heuristic for HUMAN REVIEW only; never used for auto-match."""
    na, nb = normalize_person_name(a), normalize_person_name(b)
    if not na or not nb:
        return 0.0

    ratio = difflib.SequenceMatcher(None, na, nb).ratio()
    ta, tb = na.split(), nb.split()
    sa, sb = set(ta), set(tb)

    overlap = len(sa & sb) / max(len(sa | sb), 1)
    surname_bonus = 0.18 if ta[-1] == tb[-1] else 0.0
    subset_bonus = 0.12 if (sa <= sb or sb <= sa) else 0.0

    return min(1.0, 0.65 * ratio + 0.23 * overlap + surname_bonus + subset_bonus)


def roster_candidates(
    poll_name: str,
    roster_rows: Sequence[Dict[str, str]],
    limit: int = 5,
) -> List[Tuple[float, Dict[str, str]]]:
    scored: List[Tuple[float, Dict[str, str]]] = []
    for row in roster_rows:
        name = clean_space(row.get("Student Name", ""))
        if not name:
            continue
        scored.append((name_similarity(poll_name, name), row))
    scored.sort(key=lambda x: (-x[0], x[1].get("Student Name", "").casefold()))
    return scored[:limit]

# ---------------------------------------------------------------------------
# Poll Everywhere CSV parser
# ---------------------------------------------------------------------------

def _find_line(lines: Sequence[str], prefix: str) -> int:
    for i, line in enumerate(lines):
        if line.strip().startswith(prefix):
            return i
    raise ValueError(f"Could not find expected section beginning with: {prefix!r}")


def parse_poll_everywhere_csv(path: Path) -> PollFile:
    """Parse the Poll Everywhere CSV format observed in ECS101 exports."""
    text = path.read_text(encoding="utf-8-sig")
    lines = text.splitlines()

    summary_header = _find_line(lines, "Response,Count")
    individual_header = _find_line(
        lines, "Response,Via,Screen name,Registered participant,Created At"
    )

    # Poll Everywhere HTML-escapes quotes in the summary. Because answers can
    # contain commas, parse each summary row from the final comma.
    summary_lines: List[str] = []
    for line in lines[summary_header:individual_header - 1]:
        if line.strip() == "Individual Results":
            break
        if line.strip():
            summary_lines.append(line)

    summary: List[Tuple[str, int]] = []
    for line in summary_lines[1:]:
        if "," not in line:
            continue
        response_raw, count_raw = line.rsplit(",", 1)
        response = html.unescape(response_raw.strip()).strip()
        if len(response) >= 2 and response[0] == response[-1] == '"':
            response = response[1:-1]
        if not response or response.casefold() == "total":
            continue
        try:
            count = int(count_raw.strip())
        except ValueError:
            count = 0
        summary.append((response, count))

    reader = csv.DictReader(lines[individual_header:])
    responses: List[ResponseRow] = []
    for row in reader:
        if not any(clean_space(v or "") for v in row.values()):
            continue
        responses.append(
            ResponseRow(
                response=html.unescape(clean_space(row.get("Response") or "")),
                via=clean_space(row.get("Via") or ""),
                screen_name=clean_space(row.get("Screen name") or ""),
                registered_participant=clean_space(row.get("Registered participant") or ""),
                created_at=clean_space(row.get("Created At") or ""),
            )
        )

    if not responses:
        raise ValueError(f"No Individual Results rows found in {path.name}.")

    dates = sorted({r.created_at[:10] for r in responses if len(r.created_at) >= 10})
    if len(dates) != 1:
        raise ValueError(
            f"{path.name}: expected one class date, found {dates or 'none'}."
        )

    return PollFile(
        path=path,
        question_name=path.stem,
        class_date=dates[0],
        summary=summary,
        responses=responses,
        file_hash=sha256_file(path),
    )


# ---------------------------------------------------------------------------
# Interactive scoring
# ---------------------------------------------------------------------------

def prompt_correct_answer(poll: PollFile, index: int, total: int) -> None:
    print("\n" + "-" * 72)
    print(f"Question {index} of {total}")
    print(f"File: {poll.path.name}")
    print(f"Date: {poll.class_date}")
    print(f"Responses: {len(poll.responses)}")

    if not poll.summary:
        counts: Dict[str, int] = defaultdict(int)
        for row in poll.responses:
            counts[row.response] += 1
        poll.summary = sorted(counts.items(), key=lambda x: (-x[1], x[0].casefold()))

    for i, (answer, count) in enumerate(poll.summary, start=1):
        print(f"  {i}. {answer}  ({count})")

    print("\nEnter the correct option number.")
    print("Multiple correct options: use commas, e.g. 1,3.")
    print("Enter S if this question counts for attendance but is NOT scored.")

    while True:
        raw = input("Correct answer [number(s) / S]: ").strip()
        if raw.casefold() == "s":
            poll.correct_answers = None
            print("  -> Scoring skipped.")
            return

        try:
            choices = [int(x.strip()) for x in raw.split(",") if x.strip()]
        except ValueError:
            choices = []

        if choices and all(1 <= x <= len(poll.summary) for x in choices):
            poll.correct_answers = {
                poll.summary[x - 1][0] for x in sorted(set(choices))
            }
            print("  -> Correct answer(s): " + "; ".join(sorted(poll.correct_answers)))
            return

        print(f"Please enter 1-{len(poll.summary)}, a comma-separated list, or S.")



# ---------------------------------------------------------------------------
# Canonical records
# ---------------------------------------------------------------------------

QUESTION_HEADERS = [
    "Question ID", "Date", "Question", "Source File", "Options",
    "Correct Answer", "Scored", "File Hash", "Imported At",
]

RESPONSE_HEADERS = [
    "Date", "Question ID", "Question",
    "Student Key", "Student Name", "Poll Participant", "Screen Name",
    "Response", "Correct", "Timestamp",
    "Source File", "File Hash", "Match Status",
]

IMPORT_LOG_HEADERS = [
    "Import ID", "Date", "Source File", "File Hash",
    "Question ID", "Effective Responses", "Matched Students",
    "Unmatched / Non-student", "Imported At",
]

ROSTER_HEADERS = [
    "Student Key", "Student Name", "Canvas Name", "Section",
    "Active", "First Seen", "Roster Updated At", "Notes",
]

PARTICIPANT_MAP_HEADERS = [
    "Poll Key", "Poll Participant", "Student Key", "Student Name",
    "Match Type", "Updated At", "Notes",
]


def effective_poll_responses(poll: PollFile) -> List[ResponseRow]:
    """Keep the latest response for each Poll participant in this question."""
    latest: Dict[str, ResponseRow] = {}
    for row in poll.responses:
        key = normalize_person_name(row.student_name)
        old = latest.get(key)
        if old is None or row.created_at >= old.created_at:
            latest[key] = row
    return sorted(latest.values(), key=lambda r: (r.student_name.casefold(), r.created_at))


def roster_by_key(roster_rows: Sequence[Dict[str, str]]) -> Dict[str, Dict[str, str]]:
    return {
        clean_space(r.get("Student Key", "")): r
        for r in roster_rows
        if clean_space(r.get("Student Key", ""))
    }


def active_roster_rows(roster_rows: Sequence[Dict[str, str]]) -> List[Dict[str, str]]:
    return [
        r for r in roster_rows
        if str(r.get("Active", "")).strip().upper() != "FALSE"
        and clean_space(r.get("Student Key", ""))
    ]


def participant_map_index(
    participant_map_rows: Sequence[Dict[str, str]]
) -> Dict[str, Dict[str, str]]:
    idx: Dict[str, Dict[str, str]] = {}
    for row in participant_map_rows:
        key = clean_space(row.get("Poll Key", ""))
        if not key:
            key = normalize_person_name(row.get("Poll Participant", ""))
        if key:
            idx[key] = dict(row)
    return idx


def exact_roster_name_index(
    roster_rows: Sequence[Dict[str, str]]
) -> Dict[str, Optional[Dict[str, str]]]:
    """Normalized name -> row, or None if that exact normalized name is ambiguous."""
    idx: Dict[str, Optional[Dict[str, str]]] = {}
    for row in roster_rows:
        norm = normalize_person_name(row.get("Student Name", ""))
        if not norm:
            continue
        if norm in idx:
            idx[norm] = None
        else:
            idx[norm] = row
    return idx


def make_map_row(
    poll_name: str,
    student: Optional[Dict[str, str]],
    match_type: str,
    notes: str = "",
) -> Dict[str, str]:
    return {
        "Poll Key": normalize_person_name(poll_name),
        "Poll Participant": clean_space(poll_name),
        "Student Key": "" if student is None else clean_space(student.get("Student Key", "")),
        "Student Name": "" if student is None else clean_space(student.get("Student Name", "")),
        "Match Type": match_type,
        "Updated At": now_iso(),
        "Notes": notes,
    }


def prompt_participant_match(
    poll_name: str,
    roster_rows: Sequence[Dict[str, str]],
) -> Dict[str, str]:
    print("\n" + "-" * 72)
    print("Participant match review")
    print(f"Poll Everywhere: {poll_name}")

    candidates = roster_candidates(poll_name, roster_rows, limit=5)
    if candidates:
        print("\nPossible Canvas students:")
        for i, (score, row) in enumerate(candidates, start=1):
            active = str(row.get("Active", "")).strip().upper() != "FALSE"
            status = "active" if active else "inactive"
            print(
                f"  {i}. {row.get('Student Name', '')} "
                f"({status}, similarity {score:.2f})"
            )

    print("\n  N. Mark as non-student / staff / guest")
    print("  S. Leave unresolved for now")

    while True:
        raw = input("Match [number / N / S]: ").strip().casefold()
        if raw == "n":
            return make_map_row(poll_name, None, "non-student")
        if raw == "s":
            return make_map_row(poll_name, None, "unresolved")
        try:
            choice = int(raw)
        except ValueError:
            choice = -1
        if 1 <= choice <= len(candidates):
            return make_map_row(
                poll_name,
                candidates[choice - 1][1],
                "confirmed",
            )
        print("Please choose a candidate number, N, or S.")


def resolve_participants(
    poll_names: Iterable[str],
    roster_rows: Sequence[Dict[str, str]],
    participant_map_rows: Sequence[Dict[str, str]],
    interactive: bool = True,
) -> Tuple[Dict[str, Dict[str, str]], List[Dict[str, str]], Dict[str, int]]:
    """Resolve raw Poll names to Canvas students.

    Order:
      1. existing persistent map
      2. unique exact normalized-name match
      3. human review of suggested fuzzy candidates

    Fuzzy matching is NEVER accepted automatically.
    """
    roster_idx = roster_by_key(roster_rows)
    exact_idx = exact_roster_name_index(roster_rows)
    map_idx = participant_map_index(participant_map_rows)

    stats = defaultdict(int)

    unique_names = sorted(
        {clean_space(n) for n in poll_names if clean_space(n)},
        key=str.casefold,
    )

    for poll_name in unique_names:
        pkey = normalize_person_name(poll_name)

        existing = map_idx.get(pkey)
        if existing:
            mtype = clean_space(existing.get("Match Type", ""))
            skey = clean_space(existing.get("Student Key", ""))

            if mtype == "non-student":
                stats["saved_nonstudent"] += 1
                continue

            if skey and skey in roster_idx:
                # Refresh official display name in case Canvas changed it.
                refreshed = dict(existing)
                refreshed["Student Name"] = roster_idx[skey].get("Student Name", "")
                map_idx[pkey] = refreshed
                stats["saved"] += 1
                continue

            # "unresolved", or stale map to a missing roster key: review again.
            if mtype == "unresolved":
                pass
            elif skey and skey not in roster_idx:
                print(
                    f"\nSaved mapping for {poll_name!r} points to a student "
                    "no longer present in the stored roster. Reviewing again."
                )

        exact = exact_idx.get(pkey, "__missing__")
        if exact != "__missing__" and exact is not None:
            row = make_map_row(poll_name, exact, "exact")
            map_idx[pkey] = row
            stats["exact"] += 1
            continue

        if interactive:
            row = prompt_participant_match(poll_name, roster_rows)
            map_idx[pkey] = row
            if row["Match Type"] == "confirmed":
                stats["confirmed"] += 1
            elif row["Match Type"] == "non-student":
                stats["nonstudent"] += 1
            else:
                stats["unresolved"] += 1
        else:
            row = make_map_row(poll_name, None, "unresolved")
            map_idx[pkey] = row
            stats["unresolved"] += 1

    updated_rows = sorted(
        map_idx.values(),
        key=lambda r: r.get("Poll Participant", "").casefold(),
    )
    return map_idx, updated_rows, dict(stats)


def collapse_canonical_responses(
    rows: Sequence[Dict[str, str]]
) -> List[Dict[str, str]]:
    """One latest effective response per question + canonical identity.

    Matched students collapse by Canvas student key.
    Non-students / unresolved identities collapse by normalized Poll participant.
    """
    latest: Dict[Tuple[str, str], Dict[str, str]] = {}
    for row in rows:
        qid = clean_space(row.get("Question ID", ""))
        skey = clean_space(row.get("Student Key", ""))
        poll_name = clean_space(
            row.get("Poll Participant", "") or row.get("Student Name", "")
        )
        identity = skey or f"poll:{normalize_person_name(poll_name)}"
        if not qid or not identity:
            continue
        key = (qid, identity)
        old = latest.get(key)
        if old is None or row.get("Timestamp", "") >= old.get("Timestamp", ""):
            latest[key] = dict(row)

    return sorted(
        latest.values(),
        key=lambda r: (
            r.get("Date", ""),
            r.get("Question ID", ""),
            (r.get("Student Name", "") or r.get("Poll Participant", "")).casefold(),
        ),
    )


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
        "Source File": poll.path.name,
        "Options": " | ".join(answer for answer, _ in poll.summary),
        "Correct Answer": correct_text,
        "Scored": "TRUE" if poll.scored else "FALSE",
        "File Hash": poll.file_hash,
        "Imported At": imported_at,
    }

    responses: List[Dict[str, str]] = []
    for r in effective_poll_responses(poll):
        poll_name = r.student_name
        map_row = mapping.get(normalize_person_name(poll_name), {})
        match_type = clean_space(map_row.get("Match Type", "")) or "unresolved"
        student_key = clean_space(map_row.get("Student Key", ""))
        student_name = clean_space(map_row.get("Student Name", ""))

        correct = ""
        if poll.correct_answers is not None:
            correct = "1" if r.response in poll.correct_answers else "0"

        responses.append(
            {
                "Date": poll.class_date,
                "Question ID": poll.question_id,
                "Question": poll.question_name,
                "Student Key": student_key,
                "Student Name": student_name,
                "Poll Participant": poll_name,
                "Screen Name": r.screen_name,
                "Response": r.response,
                "Correct": correct,
                "Timestamp": r.created_at,
                "Source File": poll.path.name,
                "File Hash": poll.file_hash,
                "Match Status": match_type,
            }
        )

    responses = collapse_canonical_responses(responses)
    matched_students = len({
        r["Student Key"] for r in responses if r.get("Student Key", "")
    })
    unmatched = sum(1 for r in responses if not r.get("Student Key", ""))

    import_log = {
        "Import ID": poll.file_hash[:12],
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


# ---------------------------------------------------------------------------
# Derived views
# ---------------------------------------------------------------------------

def roster_display_map(
    roster_rows: Sequence[Dict[str, str]],
) -> Tuple[List[str], Dict[str, str]]:
    """Return ACTIVE Canvas roster only, in alphabetical order."""
    active = active_roster_rows(roster_rows)
    active.sort(key=lambda r: r.get("Student Name", "").casefold())
    ordered = [r["Student Key"] for r in active]
    display = {r["Student Key"]: r.get("Student Name", r["Student Key"]) for r in active}
    return ordered, display


def latest_canonical_responses(
    rows: Sequence[Dict[str, str]]
) -> Dict[Tuple[str, str], Dict[str, str]]:
    latest: Dict[Tuple[str, str], Dict[str, str]] = {}
    for row in rows:
        qid = clean_space(row.get("Question ID", ""))
        skey = clean_space(row.get("Student Key", ""))
        if not qid or not skey:
            continue
        key = (qid, skey)
        old = latest.get(key)
        if old is None or row.get("Timestamp", "") >= old.get("Timestamp", ""):
            latest[key] = row
    return latest


def build_derived_tables(
    questions: Sequence[Dict[str, str]],
    responses: Sequence[Dict[str, str]],
    roster_rows: Sequence[Dict[str, str]],
) -> Tuple[List[List[str]], List[List[str]], List[List[str]]]:
    students, display = roster_display_map(roster_rows)

    dates = sorted({q.get("Date", "") for q in questions if q.get("Date", "")})
    scored_questions = [
        q for q in questions
        if str(q.get("Scored", "")).strip().upper() == "TRUE"
    ]
    scored_questions.sort(
        key=lambda q: (q.get("Date", ""), q.get("Question ID", ""))
    )

    active_keys = set(students)

    # Attendance is derived only from matched ACTIVE Canvas students.
    attended = {
        (clean_space(r.get("Student Key", "")), r.get("Date", ""))
        for r in responses
        if clean_space(r.get("Student Key", "")) in active_keys
        and r.get("Date", "")
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
        f'{q.get("Date", "")} | {q.get("Question", q.get("Question ID", ""))}'
        for q in scored_questions
    ]

    scores = [[
        "Student", *q_labels,
        "Total Correct", "Questions Answered", "Accuracy",
    ]]

    leaderboard_data: List[Tuple[str, int, int, Optional[float]]] = []

    for skey in students:
        vals: List[str] = []
        total_correct = 0
        answered = 0

        for q in scored_questions:
            row = latest.get((q.get("Question ID", ""), skey))
            if row is None:
                vals.append("")
                continue
            correct = str(row.get("Correct", ""))
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
        leaderboard_data.append(
            (display.get(skey, skey), total_correct, answered, accuracy)
        )

    leaderboard_data.sort(
        key=lambda x: (-x[1], -x[2], x[0].casefold())
    )

    leaderboard = [[
        "Rank", "Student", "Total Correct", "Questions Answered", "Accuracy",
    ]]
    last_score: Optional[int] = None
    rank = 0
    for i, (name, correct, answered, accuracy) in enumerate(
        leaderboard_data, start=1
    ):
        if correct != last_score:
            rank = i
            last_score = correct
        leaderboard.append([
            str(rank),
            name,
            str(correct),
            str(answered),
            "" if accuracy is None else f"{accuracy:.3f}",
        ])

    return attendance, scores, leaderboard


def build_attendance_review(
    questions: Sequence[Dict[str, str]],
    responses: Sequence[Dict[str, str]],
    roster_rows: Sequence[Dict[str, str]],
) -> List[List[str]]:
    """Build a review-oriented attendance sheet from canonical data.

    The view deliberately says "No matched Poll response" rather than
    "Absent". The importer can prove only that an active Canvas student did
    not have a matched response in the imported Poll Everywhere data. A student
    may have been physically present but not answered a poll, had a technical
    issue, or still have an unresolved Poll identity.

    The sheet contains three sections:
      1. class-level counts,
      2. one row per active Canvas student per class date,
      3. unmatched Poll identities that may explain discrepancies.
    """
    active = active_roster_rows(roster_rows)
    active.sort(key=lambda r: r.get("Student Name", "").casefold())
    active_keys = {clean_space(r.get("Student Key", "")) for r in active}

    dates = sorted({q.get("Date", "") for q in questions if q.get("Date", "")})

    # A matched response from an active Canvas student is evidence of presence.
    present_by_date: Dict[str, set[str]] = defaultdict(set)
    unresolved_by_date: Dict[str, set[str]] = defaultdict(set)
    nonstudent_by_date: Dict[str, set[str]] = defaultdict(set)

    for row in responses:
        date = clean_space(row.get("Date", ""))
        if not date:
            continue

        skey = clean_space(row.get("Student Key", ""))
        if skey in active_keys:
            present_by_date[date].add(skey)
            continue

        poll_name = clean_space(row.get("Poll Participant", ""))
        status = clean_space(row.get("Match Status", "")).casefold()
        if not poll_name:
            continue
        if status == "non-student":
            nonstudent_by_date[date].add(poll_name)
        else:
            # Includes explicit unresolved rows and any legacy unmatched row.
            unresolved_by_date[date].add(poll_name)

    matrix: List[List[str]] = []

    # Section 1: quick class-level audit.
    matrix.append(["Class Summary"])
    matrix.append([
        "Date",
        "Active Canvas Roster",
        "Present",
        "No matched Poll response",
        "Unresolved Poll participants",
        "Non-student participants",
    ])
    for date in dates:
        present = len(present_by_date.get(date, set()))
        missing = max(len(active) - present, 0)
        matrix.append([
            date,
            str(len(active)),
            str(present),
            str(missing),
            str(len(unresolved_by_date.get(date, set()))),
            str(len(nonstudent_by_date.get(date, set()))),
        ])

    matrix.append([])
    matrix.append(["Student Attendance Review"])
    matrix.append([
        "Date", "Student Key", "Student", "Status", "Notes"
    ])

    # Missing students appear first within each date so the actionable rows are
    # immediately visible. Present students remain in the table for a complete
    # audit trail and easy filtering.
    for date in dates:
        present_keys = present_by_date.get(date, set())
        detail_rows = []
        for student in active:
            skey = clean_space(student.get("Student Key", ""))
            name = clean_space(student.get("Student Name", ""))
            if skey in present_keys:
                status = "Present"
                notes = "Matched response to at least one imported poll"
                sort_order = 1
            else:
                status = "No matched Poll response"
                notes = (
                    "Review if needed; unresolved Poll identities may later "
                    "change this status"
                )
                sort_order = 0
            detail_rows.append((sort_order, name.casefold(), [
                date, skey, name, status, notes
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

# ---------------------------------------------------------------------------
# Local dry-run previews
# ---------------------------------------------------------------------------

def write_csv_matrix(path: Path, matrix: Sequence[Sequence[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f).writerows(matrix)


def write_dict_csv(
    path: Path,
    rows: Sequence[Dict[str, str]],
    headers: Sequence[str],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# Google Sheets helpers
# ---------------------------------------------------------------------------

def load_config(path: Path) -> Dict[str, str]:
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}\n"
            "Use the same config.json from v1.1."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def get_google_client(config: Dict[str, str]):
    try:
        import gspread
    except ImportError as exc:
        raise RuntimeError(
            "gspread is not installed. Run: python -m pip install -r requirements.txt"
        ) from exc

    return gspread.oauth(
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
        credentials_filename=config.get("credentials_file", "credentials.json"),
        authorized_user_filename=config.get(
            "authorized_user_file", "authorized_user.json"
        ),
    )


def ensure_worksheet(spreadsheet, title: str, rows: int = 100, cols: int = 20):
    try:
        return spreadsheet.worksheet(title)
    except Exception as exc:
        if exc.__class__.__name__ != "WorksheetNotFound":
            raise
        return spreadsheet.add_worksheet(title=title, rows=rows, cols=cols)


def worksheet_records(ws) -> List[Dict[str, str]]:
    values = ws.get_all_values()
    if not values:
        return []
    headers = [clean_space(h) for h in values[0]]
    if not any(headers):
        return []

    out: List[Dict[str, str]] = []
    for row in values[1:]:
        padded = row + [""] * (len(headers) - len(row))
        if not any(clean_space(cell) for cell in padded):
            continue
        out.append({
            headers[i]: padded[i] if i < len(padded) else ""
            for i in range(len(headers))
            if headers[i]
        })
    return out


def dicts_to_matrix(
    rows: Sequence[Dict[str, str]],
    headers: Sequence[str],
) -> List[List[str]]:
    return [list(headers)] + [
        [str(row.get(h, "")) for h in headers]
        for row in rows
    ]


def write_matrix_to_ws(ws, matrix: Sequence[Sequence[str]]) -> None:
    if not matrix:
        matrix = [[""]]

    nrows = max(len(matrix) + 10, 100)
    ncols = max(max(len(r) for r in matrix) + 2, 10)
    ws.resize(rows=nrows, cols=ncols)
    ws.clear()
    ws.update(values=[list(r) for r in matrix], range_name="A1")

    try:
        ws.freeze(rows=1, cols=1)
    except Exception:
        pass

    # Lightweight readability formatting. Failure here should never block data.
    try:
        ws.format("1:1", {
            "textFormat": {"bold": True},
            "horizontalAlignment": "CENTER",
        })
    except Exception:
        pass



# ---------------------------------------------------------------------------
# Legacy migration helpers
# ---------------------------------------------------------------------------

def normalize_response_schema(row: Dict[str, str]) -> Dict[str, str]:
    """Upgrade a v2/v1-style response row into the v2.1 schema."""
    poll_participant = clean_space(
        row.get("Poll Participant", "") or row.get("Student Name", "")
    )
    return {
        "Date": row.get("Date", ""),
        "Question ID": row.get("Question ID", ""),
        "Question": row.get("Question", ""),
        "Student Key": clean_space(row.get("Student Key", "")),
        "Student Name": clean_space(row.get("Student Name", "")),
        "Poll Participant": poll_participant,
        "Screen Name": row.get("Screen Name", ""),
        "Response": row.get("Response", ""),
        "Correct": row.get("Correct", ""),
        "Timestamp": row.get("Timestamp", ""),
        "Source File": row.get("Source File", ""),
        "File Hash": row.get("File Hash", ""),
        "Match Status": row.get("Match Status", ""),
    }


def migrate_v1_raw_responses_if_needed(
    spreadsheet,
    responses_ws,
    import_log_ws,
    questions: Sequence[Dict[str, str]],
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]], bool]:
    """Migrate v1.1 Raw Responses once if Responses is empty.

    The old Raw Responses tab is left untouched as a backup.
    """
    existing_responses = [
        normalize_response_schema(r) for r in worksheet_records(responses_ws)
    ]
    existing_log = worksheet_records(import_log_ws)
    if existing_responses:
        return existing_responses, existing_log, False

    try:
        old_ws = spreadsheet.worksheet("Raw Responses")
    except Exception as exc:
        if exc.__class__.__name__ == "WorksheetNotFound":
            return existing_responses, existing_log, False
        raise

    old_rows = worksheet_records(old_ws)
    if not old_rows:
        return existing_responses, existing_log, False

    latest: Dict[Tuple[str, str], Dict[str, str]] = {}
    for row in old_rows:
        poll_name = clean_space(row.get("Student", ""))
        if not poll_name:
            continue
        converted = {
            "Date": row.get("Date", ""),
            "Question ID": row.get("Question ID", ""),
            "Question": row.get("Question", ""),
            "Student Key": clean_space(row.get("Student Key", "")),
            "Student Name": poll_name,
            "Poll Participant": poll_name,
            "Screen Name": row.get("Screen Name", ""),
            "Response": row.get("Response", ""),
            "Correct": row.get("Correct", ""),
            "Timestamp": row.get("Timestamp", ""),
            "Source File": row.get("Source File", ""),
            "File Hash": row.get("File Hash", ""),
            "Match Status": "",
        }
        key = (
            converted["Question ID"],
            normalize_person_name(poll_name),
        )
        old = latest.get(key)
        if old is None or converted["Timestamp"] >= old.get("Timestamp", ""):
            latest[key] = converted

    migrated = collapse_canonical_responses(list(latest.values()))

    imported_at = now_iso()
    logs: List[Dict[str, str]] = []
    by_qid_count = defaultdict(int)
    for row in migrated:
        by_qid_count[row.get("Question ID", "")] += 1

    seen_hashes = set()
    for q in questions:
        file_hash = q.get("File Hash", "")
        if not file_hash or file_hash in seen_hashes:
            continue
        seen_hashes.add(file_hash)
        logs.append({
            "Import ID": file_hash[:12],
            "Date": q.get("Date", ""),
            "Source File": q.get("Source File", ""),
            "File Hash": file_hash,
            "Question ID": q.get("Question ID", ""),
            "Effective Responses": str(by_qid_count[q.get("Question ID", "")]),
            "Matched Students": "",
            "Unmatched / Non-student": "",
            "Imported At": q.get("Imported At", "") or imported_at,
        })

    write_matrix_to_ws(
        responses_ws, dicts_to_matrix(migrated, RESPONSE_HEADERS)
    )
    write_matrix_to_ws(
        import_log_ws, dicts_to_matrix(logs, IMPORT_LOG_HEADERS)
    )

    return migrated, logs, True


def remap_existing_responses(
    response_rows: Sequence[Dict[str, str]],
    mapping: Dict[str, Dict[str, str]],
) -> List[Dict[str, str]]:
    remapped: List[Dict[str, str]] = []

    for original in response_rows:
        row = normalize_response_schema(original)
        poll_name = clean_space(
            row.get("Poll Participant", "") or row.get("Student Name", "")
        )
        map_row = mapping.get(normalize_person_name(poll_name), {})
        match_type = clean_space(map_row.get("Match Type", "")) or "unresolved"

        if map_row.get("Student Key", ""):
            row["Student Key"] = clean_space(map_row.get("Student Key", ""))
            row["Student Name"] = clean_space(map_row.get("Student Name", ""))
        else:
            row["Student Key"] = ""
            row["Student Name"] = ""

        row["Poll Participant"] = poll_name
        row["Match Status"] = match_type
        remapped.append(row)

    return collapse_canonical_responses(remapped)


# ---------------------------------------------------------------------------
# Canvas roster synchronization
# ---------------------------------------------------------------------------

def build_updated_roster(
    canvas_students: Sequence[CanvasStudent],
    existing_roster: Sequence[Dict[str, str]],
    synced_at: str,
) -> Tuple[List[Dict[str, str]], Dict[str, int]]:
    existing_canvas = {
        clean_space(r.get("Student Key", "")): r
        for r in existing_roster
        if clean_space(r.get("Student Key", "")).startswith("canvas:")
    }

    current_keys = {s.student_key for s in canvas_students}
    updated: Dict[str, Dict[str, str]] = {}
    stats = defaultdict(int)

    for student in canvas_students:
        old = existing_canvas.get(student.student_key)
        if old:
            stats["retained"] += 1
            first_seen = old.get("First Seen", "") or synced_at
            notes = old.get("Notes", "")
        else:
            stats["added"] += 1
            first_seen = synced_at
            notes = ""

        updated[student.student_key] = {
            "Student Key": student.student_key,
            "Student Name": student.student_name,
            "Canvas Name": student.canvas_name,
            "Section": student.section,
            "Active": "TRUE",
            "First Seen": first_seen,
            "Roster Updated At": synced_at,
            "Notes": notes,
        }

    # Prior Canvas students who disappeared from the new roster remain as
    # historical identities, but become inactive.
    for key, old in existing_canvas.items():
        if key in current_keys:
            continue
        stats["inactivated"] += 1
        updated[key] = {
            "Student Key": key,
            "Student Name": old.get("Student Name", key),
            "Canvas Name": old.get("Canvas Name", ""),
            "Section": old.get("Section", ""),
            "Active": "FALSE",
            "First Seen": old.get("First Seen", ""),
            "Roster Updated At": synced_at,
            "Notes": old.get("Notes", ""),
        }

    rows = sorted(
        updated.values(),
        key=lambda r: (
            str(r.get("Active", "")).upper() == "FALSE",
            r.get("Student Name", "").casefold(),
        ),
    )
    return rows, dict(stats)


def ensure_course_worksheets(sh):
    return {
        "roster": ensure_worksheet(sh, "Roster"),
        "participant_map": ensure_worksheet(sh, "Participant Map"),
        "questions": ensure_worksheet(sh, "Questions"),
        "responses": ensure_worksheet(sh, "Responses"),
        "import_log": ensure_worksheet(sh, "Import Log"),
        "attendance": ensure_worksheet(sh, "Attendance"),
        "attendance_review": ensure_worksheet(sh, "Attendance Review"),
        "scores": ensure_worksheet(sh, "Scores"),
        "leaderboard": ensure_worksheet(sh, "Leaderboard"),
    }


def sync_canvas_roster(
    canvas_path: Path,
    config: Dict[str, str],
) -> None:
    canvas_students = parse_canvas_roster(canvas_path)

    gc = get_google_client(config)
    spreadsheet_id = clean_space(config.get("spreadsheet_id", ""))
    if not spreadsheet_id or spreadsheet_id == "PASTE_GOOGLE_SHEET_ID_HERE":
        raise ValueError("Please set spreadsheet_id in config.json.")

    sh = gc.open_by_key(spreadsheet_id)
    ws = ensure_course_worksheets(sh)

    existing_roster = worksheet_records(ws["roster"])
    existing_questions = worksheet_records(ws["questions"])
    existing_responses, existing_log, migrated = migrate_v1_raw_responses_if_needed(
        sh, ws["responses"], ws["import_log"], existing_questions
    )
    participant_map_rows = worksheet_records(ws["participant_map"])

    synced_at = now_iso()
    updated_roster, roster_stats = build_updated_roster(
        canvas_students, existing_roster, synced_at
    )

    if migrated:
        print("\nMigrated existing v1.1 data before roster matching.")
        print("The old Raw Responses tab was left untouched as a backup.")

    # Reconcile every previously observed Poll identity with the authoritative
    # Canvas roster. This is what migrates current v2 name-based keys to IDs.
    historical_poll_names = {
        clean_space(
            r.get("Poll Participant", "") or r.get("Student Name", "")
        )
        for r in existing_responses
        if clean_space(r.get("Poll Participant", "") or r.get("Student Name", ""))
    }

    mapping, updated_map_rows, match_stats = resolve_participants(
        historical_poll_names,
        updated_roster,
        participant_map_rows,
        interactive=True,
    )

    remapped_responses = remap_existing_responses(existing_responses, mapping)

    # Refresh legacy import-log match counts where possible.
    responses_by_qid = defaultdict(list)
    for row in remapped_responses:
        responses_by_qid[row.get("Question ID", "")].append(row)

    normalized_logs: List[Dict[str, str]] = []
    for old in existing_log:
        qid = old.get("Question ID", "")
        qrows = responses_by_qid.get(qid, [])
        normalized_logs.append({
            "Import ID": old.get("Import ID", "") or old.get("File Hash", "")[:12],
            "Date": old.get("Date", ""),
            "Source File": old.get("Source File", ""),
            "File Hash": old.get("File Hash", ""),
            "Question ID": qid,
            "Effective Responses": old.get(
                "Effective Responses",
                old.get("Responses Imported", str(len(qrows))),
            ),
            "Matched Students": str(len({
                r.get("Student Key", "") for r in qrows if r.get("Student Key", "")
            })),
            "Unmatched / Non-student": str(sum(
                1 for r in qrows if not r.get("Student Key", "")
            )),
            "Imported At": old.get("Imported At", ""),
        })

    attendance, scores, leaderboard = build_derived_tables(
        existing_questions, remapped_responses, updated_roster
    )
    attendance_review = build_attendance_review(
        existing_questions, remapped_responses, updated_roster
    )

    write_matrix_to_ws(
        ws["roster"], dicts_to_matrix(updated_roster, ROSTER_HEADERS)
    )
    write_matrix_to_ws(
        ws["participant_map"],
        dicts_to_matrix(updated_map_rows, PARTICIPANT_MAP_HEADERS),
    )
    write_matrix_to_ws(
        ws["responses"],
        dicts_to_matrix(remapped_responses, RESPONSE_HEADERS),
    )
    write_matrix_to_ws(
        ws["import_log"],
        dicts_to_matrix(normalized_logs, IMPORT_LOG_HEADERS),
    )
    write_matrix_to_ws(ws["attendance"], attendance)
    write_matrix_to_ws(ws["attendance_review"], attendance_review)
    write_matrix_to_ws(ws["scores"], scores)
    write_matrix_to_ws(ws["leaderboard"], leaderboard)

    active_count = len(active_roster_rows(updated_roster))
    inactive_count = len(updated_roster) - active_count
    matched_existing = len({
        r.get("Student Key", "") for r in remapped_responses
        if r.get("Student Key", "")
    })
    excluded_existing = sum(
        1 for r in remapped_responses if not r.get("Student Key", "")
    )

    print("\n" + "=" * 72)
    print("Canvas roster sync complete")
    print("=" * 72)
    print(f"Source: {canvas_path.name}")
    print(f"Active Canvas students: {active_count}")
    print(f"Inactive historical students: {inactive_count}")
    print(f"New roster students: {roster_stats.get('added', 0)}")
    print(f"Previously known roster students retained: {roster_stats.get('retained', 0)}")
    print(f"Students marked inactive: {roster_stats.get('inactivated', 0)}")

    if historical_poll_names:
        print("\nExisting Poll Everywhere identity reconciliation:")
        print(f"  Exact matches added: {match_stats.get('exact', 0)}")
        print(f"  Saved mappings reused: {match_stats.get('saved', 0)}")
        print(f"  Human-confirmed matches: {match_stats.get('confirmed', 0)}")
        print(
            "  Non-student / staff / guest: "
            f"{match_stats.get('nonstudent', 0) + match_stats.get('saved_nonstudent', 0)}"
        )
        print(f"  Left unresolved: {match_stats.get('unresolved', 0)}")
        print(f"  Matched historical students represented: {matched_existing}")
        print(f"  Historical unmatched/non-student responses: {excluded_existing}")

    print(f"\nGoogle Sheet updated: {sh.title}")


# ---------------------------------------------------------------------------
# Poll import synchronization
# ---------------------------------------------------------------------------

def sync_google_sheet(
    polls: Sequence[PollFile],
    config: Dict[str, str],
    replace: bool = False,
) -> None:
    gc = get_google_client(config)
    spreadsheet_id = clean_space(config.get("spreadsheet_id", ""))
    if not spreadsheet_id or spreadsheet_id == "PASTE_GOOGLE_SHEET_ID_HERE":
        raise ValueError("Please set spreadsheet_id in config.json.")

    sh = gc.open_by_key(spreadsheet_id)
    ws = ensure_course_worksheets(sh)

    roster_rows = worksheet_records(ws["roster"])
    if not any(
        clean_space(r.get("Student Key", "")).startswith("canvas:")
        for r in roster_rows
    ):
        raise RuntimeError(
            "No authoritative Canvas roster is loaded.\n"
            "Run first:\n"
            "  python ecs101_poll_importer.py --import-roster CanvasGrades.csv"
        )

    existing_questions = worksheet_records(ws["questions"])
    existing_responses, existing_log, migrated = migrate_v1_raw_responses_if_needed(
        sh, ws["responses"], ws["import_log"], existing_questions
    )
    participant_map_rows = worksheet_records(ws["participant_map"])

    if migrated:
        print("\nMigrated existing v1.1 data:")
        print("  Raw Responses -> Responses")
        print("  Existing imports -> Import Log")
        print("  The old Raw Responses tab was left untouched as a backup.")

    # Normalize any old v2 rows. If they are not mapped yet, resolve them now.
    existing_responses = [
        normalize_response_schema(r) for r in existing_responses
    ]
    historical_unmapped_names = {
        r.get("Poll Participant", "")
        for r in existing_responses
        if not clean_space(r.get("Student Key", "")).startswith("canvas:")
        and clean_space(r.get("Poll Participant", ""))
        and clean_space(r.get("Match Status", "")) != "non-student"
    }
    if historical_unmapped_names:
        historical_mapping, participant_map_rows, _ = resolve_participants(
            historical_unmapped_names,
            roster_rows,
            participant_map_rows,
            interactive=True,
        )
        existing_responses = remap_existing_responses(
            existing_responses, historical_mapping
        )

    existing_hashes = {
        row.get("File Hash", "")
        for row in existing_log
        if row.get("File Hash", "")
    }
    existing_qids = {
        row.get("Question ID", "")
        for row in existing_questions
        if row.get("Question ID", "")
    }

    exact_duplicates: List[PollFile] = []
    qid_conflicts: List[PollFile] = []
    candidates: List[PollFile] = []

    for poll in polls:
        if poll.file_hash in existing_hashes:
            exact_duplicates.append(poll)
        elif poll.question_id in existing_qids:
            qid_conflicts.append(poll)
        else:
            candidates.append(poll)

    if exact_duplicates:
        print("\nExact duplicate file(s) already imported; skipped:")
        for poll in exact_duplicates:
            print(f"  - {poll.path.name}  [{poll.file_hash[:12]}]")

    if qid_conflicts and not replace:
        print("\nQuestion ID conflict(s); skipped:")
        for poll in qid_conflicts:
            print(
                f"  - {poll.path.name} -> {poll.question_id}\n"
                "    Use --replace if this is an intentional corrected export."
            )
    elif qid_conflicts and replace:
        candidates.extend(qid_conflicts)
        replace_qids = {p.question_id for p in qid_conflicts}
        old_hashes_for_qids = {
            q.get("File Hash", "")
            for q in existing_questions
            if q.get("Question ID", "") in replace_qids
        }
        existing_questions = [
            q for q in existing_questions
            if q.get("Question ID", "") not in replace_qids
        ]
        existing_responses = [
            r for r in existing_responses
            if r.get("Question ID", "") not in replace_qids
        ]
        existing_log = [
            r for r in existing_log
            if r.get("Question ID", "") not in replace_qids
            and r.get("File Hash", "") not in old_hashes_for_qids
        ]
        print("\n--replace enabled; replacing:")
        for qid in sorted(replace_qids):
            print(f"  - {qid}")

    if not candidates:
        attendance, scores, leaderboard = build_derived_tables(
            existing_questions, existing_responses, roster_rows
        )
        attendance_review = build_attendance_review(
            existing_questions, existing_responses, roster_rows
        )
        write_matrix_to_ws(
            ws["participant_map"],
            dicts_to_matrix(participant_map_rows, PARTICIPANT_MAP_HEADERS),
        )
        write_matrix_to_ws(
            ws["responses"], dicts_to_matrix(
                collapse_canonical_responses(existing_responses), RESPONSE_HEADERS
            )
        )
        write_matrix_to_ws(ws["attendance"], attendance)
        write_matrix_to_ws(ws["attendance_review"], attendance_review)
        write_matrix_to_ws(ws["scores"], scores)
        write_matrix_to_ws(ws["leaderboard"], leaderboard)
        print(f"\nNo new files to import. Views refreshed in: {sh.title}")
        return

    incoming_poll_names = {
        r.student_name
        for poll in candidates
        for r in effective_poll_responses(poll)
    }

    mapping, updated_map_rows, match_stats = resolve_participants(
        incoming_poll_names,
        roster_rows,
        participant_map_rows,
        interactive=True,
    )

    imported_at = now_iso()
    incoming_questions: List[Dict[str, str]] = []
    incoming_responses: List[Dict[str, str]] = []
    incoming_logs: List[Dict[str, str]] = []

    for poll in candidates:
        q, responses, log = poll_to_records(poll, imported_at, mapping)
        incoming_questions.append(q)
        incoming_responses.extend(responses)
        incoming_logs.append(log)

    all_questions = [*existing_questions, *incoming_questions]
    all_responses = collapse_canonical_responses([
        *existing_responses, *incoming_responses
    ])
    all_logs = [*existing_log, *incoming_logs]

    all_questions.sort(key=lambda q: (
        q.get("Date", ""),
        q.get("Question ID", ""),
    ))
    all_logs.sort(key=lambda r: (
        r.get("Date", ""),
        r.get("Source File", "").casefold(),
    ))

    attendance, scores, leaderboard = build_derived_tables(
        all_questions, all_responses, roster_rows
    )
    attendance_review = build_attendance_review(
        all_questions, all_responses, roster_rows
    )

    write_matrix_to_ws(
        ws["questions"], dicts_to_matrix(all_questions, QUESTION_HEADERS)
    )
    write_matrix_to_ws(
        ws["responses"], dicts_to_matrix(all_responses, RESPONSE_HEADERS)
    )
    write_matrix_to_ws(
        ws["import_log"], dicts_to_matrix(all_logs, IMPORT_LOG_HEADERS)
    )
    write_matrix_to_ws(
        ws["participant_map"],
        dicts_to_matrix(updated_map_rows, PARTICIPANT_MAP_HEADERS),
    )
    write_matrix_to_ws(ws["attendance"], attendance)
    write_matrix_to_ws(ws["attendance_review"], attendance_review)
    write_matrix_to_ws(ws["scores"], scores)
    write_matrix_to_ws(ws["leaderboard"], leaderboard)

    active_keys = {
        r["Student Key"] for r in active_roster_rows(roster_rows)
    }
    incoming_matched_active = {
        r.get("Student Key", "")
        for r in incoming_responses
        if r.get("Student Key", "") in active_keys
    }
    incoming_excluded = {
        normalize_person_name(r.get("Poll Participant", ""))
        for r in incoming_responses
        if not r.get("Student Key", "")
    }

    print(f"\nGoogle Sheet updated: {sh.title}")
    print(f"Imported files: {len(candidates)}")
    print(f"Canonical questions: {len(all_questions)}")
    print(f"Canonical responses: {len(all_responses)}")
    print(f"Active students in roster: {len(active_keys)}")
    print(f"Active enrolled students participating in this batch: {len(incoming_matched_active)}")
    print(f"Unmatched / non-student participant identities in this batch: {len(incoming_excluded)}")

    if match_stats:
        print("\nParticipant matching:")
        print(f"  Exact matches added: {match_stats.get('exact', 0)}")
        print(f"  Saved mappings reused: {match_stats.get('saved', 0)}")
        print(f"  Human-confirmed matches: {match_stats.get('confirmed', 0)}")
        print(
            "  Non-student / staff / guest: "
            f"{match_stats.get('nonstudent', 0) + match_stats.get('saved_nonstudent', 0)}"
        )
        print(f"  Left unresolved: {match_stats.get('unresolved', 0)}")


def check_google_connection(config: Dict[str, str]) -> None:
    gc = get_google_client(config)
    spreadsheet_id = clean_space(config.get("spreadsheet_id", ""))
    if not spreadsheet_id or spreadsheet_id == "PASTE_GOOGLE_SHEET_ID_HERE":
        raise ValueError("Please set spreadsheet_id in config.json.")
    sh = gc.open_by_key(spreadsheet_id)
    print("\nGoogle connection successful.")
    print(f"Spreadsheet: {sh.title}")
    print("No spreadsheet data were changed.")


def refresh_google_views(config: Dict[str, str]) -> None:
    """Rebuild derived views from existing canonical Google Sheet data.

    This does not import a roster or a Poll CSV and does not change canonical
    Roster / Participant Map / Questions / Responses / Import Log records.
    """
    gc = get_google_client(config)
    spreadsheet_id = clean_space(config.get("spreadsheet_id", ""))
    if not spreadsheet_id or spreadsheet_id == "PASTE_GOOGLE_SHEET_ID_HERE":
        raise ValueError("Please set spreadsheet_id in config.json.")

    sh = gc.open_by_key(spreadsheet_id)
    ws = ensure_course_worksheets(sh)

    roster_rows = worksheet_records(ws["roster"])
    questions = worksheet_records(ws["questions"])
    responses = [normalize_response_schema(r) for r in worksheet_records(ws["responses"])]

    if not any(
        clean_space(r.get("Student Key", "")).startswith("canvas:")
        for r in roster_rows
    ):
        raise RuntimeError(
            "No authoritative Canvas roster is loaded. Run --import-roster first."
        )

    attendance, scores, leaderboard = build_derived_tables(
        questions, responses, roster_rows
    )
    attendance_review = build_attendance_review(
        questions, responses, roster_rows
    )

    write_matrix_to_ws(ws["attendance"], attendance)
    write_matrix_to_ws(ws["attendance_review"], attendance_review)
    write_matrix_to_ws(ws["scores"], scores)
    write_matrix_to_ws(ws["leaderboard"], leaderboard)

    print("\nDerived views refreshed successfully.")
    print(f"Spreadsheet: {sh.title}")
    print("Updated: Attendance, Attendance Review, Scores, Leaderboard")
    print("Canonical data were not changed.")

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def discover_csvs(input_path: Path) -> List[Path]:
    if input_path.is_file():
        if input_path.suffix.casefold() != ".csv":
            raise ValueError("Input file must be a .csv file.")
        return [input_path]

    if not input_path.exists():
        raise FileNotFoundError(f"Input path not found: {input_path}")

    files = sorted(p for p in input_path.glob("*.csv") if p.is_file())
    if not files:
        raise FileNotFoundError(f"No CSV files found in: {input_path}")
    return files


def print_batch_summary(polls: Sequence[PollFile]) -> None:
    all_students = {
        normalize_person_name(r.student_name)
        for p in polls
        for r in effective_poll_responses(p)
    }
    dates = sorted({p.class_date for p in polls})

    print("\n" + "=" * 72)
    print(f"ECS101 Poll Everywhere Importer v{VERSION}")
    print("=" * 72)
    print(f"CSV files found: {len(polls)}")
    print(f"Class date(s): {', '.join(dates)}")
    print(f"Unique Poll Everywhere participants: {len(all_students)}")

    unregistered = sorted({
        r.student_name
        for p in polls
        for r in p.responses
        if r.student_name.startswith("[UNREGISTERED]")
    })
    if unregistered:
        print(f"WARNING: {len(unregistered)} unregistered participant(s) found.")


def pseudo_roster_and_mapping_for_legacy_dry_run(
    polls: Sequence[PollFile],
) -> Tuple[List[Dict[str, str]], Dict[str, Dict[str, str]], List[Dict[str, str]]]:
    """Backward-compatible parser preview when no Canvas roster is supplied."""
    names = sorted({
        r.student_name
        for p in polls
        for r in effective_poll_responses(p)
    }, key=str.casefold)

    roster = []
    mapping_rows = []
    mapping = {}
    stamp = now_iso()

    for name in names:
        key = f"preview:{normalize_person_name(name)}"
        roster_row = {
            "Student Key": key,
            "Student Name": name,
            "Canvas Name": "",
            "Section": "",
            "Active": "TRUE",
            "First Seen": stamp,
            "Roster Updated At": stamp,
            "Notes": "Dry-run pseudo roster; not for production.",
        }
        map_row = {
            "Poll Key": normalize_person_name(name),
            "Poll Participant": name,
            "Student Key": key,
            "Student Name": name,
            "Match Type": "dry-run-only",
            "Updated At": stamp,
            "Notes": "",
        }
        roster.append(roster_row)
        mapping_rows.append(map_row)
        mapping[normalize_person_name(name)] = map_row

    return roster, mapping, mapping_rows


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Import a Canvas roster and Poll Everywhere CSVs into normalized "
            "Google Sheets tables, then rebuild attendance/scores/leaderboard."
        )
    )
    parser.add_argument(
        "input",
        nargs="?",
        default=".",
        help="Folder containing Poll Everywhere CSVs, or one Poll CSV file.",
    )
    parser.add_argument(
        "--import-roster",
        metavar="CANVAS_CSV",
        help=(
            "Sync the authoritative student roster from a Canvas gradebook CSV. "
            "Missing previously rostered students are marked inactive."
        ),
    )
    parser.add_argument(
        "--roster-csv",
        metavar="CANVAS_CSV",
        help=(
            "For --dry-run only: use this Canvas roster locally for participant "
            "matching without writing Google Sheets."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not write Google Sheets; produce local preview CSVs.",
    )
    parser.add_argument(
        "--output-dir",
        default="output_preview_v2_1",
        help="Preview folder for --dry-run.",
    )
    parser.add_argument(
        "--config",
        default="config.json",
        help="Google Sheet config JSON (default: config.json).",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Replace an existing question with the same Question ID.",
    )
    parser.add_argument(
        "--check-google",
        action="store_true",
        help="Verify Google Sheet access without importing data.",
    )
    parser.add_argument(
        "--refresh-views",
        action="store_true",
        help=(
            "Rebuild Attendance, Attendance Review, Scores, and Leaderboard "
            "from existing canonical Google Sheet data without importing files."
        ),
    )
    args = parser.parse_args(argv)

    if args.check_google:
        try:
            config = load_config(Path(args.config).expanduser().resolve())
            check_google_connection(config)
        except Exception as exc:
            print(f"ERROR while checking Google Sheets: {exc}", file=sys.stderr)
            return 3
        return 0

    if args.refresh_views:
        try:
            config = load_config(Path(args.config).expanduser().resolve())
            refresh_google_views(config)
        except Exception as exc:
            print(f"ERROR while refreshing views: {exc}", file=sys.stderr)
            return 3
        return 0

    if args.import_roster:
        if args.dry_run:
            try:
                students = parse_canvas_roster(
                    Path(args.import_roster).expanduser().resolve()
                )
                print(f"Canvas roster parsed successfully: {len(students)} students.")
                print("No Google Sheet data were changed.")
                return 0
            except Exception as exc:
                print(f"ERROR while reading Canvas roster: {exc}", file=sys.stderr)
                return 2

        try:
            config = load_config(Path(args.config).expanduser().resolve())
            sync_canvas_roster(
                Path(args.import_roster).expanduser().resolve(),
                config,
            )
        except (KeyboardInterrupt, EOFError):
            print("\nCancelled. No roster changes were written.")
            return 130
        except Exception as exc:
            print(f"\nERROR while syncing Canvas roster: {exc}", file=sys.stderr)
            return 3
        return 0

    try:
        csv_paths = discover_csvs(Path(args.input).expanduser().resolve())
        polls = [parse_poll_everywhere_csv(p) for p in csv_paths]
    except Exception as exc:
        print(f"ERROR while reading Poll Everywhere CSV files: {exc}", file=sys.stderr)
        return 2

    print_batch_summary(polls)

    if len({p.class_date for p in polls}) > 1:
        print("WARNING: input files span multiple dates; each will be stored by its own date.")

    try:
        for i, poll in enumerate(polls, start=1):
            prompt_correct_answer(poll, i, len(polls))
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled. No data were written.")
        return 130

    imported_at = now_iso()

    if args.dry_run:
        try:
            if args.roster_csv:
                students = parse_canvas_roster(
                    Path(args.roster_csv).expanduser().resolve()
                )
                roster_rows, _ = build_updated_roster(students, [], imported_at)
                poll_names = {
                    r.student_name
                    for p in polls
                    for r in effective_poll_responses(p)
                }
                mapping, map_rows, _ = resolve_participants(
                    poll_names,
                    roster_rows,
                    [],
                    interactive=True,
                )
            else:
                print(
                    "\nNOTE: no --roster-csv supplied. Dry run will use temporary "
                    "name-based identities. Production imports require the Canvas roster."
                )
                roster_rows, mapping, map_rows = (
                    pseudo_roster_and_mapping_for_legacy_dry_run(polls)
                )

            preview_questions: List[Dict[str, str]] = []
            preview_responses: List[Dict[str, str]] = []
            preview_logs: List[Dict[str, str]] = []

            for poll in polls:
                q, responses, log = poll_to_records(
                    poll, imported_at, mapping
                )
                preview_questions.append(q)
                preview_responses.extend(responses)
                preview_logs.append(log)

            preview_responses = collapse_canonical_responses(preview_responses)
            attendance, scores, leaderboard = build_derived_tables(
                preview_questions, preview_responses, roster_rows
            )
            attendance_review = build_attendance_review(
                preview_questions, preview_responses, roster_rows
            )

            out = Path(args.output_dir).expanduser().resolve()
            out.mkdir(parents=True, exist_ok=True)

            write_dict_csv(
                out / "roster_preview.csv",
                roster_rows,
                ROSTER_HEADERS,
            )
            write_dict_csv(
                out / "participant_map_preview.csv",
                map_rows,
                PARTICIPANT_MAP_HEADERS,
            )
            write_dict_csv(
                out / "questions_preview.csv",
                preview_questions,
                QUESTION_HEADERS,
            )
            write_dict_csv(
                out / "responses_preview.csv",
                preview_responses,
                RESPONSE_HEADERS,
            )
            write_dict_csv(
                out / "import_log_preview.csv",
                preview_logs,
                IMPORT_LOG_HEADERS,
            )
            write_csv_matrix(out / "attendance_preview.csv", attendance)
            write_csv_matrix(
                out / "attendance_review_preview.csv", attendance_review
            )
            write_csv_matrix(out / "scores_preview.csv", scores)
            write_csv_matrix(out / "leaderboard_preview.csv", leaderboard)

            print("\n" + "=" * 72)
            print("Dry-run batch summary")
            print("=" * 72)
            print(f"Questions: {len(polls)}")
            print(f"Canonical responses: {len(preview_responses)}")
            print(
                "Matched roster students represented: "
                f"{len({r['Student Key'] for r in preview_responses if r.get('Student Key', '')})}"
            )
            print(f"\nPreview files written to:\n  {out}")
            return 0

        except (KeyboardInterrupt, EOFError):
            print("\nCancelled. No data were written.")
            return 130
        except Exception as exc:
            print(f"ERROR during dry run: {exc}", file=sys.stderr)
            return 2

    try:
        config = load_config(Path(args.config).expanduser().resolve())
        sync_google_sheet(polls, config, replace=args.replace)
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled. No new poll data were written.")
        return 130
    except Exception as exc:
        print(f"\nERROR while updating Google Sheets: {exc}", file=sys.stderr)
        print("Original Poll Everywhere CSV files were not modified.", file=sys.stderr)
        return 3

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
