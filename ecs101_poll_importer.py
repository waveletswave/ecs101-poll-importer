#!/usr/bin/env python3
"""ECS101 Poll Everywhere -> Google Sheets importer (v2).

Architecture
------------
Original Poll Everywhere CSVs
        ↓
Python cleaning / deduplication
        ↓
Canonical Google Sheets tables:
    Roster
    Questions
    Responses
    Import Log
        ↓
Automatically rebuilt views:
    Attendance
    Scores
    Leaderboard

Key rules
---------
* Attendance: a student is present on a date if they answered ANY imported
  Poll Everywhere question that day.
* Scored questions: 1 = correct, 0 = incorrect, blank = unanswered.
* Unscored questions still count toward attendance.
* Responses contains ONE effective (latest) response per student per question.
  The original CSV files remain the raw archive.
* Exact duplicate CSVs are detected by SHA-256 file hash, even if renamed.
* Existing v1.1 "Raw Responses" data can be migrated automatically once.
"""

from __future__ import annotations

import argparse
import csv
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


VERSION = "2.0"


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
        name = self.registered_participant.strip()
        if name:
            return name
        screen = self.screen_name.strip() or "Unknown participant"
        return f"[UNREGISTERED] {screen}"

    @property
    def student_key(self) -> str:
        return make_student_key(self.student_name)


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
        # Date + filename stem is readable and deterministic.
        return f"{self.class_date}::{self.question_name}"

    @property
    def scored(self) -> bool:
        return self.correct_answers is not None


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

def clean_space(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def make_student_key(name: str) -> str:
    """Create a deterministic key from the Poll Everywhere participant name.

    This is intentionally conservative: it does not guess that two different
    spellings are the same student. The Roster tab can later serve as a place
    for human-reviewed identity management if needed.
    """
    normalized = unicodedata.normalize("NFKC", clean_space(name)).casefold()
    return normalized


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


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
    "Student Key", "Student Name", "Screen Name",
    "Response", "Correct", "Timestamp",
    "Source File", "File Hash",
]

IMPORT_LOG_HEADERS = [
    "Import ID", "Date", "Source File", "File Hash",
    "Question ID", "Responses Imported", "Imported At",
]

ROSTER_HEADERS = [
    "Student Key", "Student Name", "Active", "Notes",
]


def effective_poll_responses(poll: PollFile) -> List[ResponseRow]:
    """Keep the latest response for each student in this question."""
    latest: Dict[str, ResponseRow] = {}
    for row in poll.responses:
        key = row.student_key
        old = latest.get(key)
        if old is None or row.created_at >= old.created_at:
            latest[key] = row
    return sorted(latest.values(), key=lambda r: (r.student_name.casefold(), r.created_at))


def poll_to_records(
    poll: PollFile,
    imported_at: str,
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
        correct = ""
        if poll.correct_answers is not None:
            correct = "1" if r.response in poll.correct_answers else "0"

        responses.append(
            {
                "Date": poll.class_date,
                "Question ID": poll.question_id,
                "Question": poll.question_name,
                "Student Key": r.student_key,
                "Student Name": r.student_name,
                "Screen Name": r.screen_name,
                "Response": r.response,
                "Correct": correct,
                "Timestamp": r.created_at,
                "Source File": poll.path.name,
                "File Hash": poll.file_hash,
            }
        )

    import_log = {
        "Import ID": poll.file_hash[:12],
        "Date": poll.class_date,
        "Source File": poll.path.name,
        "File Hash": poll.file_hash,
        "Question ID": poll.question_id,
        "Responses Imported": str(len(responses)),
        "Imported At": imported_at,
    }

    return question, responses, import_log


# ---------------------------------------------------------------------------
# Derived views
# ---------------------------------------------------------------------------

def roster_display_map(
    roster_rows: Sequence[Dict[str, str]],
    response_rows: Sequence[Dict[str, str]],
) -> Tuple[List[str], Dict[str, str]]:
    """Return ordered student keys + display names.

    Roster order comes first. Students observed in Responses but absent from
    Roster are appended alphabetically.
    """
    display: Dict[str, str] = {}
    ordered: List[str] = []
    seen = set()

    for row in roster_rows:
        key = clean_space(row.get("Student Key", ""))
        name = clean_space(row.get("Student Name", ""))
        if not key and name:
            key = make_student_key(name)
        if not key:
            continue
        if str(row.get("Active", "")).strip().upper() == "FALSE":
            # Keep inactive students if they have historical responses; they
            # are added later from response data.
            continue
        if key not in seen:
            seen.add(key)
            ordered.append(key)
        display[key] = name or key

    observed: Dict[str, str] = {}
    for row in response_rows:
        key = clean_space(row.get("Student Key", ""))
        name = clean_space(row.get("Student Name", ""))
        if not key:
            key = make_student_key(name)
        if key:
            observed[key] = name or display.get(key, key)

    for key in sorted(observed, key=lambda k: observed[k].casefold()):
        if key not in seen:
            seen.add(key)
            ordered.append(key)
        display.setdefault(key, observed[key])

    return ordered, display


def latest_canonical_responses(
    rows: Sequence[Dict[str, str]]
) -> Dict[Tuple[str, str], Dict[str, str]]:
    """Latest canonical row per (question_id, student_key)."""
    latest: Dict[Tuple[str, str], Dict[str, str]] = {}
    for row in rows:
        qid = row.get("Question ID", "")
        skey = row.get("Student Key", "") or make_student_key(row.get("Student Name", ""))
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
    students, display = roster_display_map(roster_rows, responses)

    dates = sorted({q.get("Date", "") for q in questions if q.get("Date", "")})
    scored_questions = [
        q for q in questions
        if str(q.get("Scored", "")).strip().upper() == "TRUE"
    ]
    scored_questions.sort(
        key=lambda q: (q.get("Date", ""), q.get("Question ID", ""))
    )

    # Attendance is derived from ANY response on that class date.
    attended = {
        (
            r.get("Student Key", "") or make_student_key(r.get("Student Name", "")),
            r.get("Date", ""),
        )
        for r in responses
        if r.get("Date", "")
    }

    attendance = [["Student", *dates, "Classes Attended"]]
    for skey in students:
        vals = ["P" if (skey, d) in attended else "" for d in dates]
        attendance.append([display.get(skey, skey), *vals, str(sum(v == "P" for v in vals))])

    latest = latest_canonical_responses(responses)
    q_labels = [
        f'{q.get("Date", "")} | {q.get("Question", q.get("Question ID", ""))}'
        for q in scored_questions
    ]

    scores = [[
        "Student", *q_labels,
        "Total Correct", "Questions Answered", "Accuracy"
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
        "Rank", "Student", "Total Correct", "Questions Answered", "Accuracy"
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
# v1.1 -> v2 migration
# ---------------------------------------------------------------------------

def migrate_v1_raw_responses_if_needed(
    spreadsheet,
    responses_ws,
    import_log_ws,
    questions: Sequence[Dict[str, str]],
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]], bool]:
    """Migrate v1.1 Raw Responses once if v2 Responses is empty.

    The old Raw Responses tab is left untouched as a backup.
    """
    existing_responses = worksheet_records(responses_ws)
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

    # Keep one latest effective response per question/student.
    latest: Dict[Tuple[str, str], Dict[str, str]] = {}
    for row in old_rows:
        student_name = clean_space(row.get("Student", ""))
        if not student_name:
            continue
        skey = make_student_key(student_name)
        converted = {
            "Date": row.get("Date", ""),
            "Question ID": row.get("Question ID", ""),
            "Question": row.get("Question", ""),
            "Student Key": skey,
            "Student Name": student_name,
            "Screen Name": row.get("Screen Name", ""),
            "Response": row.get("Response", ""),
            "Correct": row.get("Correct", ""),
            "Timestamp": row.get("Timestamp", ""),
            "Source File": row.get("Source File", ""),
            "File Hash": row.get("File Hash", ""),
        }
        key = (converted["Question ID"], skey)
        old = latest.get(key)
        if old is None or converted["Timestamp"] >= old.get("Timestamp", ""):
            latest[key] = converted

    migrated = list(latest.values())
    migrated.sort(key=lambda r: (
        r.get("Date", ""),
        r.get("Question ID", ""),
        r.get("Student Name", "").casefold(),
    ))

    # Build an import log from existing Questions.
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
            "Responses Imported": str(by_qid_count[q.get("Question ID", "")]),
            "Imported At": q.get("Imported At", "") or imported_at,
        })

    write_matrix_to_ws(
        responses_ws, dicts_to_matrix(migrated, RESPONSE_HEADERS)
    )
    write_matrix_to_ws(
        import_log_ws, dicts_to_matrix(logs, IMPORT_LOG_HEADERS)
    )

    return migrated, logs, True


# ---------------------------------------------------------------------------
# Google synchronization
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

    roster_ws = ensure_worksheet(sh, "Roster")
    questions_ws = ensure_worksheet(sh, "Questions")
    responses_ws = ensure_worksheet(sh, "Responses")
    import_log_ws = ensure_worksheet(sh, "Import Log")
    attendance_ws = ensure_worksheet(sh, "Attendance")
    scores_ws = ensure_worksheet(sh, "Scores")
    leaderboard_ws = ensure_worksheet(sh, "Leaderboard")

    # Initialize Roster if empty.
    roster_rows = worksheet_records(roster_ws)
    if not roster_rows and not roster_ws.get_all_values():
        write_matrix_to_ws(roster_ws, [ROSTER_HEADERS])

    existing_questions = worksheet_records(questions_ws)
    existing_responses, existing_log, migrated = migrate_v1_raw_responses_if_needed(
        sh, responses_ws, import_log_ws, existing_questions
    )
    roster_rows = worksheet_records(roster_ws)

    if migrated:
        print("\nMigrated existing v1.1 data:")
        print("  Raw Responses -> Responses")
        print("  Existing imports -> Import Log")
        print("  The old Raw Responses tab was left untouched as a backup.")

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

    # Decide which incoming polls are actually new before scoring has any effect.
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
        # Still rebuild views in case Roster or canonical data changed manually.
        attendance, scores, leaderboard = build_derived_tables(
            existing_questions, existing_responses, roster_rows
        )
        write_matrix_to_ws(attendance_ws, attendance)
        write_matrix_to_ws(scores_ws, scores)
        write_matrix_to_ws(leaderboard_ws, leaderboard)
        print(f"\nNo new files to import. Views refreshed in: {sh.title}")
        return

    imported_at = now_iso()
    incoming_questions: List[Dict[str, str]] = []
    incoming_responses: List[Dict[str, str]] = []
    incoming_logs: List[Dict[str, str]] = []

    for poll in candidates:
        q, responses, log = poll_to_records(poll, imported_at)
        incoming_questions.append(q)
        incoming_responses.extend(responses)
        incoming_logs.append(log)

    all_questions = [*existing_questions, *incoming_questions]
    all_responses = [*existing_responses, *incoming_responses]
    all_logs = [*existing_log, *incoming_logs]

    all_questions.sort(key=lambda q: (
        q.get("Date", ""),
        q.get("Question ID", ""),
    ))
    all_responses.sort(key=lambda r: (
        r.get("Date", ""),
        r.get("Question ID", ""),
        r.get("Student Name", "").casefold(),
    ))
    all_logs.sort(key=lambda r: (
        r.get("Date", ""),
        r.get("Source File", "").casefold(),
    ))

    # Auto-add newly observed students to Roster. Human edits to Active/Notes
    # are preserved for existing rows.
    roster_by_key: Dict[str, Dict[str, str]] = {}
    for row in roster_rows:
        key = clean_space(row.get("Student Key", ""))
        name = clean_space(row.get("Student Name", ""))
        if not key and name:
            key = make_student_key(name)
        if key:
            roster_by_key[key] = {
                "Student Key": key,
                "Student Name": name or key,
                "Active": row.get("Active", "") or "TRUE",
                "Notes": row.get("Notes", ""),
            }

    for row in incoming_responses:
        key = row["Student Key"]
        if key not in roster_by_key:
            roster_by_key[key] = {
                "Student Key": key,
                "Student Name": row["Student Name"],
                "Active": "TRUE",
                "Notes": "",
            }

    updated_roster = sorted(
        roster_by_key.values(),
        key=lambda r: r["Student Name"].casefold()
    )

    attendance, scores, leaderboard = build_derived_tables(
        all_questions, all_responses, updated_roster
    )

    # Canonical tables.
    write_matrix_to_ws(
        roster_ws, dicts_to_matrix(updated_roster, ROSTER_HEADERS)
    )
    write_matrix_to_ws(
        questions_ws, dicts_to_matrix(all_questions, QUESTION_HEADERS)
    )
    write_matrix_to_ws(
        responses_ws, dicts_to_matrix(all_responses, RESPONSE_HEADERS)
    )
    write_matrix_to_ws(
        import_log_ws, dicts_to_matrix(all_logs, IMPORT_LOG_HEADERS)
    )

    # Rebuildable views.
    write_matrix_to_ws(attendance_ws, attendance)
    write_matrix_to_ws(scores_ws, scores)
    write_matrix_to_ws(leaderboard_ws, leaderboard)

    print(f"\nGoogle Sheet updated: {sh.title}")
    print(f"Imported files: {len(candidates)}")
    print(f"Canonical questions: {len(all_questions)}")
    print(f"Canonical responses: {len(all_responses)}")
    print(f"Students in roster: {len(updated_roster)}")


def check_google_connection(config: Dict[str, str]) -> None:
    gc = get_google_client(config)
    spreadsheet_id = clean_space(config.get("spreadsheet_id", ""))
    if not spreadsheet_id or spreadsheet_id == "PASTE_GOOGLE_SHEET_ID_HERE":
        raise ValueError("Please set spreadsheet_id in config.json.")
    sh = gc.open_by_key(spreadsheet_id)
    print("\nGoogle connection successful.")
    print(f"Spreadsheet: {sh.title}")
    print("No spreadsheet data were changed.")


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
        r.student_key
        for p in polls
        for r in effective_poll_responses(p)
    }
    dates = sorted({p.class_date for p in polls})

    print("\n" + "=" * 72)
    print(f"ECS101 Poll Everywhere Importer v{VERSION}")
    print("=" * 72)
    print(f"CSV files found: {len(polls)}")
    print(f"Class date(s): {', '.join(dates)}")
    print(f"Unique participants across all questions: {len(all_students)}")

    unregistered = sorted({
        r.student_name
        for p in polls
        for r in p.responses
        if r.student_name.startswith("[UNREGISTERED]")
    })
    if unregistered:
        print(f"WARNING: {len(unregistered)} unregistered participant(s) found.")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Import Poll Everywhere CSVs into normalized Google Sheets tables "
            "and rebuild attendance/scores/leaderboard."
        )
    )
    parser.add_argument(
        "input",
        nargs="?",
        default=".",
        help="Folder containing Poll Everywhere CSVs, or one CSV file.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not write Google Sheets; produce local preview CSVs.",
    )
    parser.add_argument(
        "--output-dir",
        default="output_preview_v2",
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
    args = parser.parse_args(argv)

    if args.check_google:
        try:
            config = load_config(Path(args.config).expanduser().resolve())
            check_google_connection(config)
        except Exception as exc:
            print(f"ERROR while checking Google Sheets: {exc}", file=sys.stderr)
            return 3
        return 0

    try:
        csv_paths = discover_csvs(Path(args.input).expanduser().resolve())
        polls = [parse_poll_everywhere_csv(p) for p in csv_paths]
    except Exception as exc:
        print(f"ERROR while reading CSV files: {exc}", file=sys.stderr)
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
    preview_questions: List[Dict[str, str]] = []
    preview_responses: List[Dict[str, str]] = []
    preview_logs: List[Dict[str, str]] = []

    for poll in polls:
        q, responses, log = poll_to_records(poll, imported_at)
        preview_questions.append(q)
        preview_responses.extend(responses)
        preview_logs.append(log)

    print("\n" + "=" * 72)
    print("Batch summary")
    print("=" * 72)
    for poll in polls:
        effective = effective_poll_responses(poll)
        if poll.scored:
            correct = sum(
                1 for r in effective
                if r.response in (poll.correct_answers or set())
            )
            print(
                f"{poll.path.name}: "
                f"{len(effective)} effective responses, {correct} correct"
            )
        else:
            print(
                f"{poll.path.name}: "
                f"{len(effective)} effective responses, scoring skipped"
            )

    print(
        "Attendance this batch: "
        f"{len({r['Student Key'] for r in preview_responses})} unique students"
    )

    if args.dry_run:
        out = Path(args.output_dir).expanduser().resolve()
        out.mkdir(parents=True, exist_ok=True)

        preview_roster = [{
            "Student Key": r["Student Key"],
            "Student Name": r["Student Name"],
            "Active": "TRUE",
            "Notes": "",
        } for r in {
            r["Student Key"]: r for r in preview_responses
        }.values()]
        preview_roster.sort(key=lambda r: r["Student Name"].casefold())

        attendance, scores, leaderboard = build_derived_tables(
            preview_questions, preview_responses, preview_roster
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
        write_dict_csv(
            out / "roster_preview.csv",
            preview_roster,
            ROSTER_HEADERS,
        )
        write_csv_matrix(out / "attendance_preview.csv", attendance)
        write_csv_matrix(out / "scores_preview.csv", scores)
        write_csv_matrix(out / "leaderboard_preview.csv", leaderboard)

        print(f"\nDry run complete. Preview files written to:\n  {out}")
        return 0

    try:
        config = load_config(Path(args.config).expanduser().resolve())
        sync_google_sheet(polls, config, replace=args.replace)
    except Exception as exc:
        print(f"\nERROR while updating Google Sheets: {exc}", file=sys.stderr)
        print("Original Poll Everywhere CSV files were not modified.", file=sys.stderr)
        return 3

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
