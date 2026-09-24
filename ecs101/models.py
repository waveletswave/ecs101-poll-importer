"""Data structures and the canonical Google Sheets table schemas."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .normalize import (
    clean_space,
    normalize_answer_text,
    normalize_email,
    normalize_person_name,
    poll_identity_key,
    timestamp_sort_key,
)

__all__ = [
    "ResponseRow",
    "OffDateParticipant",
    "PollFile",
    "CanvasStudent",
    "ParseWarning",
    "QUESTION_HEADERS",
    "RESPONSE_HEADERS",
    "IMPORT_LOG_HEADERS",
    "ROSTER_HEADERS",
    "PARTICIPANT_MAP_HEADERS",
    "EXCUSED_HEADERS",
    "EXCUSED_SEED_ROWS",
    "LEGACY_RESPONSE_COLUMNS",
]


# ---------------------------------------------------------------------------
# Table schemas
# ---------------------------------------------------------------------------

QUESTION_HEADERS = [
    "Question ID", "Date", "Question", "Question Order", "Source Format",
    "Source File", "First Response At", "Options", "Correct Answer", "Scored",
    "File Hash", "Imported At",
]

# v3 drops "Question" and "Source File" from Responses. Both are reachable from
# Questions through "Question ID" (which already embeds the question text), and
# "File Hash" is stored as a 12-character prefix rather than all 64. On a
# 90-student, 4-question, 28-lecture semester this removes roughly a third of
# the payload that gets rewritten on every single import.
RESPONSE_HEADERS = [
    "Date", "Question ID",
    "Student Key", "Student Name", "Poll Key", "Poll Participant",
    "Poll Email", "Screen Name",
    "Response", "Correct", "Timestamp", "File Hash", "Match Status",
]

LEGACY_RESPONSE_COLUMNS = ("Question", "Source File", "Student")

IMPORT_LOG_HEADERS = [
    "Import ID", "Date", "Source File", "File Hash",
    "Question ID", "Effective Responses", "Matched Students",
    "Unmatched / Non-student", "Imported At",
]

ROSTER_HEADERS = [
    "Student Key", "Student Name", "Canvas Name", "Section", "Login",
    "Active", "First Seen", "Roster Updated At", "Notes",
]

PARTICIPANT_MAP_HEADERS = [
    "Poll Key", "Poll Participant", "Poll Email", "Student Key", "Student Name",
    "Match Type", "Updated At", "Notes",
]

# The one tab the importer reads but never writes. The instructor records
# excused absences here, one row per student per day or per span of days, and
# the importer renders them into Attendance. Keeping the annotation in its own
# tab is what makes it survive: every other tab is rebuilt from scratch on each
# import.
#
# End Date is optional and comes last. The first three columns match the
# detail rows of Attendance Review, so rows can be pasted straight across.
EXCUSED_HEADERS = ["Date", "Student Key", "Student Name", "Reason", "End Date"]

# Seeded once, when the tab is created. A row whose Date cell starts with # is
# a comment, so the worked examples can sit there being useful without being
# reported as unresolvable rows on every run.
EXCUSED_SEED_ROWS = [
    ["# 2026-09-02", "canvas:1234567", "Jane Doe", "Dean's note", ""],
    ["# 2026-10-14", "canvas:1234567", "Jane Doe", "Athletics", "2026-10-19"],
    ["# Examples above. Remove the # to use a row. With an End Date, every class "
     "from Date through End Date is excused. Copy Date, Student Key and Student "
     "Name from the Attendance Review tab."],
]


# ---------------------------------------------------------------------------
# Parsed CSV records
# ---------------------------------------------------------------------------

@dataclass
class ParseWarning:
    """A recoverable problem found while reading a CSV.

    v2.3.1 raised on the first bad row and lost the whole file. Collecting
    warnings lets the importer report every problem at once, with row numbers,
    and still import the rows that are fine.
    """
    source: str
    row: Optional[int]
    message: str

    def __str__(self) -> str:
        where = f"row {self.row}" if self.row else "file"
        return f"{self.source} ({where}): {self.message}"


@dataclass
class ResponseRow:
    response: str
    via: str
    screen_name: str
    registered_participant: str
    created_at: str
    email: str = ""

    @property
    def student_name(self) -> str:
        """Raw Poll Everywhere participant label used for identity matching."""
        name = clean_space(self.registered_participant)
        if name:
            return name
        screen = clean_space(self.screen_name) or "Unknown participant"
        return f"[UNREGISTERED] {screen}"

    @property
    def poll_key(self) -> str:
        return poll_identity_key(self.student_name, self.email)

    @property
    def normalized_email(self) -> str:
        return normalize_email(self.email)


@dataclass
class OffDateParticipant:
    """A participant whose row in a lecture export started on another day.

    Poll Everywhere gives each participant one row per export and stamps it
    with the time of their first answer. Someone who answered a question opened
    during an earlier class carries that earlier time on every answer in the
    row, including answers given on the class date, so the row cannot date
    itself. It is held back until the TA decides whether it counts.
    """
    name: str
    email: str
    started_at: str                              # ISO, course timezone
    responses: Dict[int, ResponseRow]            # question order -> answer

    @property
    def started_date(self) -> str:
        return self.started_at[:10]

    @property
    def orders(self) -> List[int]:
        return sorted(self.responses)


@dataclass
class PollFile:
    path: Path
    question_name: str
    class_date: str
    summary: List[Tuple[str, int]]
    responses: List[ResponseRow]
    file_hash: str
    correct_answers: Optional[Set[str]] = None
    question_order: int = 1
    source_format: str = "single-question"
    timezone_label: str = ""
    warnings: List[ParseWarning] = field(default_factory=list)
    # Participants of this export who started on another day. Every question
    # parsed from the same file shares one list.
    off_date: List[OffDateParticipant] = field(default_factory=list)

    @property
    def question_id(self) -> str:
        return f"{self.class_date}::{self.question_name}"

    @property
    def scored(self) -> bool:
        return self.correct_answers is not None

    @property
    def first_response_at(self) -> str:
        stamps = [clean_space(r.created_at) for r in self.responses if clean_space(r.created_at)]
        if not stamps:
            return ""
        return min(stamps, key=timestamp_sort_key)

    def response_datetimes(self) -> List[datetime]:
        out: List[datetime] = []
        for r in self.responses:
            text = clean_space(r.created_at)
            if not text:
                continue
            try:
                out.append(datetime.fromisoformat(text))
            except ValueError:
                continue
        return out

    def answer_counts(self) -> List[Tuple[str, int]]:
        counts: Dict[str, int] = {}
        for r in self.responses:
            answer = normalize_answer_text(r.response)
            if answer:
                counts[answer] = counts.get(answer, 0) + 1
        return sorted(counts.items(), key=lambda x: (-x[1], x[0].casefold()))


@dataclass
class CanvasStudent:
    student_key: str
    student_name: str
    canvas_name: str
    section: str
    login: str = ""

    @property
    def normalized_name(self) -> str:
        return normalize_person_name(self.student_name)


def effective_poll_responses(poll: PollFile) -> List[ResponseRow]:
    """Keep the latest response per Poll identity within one question.

    Deduplication is by ``poll_key``, so the same person answering from two
    devices under one e-mail collapses, while two different students who share
    a display name do not.
    """
    latest: Dict[str, ResponseRow] = {}
    for row in poll.responses:
        key = row.poll_key or normalize_person_name(row.student_name)
        old = latest.get(key)
        if old is None or timestamp_sort_key(row.created_at) >= timestamp_sort_key(old.created_at):
            latest[key] = row
    return sorted(latest.values(), key=lambda r: (r.student_name.casefold(), r.created_at))


def unique_poll_identities(polls: Sequence[PollFile]) -> Dict[str, ResponseRow]:
    """Map poll_key -> a representative response row across a batch."""
    out: Dict[str, ResponseRow] = {}
    for poll in polls:
        for row in effective_poll_responses(poll):
            key = row.poll_key
            if key and key not in out:
                out[key] = row
    return out
