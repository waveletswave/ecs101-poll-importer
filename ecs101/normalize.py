"""Text, name, answer, e-mail and timestamp normalization.

Every normalization rule in the project lives here. v2.3.1 applied different
cleaning steps to the same logical value in different code paths, which silently
scored a whole class zero (see tests/test_regressions.py::test_legacy_summary_
and_individual_answers_use_the_same_normalization). Keeping one implementation
per concept is the fix.
"""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = [
    "clean_space",
    "strip_diacritics",
    "normalize_person_name",
    "normalize_answer_text",
    "normalize_email",
    "email_local_part",
    "name_from_email_local_part",
    "canvas_student_key",
    "canvas_display_name",
    "poll_identity_key",
    "sha256_file",
    "short_hash",
    "now_iso",
    "parse_timestamp",
    "parse_calendar_date",
    "timestamp_sort_key",
    "tz_from_header_label",
    "offset_from_tz_label",
    "DEFAULT_COURSE_TZ",
]

DEFAULT_COURSE_TZ = "America/New_York"


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------

def clean_space(text: object) -> str:
    """Collapse all whitespace runs (including NBSP) and trim."""
    return re.sub(r"\s+", " ", str(text or "").strip())


def strip_diacritics(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def normalize_person_name(name: object) -> str:
    """Conservative normalized name for exact matching and map lookup.

    Diacritics are stripped so 'Damian' and 'Damian' with an accent collapse,
    and every non-alphanumeric run becomes a single space so a display name
    carrying a nickname in parentheses agrees with the same name without it.
    """
    text = strip_diacritics(clean_space(name)).casefold()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def normalize_answer_text(value: object) -> str:
    """The ONE way an answer string is cleaned, wherever it comes from.

    HTML entities are decoded first, then whitespace is collapsed. Doing it in
    this order matters: '&nbsp;' must become a space before the collapse, not
    after. Poll Everywhere's summary block and its individual-results block
    escape text differently, so both must arrive here before being compared.
    """
    text = html.unescape(str(value or ""))
    text = clean_space(text)
    # Poll Everywhere sometimes wraps a summary value in literal double quotes
    # that are not CSV quoting (they survive as characters after unescaping).
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = clean_space(text[1:-1])
    return text


# ---------------------------------------------------------------------------
# E-mail
# ---------------------------------------------------------------------------

def normalize_email(value: object) -> str:
    """Lower-cased, trimmed e-mail. Returns '' when the value is not an e-mail."""
    text = clean_space(value).casefold()
    if not text or "@" not in text:
        return ""
    if text.count("@") != 1:
        return ""
    local, _, domain = text.partition("@")
    if not local or "." not in domain:
        return ""
    return text


def email_local_part(email: str) -> str:
    return normalize_email(email).partition("@")[0]


def name_from_email_local_part(email: str) -> str:
    """Reconstruct a normalized person name from a 'first.last@' style address.

    Returns '' for addresses that do not look like a name, so a NetID such as
    'iv517@example.edu' or 'r.okafor@example.edu' never fabricates a match. About half
    of the ECS101 participants use the first.last form, and for those this gives
    an exact, human-free match against the Canvas roster.
    """
    local = email_local_part(email)
    if not local or "." not in local:
        return ""
    parts = [p for p in local.split(".") if p]
    if len(parts) < 2:
        return ""
    # Any part that is not purely alphabetic (sb904) or is a single letter
    # (a.arora) means this is a NetID, not a name.
    for part in parts:
        if not part.isalpha() or len(part) < 2:
            return ""
    return normalize_person_name(" ".join(parts))


# ---------------------------------------------------------------------------
# Identity keys
# ---------------------------------------------------------------------------

def canvas_student_key(canvas_id: object) -> str:
    return f"canvas:{clean_space(canvas_id)}"


def canvas_display_name(canvas_name: object) -> str:
    """Convert Canvas 'Last, First Middle' to 'First Middle Last'."""
    raw = clean_space(canvas_name)
    if "," not in raw:
        return raw
    last, given = raw.split(",", 1)
    return clean_space(f"{given} {last}")


def poll_identity_key(poll_name: object, email: object = "") -> str:
    """Stable key for one Poll Everywhere identity.

    E-mail wins when present: a student who edits their Poll Everywhere display
    name between lectures keeps the same key, so their saved mapping and their
    history follow them instead of becoming a fresh unresolved identity.
    """
    normalized = normalize_email(email)
    if normalized:
        return f"email:{normalized}"
    name = normalize_person_name(poll_name)
    return f"name:{name}" if name else ""


# ---------------------------------------------------------------------------
# Hashes and clocks
# ---------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def short_hash(file_hash: object, length: int = 12) -> str:
    return clean_space(file_hash)[:length]


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------

_NAIVE_FORMATS = (
    "%m/%d/%y %H:%M:%S", "%m/%d/%y %H:%M",
    "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M",
    "%m/%d/%y %I:%M:%S %p", "%m/%d/%y %I:%M %p",
    "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p",
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
)

# Poll Everywhere writes the export timezone into the column header, e.g.
# 'Started At (CDT)'. Mapping the abbreviation to a fixed UTC offset is correct
# because the abbreviation already encodes whether DST was in effect.
_TZ_LABEL_OFFSETS = {
    "UTC": 0, "GMT": 0, "Z": 0,
    "EST": -5, "EDT": -4,
    "CST": -6, "CDT": -5,
    "MST": -7, "MDT": -6,
    "PST": -8, "PDT": -7,
    "AKST": -9, "AKDT": -8,
    "HST": -10,
}


def offset_from_tz_label(label: object) -> Optional[timedelta]:
    """UTC offset for a timezone abbreviation such as 'CDT'. None if unknown."""
    key = clean_space(label).upper()
    if not key:
        return None
    if key in _TZ_LABEL_OFFSETS:
        return timedelta(hours=_TZ_LABEL_OFFSETS[key])
    match = re.fullmatch(r"UTC([+-])(\d{1,2})(?::?(\d{2}))?", key)
    if match:
        sign = -1 if match.group(1) == "-" else 1
        hours = int(match.group(2))
        minutes = int(match.group(3) or 0)
        return sign * timedelta(hours=hours, minutes=minutes)
    return None


def tz_from_header_label(header: str) -> Tuple[Optional[str], Optional[timedelta]]:
    """Extract a timezone label and its offset from a 'Started At (CDT)' header.

    Returns (label, offset) or (None, None) when the header carries no label.
    An unknown label returns (label, None) so the caller can warn rather than
    silently guess.
    """
    match = re.search(r"\(([^)]+)\)", str(header or ""))
    if not match:
        return None, None
    label = clean_space(match.group(1)).upper()
    if label in _TZ_LABEL_OFFSETS:
        return label, timedelta(hours=_TZ_LABEL_OFFSETS[label])
    return label, None


def parse_timestamp(
    raw: object,
    source_offset: Optional[timedelta] = None,
    course_tz: str = DEFAULT_COURSE_TZ,
) -> datetime:
    """Parse a Poll Everywhere timestamp into an aware datetime in course_tz.

    ``source_offset`` is the UTC offset the CSV's own clock is expressed in,
    normally derived from the column header label. When it is None the value is
    assumed to already be in the course timezone, which reproduces the v2.3.1
    behaviour rather than inventing a shift.

    Raises ValueError with the offending text so callers can report the row.
    """
    value = clean_space(raw)
    if not value:
        raise ValueError("empty timestamp")

    try:
        zone = ZoneInfo(course_tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown course timezone {course_tz!r}") from exc

    parsed: Optional[datetime] = None
    for fmt in _NAIVE_FORMATS:
        try:
            parsed = datetime.strptime(value, fmt)
            break
        except ValueError:
            continue

    if parsed is None:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"unrecognized timestamp: {value!r}") from exc

    if parsed.tzinfo is not None:
        return parsed.astimezone(zone)

    if source_offset is None:
        return parsed.replace(tzinfo=zone)

    from datetime import timezone as _timezone
    return parsed.replace(tzinfo=_timezone(source_offset)).astimezone(zone)


def timestamp_sort_key(value: object) -> Tuple[int, float, str]:
    """Order timestamps by real instant, falling back to text.

    Comparing ISO strings breaks as soon as two rows carry different formats,
    which happens whenever a legacy export ('2026-08-24 12:39:10') and a
    lecture-wide export ('2026-08-26T13:27:00-04:00') share a sheet. Parsed
    instants sort before unparseable text so a real timestamp always wins.
    """
    text = clean_space(value)
    if not text:
        return (0, 0.0, "")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        for fmt in _NAIVE_FORMATS:
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return (1, 0.0, text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(DEFAULT_COURSE_TZ))
    return (2, dt.timestamp(), text)


def parse_clock(value: object, field: str) -> time:
    """Parse an 'HH:MM' course-schedule value from config."""
    text = clean_space(value)
    for fmt in ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M%p"):
        try:
            return datetime.strptime(text.upper(), fmt).time()
        except ValueError:
            continue
    raise ValueError(f"{field}: expected HH:MM, got {text!r}")


# ---------------------------------------------------------------------------
# Hand-typed calendar dates
# ---------------------------------------------------------------------------

_WRITTEN_DATE_FORMATS = ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y")


def parse_calendar_date(value: object) -> Optional[str]:
    """Read a hand-typed calendar date and return it as YYYY-MM-DD.

    The Excused tab is typed by people, and Google Sheets may reformat what
    they type, so 2026-11-2, 2026/11/02, 11/2/2026, 11/2/26 and Nov 2, 2026 must
    all mean the same day. Slashed dates are read month first, as at a US
    university. Returns None for anything that is not a real date.
    """
    text = clean_space(value)
    if not text:
        return None
    iso = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text)
    us = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})", text)
    if iso:
        year, month, day = (int(g) for g in iso.groups())
    elif us:
        month, day, year = (int(g) for g in us.groups())
        year += 2000 if year < 100 else 0
    else:
        for fmt in _WRITTEN_DATE_FORMATS:
            try:
                return datetime.strptime(text, fmt).date().isoformat()
            except ValueError:
                continue
        return None
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None
