"""Command-line interface."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from . import VERSION
from .models import ParseWarning, PollFile, effective_poll_responses
from .normalize import clean_space
from .parsers import (
    check_class_window,
    looks_like_poll_export,
    parse_canvas_roster,
    parse_poll_everywhere_export,
)
from .pipeline import (
    SheetWriteInterrupted,
    check_google_connection,
    import_polls,
    refresh_views,
    remap_identities,
    run_dry_run,
    sync_canvas_roster,
)
from .scoring import configure_daily_scoring
from .sheets import class_window, load_config

__all__ = ["main"]


def discover_csvs(input_path: Path, output_fn=print) -> List[Path]:
    """Find the Poll Everywhere exports to import.

    Naming one file imports that file. Naming a folder imports the Poll
    exports in it and skips everything else, so a Canvas roster or a preview
    CSV sitting alongside them does not abort the run.
    """
    input_path = Path(input_path)
    if input_path.is_file():
        if input_path.suffix.casefold() != ".csv":
            raise ValueError("Input file must be a .csv file.")
        return [input_path]
    if not input_path.exists():
        raise FileNotFoundError(f"Input path not found: {input_path}")

    # Top level first, so pointing at one lecture's folder stays predictable.
    # The documented layout nests exports under polls/<date>/, so fall back to
    # a recursive search when the top level holds no Poll exports.
    all_csvs = sorted(p for p in input_path.glob("*.csv") if p.is_file())
    files = [p for p in all_csvs if looks_like_poll_export(p)]

    if not files:
        nested = sorted(p for p in input_path.rglob("*.csv") if p.is_file())
        nested = [p for p in nested if p not in all_csvs]
        found = [p for p in nested if looks_like_poll_export(p)]
        if found:
            output_fn(
                f"Found {len(found)} Poll export(s) in subfolders of {input_path.name}/."
            )
            files = found
            all_csvs = all_csvs + nested

    skipped = [p.name for p in all_csvs if p not in files]
    if skipped:
        output_fn(
            f"Skipped {len(skipped)} CSV file(s) that are not Poll Everywhere "
            f"exports: {', '.join(sorted(skipped))}"
        )
    if not files:
        raise FileNotFoundError(
            f"No Poll Everywhere export CSVs found in {input_path} or its subfolders."
        )
    return files


def print_batch_summary(polls: Sequence[PollFile], output_fn=print) -> None:
    identities = {
        r.poll_key for p in polls for r in effective_poll_responses(p) if r.poll_key
    }
    dates = sorted({p.class_date for p in polls})
    source_files = {str(p.path) for p in polls}
    lecture_files = {str(p.path) for p in polls if p.source_format == "lecture-wide"}

    output_fn("\n" + "=" * 72)
    output_fn(f"ECS101 Poll Everywhere Importer v{VERSION}")
    output_fn("=" * 72)
    output_fn(f"CSV files found: {len(source_files)}")
    output_fn(f"Questions detected: {len(polls)}")
    if lecture_files:
        output_fn(f"Lecture-wide CSV files: {len(lecture_files)}")
    labels = {p.timezone_label for p in polls if p.timezone_label}
    if labels:
        output_fn(f"Export timezone label(s): {', '.join(sorted(labels))}")
    output_fn(f"Class date(s): {', '.join(dates)}")
    output_fn(f"Unique Poll participants with at least one answer: {len(identities)}")

    unregistered = sorted({
        r.student_name for p in polls for r in p.responses
        if r.student_name.startswith("[UNREGISTERED]")
    })
    if unregistered:
        output_fn(f"WARNING: {len(unregistered)} unregistered participant(s) found.")


def _print_warnings(warnings: Sequence[ParseWarning], output_fn, limit: int = 25) -> None:
    if not warnings:
        return
    output_fn(f"\n{len(warnings)} parser warning(s):")
    for w in warnings[:limit]:
        output_fn(f"  - {w}")
    if len(warnings) > limit:
        output_fn(f"  ... and {len(warnings) - limit} more")


def _flagged_questions(polls: Sequence[PollFile]) -> Dict[str, str]:
    """Collect suspicious-column notes emitted by the parser."""
    flagged: Dict[str, str] = {}
    for poll in polls:
        for w in poll.warnings:
            if "may not be a question" in w.message:
                flagged[poll.question_name] = w.message
    return flagged


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ecs101_poll_importer.py",
        description=(
            "Import a Canvas roster and Poll Everywhere CSVs into normalized "
            "Google Sheets tables, then rebuild attendance, first-question "
            "scores and the leaderboard."
        ),
    )
    parser.add_argument(
        "input", nargs="?", default=".",
        help="Folder containing Poll Everywhere CSVs, or one Poll CSV file.",
    )
    parser.add_argument(
        "--import-roster", metavar="CANVAS_CSV",
        help="Sync the authoritative student roster from a Canvas gradebook CSV.",
    )
    parser.add_argument(
        "--roster-csv", metavar="CANVAS_CSV",
        help="For --dry-run only: match participants against this Canvas roster locally.",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Do not write Google Sheets; produce local preview CSVs.")
    parser.add_argument("--non-interactive", action="store_true",
                        help="Never prompt. Unknown identities are left unresolved. "
                             "Requires --dry-run.")
    parser.add_argument("--output-dir", default="output_preview_v3",
                        help="Preview folder for --dry-run.")
    parser.add_argument("--config", default="config.json",
                        help="Google Sheet config JSON (default: config.json).")
    parser.add_argument("--replace", action="store_true",
                        help="Replace an existing question with the same Question ID.")
    parser.add_argument("--check-google", action="store_true",
                        help="Verify Google Sheet access without importing data.")
    parser.add_argument("--refresh-views", action="store_true",
                        help="Rebuild Attendance, Attendance Review, Scores and "
                             "Leaderboard from existing canonical data.")
    parser.add_argument("--remap-identities", action="store_true",
                        help="Re-apply the stored Participant Map to Responses and "
                             "rebuild views. Use after editing Participant Map by hand.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.check_google:
        try:
            check_google_connection(load_config(Path(args.config).expanduser().resolve()))
        except Exception as exc:
            print(f"ERROR while checking Google Sheets: {exc}", file=sys.stderr)
            return 3
        return 0

    if args.refresh_views:
        try:
            refresh_views(load_config(Path(args.config).expanduser().resolve()))
        except SheetWriteInterrupted as exc:
            print(f"\nERROR after writing had begun: {exc}", file=sys.stderr)
            print("The spreadsheet may be partially updated. Restore from the "
                  "backup folder printed above, or rerun --refresh-views.", file=sys.stderr)
            return 4
        except Exception as exc:
            print(f"ERROR while refreshing views: {exc}", file=sys.stderr)
            return 3
        return 0

    if args.remap_identities:
        try:
            remap_identities(load_config(Path(args.config).expanduser().resolve()))
        except SheetWriteInterrupted as exc:
            print(f"\nERROR after writing had begun: {exc}", file=sys.stderr)
            print("The spreadsheet may be partially updated. Restore from the "
                  "backup folder printed above.", file=sys.stderr)
            return 4
        except Exception as exc:
            print(f"ERROR while remapping identities: {exc}", file=sys.stderr)
            return 3
        return 0

    if args.import_roster:
        roster_path = Path(args.import_roster).expanduser().resolve()
        if args.dry_run:
            try:
                students, warnings = parse_canvas_roster(roster_path)
                _print_warnings(warnings, print)
                print(f"Canvas roster parsed successfully: {len(students)} students.")
                with_login = sum(1 for s in students if s.login)
                print(f"Students with a login/e-mail alias: {with_login}")
                print("No Google Sheet data were changed.")
                return 0
            except Exception as exc:
                print(f"ERROR while reading Canvas roster: {exc}", file=sys.stderr)
                return 2
        try:
            sync_canvas_roster(roster_path, load_config(Path(args.config).expanduser().resolve()))
        except (KeyboardInterrupt, EOFError):
            print("\nCancelled before any write. No roster changes were made.")
            return 130
        except SheetWriteInterrupted as exc:
            print(f"\nERROR after writing had begun: {exc}", file=sys.stderr)
            print("The spreadsheet may be partially updated. Restore from the "
                  "backup folder printed above.", file=sys.stderr)
            return 4
        except Exception as exc:
            print(f"\nERROR while syncing Canvas roster: {exc}", file=sys.stderr)
            return 3
        return 0

    if args.non_interactive and not args.dry_run:
        print("ERROR: --non-interactive requires --dry-run. A production import "
              "must not silently leave identities unresolved.", file=sys.stderr)
        return 2

    try:
        config = load_config(Path(args.config).expanduser().resolve())
    except Exception as exc:
        if args.dry_run:
            config = {"course_timezone": "America/New_York", "class_start": "", "class_end": ""}
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

    course_tz = clean_space(config.get("course_timezone")) or "America/New_York"
    export_tz = clean_space(config.get("poll_export_timezone"))

    try:
        csv_paths = discover_csvs(Path(args.input).expanduser().resolve())
        polls: List[PollFile] = []
        warnings: List[ParseWarning] = []
        for path in csv_paths:
            parsed, file_warnings = parse_poll_everywhere_export(path, course_tz, export_tz)
            for poll in parsed:
                poll.warnings = list(file_warnings)
            polls.extend(parsed)
            warnings.extend(file_warnings)
    except Exception as exc:
        print(f"ERROR while reading Poll Everywhere CSV files: {exc}", file=sys.stderr)
        return 2

    print_batch_summary(polls)
    _print_warnings(warnings, print)

    start, end = class_window(config)
    problems = check_class_window(polls, start, end)
    if problems:
        print("\n" + "!" * 72)
        print("TIMEZONE CHECK FAILED")
        print("!" * 72)
        for problem in problems:
            print(f"  {problem}")
        if not args.dry_run:
            answer = input("\nContinue anyway? [y/N]: ").strip().casefold()
            if answer not in {"y", "yes"}:
                print("Stopped. No data were written.")
                return 2
    elif start and end:
        print(
            f"Timezone check passed (class window {start.strftime('%H:%M')}-"
            f"{end.strftime('%H:%M')} {course_tz})."
        )

    if len({p.class_date for p in polls}) > 1:
        print("WARNING: input files span multiple dates; each is stored by its own date.")

    # Validate the roster before the TA answers anything. A wrong path used to
    # surface only after every scoring prompt had been worked through.
    roster_path = Path(args.roster_csv).expanduser().resolve() if args.roster_csv else None
    if roster_path is not None:
        try:
            students, roster_warnings = parse_canvas_roster(roster_path)
        except Exception as exc:
            print(f"ERROR while reading the Canvas roster: {exc}", file=sys.stderr)
            return 2
        _print_warnings(roster_warnings, print)
        print(f"Canvas roster: {len(students)} students from {roster_path.name}")

    interactive = not (args.dry_run and args.non_interactive)

    if interactive:
        try:
            configure_daily_scoring(polls, _flagged_questions(polls))
        except (KeyboardInterrupt, EOFError):
            print("\nCancelled. No data were written.")
            return 130

    if args.dry_run:
        try:
            result = run_dry_run(
                polls,
                Path(args.output_dir).expanduser().resolve(),
                roster_path,
                interactive=interactive,
            )
        except (KeyboardInterrupt, EOFError):
            print("\nCancelled. No data were written.")
            return 130
        except Exception as exc:
            print(f"ERROR during dry run: {exc}", file=sys.stderr)
            return 2

        responses = result["responses"]
        print("\n" + "=" * 72)
        print("Dry-run batch summary")
        print("=" * 72)
        print(f"Questions detected: {len(polls)}")
        print(f"Canonical responses: {len(responses)}")
        matched = len({r["Student Key"] for r in responses if r.get("Student Key")})
        print(f"Matched roster students represented: {matched}")
        print(f"\nPreview files written to:\n  {result['output_dir']}")
        return 0

    try:
        import_polls(polls, config, replace=args.replace)
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled before any write. No new poll data were written.")
        return 130
    except SheetWriteInterrupted as exc:
        print(f"\nERROR after writing had begun: {exc}", file=sys.stderr)
        print("The spreadsheet may be partially updated. Restore from the backup "
              "folder printed above, then rerun.", file=sys.stderr)
        return 4
    except Exception as exc:
        print(f"\nERROR while updating Google Sheets: {exc}", file=sys.stderr)
        print("No spreadsheet data were written. The original Poll Everywhere "
              "CSV files were not modified.", file=sys.stderr)
        return 3

    return 0
