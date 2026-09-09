"""Orchestration: roster sync, poll import, view refresh, identity remap."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .identity import MatchJournal, resolve_participants
from .models import (
    IMPORT_LOG_HEADERS,
    PARTICIPANT_MAP_HEADERS,
    QUESTION_HEADERS,
    RESPONSE_HEADERS,
    ROSTER_HEADERS,
    ParseWarning,
    PollFile,
    effective_poll_responses,
)
from .normalize import clean_space, now_iso, short_hash
from .parsers import parse_canvas_roster
from .records import (
    active_roster_rows,
    parse_excused_rows,
    build_updated_roster,
    collapse_canonical_responses,
    count_blank_active,
    normalize_response_schema,
    poll_to_records,
    remap_existing_responses,
)
from .sheets import (
    CANONICAL_TABS,
    READ_ONLY_TABS,
    WORKSHEET_TITLES,
    SheetIO,
    dicts_to_matrix,
    open_spreadsheet,
)
from .views import build_attendance_review, build_derived_tables

__all__ = [
    "sync_canvas_roster",
    "import_polls",
    "refresh_views",
    "remap_identities",
    "check_google_connection",
    "run_dry_run",
    "SheetWriteInterrupted",
]


class SheetWriteInterrupted(Exception):
    """Raised when a run stopped after writes had already begun.

    v2.3.1 printed "no data were written" for every failure, including ones
    that happened halfway through eight sequential tab writes. Callers use this
    to tell the difference.
    """


def _title(key: str) -> str:
    return WORKSHEET_TITLES[key]


def _canonical_titles() -> List[str]:
    return [_title(k) for k in CANONICAL_TABS]


def _readable_titles() -> List[str]:
    return _canonical_titles() + [_title(k) for k in READ_ONLY_TABS]


def _open(config: Dict[str, str]) -> Tuple[object, SheetIO]:
    sh = open_spreadsheet(config)
    io = SheetIO(sh)
    io.ensure_worksheets()
    io.read_all(_readable_titles())
    return sh, io


def _require_canvas_roster(roster_rows: Sequence[Dict[str, str]]) -> None:
    if not any(
        clean_space(r.get("Student Key")).startswith("canvas:") for r in roster_rows
    ):
        raise RuntimeError(
            "No authoritative Canvas roster is loaded.\n"
            "Run first:\n"
            "  python ecs101_poll_importer.py --import-roster CanvasGrades.csv"
        )


def _report_blank_active(roster_rows: Sequence[Dict[str, str]], output_fn) -> None:
    blanks = count_blank_active(roster_rows)
    if blanks:
        output_fn(
            f"\nNOTE: {blanks} roster row(s) have a blank Active cell and are "
            "being counted as active. Set Active to TRUE or FALSE explicitly."
        )


def _stage_views(
    io: SheetIO,
    questions: Sequence[Dict[str, str]],
    responses: Sequence[Dict[str, str]],
    roster_rows: Sequence[Dict[str, str]],
    output_fn=print,
) -> None:
    class_dates = sorted({
        clean_space(q.get("Date")) for q in questions if clean_space(q.get("Date"))
    })
    excused, problems = parse_excused_rows(
        io.records(_title("excused")), roster_rows, class_dates
    )
    if excused:
        output_fn(f"\nExcused absences read from the Excused tab: {len(excused)}")
    if problems:
        output_fn(f"{len(problems)} Excused row(s) could not be used:")
        for problem in problems[:15]:
            output_fn(f"  - {problem}")
        if len(problems) > 15:
            output_fn(f"  ... and {len(problems) - 15} more")

    attendance, scores, leaderboard = build_derived_tables(
        questions, responses, roster_rows, excused
    )
    review = build_attendance_review(
        questions, responses, roster_rows, excused=excused
    )
    io.stage(_title("attendance"), attendance)
    io.stage(_title("attendance_review"), review)
    io.stage(_title("scores"), scores)
    io.stage(_title("leaderboard"), leaderboard)


def _commit(io: SheetIO, output_fn) -> None:
    try:
        summary = io.commit()
    except Exception as exc:
        if io.write_started:
            raise SheetWriteInterrupted(str(exc)) from exc
        raise
    output_fn(
        f"\nWrote {summary['tabs']} tab(s) in {summary['api_calls']} Google Sheets API call(s)."
    )


def _print_warnings(warnings: Sequence[ParseWarning], output_fn, limit: int = 25) -> None:
    if not warnings:
        return
    output_fn(f"\n{len(warnings)} parser warning(s):")
    for w in warnings[:limit]:
        output_fn(f"  - {w}")
    if len(warnings) > limit:
        output_fn(f"  ... and {len(warnings) - limit} more")


# ---------------------------------------------------------------------------
# Canvas roster sync
# ---------------------------------------------------------------------------

def sync_canvas_roster(
    canvas_path: Path,
    config: Dict[str, str],
    input_fn=input,
    output_fn=print,
) -> None:
    canvas_students, warnings = parse_canvas_roster(canvas_path)
    _print_warnings(warnings, output_fn)

    sh, io = _open(config)
    backup = io.backup(Path(config.get("backup_dir", "backups")), _canonical_titles())
    if backup:
        output_fn(f"\nBacked up canonical tabs to: {backup}")

    existing_roster = io.records(_title("roster"))
    existing_questions = io.records(_title("questions"))
    existing_responses = [
        normalize_response_schema(r) for r in io.records(_title("responses"))
    ]
    existing_log = io.records(_title("import_log"))
    participant_map_rows = io.records(_title("participant_map"))

    synced_at = now_iso()
    updated_roster, roster_stats = build_updated_roster(
        canvas_students, existing_roster, synced_at
    )

    # Reconcile every previously observed Poll identity against the new roster.
    historical = [
        (clean_space(r.get("Poll Participant")), clean_space(r.get("Poll Email")))
        for r in existing_responses
        if clean_space(r.get("Poll Participant"))
    ]

    journal = MatchJournal(Path(config.get("backup_dir", "backups")) / "roster-match.journal.jsonl")
    mapping, updated_map_rows, match_stats = resolve_participants(
        historical, updated_roster, participant_map_rows,
        interactive=True, journal=journal, input_fn=input_fn, output_fn=output_fn,
    )

    remapped = remap_existing_responses(existing_responses, mapping)

    responses_by_qid: Dict[str, List[Dict[str, str]]] = {}
    for row in remapped:
        responses_by_qid.setdefault(clean_space(row.get("Question ID")), []).append(row)

    normalized_logs = [
        {
            "Import ID": clean_space(old.get("Import ID")) or short_hash(old.get("File Hash")),
            "Date": clean_space(old.get("Date")),
            "Source File": clean_space(old.get("Source File")),
            "File Hash": clean_space(old.get("File Hash")),
            "Question ID": clean_space(old.get("Question ID")),
            "Effective Responses": clean_space(
                old.get("Effective Responses")
                or old.get("Responses Imported")
                or str(len(responses_by_qid.get(clean_space(old.get("Question ID")), [])))
            ),
            "Matched Students": str(len({
                r.get("Student Key", "")
                for r in responses_by_qid.get(clean_space(old.get("Question ID")), [])
                if r.get("Student Key")
            })),
            "Unmatched / Non-student": str(sum(
                1 for r in responses_by_qid.get(clean_space(old.get("Question ID")), [])
                if not r.get("Student Key")
            )),
            "Imported At": clean_space(old.get("Imported At")),
        }
        for old in existing_log
    ]

    io.stage(_title("roster"), dicts_to_matrix(updated_roster, ROSTER_HEADERS))
    io.stage(_title("participant_map"), dicts_to_matrix(updated_map_rows, PARTICIPANT_MAP_HEADERS))
    io.stage(_title("responses"), dicts_to_matrix(remapped, RESPONSE_HEADERS))
    io.stage(_title("import_log"), dicts_to_matrix(normalized_logs, IMPORT_LOG_HEADERS))
    _stage_views(io, existing_questions, remapped, updated_roster)
    _commit(io, output_fn)
    journal.clear()

    active_count = len(active_roster_rows(updated_roster))
    output_fn("\n" + "=" * 72)
    output_fn("Canvas roster sync complete")
    output_fn("=" * 72)
    output_fn(f"Source: {Path(canvas_path).name}")
    output_fn(f"Active Canvas students: {active_count}")
    output_fn(f"Inactive historical students: {len(updated_roster) - active_count}")
    output_fn(f"New roster students: {roster_stats.get('added', 0)}")
    output_fn(f"Retained: {roster_stats.get('retained', 0)}")
    output_fn(f"Marked inactive: {roster_stats.get('inactivated', 0)}")
    _print_match_stats(match_stats, output_fn)
    _report_blank_active(updated_roster, output_fn)
    output_fn(f"\nGoogle Sheet updated: {sh.title}")


def _print_match_stats(stats: Dict[str, int], output_fn) -> None:
    if not stats:
        return
    output_fn("\nParticipant matching:")
    output_fn(f"  Saved mappings reused        : {stats.get('saved', 0)}")
    output_fn(f"  Canvas login / e-mail match  : {stats.get('login', 0)}")
    output_fn(f"  E-mail-derived name match    : {stats.get('email_name', 0)}")
    output_fn(f"  Exact name match             : {stats.get('exact', 0)}")
    output_fn(f"  Human-confirmed              : {stats.get('confirmed', 0)}")
    output_fn(
        "  Non-student / staff / guest  : "
        f"{stats.get('nonstudent', 0) + stats.get('saved_nonstudent', 0)}"
    )
    output_fn(f"  Left unresolved              : {stats.get('unresolved', 0)}")


# ---------------------------------------------------------------------------
# Poll import
# ---------------------------------------------------------------------------

def import_polls(
    polls: Sequence[PollFile],
    config: Dict[str, str],
    replace: bool = False,
    input_fn=input,
    output_fn=print,
) -> None:
    sh, io = _open(config)
    roster_rows = io.records(_title("roster"))
    _require_canvas_roster(roster_rows)
    _report_blank_active(roster_rows, output_fn)

    backup = io.backup(Path(config.get("backup_dir", "backups")), _canonical_titles())
    if backup:
        output_fn(f"\nBacked up canonical tabs to: {backup}")

    existing_questions = io.records(_title("questions"))
    existing_responses = [
        normalize_response_schema(r) for r in io.records(_title("responses"))
    ]
    existing_log = io.records(_title("import_log"))
    participant_map_rows = io.records(_title("participant_map"))

    existing_hashes = {
        clean_space(r.get("File Hash")) for r in existing_log if clean_space(r.get("File Hash"))
    }
    existing_qids = {
        clean_space(q.get("Question ID")) for q in existing_questions
        if clean_space(q.get("Question ID"))
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
        output_fn("\nExact duplicate file(s) already imported; skipped:")
        for path in sorted({p.path.name for p in exact_duplicates}):
            output_fn(f"  - {path}")

    if qid_conflicts and not replace:
        output_fn("\nQuestion ID conflict(s); skipped:")
        for poll in qid_conflicts:
            output_fn(
                f"  - {poll.path.name} -> {poll.question_id}\n"
                "    Use --replace if this is an intentional corrected export."
            )
    elif qid_conflicts and replace:
        candidates.extend(qid_conflicts)
        replace_qids = {p.question_id for p in qid_conflicts}
        existing_questions = [
            q for q in existing_questions
            if clean_space(q.get("Question ID")) not in replace_qids
        ]
        existing_responses = [
            r for r in existing_responses
            if clean_space(r.get("Question ID")) not in replace_qids
        ]
        # Only the log rows for the replaced questions go. v2.3.1 also deleted
        # every row sharing the file hash, which for a lecture-wide CSV is every
        # other question in the same lecture: those questions kept their data
        # but lost their import record, and the file became re-importable.
        existing_log = [
            r for r in existing_log
            if clean_space(r.get("Question ID")) not in replace_qids
        ]
        output_fn("\n--replace enabled; replacing:")
        for qid in sorted(replace_qids):
            output_fn(f"  - {qid}")

    existing_scored_by_date = {
        clean_space(q.get("Date")): clean_space(q.get("Question ID"))
        for q in existing_questions
        if str(q.get("Scored", "")).strip().upper() == "TRUE" and clean_space(q.get("Date"))
    }
    forced = [
        p for p in candidates
        if existing_scored_by_date.get(p.class_date) and p.correct_answers is not None
    ]
    for poll in forced:
        poll.correct_answers = None
    if forced:
        output_fn("\nThis class date already has a scored question stored.")
        output_fn("These newly imported Polls will be attendance-only:")
        for poll in forced:
            output_fn(f"  - {poll.question_name}")

    journal = MatchJournal(Path(config.get("backup_dir", "backups")) / "import-match.journal.jsonl")

    # Every identity that needs a decision this run: the ones in the incoming
    # batch, plus any stored identity still unresolved. Resolving them together
    # means a participant is only ever asked about once per run.
    incoming_rows = [r for poll in candidates for r in effective_poll_responses(poll)]
    incoming_keys = {r.poll_key for r in incoming_rows if r.poll_key}
    historical_pending = [
        (clean_space(r.get("Poll Participant")), clean_space(r.get("Poll Email")))
        for r in existing_responses
        if not clean_space(r.get("Student Key"))
        and clean_space(r.get("Poll Participant"))
        and clean_space(r.get("Match Status")).casefold() != "non-student"
        and clean_space(r.get("Poll Key")) not in incoming_keys
    ]

    identities: List = list(incoming_rows) + historical_pending
    mapping, updated_map_rows, match_stats = resolve_participants(
        identities, roster_rows, participant_map_rows,
        interactive=True, journal=journal, input_fn=input_fn, output_fn=output_fn,
    )

    # THE fix for the v2.3.1 backfill bug. The mapping built above is the full
    # index, so applying it to stored responses gives every identity confirmed
    # in this run its earlier attendance and scores back. Without this line a
    # student confirmed in week 2 stays absent for week 1 forever, silently.
    existing_responses = remap_existing_responses(existing_responses, mapping)

    imported_at = now_iso()
    incoming_questions: List[Dict[str, str]] = []
    incoming_responses: List[Dict[str, str]] = []
    incoming_logs: List[Dict[str, str]] = []
    for poll in candidates:
        q, responses, log = poll_to_records(poll, imported_at, mapping)
        incoming_questions.append(q)
        incoming_responses.extend(responses)
        incoming_logs.append(log)

    all_questions = sorted(
        [*existing_questions, *incoming_questions],
        key=lambda q: (clean_space(q.get("Date")), clean_space(q.get("Question ID"))),
    )
    all_responses = collapse_canonical_responses([*existing_responses, *incoming_responses])
    all_logs = sorted(
        [*existing_log, *incoming_logs],
        key=lambda r: (clean_space(r.get("Date")), clean_space(r.get("Source File")).casefold()),
    )

    io.stage(_title("questions"), dicts_to_matrix(all_questions, QUESTION_HEADERS))
    io.stage(_title("responses"), dicts_to_matrix(all_responses, RESPONSE_HEADERS))
    io.stage(_title("import_log"), dicts_to_matrix(all_logs, IMPORT_LOG_HEADERS))
    io.stage(_title("participant_map"), dicts_to_matrix(updated_map_rows, PARTICIPANT_MAP_HEADERS))
    _stage_views(io, all_questions, all_responses, roster_rows)
    _commit(io, output_fn)
    journal.clear()

    active_keys = {clean_space(r["Student Key"]) for r in active_roster_rows(roster_rows)}
    matched_now = {
        r.get("Student Key") for r in incoming_responses
        if r.get("Student Key") in active_keys
    }
    unmatched_now = {
        r.get("Poll Key") for r in incoming_responses if not r.get("Student Key")
    }

    output_fn(f"\nGoogle Sheet updated: {sh.title}")
    output_fn(f"Imported source CSV files: {len({str(p.path) for p in candidates})}")
    output_fn(f"Imported questions: {len(candidates)}")
    output_fn(f"Canonical questions: {len(all_questions)}")
    output_fn(f"Canonical responses: {len(all_responses)}")
    output_fn(f"Active students in roster: {len(active_keys)}")
    output_fn(f"Active students participating in this batch: {len(matched_now)}")
    output_fn(f"Unmatched / non-student identities in this batch: {len(unmatched_now)}")
    _print_match_stats(match_stats, output_fn)


# ---------------------------------------------------------------------------
# View refresh and identity remap
# ---------------------------------------------------------------------------

def refresh_views(config: Dict[str, str], output_fn=print) -> None:
    """Rebuild derived views from existing canonical data. Changes nothing else."""
    sh, io = _open(config)
    roster_rows = io.records(_title("roster"))
    _require_canvas_roster(roster_rows)
    questions = io.records(_title("questions"))
    responses = [normalize_response_schema(r) for r in io.records(_title("responses"))]

    _stage_views(io, questions, responses, roster_rows)
    _commit(io, output_fn)

    output_fn("\nDerived views refreshed.")
    output_fn(f"Spreadsheet: {sh.title}")
    output_fn("Updated: Attendance, Attendance Review, Scores, Leaderboard")
    output_fn("Canonical data were not changed.")


def remap_identities(config: Dict[str, str], output_fn=print) -> None:
    """Re-apply the stored Participant Map to Responses, then rebuild views.

    Use this after editing Participant Map by hand. --refresh-views deliberately
    does not do it, because it must not touch canonical data.
    """
    sh, io = _open(config)
    roster_rows = io.records(_title("roster"))
    _require_canvas_roster(roster_rows)

    backup = io.backup(Path(config.get("backup_dir", "backups")), _canonical_titles())
    if backup:
        output_fn(f"\nBacked up canonical tabs to: {backup}")

    questions = io.records(_title("questions"))
    responses = [normalize_response_schema(r) for r in io.records(_title("responses"))]
    participant_map_rows = io.records(_title("participant_map"))

    before = sum(1 for r in responses if clean_space(r.get("Student Key")))
    mapping, updated_map_rows, _ = resolve_participants(
        [(clean_space(r.get("Poll Participant")), clean_space(r.get("Poll Email")))
         for r in responses if clean_space(r.get("Poll Participant"))],
        roster_rows, participant_map_rows,
        interactive=False, output_fn=output_fn,
    )
    remapped = remap_existing_responses(responses, mapping)
    after = sum(1 for r in remapped if clean_space(r.get("Student Key")))

    io.stage(_title("responses"), dicts_to_matrix(remapped, RESPONSE_HEADERS))
    io.stage(_title("participant_map"), dicts_to_matrix(updated_map_rows, PARTICIPANT_MAP_HEADERS))
    _stage_views(io, questions, remapped, roster_rows)
    _commit(io, output_fn)

    output_fn("\nIdentity remap complete.")
    output_fn(f"Spreadsheet: {sh.title}")
    output_fn(f"Matched responses before: {before}")
    output_fn(f"Matched responses after : {after}  ({after - before:+d})")


def check_google_connection(config: Dict[str, str], output_fn=print) -> None:
    sh = open_spreadsheet(config)
    output_fn("\nGoogle connection successful.")
    output_fn(f"Spreadsheet: {sh.title}")
    output_fn("No spreadsheet data were changed.")


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

def _write_csv_matrix(path: Path, matrix: Sequence[Sequence[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f).writerows(matrix)


def _write_dict_csv(path: Path, rows: Sequence[Dict[str, str]], headers: Sequence[str]) -> None:
    _write_csv_matrix(path, dicts_to_matrix(rows, headers))


def pseudo_roster_for_preview(polls: Sequence[PollFile]) -> List[Dict[str, str]]:
    """Name-based stand-in roster so a dry run works without a Canvas CSV."""
    stamp = now_iso()
    seen: Dict[str, Dict[str, str]] = {}
    for poll in polls:
        for r in effective_poll_responses(poll):
            key = f"preview:{r.poll_key}"
            seen.setdefault(key, {
                "Student Key": key,
                "Student Name": r.student_name,
                "Canvas Name": "",
                "Section": "",
                "Login": r.normalized_email,
                "Active": "TRUE",
                "First Seen": stamp,
                "Roster Updated At": stamp,
                "Notes": "Dry-run pseudo roster; not for production.",
            })
    return sorted(seen.values(), key=lambda r: r["Student Name"].casefold())


def run_dry_run(
    polls: Sequence[PollFile],
    output_dir: Path,
    roster_csv: Optional[Path] = None,
    interactive: bool = True,
    input_fn=input,
    output_fn=print,
) -> Dict[str, object]:
    imported_at = now_iso()

    if roster_csv:
        students, warnings = parse_canvas_roster(Path(roster_csv))
        _print_warnings(warnings, output_fn)
        roster_rows, _ = build_updated_roster(students, [], imported_at)
        identities = [r for poll in polls for r in effective_poll_responses(poll)]
        mapping, map_rows, match_stats = resolve_participants(
            identities, roster_rows, [], interactive=interactive,
            input_fn=input_fn, output_fn=output_fn,
        )
    else:
        output_fn(
            "\nNOTE: no --roster-csv supplied. Dry run uses temporary name-based "
            "identities. Production imports require the Canvas roster."
        )
        roster_rows = pseudo_roster_for_preview(polls)
        identities = [r for poll in polls for r in effective_poll_responses(poll)]
        mapping, map_rows, match_stats = resolve_participants(
            identities, roster_rows, [], interactive=False, output_fn=output_fn,
        )

    questions: List[Dict[str, str]] = []
    responses: List[Dict[str, str]] = []
    logs: List[Dict[str, str]] = []
    for poll in polls:
        q, rows, log = poll_to_records(poll, imported_at, mapping)
        questions.append(q)
        responses.extend(rows)
        logs.append(log)
    responses = collapse_canonical_responses(responses)

    attendance, scores, leaderboard = build_derived_tables(questions, responses, roster_rows)
    review = build_attendance_review(questions, responses, roster_rows)

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    _write_dict_csv(out / "roster_preview.csv", roster_rows, ROSTER_HEADERS)
    _write_dict_csv(out / "participant_map_preview.csv", map_rows, PARTICIPANT_MAP_HEADERS)
    _write_dict_csv(out / "questions_preview.csv", questions, QUESTION_HEADERS)
    _write_dict_csv(out / "responses_preview.csv", responses, RESPONSE_HEADERS)
    _write_dict_csv(out / "import_log_preview.csv", logs, IMPORT_LOG_HEADERS)
    _write_csv_matrix(out / "attendance_preview.csv", attendance)
    _write_csv_matrix(out / "attendance_review_preview.csv", review)
    _write_csv_matrix(out / "scores_preview.csv", scores)
    _write_csv_matrix(out / "leaderboard_preview.csv", leaderboard)

    return {
        "roster": roster_rows,
        "questions": questions,
        "responses": responses,
        "logs": logs,
        "attendance": attendance,
        "scores": scores,
        "leaderboard": leaderboard,
        "attendance_review": review,
        "match_stats": match_stats,
        "output_dir": out,
    }
