"""Participant matching: the auto-match cascade, review, and the resume journal."""

from __future__ import annotations

from pathlib import Path

import pytest

from ecs101.identity import (
    MatchJournal,
    RosterIndex,
    lookup_mapping,
    name_similarity,
    participant_map_index,
    resolve_participants,
    roster_candidates,
)
from ecs101.models import ResponseRow


def _row(name, email="", response="Kamchatka", at="2026-08-26T13:27:00-04:00"):
    return ResponseRow(
        response=response, via="", screen_name="", registered_participant=name,
        created_at=at, email=email,
    )


# ---------------------------------------------------------------------------
# Similarity ranking
# ---------------------------------------------------------------------------

def test_similarity_is_order_invariant():
    """v2.3.1 only rewarded a surname in last position."""
    forward = name_similarity("Wei Lin", "Wei Lin")
    reversed_ = name_similarity("Lin Wei", "Wei Lin")
    assert reversed_ > 0.7
    assert forward == pytest.approx(1.0)


def test_similarity_ranks_the_right_student_first(roster_rows):
    ranked = roster_candidates("Thi Lan Pham Mai", roster_rows, limit=3)
    assert ranked[0][1]["Student Name"] == "Mai Thi Lan Pham"


def test_similarity_ignores_diacritics():
    assert name_similarity("Adrián Solís", "Adrian Solis") > 0.8


# ---------------------------------------------------------------------------
# Auto-match cascade
# ---------------------------------------------------------------------------

def test_login_match_wins_for_a_netid_address(roster_rows, silent):
    mapping, _, stats = resolve_participants(
        [_row("Imogen V.", "iv517@example.edu")], roster_rows, [],
        interactive=False, output_fn=silent,
    )
    hit = lookup_mapping(mapping, "Imogen V.", "iv517@example.edu")
    assert hit["Student Key"] == "canvas:100002"
    assert stats.get("login") == 1


def test_first_dot_last_address_matches_by_reconstructed_name(roster_rows, silent):
    mapping, _, stats = resolve_participants(
        [_row("R. Fletcher", "rowan.fletcher@example.edu")], roster_rows, [],
        interactive=False, output_fn=silent,
    )
    hit = lookup_mapping(mapping, "R. Fletcher", "rowan.fletcher@example.edu")
    assert hit["Student Key"] == "canvas:100001"
    assert stats.get("email_name") == 1


def test_exact_name_match_without_any_email(roster_rows, silent):
    mapping, _, stats = resolve_participants(
        [_row("Noor Haddad", "")], roster_rows, [], interactive=False, output_fn=silent,
    )
    assert lookup_mapping(mapping, "Noor Haddad", "")["Student Key"] == "canvas:100003"
    assert stats.get("exact") == 1


def test_unknown_gmail_address_is_left_for_a_human(roster_rows, silent):
    mapping, _, stats = resolve_participants(
        [_row("Thi Lan Pham Mai", "mailanpham@example.com")], roster_rows, [],
        interactive=False, output_fn=silent,
    )
    hit = lookup_mapping(mapping, "Thi Lan Pham Mai", "mailanpham@example.com")
    assert hit["Student Key"] == ""
    assert stats.get("unresolved") == 1


def test_numeric_sis_id_never_matches_an_email(roster_rows, silent):
    """SIS User IDs are stored but must not create matches."""
    mapping, _, _ = resolve_participants(
        [_row("Someone", "9000002@example.edu")], roster_rows, [],
        interactive=False, output_fn=silent,
    )
    assert lookup_mapping(mapping, "Someone", "9000002@example.edu")["Student Key"] == ""


def test_fuzzy_similarity_never_auto_confirms(roster_rows, silent):
    """'Rowin Fletchr' is close to 'Rowan Fletcher' but must still ask a human."""
    mapping, _, stats = resolve_participants(
        [_row("Rowin Fletchr", "")], roster_rows, [], interactive=False, output_fn=silent,
    )
    assert lookup_mapping(mapping, "Rowin Fletchr", "")["Student Key"] == ""
    assert stats.get("unresolved") == 1


# ---------------------------------------------------------------------------
# Saved mappings
# ---------------------------------------------------------------------------

def test_a_saved_mapping_is_reused_without_prompting(roster_rows, silent):
    saved = [{
        "Poll Key": "email:mystery@example.com",
        "Poll Participant": "Mystery Person",
        "Poll Email": "mystery@example.com",
        "Student Key": "canvas:100004",
        "Student Name": "Petra Solano",
        "Match Type": "confirmed",
    }]
    mapping, _, stats = resolve_participants(
        [_row("Mystery Person", "mystery@example.com")], roster_rows, saved,
        interactive=False, output_fn=silent,
    )
    assert lookup_mapping(mapping, "Mystery Person", "mystery@example.com")["Student Key"] == "canvas:100004"
    assert stats.get("saved") == 1


def test_a_display_name_change_keeps_the_saved_mapping(roster_rows, silent):
    """The reason e-mail is the identity key rather than the name."""
    saved = [{
        "Poll Key": "email:mystery@example.com",
        "Poll Participant": "Old Nickname",
        "Poll Email": "mystery@example.com",
        "Student Key": "canvas:100004",
        "Student Name": "Petra Solano",
        "Match Type": "confirmed",
    }]
    mapping, _, stats = resolve_participants(
        [_row("Brand New Nickname", "mystery@example.com")], roster_rows, saved,
        interactive=False, output_fn=silent,
    )
    assert stats.get("saved") == 1
    assert lookup_mapping(mapping, "Brand New Nickname", "mystery@example.com")["Student Key"] == "canvas:100004"


def test_a_pre_v3_name_keyed_map_still_works(roster_rows, silent):
    """Old sheets stored a bare normalized name in Poll Key."""
    legacy_map = [{
        "Poll Key": "mystery person",
        "Poll Participant": "Mystery Person",
        "Student Key": "canvas:100004",
        "Student Name": "Petra Solano",
        "Match Type": "confirmed",
    }]
    idx = participant_map_index(legacy_map)
    assert "name:mystery person" in idx

    mapping, _, stats = resolve_participants(
        [_row("Mystery Person", "")], roster_rows, legacy_map,
        interactive=False, output_fn=silent,
    )
    assert stats.get("saved") == 1


def test_a_saved_non_student_is_never_asked_about_again(roster_rows, silent):
    saved = [{
        "Poll Key": "name:prof marlowe", "Poll Participant": "Prof Marlowe",
        "Poll Email": "", "Student Key": "", "Student Name": "",
        "Match Type": "non-student",
    }]
    _, rows, stats = resolve_participants(
        [_row("Prof Marlowe", "")], roster_rows, saved,
        interactive=False, output_fn=silent,
    )
    assert stats.get("saved_nonstudent") == 1
    assert stats.get("unresolved") is None


def test_map_rows_have_one_entry_per_key(roster_rows, silent):
    """The alias index must not leak duplicate rows into the sheet."""
    _, rows, _ = resolve_participants(
        [_row("Rowan Fletcher", "rowan.fletcher@example.edu"), _row("Noor Haddad", "noor.haddad@example.edu")],
        roster_rows, [], interactive=False, output_fn=silent,
    )
    keys = [r["Poll Key"] for r in rows]
    assert len(keys) == len(set(keys)) == 2


# ---------------------------------------------------------------------------
# Roster index search
# ---------------------------------------------------------------------------

def test_search_finds_a_student_by_partial_name(roster_rows):
    index = RosterIndex(roster_rows)
    hits = index.search("pham")
    assert [h["Student Name"] for h in hits] == ["Mai Thi Lan Pham"]


def test_search_finds_a_student_by_login(roster_rows):
    index = RosterIndex(roster_rows)
    assert index.search("iv517")[0]["Student Name"] == "Imogen Vance"


def test_ambiguous_exact_name_refuses_to_auto_match():
    rows = [
        {"Student Key": "canvas:1", "Student Name": "Anh Pham", "Active": "TRUE", "Login": ""},
        {"Student Key": "canvas:2", "Student Name": "Anh Pham", "Active": "TRUE", "Login": ""},
    ]
    index = RosterIndex(rows)
    assert index.match_exact_name("Anh Pham") is None


# ---------------------------------------------------------------------------
# Interactive review
# ---------------------------------------------------------------------------

def test_direct_canvas_id_entry(roster_rows, scripted):
    from ecs101.identity import prompt_participant_match

    index = RosterIndex(roster_rows)
    row = prompt_participant_match("Whoever", "", index, input_fn=scripted("#100003"))
    assert row["Student Key"] == "canvas:100003"


def test_invalid_input_reprompts(roster_rows, scripted):
    from ecs101.identity import prompt_participant_match

    index = RosterIndex(roster_rows)
    answers = scripted("banana", "#999", "#100001")
    row = prompt_participant_match("Whoever", "", index, input_fn=answers)
    assert row["Student Key"] == "canvas:100001"


def test_non_student_confirmed(roster_rows, scripted):
    from ecs101.identity import prompt_participant_match

    index = RosterIndex(roster_rows)
    row = prompt_participant_match("AV Cart", "", index, input_fn=scripted("N", "y"))
    assert row["Match Type"] == "non-student"
    assert row["Student Key"] == ""


# ---------------------------------------------------------------------------
# Resume journal
# ---------------------------------------------------------------------------

def test_journal_round_trip(tmp_path: Path):
    journal = MatchJournal(tmp_path / "j.jsonl")
    journal.append({"Poll Key": "email:a@example.edu", "Student Key": "canvas:1",
                    "Match Type": "confirmed"})
    journal.append({"Poll Key": "email:b@example.edu", "Student Key": "canvas:2",
                    "Match Type": "confirmed"})
    loaded = journal.load()
    assert set(loaded) == {"email:a@example.edu", "email:b@example.edu"}
    journal.clear()
    assert journal.load() == {}


def test_an_interrupted_review_resumes(roster_rows, tmp_path: Path, silent, scripted):
    """Confirmations survive a crash between the prompt and the sheet write."""
    journal = MatchJournal(tmp_path / "j.jsonl")
    journal.append({
        "Poll Key": "email:mystery@example.com", "Poll Participant": "Mystery Person",
        "Poll Email": "mystery@example.com", "Student Key": "canvas:100004",
        "Student Name": "Petra Solano", "Match Type": "confirmed",
        "Updated At": "", "Notes": "",
    })
    mapping, _, stats = resolve_participants(
        [_row("Mystery Person", "mystery@example.com")], roster_rows, [],
        interactive=True, journal=journal,
        input_fn=scripted(),          # no prompt may happen
        output_fn=silent,
    )
    assert stats.get("resumed") == 1
    assert lookup_mapping(mapping, "Mystery Person", "mystery@example.com")["Student Key"] == "canvas:100004"
