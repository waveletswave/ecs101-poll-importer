"""Google Sheets access. This is the only module that imports gspread.

Writing changed shape in v3. v2.3.1 wrote each tab with resize + clear +
update + freeze + format, which is five API calls per tab and forty per import,
against a quota of sixty per minute, with no retry. Worse, ``clear()`` ran
before ``update()``: a failure between them left a canonical tab empty with no
backup, while the CLI printed "no data were written".

Here every tab is staged in memory and committed together: grow the grids,
write all values in one values.batchUpdate, then clear only the rows below the
new data. A canonical tab is never empty at any point, and the whole import
costs roughly four API calls.
"""

from __future__ import annotations

import csv
import json
import random
import time as _time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .models import (
    EXCUSED_HEADERS,
    EXCUSED_SEED_ROWS,
    IMPORT_LOG_HEADERS,
    PARTICIPANT_MAP_HEADERS,
    QUESTION_HEADERS,
    RESPONSE_HEADERS,
    ROSTER_HEADERS,
)
from .normalize import DEFAULT_COURSE_TZ, clean_space, parse_clock

__all__ = [
    "load_config",
    "get_google_client",
    "open_spreadsheet",
    "SheetIO",
    "WORKSHEET_TITLES",
    "CANONICAL_TABS",
    "with_retry",
]

WORKSHEET_TITLES = {
    "roster": "Roster",
    "participant_map": "Participant Map",
    "questions": "Questions",
    "responses": "Responses",
    "import_log": "Import Log",
    "attendance": "Attendance",
    "attendance_review": "Attendance Review",
    "scores": "Scores",
    "leaderboard": "Leaderboard",
    "excused": "Excused",
}

# Excused is the instructor's tab. The importer reads it and creates it once
# with a header row, but never stages it for writing again, so nothing entered
# there can be overwritten by a later import.
READ_ONLY_TABS = ("excused",)

_SEED_ON_CREATE = {
    "Excused": [list(EXCUSED_HEADERS), *(list(r) for r in EXCUSED_SEED_ROWS)],
}

CANONICAL_TABS = ("roster", "participant_map", "questions", "responses", "import_log")

_HEADERS_BY_TAB = {
    "roster": ROSTER_HEADERS,
    "participant_map": PARTICIPANT_MAP_HEADERS,
    "questions": QUESTION_HEADERS,
    "responses": RESPONSE_HEADERS,
    "import_log": IMPORT_LOG_HEADERS,
}

_RETRY_STATUS = {429, 500, 502, 503, 504}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_CONFIG_DEFAULTS = {
    "credentials_file": "credentials.json",
    "authorized_user_file": "authorized_user.json",
    "course_timezone": DEFAULT_COURSE_TZ,
    "poll_export_timezone": "",
    "class_start": "",
    "class_end": "",
    "backup_dir": "backups",
}

_KNOWN_KEYS = set(_CONFIG_DEFAULTS) | {"spreadsheet_id"}


def load_config(path: Path) -> Dict[str, str]:
    """Read and validate config.json, with messages that name the real problem."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}\n"
            "Copy config.example.json to config.json and set spreadsheet_id."
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{path.name}: expected a JSON object at the top level.")

    config = dict(_CONFIG_DEFAULTS)
    for key, value in raw.items():
        key = clean_space(key)
        if key not in _KNOWN_KEYS:
            known = ", ".join(sorted(_KNOWN_KEYS))
            raise ValueError(
                f"{path.name}: unknown setting {key!r}. Known settings: {known}"
            )
        config[key] = value if isinstance(value, str) else str(value)

    spreadsheet_id = clean_space(config.get("spreadsheet_id"))
    if not spreadsheet_id or spreadsheet_id == "PASTE_GOOGLE_SHEET_ID_HERE":
        raise ValueError(
            f"{path.name}: set spreadsheet_id to the shared Google Sheet's ID "
            "(the long token in its URL between /d/ and /edit)."
        )
    config["spreadsheet_id"] = spreadsheet_id

    # Validate the schedule window here so a typo surfaces before any API call.
    for field in ("class_start", "class_end"):
        if clean_space(config.get(field)):
            parse_clock(config[field], f"{path.name}: {field}")
    if bool(clean_space(config.get("class_start"))) != bool(clean_space(config.get("class_end"))):
        raise ValueError(
            f"{path.name}: set class_start and class_end together, or neither."
        )

    return config


def class_window(config: Dict[str, str]):
    start = clean_space(config.get("class_start"))
    end = clean_space(config.get("class_end"))
    if not start or not end:
        return None, None
    return parse_clock(start, "class_start"), parse_clock(end, "class_end")


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

def with_retry(fn: Callable, *args, attempts: int = 5, base_delay: float = 1.5, **kwargs):
    """Call a Sheets API function, backing off on quota and transient errors.

    Google's per-user write quota is sixty requests a minute. A busy import that
    lands on the limit used to abort mid-write; now it waits and finishes.
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - gspread wraps everything in APIError
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status not in _RETRY_STATUS or attempt == attempts - 1:
                raise
            last_exc = exc
            delay = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
            print(
                f"  Google Sheets returned {status}; retrying in {delay:.1f}s "
                f"(attempt {attempt + 2}/{attempts})."
            )
            _time.sleep(delay)
    if last_exc:
        raise last_exc


def get_google_client(config: Dict[str, str]):
    try:
        import gspread
    except ImportError as exc:
        raise RuntimeError(
            "gspread is not installed. Run: python -m pip install -r requirements.txt"
        ) from exc

    token_file = config.get("authorized_user_file", "authorized_user.json")
    try:
        return gspread.oauth(
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
            credentials_filename=config.get("credentials_file", "credentials.json"),
            authorized_user_filename=token_file,
        )
    except Exception as exc:
        if not _is_expired_token(exc):
            raise
        raise RuntimeError(
            "The saved Google authorization has expired or been revoked.\n"
            f"Delete {token_file} and run the command again; a browser will "
            "open to reauthorize.\n"
            "\n"
            "If this keeps happening every week: an OAuth consent screen set "
            "to External with a publishing status of Testing issues refresh "
            "tokens that expire after seven days. Publishing the app, or "
            "switching it to Internal if the project sits in a Google "
            "Workspace organization, removes that limit."
        ) from exc


def _is_expired_token(exc: BaseException) -> bool:
    """Recognise google-auth's refresh failure without importing it eagerly."""
    if exc.__class__.__name__ == "RefreshError":
        return True
    text = str(exc).casefold()
    return "invalid_grant" in text or "token has been expired or revoked" in text


def open_spreadsheet(config: Dict[str, str]):
    gc = get_google_client(config)
    return with_retry(gc.open_by_key, clean_space(config["spreadsheet_id"]))


# ---------------------------------------------------------------------------
# Batched reader / writer
# ---------------------------------------------------------------------------

class SheetIO:
    """Reads every tab in one call, stages writes, commits them together."""

    def __init__(self, spreadsheet):
        self.sh = spreadsheet
        self._values: Dict[str, List[List[str]]] = {}
        self._grid: Dict[str, Tuple[int, int]] = {}
        self._sheet_ids: Dict[str, int] = {}
        self._staged: Dict[str, List[List[str]]] = {}
        self._new_titles: List[str] = []
        self.write_started = False
        self._load_metadata()

    # -- reading ----------------------------------------------------------

    def _load_metadata(self) -> None:
        meta = with_retry(self.sh.fetch_sheet_metadata)
        for sheet in meta.get("sheets", []):
            props = sheet.get("properties", {})
            title = props.get("title", "")
            grid = props.get("gridProperties", {}) or {}
            self._sheet_ids[title] = props.get("sheetId")
            self._grid[title] = (
                int(grid.get("rowCount", 0) or 0),
                int(grid.get("columnCount", 0) or 0),
            )

    def ensure_worksheets(self, titles: Sequence[str] = tuple(WORKSHEET_TITLES.values())) -> None:
        """Create any missing tab. Uses the real gspread exception class."""
        from gspread.exceptions import WorksheetNotFound

        for title in titles:
            if title in self._sheet_ids:
                continue
            try:
                ws = with_retry(self.sh.add_worksheet, title=title, rows=200, cols=26)
            except WorksheetNotFound:  # pragma: no cover - defensive
                raise
            self._sheet_ids[title] = ws.id
            self._grid[title] = (200, 26)
            self._new_titles.append(title)
            seed = _SEED_ON_CREATE.get(title)
            if seed is not None:
                self.stage(title, seed)

    def read_all(self, titles: Sequence[str]) -> Dict[str, List[List[str]]]:
        """Fetch several tabs in a single values.batchGet call."""
        wanted = [t for t in titles if t in self._sheet_ids]
        if not wanted:
            return {}
        ranges = [_quote(t) for t in wanted]
        result = with_retry(self.sh.values_batch_get, ranges)
        for title, block in zip(wanted, result.get("valueRanges", [])):
            self._values[title] = [list(map(str, r)) for r in block.get("values", [])]
        for title in wanted:
            self._values.setdefault(title, [])
        return {t: self._values[t] for t in wanted}

    def records(self, title: str) -> List[Dict[str, str]]:
        values = self._values.get(title, [])
        if not values:
            return []
        headers = [clean_space(h) for h in values[0]]
        if not any(headers):
            return []
        out: List[Dict[str, str]] = []
        for row in values[1:]:
            padded = list(row) + [""] * (len(headers) - len(row))
            if not any(clean_space(cell) for cell in padded):
                continue
            out.append({
                headers[i]: padded[i]
                for i in range(len(headers))
                if headers[i]
            })
        return out

    # -- writing ----------------------------------------------------------

    def stage(self, title: str, matrix: Sequence[Sequence[str]]) -> None:
        """Queue a tab's contents, padded to one rectangle.

        Padding is not cosmetic. values.update only touches the cells it is
        given, so a short row leaves whatever sat to its right untouched and an
        empty row is skipped entirely. A ragged matrix therefore writes the new
        content over the old one column by column and leaves stale text behind:
        Attendance Review's section headers ended up sharing their rows with
        leftovers from the previous import, and its blank separator rows never
        appeared at all. Writing a full rectangle overwrites every cell in the
        used range, which is what ws.clear() used to guarantee.
        """
        rows = [[("" if c is None else str(c)) for c in row] for row in (matrix or [[""]])]
        width = max((len(r) for r in rows), default=1) or 1
        self._staged[title] = [r + [""] * (width - len(r)) for r in rows]

    def backup(self, directory: Path, tabs: Sequence[str]) -> Optional[Path]:
        """Write the tabs as they were read to timestamped local CSVs.

        Cheap insurance taken before anything is written back. The Responses tab
        has no upstream other than the original Poll Everywhere exports, so a
        local copy is the difference between an inconvenience and re-importing
        the whole semester.
        """
        present = [t for t in tabs if self._values.get(t)]
        if not present:
            return None
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        out = Path(directory) / stamp
        out.mkdir(parents=True, exist_ok=True)
        for title in present:
            safe = title.replace(" ", "_").lower()
            with (out / f"{safe}.csv").open("w", encoding="utf-8-sig", newline="") as f:
                csv.writer(f).writerows(self._values[title])
        return out

    def commit(self) -> Dict[str, int]:
        """Grow grids, write every staged tab, then clear the stale tail.

        Returns a small call-count summary so the CLI can report it.
        """
        if not self._staged:
            return {"api_calls": 0, "tabs": 0}

        calls = 0
        requests = []
        for title, matrix in self._staged.items():
            sheet_id = self._sheet_ids.get(title)
            if sheet_id is None:
                continue
            have_rows, have_cols = self._grid.get(title, (0, 0))
            need_rows = max(len(matrix) + 20, 100)
            need_cols = max(max((len(r) for r in matrix), default=1) + 2, 12)
            if need_rows > have_rows or need_cols > have_cols:
                requests.append({
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": sheet_id,
                            "gridProperties": {
                                "rowCount": max(need_rows, have_rows),
                                "columnCount": max(need_cols, have_cols),
                                "frozenRowCount": 1,
                            },
                        },
                        "fields": (
                            "gridProperties.rowCount,gridProperties.columnCount,"
                            "gridProperties.frozenRowCount"
                        ),
                    }
                })
                self._grid[title] = (max(need_rows, have_rows), max(need_cols, have_cols))

        for title in self._new_titles:
            sheet_id = self._sheet_ids.get(title)
            if sheet_id is None:
                continue
            requests.append({
                "repeatCell": {
                    "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                    "cell": {"userEnteredFormat": {
                        "textFormat": {"bold": True},
                        "horizontalAlignment": "CENTER",
                    }},
                    "fields": "userEnteredFormat(textFormat,horizontalAlignment)",
                }
            })

        if requests:
            with_retry(self.sh.batch_update, {"requests": requests})
            calls += 1

        # Values are written before anything is cleared, so a failure here
        # leaves the previous contents in place rather than an empty tab.
        self.write_started = True
        data = [
            {"range": f"{_quote(title)}!A1", "values": matrix}
            for title, matrix in self._staged.items()
            if title in self._sheet_ids
        ]
        with_retry(self.sh.values_batch_update, {
            "valueInputOption": "RAW",
            "data": data,
        })
        calls += 1

        # RAW keeps a student named "+Ana" or an answer starting with "=" as
        # text rather than a formula. Do not switch this to USER_ENTERED.

        # Anything outside the rectangle just written is left over from a
        # previous, larger import: the rows below it and the columns to its
        # right. Both are cleared, and only after the new values are in place.
        stale_ranges = []
        for title, matrix in self._staged.items():
            if title not in self._sheet_ids:
                continue
            have_rows, have_cols = self._grid.get(title, (0, 0))
            width = max((len(r) for r in matrix), default=1) or 1
            last_col = _column_letter(have_cols) if have_cols else "ZZ"

            first_stale_row = len(matrix) + 1
            if have_rows >= first_stale_row:
                stale_ranges.append(
                    f"{_quote(title)}!A{first_stale_row}:{last_col}{have_rows}"
                )
            if have_cols > width:
                first_stale_col = _column_letter(width + 1)
                stale_ranges.append(
                    f"{_quote(title)}!{first_stale_col}1:{last_col}{max(have_rows, len(matrix))}"
                )
        if stale_ranges:
            with_retry(self.sh.values_batch_clear, {"ranges": stale_ranges})
            calls += 1

        tabs = len(self._staged)
        self._staged = {}
        self._new_titles = []
        return {"api_calls": calls, "tabs": tabs}


def _quote(title: str) -> str:
    return f"'{title}'" if any(c in title for c in " '!") else title


def _column_letter(index: int) -> str:
    """1 -> A, 26 -> Z, 27 -> AA. Used to bound the stale-cell clear ranges."""
    if index < 1:
        return "A"
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def dicts_to_matrix(
    rows: Sequence[Dict[str, str]],
    headers: Sequence[str],
) -> List[List[str]]:
    return [list(headers)] + [
        [clean_space(row.get(h, "")) for h in headers]
        for row in rows
    ]
