"""Normalization: names, answers, e-mail, timestamps."""

from __future__ import annotations

from datetime import timedelta

import pytest

from ecs101.normalize import (
    canvas_display_name,
    clean_space,
    name_from_email_local_part,
    normalize_answer_text,
    normalize_email,
    normalize_person_name,
    parse_timestamp,
    poll_identity_key,
    timestamp_sort_key,
    tz_from_header_label,
)


@pytest.mark.parametrize("raw,expected", [
    ("  Alina   Ashworth ", "Alina Ashworth"),
    ("Alina Ashworth", "Alina Ashworth"),
    ("Alina\tAshworth\n", "Alina Ashworth"),
    (None, ""),
])
def test_clean_space(raw, expected):
    assert clean_space(raw) == expected


@pytest.mark.parametrize("a,b", [
    ("Adrián Solís", "Adrian Solis"),
    ("Mai Thi Lan (Bea) Pham", "Mai Thi Lan Bea Pham"),
    ("willem van doorn", "Willem van Doorn"),
    ("Willem van Doorn", "Willem  van  Doorn"),
])
def test_names_that_must_normalize_together(a, b):
    assert normalize_person_name(a) == normalize_person_name(b)


def test_names_that_must_stay_apart():
    assert normalize_person_name("Pham Pham") != normalize_person_name("Lan Pham")


def test_canvas_display_name_reorders():
    assert canvas_display_name("Pham, Mai Thi Lan") == "Mai Thi Lan Pham"
    assert canvas_display_name("Cher") == "Cher"


@pytest.mark.parametrize("raw,expected", [
    ("&quot;Snake River Canyon, Oregon&quot;", "Snake River Canyon, Oregon"),
    ('"Grand Canyon, Arizona"', "Grand Canyon, Arizona"),
    ("Yellowstone  caldera", "Yellowstone caldera"),
    ("At the mid-ocean ridge", "At the mid-ocean ridge"),
    ("0&deg;", "0°"),
    ("", ""),
])
def test_normalize_answer_text(raw, expected):
    assert normalize_answer_text(raw) == expected


def test_answer_normalization_is_idempotent():
    once = normalize_answer_text("&quot;A,  B&quot;")
    assert normalize_answer_text(once) == once


@pytest.mark.parametrize("raw,expected", [
    ("Rowan.Fletcher@Example.EDU", "rowan.fletcher@example.edu"),
    ("  noor.haddad@example.edu ", "noor.haddad@example.edu"),
    ("not-an-email", ""),
    ("two@@example.edu", ""),
    ("", ""),
])
def test_normalize_email(raw, expected):
    assert normalize_email(raw) == expected


@pytest.mark.parametrize("email,expected", [
    ("rowan.fletcher@example.edu", "rowan fletcher"),
    ("nadia.cruz.mora@example.edu", "nadia cruz mora"),
    ("iv517@example.edu", ""),          # NetID, no dot
    ("r.okafor@example.edu", ""),        # initial + surname, not a full name
    ("tkm82@example.edu", ""),
    ("bexbadger7@example.com", ""),
])
def test_name_from_email_local_part_never_guesses(email, expected):
    assert name_from_email_local_part(email) == expected


def test_poll_identity_key_prefers_email():
    # The same person changing their display name keeps one key.
    assert (poll_identity_key("Lan P.", "lan.ph@example.edu")
            == poll_identity_key("Mai Thi Lan Pham", "lan.ph@example.edu"))
    # Without an e-mail the name is the key.
    assert poll_identity_key("Lan P.", "") == "name:lan p"


@pytest.mark.parametrize("header,label,offset", [
    ("Started At (CDT)", "CDT", timedelta(hours=-5)),
    ("Started At (EDT)", "EDT", timedelta(hours=-4)),
    ("Started At (UTC)", "UTC", timedelta(0)),
    ("Started At", None, None),
    ("Started At (Mars/Olympus)", "MARS/OLYMPUS", None),
])
def test_tz_from_header_label(header, label, offset):
    assert tz_from_header_label(header) == (label, offset)


def test_cdt_export_becomes_correct_eastern_class_time():
    """The real ECS101 case: 12:27 CDT is 13:27 Eastern, two minutes into class."""
    dt = parse_timestamp("8/26/26 12:27", timedelta(hours=-5), "America/New_York")
    assert dt.isoformat(timespec="seconds") == "2026-08-26T13:27:00-04:00"
    assert dt.date().isoformat() == "2026-08-26"


def test_timestamp_without_a_label_is_read_as_course_local():
    dt = parse_timestamp("2026-08-24 12:37:00", None, "America/New_York")
    assert dt.isoformat(timespec="seconds") == "2026-08-24T12:37:00-04:00"


def test_a_utc_export_would_still_land_on_the_right_date():
    dt = parse_timestamp("8/26/26 17:27", timedelta(0), "America/New_York")
    assert dt.date().isoformat() == "2026-08-26"


def test_unparseable_timestamp_names_the_value():
    with pytest.raises(ValueError, match="unrecognized timestamp"):
        parse_timestamp("yesterday afternoon", None, "America/New_York")


def test_timestamp_ordering_survives_mixed_formats():
    """A sheet can hold legacy and v3 timestamps at once; string order lies."""
    legacy = "2026-08-26 09:00:00"
    modern = "2026-08-26T13:27:00-04:00"
    assert timestamp_sort_key(modern) > timestamp_sort_key(legacy)
    # Naive string comparison gets this one right, but not this one:
    early_modern = "2026-08-26T08:00:00-04:00"
    late_legacy = "2026-08-26 09:00:00"
    assert timestamp_sort_key(late_legacy) > timestamp_sort_key(early_modern)
    assert not (late_legacy > early_modern)  # the v2.3.1 comparison disagreed


def test_blank_timestamp_sorts_below_everything():
    assert timestamp_sort_key("") < timestamp_sort_key("2020-01-01T00:00:00-05:00")
