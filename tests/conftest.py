"""Shared fixtures. Nothing here touches the network.

Every person in these fixtures is invented. The addresses use example.edu and
example.com, which RFC 2606 reserves so they can never belong to anyone. What
the fixtures preserve is the SHAPE of the real data the importer has to cope
with: hyphenated surnames, word-order variation, lowercase name particles,
diacritics, NetID versus first.last addresses, and one person holding several
addresses under several spellings.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ecs101.normalize import now_iso  # noqa: E402
from ecs101.parsers import parse_canvas_roster  # noqa: E402
from ecs101.records import build_updated_roster  # noqa: E402


LECTURE_HEADER = (
    "Response #,Started At (CDT),Participant First Name,Participant Last Name,"
    "Email,Custom Report ID,Screen Name,Public ID,"
    "Where is this photo taken?,When was plate tectonics first proposed?"
)

LECTURE_ROWS = [
    # first.last address: reconstructs to a name
    '1,8/26/26 12:27,Rowan,Fletcher,rowan.fletcher@example.edu,,Rowan F,481496,"Kamchatka, Russia",1960s',
    # NetID address: must not be guessed at
    '2,8/26/26 12:27,Imogen,Vance,iv517@example.edu,,Imogen V.,821333,"Kamchatka, Russia",1890s',
    '3,8/26/26 12:28,Noor,Haddad,noor.haddad@example.edu,,Noor H.,979052,"Hokkaido, Japan",1960s',
    # answered Q2 only, so Q1 must not create a response row for this person
    '4,8/26/26 12:31,Petra,Solano,petra.solano@example.edu,,Petra S.,681724,,1960s',
]


@pytest.fixture
def lecture_csv(tmp_path: Path) -> Path:
    path = tmp_path / "Lecture2_Kamchatka.csv"
    path.write_text("\n".join([LECTURE_HEADER, *LECTURE_ROWS]) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def legacy_csv(tmp_path: Path) -> Path:
    """Legacy export whose summary block uses HTML-escaped quotes.

    This mirrors the shape of the older Poll Everywhere export: the summary
    block escapes its quotes as &quot; while the Individual Results block uses
    real CSV quoting.
    """
    path = tmp_path / "Lecture1_Yellowstone.csv"
    path.write_text(
        "Results Summary\n"
        "\n"
        "Response,Count\n"
        "&quot;Snake River Canyon, Oregon&quot;,2\n"
        "&quot;Grand Canyon, Arizona&quot;,1\n"
        "Total,3\n"
        "\n"
        "\n"
        "Individual Results\n"
        "\n"
        "Response,Via,Screen name,Registered participant,Created At\n"
        '"Snake River Canyon, Oregon",pollev.com/x,Rowan F,Rowan Fletcher,2026-08-24 12:37:00\n'
        '"Snake River Canyon, Oregon",pollev.com/x,Noor H.,Noor Haddad,2026-08-24 12:37:20\n'
        '"Grand Canyon, Arizona",pollev.com/x,Imogen V.,Imogen Vance,2026-08-24 12:38:00\n',
        encoding="utf-8",
    )
    return path


@pytest.fixture
def canvas_csv(tmp_path: Path) -> Path:
    path = tmp_path / "canvas.csv"
    path.write_text(
        "Student,ID,SIS User ID,SIS Login ID,Section,Assignment 1\n"
        "    Points Possible,,,,,10\n"
        '"Fletcher, Rowan",100001,9000001,rowanf,ECS101-01,9\n'
        '"Vance, Imogen",100002,9000002,iv517,ECS101-01,8\n'
        '"Haddad, Noor",100003,9000003,noorh,ECS101-01,10\n'
        '"Solano, Petra",100004,9000004,petras,ECS101-01,7\n'
        '"Pham, Mai Thi Lan",100005,9000005,mp204,ECS101-01,6\n'
        '"Student, Test",999999,,,ECS101-01,\n',
        encoding="utf-8",
    )
    return path


@pytest.fixture
def roster_rows(canvas_csv: Path):
    students, _ = parse_canvas_roster(canvas_csv)
    rows, _ = build_updated_roster(students, [], now_iso())
    return rows


class ScriptedInput:
    """Feed a fixed sequence of answers to an interactive prompt."""

    def __init__(self, *answers: str):
        self.answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, prompt: str = "") -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError(f"unexpected extra prompt: {prompt!r}")
        return self.answers.pop(0)


@pytest.fixture
def scripted():
    return ScriptedInput


@pytest.fixture
def silent():
    def _sink(*args, **kwargs):
        return None
    return _sink
