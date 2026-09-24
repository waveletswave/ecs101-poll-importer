"""Canvas roster and Poll Everywhere CSV parsing."""

from __future__ import annotations

from datetime import time
from pathlib import Path

import pytest

from ecs101.models import effective_poll_responses
from ecs101.parsers import (
    check_class_window,
    looks_like_lecture_export,
    parse_canvas_roster,
    parse_poll_everywhere_export,
)


# ---------------------------------------------------------------------------
# Canvas roster
# ---------------------------------------------------------------------------

def test_canvas_roster_skips_pseudo_rows(canvas_csv: Path):
    students, warnings = parse_canvas_roster(canvas_csv)
    names = [s.student_name for s in students]
    assert "Test Student" not in names
    assert "Points Possible" not in names
    assert len(students) == 5
    assert not [w for w in warnings if "duplicate" in w.message]


def test_canvas_roster_captures_login_aliases(canvas_csv: Path):
    students, _ = parse_canvas_roster(canvas_csv)
    imogen = next(s for s in students if s.student_name == "Imogen Vance")
    assert "iv517" in imogen.login
    assert "9000002" in imogen.login


def test_canvas_roster_without_login_columns_warns(tmp_path: Path):
    path = tmp_path / "bare.csv"
    path.write_text(
        "Student,ID,Section\n"
        '"Fletcher, Rowan",100001,ECS101-01\n',
        encoding="utf-8",
    )
    students, warnings = parse_canvas_roster(path)
    assert len(students) == 1
    assert any("no login/e-mail column" in w.message for w in warnings)


def test_canvas_roster_reports_duplicate_normalized_names(tmp_path: Path):
    path = tmp_path / "dupes.csv"
    path.write_text(
        "Student,ID,Section\n"
        '"Pham, Anh",100001,ECS101-01\n'
        '"Pham, Anh",100002,ECS101-01\n',
        encoding="utf-8",
    )
    _, warnings = parse_canvas_roster(path)
    assert any("share the normalized name" in w.message for w in warnings)


def test_canvas_roster_missing_required_column(tmp_path: Path):
    path = tmp_path / "bad.csv"
    path.write_text("Student,Section\nFletcher,ECS101\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing Canvas column"):
        parse_canvas_roster(path)


# ---------------------------------------------------------------------------
# Lecture-wide export
# ---------------------------------------------------------------------------

def test_lecture_export_is_detected(lecture_csv: Path, legacy_csv: Path):
    assert looks_like_lecture_export(lecture_csv) is True
    assert looks_like_lecture_export(legacy_csv) is False


def test_lecture_export_creates_one_poll_per_question(lecture_csv: Path):
    polls, warnings = parse_poll_everywhere_export(lecture_csv, "America/New_York")
    assert [p.question_name for p in polls] == [
        "Where is this photo taken?",
        "When was plate tectonics first proposed?",
    ]
    assert [p.question_order for p in polls] == [1, 2]
    assert all(p.class_date == "2026-08-26" for p in polls)
    assert all(p.source_format == "lecture-wide" for p in polls)
    assert polls[0].timezone_label == "CDT"


def test_blank_answers_do_not_create_responses(lecture_csv: Path):
    """A student who skipped Q1 still gets attendance from Q2."""
    polls, _ = parse_poll_everywhere_export(lecture_csv, "America/New_York")
    q1, q2 = polls
    assert len(q1.responses) == 3   # Zoe left Q1 blank
    assert len(q2.responses) == 4
    assert "Petra Solano" not in {r.student_name for r in q1.responses}
    assert "Petra Solano" in {r.student_name for r in q2.responses}


def test_lecture_timestamps_are_converted_from_the_header_label(lecture_csv: Path):
    polls, _ = parse_poll_everywhere_export(lecture_csv, "America/New_York")
    stamps = {r.created_at for r in polls[0].responses}
    assert "2026-08-26T13:27:00-04:00" in stamps


def test_email_is_carried_onto_every_response(lecture_csv: Path):
    polls, _ = parse_poll_everywhere_export(lecture_csv, "America/New_York")
    rowan = next(r for r in polls[0].responses if r.student_name == "Rowan Fletcher")
    assert rowan.normalized_email == "rowan.fletcher@example.edu"
    assert rowan.poll_key == "email:rowan.fletcher@example.edu"


def test_a_bad_timestamp_row_is_reported_not_fatal(tmp_path: Path):
    """One unreadable row must not sink the whole lecture."""
    path = tmp_path / "mixed.csv"
    path.write_text(
        "Response #,Started At (CDT),Participant First Name,Participant Last Name,"
        "Email,Screen Name,Public ID,Q1\n"
        "1,8/26/26 12:27,Rowan,Fletcher,rowan.fletcher@example.edu,Rowan F,1,Kamchatka\n"
        "2,sometime tuesday,Noor,Haddad,noor.haddad@example.edu,Noor H.,2,Kamchatka\n"
        "3,8/26/26 12:28,Imogen,Vance,iv517@example.edu,Imogen V.,3,Hokkaido\n",
        encoding="utf-8",
    )
    polls, warnings = parse_poll_everywhere_export(path, "America/New_York")
    assert len(polls[0].responses) == 2
    assert any("row 3" in str(w) and "unrecognized" in str(w) for w in warnings)


def test_missing_started_at_column_is_fatal(tmp_path: Path):
    path = tmp_path / "nostart.csv"
    path.write_text(
        "Response #,Participant First Name,Participant Last Name,Screen Name,Public ID,Q1\n"
        "1,Rowan,Fletcher,Rowan F,1,Kamchatka\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        parse_poll_everywhere_export(path, "America/New_York")


def test_rows_from_another_date_are_held_back_with_a_warning(tmp_path: Path):
    path = tmp_path / "twodates.csv"
    path.write_text(
        "Response #,Started At (CDT),Participant First Name,Participant Last Name,"
        "Email,Screen Name,Public ID,Q1\n"
        "1,8/26/26 12:27,Rowan,Fletcher,rowan.fletcher@example.edu,Rowan F,1,Kamchatka\n"
        "2,8/26/26 12:28,Noor,Haddad,noor.haddad@example.edu,Noor H.,2,Kamchatka\n"
        "3,8/19/26 12:28,Ghost,Tester,ghost@example.edu,Ghost,3,Hokkaido\n",
        encoding="utf-8",
    )
    polls, warnings = parse_poll_everywhere_export(path, "America/New_York")
    assert polls[0].class_date == "2026-08-26"
    assert len(polls[0].responses) == 2
    assert any("more than one date" in w.message for w in warnings)
    # Held for the TA to decide on, not thrown away.
    [held] = polls[0].off_date
    assert held.name == "Ghost Tester" and held.started_date == "2026-08-19"


def test_questions_that_share_a_title_stay_separate(tmp_path: Path):
    """Three photos, one caption. Each is its own question with its own answers."""
    path = tmp_path / "rocks.csv"
    path.write_text(
        "Response #,Started At (CDT),Participant First Name,Participant Last Name,"
        "Email,Screen Name,Public ID,Where is this photo taken?,"
        "What type of rock is this?,What type of rock is this?,What type of rock is this?\n"
        "1,9/21/26 12:30,Rowan,Fletcher,rowan.fletcher@example.edu,Rowan F,1,Victoria,Chalk,Sandstone,Conglomerate\n"
        "2,9/21/26 12:31,Noor,Haddad,noor.haddad@example.edu,Noor H.,2,Victoria,Chalk,,Breccia\n"
        "3,9/21/26 12:32,Petra,Solano,petra.solano@example.edu,Petra S.,3,Victoria,Chert,Sandstone,\n",
        encoding="utf-8",
    )
    polls, warnings = parse_poll_everywhere_export(path, "America/New_York")

    assert [p.question_name for p in polls] == [
        "Where is this photo taken?",
        "What type of rock is this?",
        "What type of rock is this? (2)",
        "What type of rock is this? (3)",
    ]
    assert len({p.question_id for p in polls}) == 4
    answers = [sorted(r.response for r in p.responses) for p in polls[1:]]
    assert answers == [
        ["Chalk", "Chalk", "Chert"],
        ["Sandstone", "Sandstone"],
        ["Breccia", "Conglomerate"],
    ]
    assert any("3 questions share the title" in w.message for w in warnings)


# ---------------------------------------------------------------------------
# Legacy export
# ---------------------------------------------------------------------------

def test_legacy_export_parses_summary_and_individual(legacy_csv: Path):
    polls, warnings = parse_poll_everywhere_export(legacy_csv, "America/New_York")
    poll = polls[0]
    assert poll.source_format == "single-question"
    assert poll.class_date == "2026-08-24"
    assert len(poll.responses) == 3
    assert poll.summary == [
        ("Snake River Canyon, Oregon", 2),
        ("Grand Canyon, Arizona", 1),
    ]
    assert not warnings


def test_legacy_export_warns_when_the_blocks_disagree(tmp_path: Path):
    path = tmp_path / "skewed.csv"
    path.write_text(
        "Response,Count\n"
        "Mount Rainier,1\n"
        "Total,1\n"
        "\n"
        "Individual Results\n"
        "Response,Via,Screen name,Registered participant,Created At\n"
        "Mount Baker,web,a,Alina Ashworth,2026-08-24 12:37:00\n",
        encoding="utf-8",
    )
    _, warnings = parse_poll_everywhere_export(path, "America/New_York")
    assert any("no individual response matches" in w.message for w in warnings)


# ---------------------------------------------------------------------------
# Deduplication within a question
# ---------------------------------------------------------------------------

def test_two_devices_one_email_collapse_to_the_latest(tmp_path: Path):
    """Dev Raman answered from two registrations in the real Lecture 2."""
    path = tmp_path / "dual.csv"
    path.write_text(
        "Response #,Started At (CDT),Participant First Name,Participant Last Name,"
        "Email,Screen Name,Public ID,Q1\n"
        "1,8/26/26 12:27,Dev,Raman,dev.raman@example.edu,Dev R.,397602,Kamchatka\n"
        "2,8/26/26 12:30,Dev,Raman,dev.raman@example.edu,Dev R.,363663,Hokkaido\n",
        encoding="utf-8",
    )
    polls, _ = parse_poll_everywhere_export(path, "America/New_York")
    effective = effective_poll_responses(polls[0])
    assert len(effective) == 1
    assert effective[0].response == "Hokkaido"       # the later answer wins


def test_different_emails_stay_separate_identities(tmp_path: Path):
    path = tmp_path / "split.csv"
    path.write_text(
        "Response #,Started At (CDT),Participant First Name,Participant Last Name,"
        "Email,Screen Name,Public ID,Q1\n"
        "1,8/26/26 12:27,Dev,Raman,dev.raman@example.edu,Dev R.,1,Kamchatka\n"
        "2,8/26/26 12:30,Dev,Raman,dr118@example.edu,Dev R.,2,Hokkaido\n",
        encoding="utf-8",
    )
    polls, _ = parse_poll_everywhere_export(path, "America/New_York")
    assert len(effective_poll_responses(polls[0])) == 2


# ---------------------------------------------------------------------------
# Class-window guard
# ---------------------------------------------------------------------------

def test_class_window_accepts_the_real_schedule(lecture_csv: Path):
    polls, _ = parse_poll_everywhere_export(lecture_csv, "America/New_York")
    assert check_class_window(polls, time(13, 25), time(14, 40)) == []


def test_class_window_catches_a_wrong_timezone(lecture_csv: Path):
    """Reading the CDT export as if it were UTC shifts it five hours."""
    polls, _ = parse_poll_everywhere_export(lecture_csv, "UTC")
    problems = check_class_window(polls, time(13, 25), time(14, 40))
    assert problems and "course_timezone" in problems[0]


def test_class_window_is_skipped_when_not_configured(lecture_csv: Path):
    polls, _ = parse_poll_everywhere_export(lecture_csv, "America/New_York")
    assert check_class_window(polls, None, None) == []


# ---------------------------------------------------------------------------
# poll_export_timezone: the legacy format never states its own clock
# ---------------------------------------------------------------------------

def test_legacy_export_can_be_told_its_source_timezone(legacy_csv: Path):
    """Lecture 1 has no timezone anywhere, but the account exports in CDT."""
    default, _ = parse_poll_everywhere_export(legacy_csv, "America/New_York")
    assert default[0].responses[0].created_at == "2026-08-24T12:37:00-04:00"

    told, _ = parse_poll_everywhere_export(legacy_csv, "America/New_York", "CDT")
    assert told[0].responses[0].created_at == "2026-08-24T13:37:00-04:00"
    assert told[0].class_date == "2026-08-24"      # the date does not move


def test_declared_source_timezone_brings_legacy_into_the_class_window(legacy_csv: Path):
    untold, _ = parse_poll_everywhere_export(legacy_csv, "America/New_York")
    assert check_class_window(untold, time(13, 25), time(14, 40)) != []

    told, _ = parse_poll_everywhere_export(legacy_csv, "America/New_York", "CDT")
    assert check_class_window(told, time(13, 25), time(14, 40)) == []


def test_a_header_label_beats_the_configured_default(lecture_csv: Path):
    """The file's own label always wins; config only fills a gap."""
    polls, _ = parse_poll_everywhere_export(lecture_csv, "America/New_York", "UTC")
    assert polls[0].timezone_label == "CDT"
    assert "2026-08-26T13:27:00-04:00" in {r.created_at for r in polls[0].responses}


def test_unrecognised_configured_timezone_warns(legacy_csv: Path):
    _, warnings = parse_poll_everywhere_export(legacy_csv, "America/New_York", "Narnia")
    assert any("not recognised" in w.message for w in warnings)


# ---------------------------------------------------------------------------
# Folder scanning must ignore CSVs that are not Poll exports
# ---------------------------------------------------------------------------

def test_looks_like_poll_export(lecture_csv: Path, legacy_csv: Path, canvas_csv: Path):
    from ecs101.parsers import looks_like_poll_export

    assert looks_like_poll_export(lecture_csv) is True
    assert looks_like_poll_export(legacy_csv) is True
    assert looks_like_poll_export(canvas_csv) is False


def test_discover_skips_a_canvas_roster_in_the_same_folder(
    lecture_csv: Path, canvas_csv: Path, silent
):
    from ecs101.cli import discover_csvs

    found = discover_csvs(lecture_csv.parent, output_fn=silent)
    assert found == [lecture_csv]


def test_a_non_poll_csv_named_directly_gets_a_clear_error(canvas_csv: Path):
    with pytest.raises(ValueError, match="not a Poll Everywhere export"):
        parse_poll_everywhere_export(canvas_csv, "America/New_York")


def test_discover_finds_exports_in_dated_subfolders(tmp_path: Path, lecture_csv: Path, silent):
    """The README's polls/<date>/ layout must actually be importable."""
    from ecs101.cli import discover_csvs

    polls = tmp_path / "polls"
    (polls / "2026-08-26").mkdir(parents=True)
    nested = polls / "2026-08-26" / "Lecture2.csv"
    nested.write_text(lecture_csv.read_text(encoding="utf-8"), encoding="utf-8")

    assert discover_csvs(polls, output_fn=silent) == [nested]


def test_a_top_level_export_wins_over_subfolders(tmp_path: Path, lecture_csv: Path, silent):
    """Pointing at one lecture's own folder must not pull in its neighbours."""
    from ecs101.cli import discover_csvs

    day = tmp_path / "2026-08-26"
    (day / "archive").mkdir(parents=True)
    top = day / "Lecture2.csv"
    top.write_text(lecture_csv.read_text(encoding="utf-8"), encoding="utf-8")
    buried = day / "archive" / "OldLecture.csv"
    buried.write_text(lecture_csv.read_text(encoding="utf-8"), encoding="utf-8")

    assert discover_csvs(day, output_fn=silent) == [top]


def test_discover_ignores_backups_and_dry_run_previews(
    tmp_path: Path, lecture_csv: Path, canvas_csv: Path
):
    """The backup folder and preview CSVs are the importer's own output."""
    from ecs101.cli import discover_csvs

    root = tmp_path / "course"
    export = root / "polls" / "2026-08-26" / "Lecture2.csv"
    export.parent.mkdir(parents=True)
    export.write_text(lecture_csv.read_text(encoding="utf-8"), encoding="utf-8")
    (root / "rosters").mkdir()
    (root / "rosters" / "canvas.csv").write_text(canvas_csv.read_text(encoding="utf-8"),
                                                 encoding="utf-8")
    snapshot = root / "backups" / "20260916-152332"
    snapshot.mkdir(parents=True)
    (snapshot / "responses.csv").write_text("Date,Question ID\n", encoding="utf-8")
    (root / "preview").mkdir()
    (root / "preview" / "responses_preview.csv").write_text("Date\n", encoding="utf-8")

    printed = []
    found = discover_csvs(root, output_fn=printed.append, ignore_dirs=[root / "backups"])

    assert found == [export]
    assert [line for line in printed if line.startswith("Skipped")] == [
        "Skipped 1 CSV file(s) that are not Poll Everywhere exports: canvas.csv"
    ]


def test_a_missing_roster_path_fails_before_any_prompt(tmp_path: Path, lecture_csv: Path, capsys):
    """A wrong --roster-csv must not cost the TA a full round of scoring prompts."""
    from ecs101.cli import main

    def explode(*args, **kwargs):  # pragma: no cover - must never be reached
        raise AssertionError("the TA was prompted before the roster was checked")

    import ecs101.cli as cli
    original = cli.configure_daily_scoring
    cli.configure_daily_scoring = explode
    try:
        code = main([
            str(lecture_csv), "--dry-run",
            "--roster-csv", str(tmp_path / "nope.csv"),
            "--output-dir", str(tmp_path / "out"),
        ])
    finally:
        cli.configure_daily_scoring = original

    assert code == 2
    assert "Canvas roster file not found" in capsys.readouterr().err
