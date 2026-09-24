"""Participants who started a lecture's poll on another day.

Poll Everywhere gives each participant one row per lecture export, stamped
with the time of their first answer. If one of a lecture's questions is opened
during an earlier class, everyone who answers it then carries that earlier date
on every answer in their row, including the ones they give on the class date.
The importer cannot split such a row by day, so it holds the row back and asks.

Every person here is invented; every address uses example.edu.
"""

from __future__ import annotations

from pathlib import Path

from ecs101.parsers import parse_poll_everywhere_export
from ecs101.scoring import review_off_date_participants

HEADER = (
    "Response #,Started At (CDT),Participant First Name,Participant Last Name,"
    "Email,Screen Name,Public ID,Where is this photo taken?,Which rock weathers slowest?,"
    "Which rock is your favorite?\n"
)


def lecture(tmp_path: Path) -> Path:
    """Class on 9/23. Question 2 was also opened near the end of the 9/21 class."""
    rows = [
        # Answered question 2 on 9/21 and nothing else.
        "1,9/21/26 12:47 PM,Odile,Fairbanks,odile.fairbanks@example.edu,Odile F,11,,Quartzite,\n",
        "2,9/21/26 12:48 PM,Rafferty,Quill,rafferty.quill@example.edu,Rafferty Q,12,,Quartzite,\n",
        # Started on 9/21 with question 2, then answered question 3, which only
        # the 9/23 class saw.
        "3,9/21/26 12:52 PM,Linnea,Stroud,linnea.stroud@example.edu,Linnea S,13,,Shale,Granite\n",
        # Everyone else started on 9/23, including Rafferty, whose answers
        # that day went into a fresh row.
        "4,9/23/26 12:29 PM,Rafferty,Quill,rafferty.quill@example.edu,Rafferty Q,12,Maine,,Basalt\n",
        "4,9/23/26 12:30 PM,Rowan,Fletcher,rowan.fletcher@example.edu,Rowan F,14,Maine,Quartzite,Basalt\n",
        "5,9/23/26 12:31 PM,Noor,Haddad,noor.haddad@example.edu,Noor H.,15,Maine,Shale,Granite\n",
        "6,9/23/26 12:31 PM,Petra,Solano,petra.solano@example.edu,Petra S.,16,Ireland,Quartzite,Basalt\n",
    ]
    path = tmp_path / "Lecture9.csv"
    path.write_text(HEADER + "".join(rows), encoding="utf-8")
    return path


def test_the_class_date_follows_the_majority_and_the_rest_are_held(tmp_path):
    polls, _ = parse_poll_everywhere_export(lecture(tmp_path), "America/New_York")
    assert {p.class_date for p in polls} == {"2026-09-23"}
    assert sorted(p.name for p in polls[0].off_date) == [
        "Linnea Stroud", "Odile Fairbanks", "Rafferty Quill",
    ]
    assert all(len(p.responses) <= 4 for p in polls), "held rows are not counted yet"


def test_the_review_shows_what_nobody_else_answered_that_day(tmp_path, scripted):
    polls, _ = parse_poll_everywhere_export(lecture(tmp_path), "America/New_York")
    printed = []
    review_off_date_participants(polls, input_fn=scripted(""), output_fn=printed.append)
    text = "\n".join(printed)

    # Rafferty also has a row started on 9/23, so he already counts and is
    # only mentioned. The other two are numbered alphabetically.
    assert "Already counted on 2026-09-23 through a row started that day: Rafferty Quill" in text
    assert "1. Linnea Stroud" in text and "started 9/21 1:52 PM" in text
    assert "2. Odile Fairbanks" in text and "  3. " not in text
    assert "Q3: nobody else who started on 9/21 answered this" in text
    assert text.count("nobody else") == 1, "Odile answered only Q2 and gets no note"


def test_nothing_is_asked_when_every_early_starter_already_counts(tmp_path, scripted, silent):
    path = tmp_path / "Lecture9.csv"
    path.write_text(
        HEADER
        + "1,9/21/26 12:48 PM,Rafferty,Quill,rafferty.quill@example.edu,Rafferty Q,12,,Quartzite,\n"
        + "2,9/23/26 12:29 PM,Rafferty,Quill,rafferty.quill@example.edu,Rafferty Q,12,Maine,,Basalt\n"
        + "3,9/23/26 12:30 PM,Rowan,Fletcher,rowan.fletcher@example.edu,Rowan F,14,Maine,Quartzite,Basalt\n",
        encoding="utf-8",
    )
    polls, _ = parse_poll_everywhere_export(path, "America/New_York")
    review_off_date_participants(polls, input_fn=scripted(), output_fn=silent)
    assert polls[0].off_date == []


def test_enter_counts_nobody(tmp_path, scripted, silent):
    polls, _ = parse_poll_everywhere_export(lecture(tmp_path), "America/New_York")
    before = [len(p.responses) for p in polls]
    review_off_date_participants(polls, input_fn=scripted(""), output_fn=silent)
    assert [len(p.responses) for p in polls] == before
    assert polls[0].off_date == []


def test_a_chosen_participant_counts_on_the_class_date(tmp_path, scripted, silent):
    polls, _ = parse_poll_everywhere_export(lecture(tmp_path), "America/New_York")
    review_off_date_participants(polls, input_fn=scripted("1"), output_fn=silent)

    by_question = {p.question_order: p for p in polls}
    linnea = [
        r for p in polls for r in p.responses if r.student_name == "Linnea Stroud"
    ]
    assert sorted(r.response for r in linnea) == ["Granite", "Shale"]
    assert all(p.class_date == "2026-09-23" for p in polls)
    # Her real start time is kept; only the date it counts toward changes.
    assert all(r.created_at.startswith("2026-09-21T13:52") for r in linnea)
    assert "Linnea Stroud" not in {r.student_name for r in by_question[1].responses}


def test_a_bad_answer_is_asked_again(tmp_path, scripted, silent):
    polls, _ = parse_poll_everywhere_export(lecture(tmp_path), "America/New_York")
    answers = scripted("3", "two", "2")
    review_off_date_participants(polls, input_fn=answers, output_fn=silent)
    added = {r.student_name for p in polls for r in p.responses}
    assert "Odile Fairbanks" in added
    assert "Linnea Stroud" not in added
    assert answers.answers == []


def test_no_question_is_asked_when_everyone_started_on_the_class_date(
    lecture_csv, scripted, silent
):
    polls, _ = parse_poll_everywhere_export(lecture_csv, "America/New_York")
    review_off_date_participants(polls, input_fn=scripted(), output_fn=silent)
