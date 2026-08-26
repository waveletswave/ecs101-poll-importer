"""ECS101 Poll Everywhere to Google Sheets importer.

Canvas roster CSV
        |
Authoritative Roster + persistent Participant Map
        |
Poll Everywhere CSVs
        |
parsing / identity matching / deduplication
        |
canonical tables:  Roster, Participant Map, Questions, Responses, Import Log
        |
rebuilt views:     Attendance, Attendance Review, Scores, Leaderboard

Course rules
------------
* Canvas is the authoritative source for enrolled students.
* Exactly one question per class date is scored: the first question in the
  lecture-wide CSV's column order. Every later question is attendance-only.
* Scored question: 1 = correct, 0 = incorrect, blank = unanswered.
* Attendance: an active Canvas student is present on a date if they answered
  any imported Poll Everywhere question that day.
* Fuzzy name similarity only ranks candidates for a human. It never confirms.

Module layout. Only ``sheets`` and ``pipeline`` touch Google; everything else
is pure data in, data out, which is what makes the test suite possible.
"""

VERSION = "3.0.0"

__all__ = ["VERSION"]
