"""Canvas roster and Poll Everywhere CSV parsing.

Two Poll Everywhere export shapes are supported:

* ``lecture-wide``   one CSV per lecture, one column per question. This is the
  current format and the one every future ECS101 lecture will use.
* ``single-question``  the older summary + Individual Results layout used for
  Lecture 1. Kept working, and its answer text now goes through exactly the
  same normalization as the lecture-wide path.
"""

from __future__ import annotations

import csv
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .models import CanvasStudent, OffDateParticipant, ParseWarning, PollFile, ResponseRow
from .normalize import (
    DEFAULT_COURSE_TZ,
    canvas_display_name,
    canvas_student_key,
    clean_space,
    normalize_answer_text,
    normalize_email,
    normalize_person_name,
    offset_from_tz_label,
    parse_timestamp,
    sha256_file,
    tz_from_header_label,
)

__all__ = [
    "parse_canvas_roster",
    "parse_poll_everywhere_export",
    "looks_like_lecture_export",
    "looks_like_poll_export",
    "lecture_question_columns",
    "suspicious_question_columns",
    "check_class_window",
    "CANVAS_LOGIN_COLUMNS",
]


# ---------------------------------------------------------------------------
# Canvas roster
# ---------------------------------------------------------------------------

# Tried in order. Whichever exists in the export is used as the student's login
# alias, which lets a Poll Everywhere e-mail match a Canvas student exactly.
# A purely numeric value (SIS User ID at most institutions) is stored but never
# used for e-mail matching, because it cannot collide with an address.
CANVAS_LOGIN_COLUMNS = (
    "SIS Login ID", "Login ID", "Email", "Primary Email",
    "SIS User ID", "Integration ID",
)

_CANVAS_SKIP_NAMES = {"points possible", "test student", "student, test"}


def parse_canvas_roster(path: Path) -> Tuple[List[CanvasStudent], List[ParseWarning]]:
    """Read roster-identifying fields from a Canvas gradebook export.

    Assignment and grade columns are ignored on purpose. Returns the students
    plus any warnings, so a duplicate or an unusable row is reported instead of
    being silently dropped.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Canvas roster file not found: {path}")

    warnings: List[ParseWarning] = []
    students: List[CanvasStudent] = []
    seen_keys: Dict[str, str] = {}

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = [clean_space(h) for h in (reader.fieldnames or [])]
        missing = {"Student", "ID"} - set(fieldnames)
        if missing:
            raise ValueError(
                f"{path.name}: missing Canvas column(s): {', '.join(sorted(missing))}"
            )

        login_columns = [c for c in CANVAS_LOGIN_COLUMNS if c in fieldnames]
        if not login_columns:
            warnings.append(ParseWarning(
                path.name, None,
                "no login/e-mail column found (looked for "
                f"{', '.join(CANVAS_LOGIN_COLUMNS)}); participant matching will "
                "fall back to names only",
            ))

        for line_no, row in enumerate(reader, start=2):
            raw_canvas_name = clean_space(row.get("Student"))
            canvas_id = clean_space(row.get("ID"))
            section = clean_space(row.get("Section"))

            if not raw_canvas_name:
                continue
            if normalize_person_name(raw_canvas_name) in _CANVAS_SKIP_NAMES:
                continue

            display = canvas_display_name(raw_canvas_name)
            if normalize_person_name(display) in _CANVAS_SKIP_NAMES:
                continue

            if not canvas_id:
                warnings.append(ParseWarning(
                    path.name, line_no,
                    f"{display!r} has no Canvas ID and was skipped",
                ))
                continue

            logins = []
            for column in login_columns:
                value = clean_space(row.get(column))
                if value and value not in logins:
                    logins.append(value)

            key = canvas_student_key(canvas_id)
            if key in seen_keys:
                warnings.append(ParseWarning(
                    path.name, line_no,
                    f"duplicate Canvas ID {canvas_id} ({display!r} and "
                    f"{seen_keys[key]!r}); keeping the first",
                ))
                continue
            seen_keys[key] = display

            students.append(CanvasStudent(
                student_key=key,
                student_name=display,
                canvas_name=raw_canvas_name,
                section=section,
                login=" | ".join(logins),
            ))

    if not students:
        raise ValueError(f"No student roster rows found in {path.name}.")

    students.sort(key=lambda s: s.student_name.casefold())

    # A normalized-name collision means exact-name matching cannot be trusted
    # for those two students. Surface it rather than letting the matcher quietly
    # refuse to auto-match them every week.
    by_name: Dict[str, List[str]] = {}
    for s in students:
        by_name.setdefault(s.normalized_name, []).append(s.student_name)
    for norm, names in by_name.items():
        if len(names) > 1:
            warnings.append(ParseWarning(
                path.name, None,
                f"{len(names)} students share the normalized name {norm!r} "
                f"({', '.join(names)}); these always need human review",
            ))

    return students, warnings


# ---------------------------------------------------------------------------
# Poll Everywhere: shared column knowledge
# ---------------------------------------------------------------------------

_LEGACY_SUMMARY_HEADER = "Response,Count"
_LEGACY_INDIVIDUAL_HEADER = "Response,Via,Screen name,Registered participant,Created At"

_METADATA_COLUMNS = {
    "response #", "response number", "participant first name",
    "participant last name", "first name", "last name", "name",
    "email", "e-mail", "participant email", "custom report id",
    "screen name", "public id", "participant id", "registration id",
    "device", "session id", "total responses",
}

_METADATA_PREFIXES = (
    "started at", "created at", "completed at", "submitted at", "ended at",
    "response time", "time spent", "duration",
)


def _is_metadata_column(header: str) -> bool:
    name = clean_space(header).casefold()
    if not name:
        return True
    if name in _METADATA_COLUMNS:
        return True
    return any(name.startswith(prefix) for prefix in _METADATA_PREFIXES)


def looks_like_lecture_export(path: Path) -> bool:
    """Detect Poll Everywhere's one-lecture-per-CSV response export."""
    with Path(path).open("r", encoding="utf-8-sig", newline="") as f:
        header = next(csv.reader(f), [])
    normalized = {clean_space(h) for h in header}
    lowered = {h.casefold() for h in normalized}
    return (
        "response #" in lowered
        and "participant first name" in lowered
        and "participant last name" in lowered
        and any(h.casefold().startswith("started at") for h in normalized)
    )


def looks_like_poll_export(path: Path) -> bool:
    """True when the file is either supported Poll Everywhere export shape.

    Used to skip unrelated CSVs when a whole folder is scanned. A Canvas roster
    sitting next to the lecture exports used to abort the run with a message
    about a missing 'Response,Count' section.
    """
    path = Path(path)
    try:
        if looks_like_lecture_export(path):
            return True
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            for _ in range(200):
                line = f.readline()
                if not line:
                    break
                if line.strip().startswith(_LEGACY_INDIVIDUAL_HEADER):
                    return True
    except (OSError, UnicodeDecodeError):
        return False
    return False


def _question_positions(headers: Sequence[str]) -> List[int]:
    """Positions of the question columns, in exported order."""
    clean_headers = [clean_space(h) for h in headers]
    lowered = [h.casefold() for h in clean_headers]
    first = lowered.index("public id") + 1 if "public id" in lowered else 0

    picked = [
        i for i in range(first, len(clean_headers))
        if not _is_metadata_column(clean_headers[i])
    ]
    if picked:
        return picked

    # Fall back to scanning the whole header when the slice yielded nothing.
    return [i for i, h in enumerate(clean_headers) if not _is_metadata_column(h)]


def lecture_question_columns(headers: Sequence[str]) -> List[str]:
    """Return question columns in the exact order Poll Everywhere exported them.

    v2.3.1 treated *every* column after 'Public ID' as a question. Any new
    metadata column Poll Everywhere adds there became question 1, which is the
    column the importer proposes as that day's scored question. The metadata
    filter now applies to the slice as well, so an unrecognised metadata column
    has to get past the name filter *and* the suspicious-column check before it
    can be scored.
    """
    clean_headers = [clean_space(h) for h in headers]
    return [clean_headers[i] for i in _question_positions(headers)]


def _distinct_titles(titles: Sequence[str]) -> List[str]:
    """Number repeated question titles so that each question keeps its own ID.

    A lecture can ask several different questions under one title, such as
    three photos each captioned "What type of rock is this?". The first keeps
    the title; the repeats become "... (2)", "... (3)".
    """
    seen: Dict[str, int] = {}
    distinct: List[str] = []
    for title in titles:
        seen[title] = seen.get(title, 0) + 1
        distinct.append(title if seen[title] == 1 else f"{title} ({seen[title]})")
    return distinct


def suspicious_question_columns(
    rows: Sequence[Dict[str, str]],
    question_columns: Sequence[str],
    distinct_ratio: float = 0.85,
    min_distinct: int = 8,
) -> Dict[str, str]:
    """Flag question columns that do not look like multiple-choice questions.

    A choice question has few distinct answers shared by many people. A
    timestamp, an identifier or a response-time measurement has almost as many
    distinct values as it has rows. Free-response poll questions look the same
    way, so this only warns; it never drops a column.
    """
    flagged: Dict[str, str] = {}
    for column in question_columns:
        values = [normalize_answer_text(r.get(column)) for r in rows]
        values = [v for v in values if v]
        if len(values) < min_distinct:
            continue
        distinct = len(set(values))
        if distinct >= min_distinct and distinct / len(values) >= distinct_ratio:
            flagged[column] = (
                f"{distinct} distinct values across {len(values)} answers "
                "(looks like free text, an identifier or a measurement rather "
                "than a multiple-choice question)"
            )
    return flagged


# ---------------------------------------------------------------------------
# Poll Everywhere: lecture-wide export
# ---------------------------------------------------------------------------

def _parse_lecture_export(
    path: Path,
    course_tz: str,
    default_source_tz: str = "",
) -> Tuple[List[PollFile], List[ParseWarning]]:
    path = Path(path)
    warnings: List[ParseWarning] = []

    # Read by position, not by header name. Two questions can share a title,
    # and a dict keyed by title keeps only the last of them: every earlier
    # question with that title silently took the last one's answers.
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        table = list(csv.reader(f))
    if not table:
        raise ValueError(f"{path.name}: the file is empty.")
    raw_headers = table[0]
    headers = [clean_space(h) for h in raw_headers]

    positions = _question_positions(headers)
    if not positions:
        raise ValueError(f"{path.name}: no question columns were detected.")
    titles = [headers[i] for i in positions]
    question_names = _distinct_titles(titles)
    for title in sorted({t for t in titles if titles.count(t) > 1}):
        warnings.append(ParseWarning(
            path.name, None,
            f"{titles.count(title)} questions share the title {title!r}; they are "
            f"kept apart as {title!r}, {title + ' (2)'!r} and so on",
        ))

    def column(name: str) -> Optional[int]:
        wanted = name.casefold()
        return next((i for i, h in enumerate(headers) if h.casefold() == wanted), None)

    started_index = next(
        (i for i, h in enumerate(headers) if h.casefold().startswith("started at")),
        None,
    )
    if started_index is None:
        raise ValueError(f"{path.name}: missing Started At column.")
    started_header = raw_headers[started_index]
    first_index = column("Participant First Name")
    last_index = column("Participant Last Name")
    email_index = column("Email")
    screen_index = column("Screen Name")

    def cell(row: Sequence[str], index: Optional[int]) -> str:
        return row[index] if index is not None and index < len(row) else ""

    tz_label, tz_offset = tz_from_header_label(started_header)
    if tz_label and tz_offset is None:
        warnings.append(ParseWarning(
            path.name, None,
            f"unrecognised timezone label {tz_label!r} in column "
            f"{started_header!r}; timestamps are being read as course-local time",
        ))
    if tz_offset is None and default_source_tz:
        # config's poll_export_timezone fills in for an export that does not
        # label its own clock. Lecture 1's legacy format never does.
        fallback = offset_from_tz_label(default_source_tz)
        if fallback is None:
            warnings.append(ParseWarning(
                path.name, None,
                f"poll_export_timezone {default_source_tz!r} is not recognised",
            ))
        else:
            tz_offset = fallback
            tz_label = tz_label or clean_space(default_source_tz).upper()
    elif tz_offset is None and not tz_label:
        warnings.append(ParseWarning(
            path.name, None,
            f"column {started_header!r} carries no timezone label; timestamps "
            "are being read as course-local time. Set poll_export_timezone in "
            "config.json if the export uses a different clock.",
        ))

    rows = [r for r in table[1:] if any(clean_space(v) for v in r)]
    if not rows:
        raise ValueError(f"{path.name}: no participant rows found.")

    # Parse every row's timestamp once, collecting failures instead of aborting.
    stamps: Dict[int, datetime] = {}
    for index, row in enumerate(rows):
        raw = cell(row, started_index)
        if not clean_space(raw):
            warnings.append(ParseWarning(path.name, index + 2, "blank Started At"))
            continue
        try:
            stamps[index] = parse_timestamp(raw, tz_offset, course_tz)
        except ValueError as exc:
            warnings.append(ParseWarning(path.name, index + 2, str(exc)))

    if not stamps:
        raise ValueError(f"{path.name}: no usable Started At values.")

    by_date: Dict[str, int] = {}
    for dt in stamps.values():
        key = dt.date().isoformat()
        by_date[key] = by_date.get(key, 0) + 1

    class_date = max(by_date.items(), key=lambda kv: (kv[1], kv[0]))[0]
    if len(by_date) > 1:
        detail = ", ".join(f"{d} ({n} rows)" for d, n in sorted(by_date.items()))
        warnings.append(ParseWarning(
            path.name, None,
            f"rows span more than one date: {detail}; using {class_date} and "
            "holding the other rows back for review",
        ))

    named_rows = [
        {name: cell(row, i) for name, i in zip(question_names, positions)} for row in rows
    ]
    for name, reason in suspicious_question_columns(named_rows, question_names).items():
        warnings.append(ParseWarning(
            path.name, None, f"column {name!r} may not be a question: {reason}"
        ))

    per_question: List[List[ResponseRow]] = [[] for _ in positions]
    off_date: List[OffDateParticipant] = []
    for index, row in enumerate(rows):
        dt = stamps.get(index)
        if dt is None:
            continue
        first = clean_space(cell(row, first_index))
        last = clean_space(cell(row, last_index))
        answers: Dict[int, ResponseRow] = {}
        for order, i in enumerate(positions, start=1):
            answer = normalize_answer_text(cell(row, i))
            if answer:
                answers[order] = ResponseRow(
                    response=answer,
                    via="",
                    screen_name=clean_space(cell(row, screen_index)),
                    registered_participant=clean_space(f"{first} {last}"),
                    created_at=dt.isoformat(timespec="seconds"),
                    email=normalize_email(cell(row, email_index)),
                )
        if not answers:
            continue
        if dt.date().isoformat() == class_date:
            for order, response in answers.items():
                per_question[order - 1].append(response)
        else:
            sample = next(iter(answers.values()))
            off_date.append(OffDateParticipant(
                name=sample.student_name,
                email=sample.normalized_email,
                started_at=sample.created_at,
                responses=answers,
            ))

    file_hash = sha256_file(path)
    polls: List[PollFile] = []
    for question_order, (question, responses) in enumerate(
        zip(question_names, per_question), start=1
    ):
        poll = PollFile(
            path=path,
            question_name=question,
            class_date=class_date,
            summary=[],
            responses=responses,
            file_hash=file_hash,
            question_order=question_order,
            source_format="lecture-wide",
            timezone_label=tz_label or "",
            off_date=off_date,
        )
        poll.summary = poll.answer_counts()
        polls.append(poll)

    return polls, warnings


# ---------------------------------------------------------------------------
# Poll Everywhere: legacy single-question export
# ---------------------------------------------------------------------------

def _find_line(lines: Sequence[str], prefix: str) -> int:
    for i, line in enumerate(lines):
        if line.strip().startswith(prefix):
            return i
    raise ValueError(f"Could not find expected section beginning with: {prefix!r}")


def _parse_single_question_export(
    path: Path,
    course_tz: str,
    default_source_tz: str = "",
) -> Tuple[PollFile, List[ParseWarning]]:
    path = Path(path)
    warnings: List[ParseWarning] = []
    lines = path.read_text(encoding="utf-8-sig").splitlines()

    # The legacy export carries no timezone label anywhere in the file, so the
    # only way to read its clock correctly is to be told.
    source_offset = offset_from_tz_label(default_source_tz) if default_source_tz else None
    if default_source_tz and source_offset is None:
        warnings.append(ParseWarning(
            path.name, None,
            f"poll_export_timezone {default_source_tz!r} is not recognised",
        ))

    summary_header = _find_line(lines, _LEGACY_SUMMARY_HEADER)
    individual_header = _find_line(lines, _LEGACY_INDIVIDUAL_HEADER)

    summary: List[Tuple[str, int]] = []
    for line in lines[summary_header + 1:individual_header]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == "Individual Results":
            break
        if "," not in stripped:
            continue
        response_raw, _, count_raw = stripped.rpartition(",")
        # normalize_answer_text is the SAME function the individual-results rows
        # go through. In v2.3.1 the two paths cleaned differently, so a summary
        # answer could never equal any student's answer and the whole class was
        # scored zero without a warning.
        response = normalize_answer_text(response_raw)
        if not response or response.casefold() == "total":
            continue
        try:
            count = int(clean_space(count_raw))
        except ValueError:
            count = 0
        summary.append((response, count))

    responses: List[ResponseRow] = []
    reader = csv.DictReader(lines[individual_header:])
    for index, row in enumerate(reader):
        if not any(clean_space(v) for v in row.values()):
            continue
        raw_stamp = row.get("Created At")
        created_at = ""
        if clean_space(raw_stamp):
            try:
                created_at = parse_timestamp(raw_stamp, source_offset, course_tz).isoformat(
                    timespec="seconds"
                )
            except ValueError as exc:
                warnings.append(ParseWarning(path.name, individual_header + index + 2, str(exc)))
                continue
        responses.append(ResponseRow(
            response=normalize_answer_text(row.get("Response")),
            via=clean_space(row.get("Via")),
            screen_name=clean_space(row.get("Screen name")),
            registered_participant=clean_space(row.get("Registered participant")),
            created_at=created_at,
            email=normalize_email(row.get("Email")),
        ))

    responses = [r for r in responses if r.response]
    if not responses:
        raise ValueError(f"No Individual Results rows found in {path.name}.")

    by_date: Dict[str, int] = {}
    for r in responses:
        if len(r.created_at) >= 10:
            key = r.created_at[:10]
            by_date[key] = by_date.get(key, 0) + 1
    if not by_date:
        raise ValueError(f"{path.name}: no usable Created At values.")

    class_date = max(by_date.items(), key=lambda kv: (kv[1], kv[0]))[0]
    if len(by_date) > 1:
        detail = ", ".join(f"{d} ({n} rows)" for d, n in sorted(by_date.items()))
        warnings.append(ParseWarning(
            path.name, None,
            f"rows span more than one date: {detail}; using {class_date}",
        ))
    responses = [r for r in responses if r.created_at[:10] == class_date]

    poll = PollFile(
        path=path,
        question_name=path.stem,
        class_date=class_date,
        summary=summary,
        responses=responses,
        file_hash=sha256_file(path),
        question_order=1,
        source_format="single-question",
        timezone_label=clean_space(default_source_tz).upper(),
    )

    # Cross-check the two blocks. Any answer present in one but not the other
    # means the export is inconsistent and scoring cannot be trusted.
    observed = {r.response for r in poll.responses}
    declared = {a for a, _ in summary}
    for missing in sorted(declared - observed):
        warnings.append(ParseWarning(
            path.name, None,
            f"summary lists {missing!r} but no individual response matches it",
        ))
    if not summary:
        poll.summary = poll.answer_counts()

    return poll, warnings


def parse_poll_everywhere_export(
    path: Path,
    course_tz: str = DEFAULT_COURSE_TZ,
    default_source_tz: str = "",
) -> Tuple[List[PollFile], List[ParseWarning]]:
    """Parse either supported Poll Everywhere export into questions.

    ``default_source_tz`` is config's ``poll_export_timezone``. It supplies the
    export's own clock when the file does not state it, which is always for the
    legacy format and would be for a lecture export whose header lost its label.
    """
    path = Path(path)
    if looks_like_lecture_export(path):
        return _parse_lecture_export(path, course_tz, default_source_tz)
    if not looks_like_poll_export(path):
        raise ValueError(
            f"{path.name} is not a Poll Everywhere export. A lecture export "
            "needs a 'Response #' / 'Started At' header row; the legacy export "
            "needs an 'Individual Results' section."
        )
    poll, warnings = _parse_single_question_export(path, course_tz, default_source_tz)
    return [poll], warnings


# ---------------------------------------------------------------------------
# Class-window validation
# ---------------------------------------------------------------------------

def check_class_window(
    polls: Sequence[PollFile],
    class_start: Optional[time],
    class_end: Optional[time],
    tolerance_minutes: int = 45,
) -> List[str]:
    """Confirm parsed response times land inside the scheduled class period.

    This is the guard that makes the timezone handling self-checking. If Poll
    Everywhere's account timezone changes, or a label is misread, the parsed
    times drift out of the class window and the importer says so instead of
    quietly filing the lecture under a shifted clock or a shifted date.
    """
    if class_start is None or class_end is None:
        return []

    problems: List[str] = []
    for poll in polls:
        stamps = poll.response_datetimes()
        if not stamps:
            continue
        stamps.sort()
        median = stamps[len(stamps) // 2]
        window_start = datetime.combine(median.date(), class_start, median.tzinfo)
        window_end = datetime.combine(median.date(), class_end, median.tzinfo)
        slack = timedelta(minutes=tolerance_minutes)
        if window_start - slack <= median <= window_end + slack:
            continue
        drift = median - window_start
        hours = drift.total_seconds() / 3600.0
        problems.append(
            f"{poll.path.name} / {poll.question_name!r}: median response time "
            f"{median.strftime('%Y-%m-%d %H:%M %Z')} is {hours:+.1f} h from the "
            f"scheduled start {class_start.strftime('%H:%M')}. Check "
            f"course_timezone and the Poll Everywhere export timezone "
            f"(label read: {poll.timezone_label or 'none'})."
        )
        break
    return problems
