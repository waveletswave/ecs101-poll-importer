# ECS101 Poll Everywhere Importer

A Python tool for maintaining the ECS101 **attendance, daily trivia score, and leaderboard** in a shared Google Sheet.

**Current version: v3.0.0**

## Course rules

For each class meeting:

- **Attendance:** a student is present if they answered **any** Poll Everywhere question that day.
- **Trivia score:** only the **first question** is scored.
  - `1` = first question answered correctly
  - `0` = first question answered incorrectly
  - blank = first question not answered
- Later questions are attendance-only.

Poll Everywhere's current lecture export contains all questions from one lecture in a single CSV. The importer uses the exported question-column order, confirms the first question with the TA, and asks for its correct answer once.

The older one-question-per-CSV format used for Lecture 1 is still supported.

## Workflow

```text
Canvas roster CSV
      ↓
Authoritative Roster + Participant Map
      ↓
Poll Everywhere lecture CSV
      ↓
Q1 → score + attendance
Q2...Qn → attendance only
      ↓
Attendance / Attendance Review / Scores / Leaderboard
      ↓
shared Google Sheet
```

Canvas is the authoritative roster. Poll Everywhere participants are matched to Canvas students before they count toward attendance or scores.

## Normal weekly workflow

Save each lecture export under `polls/`, for example:

```text
polls/
├── 2026-08-24/
│   └── Lecture1_Yellowstone.csv
└── 2026-08-26/
    └── Lecture2_Kamchatka.csv
```

Import a lecture:

```bash
python ecs101_poll_importer.py polls/2026-08-26/Lecture2_Kamchatka.csv
```

The importer will:

1. detect the questions in their exported order;
2. check the response times against the scheduled class window;
3. propose question 1 as the scored question;
4. ask the TA to confirm it;
5. ask for the correct answer to question 1, and refuse an answer that matches no student response;
6. treat all later questions as attendance-only;
7. match participants to the Canvas roster, prompting only for identities it cannot resolve; and
8. update the shared Google Sheet in one batched write.

## Participant identity matching

Matching runs strictest first. Fuzzy name similarity only ranks candidates for a human. It never confirms a match on its own.

1. **Saved Participant Map entry.** Looked up by e-mail first, then by name.
2. **Canvas login or e-mail.** Requires a `SIS Login ID`, `Login ID` or `Email` column in the Canvas export. Purely numeric columns such as `SIS User ID` are stored but never used for matching, since a number cannot legitimately equal an e-mail local part.
3. **A `first.last@` address reconstructed to a name.** The address is turned back into a name and matched against the roster only when it decomposes cleanly. A NetID form such as `ab123@` or an initial form such as `a.surname@` yields nothing rather than a guess.
4. **Unique exact normalized name.** Diacritics stripped, punctuation collapsed. An ambiguous name never auto-matches.
5. **Human review.**

**E-mail is the identity key** where the export provides one. A student who changes their Poll Everywhere display name between lectures keeps the same key, so their saved mapping and their history follow them instead of becoming a new unresolved identity.

In practice most participants resolve with no human input at all: saved mappings carry over week to week, and an institutional `first.last@` address matches the roster exactly. What reaches a human is the genuinely ambiguous case, such as one student answering from several addresses under several name spellings.

During review:

```text
1-8        choose a Canvas student
F <text>   search the roster by name or login
#<id>      match by Canvas ID directly
N          mark as non-student / staff / guest (asks for confirmation)
S          leave unresolved for now
```

`N` is the one irreversible choice: a participant marked non-student is never asked about again and never counts toward attendance. It asks for confirmation for that reason. If the right student is not in the candidate list, use `F` or `#`, not `N`.

Confirmed mappings are saved for future imports. Every confirmation is also appended to a local journal file as it is made, so an interrupted review resumes where it stopped instead of discarding the answers already given.

Students missing from a later Canvas roster are marked inactive rather than deleted.

### Identities confirmed later are backfilled

When an identity is confirmed in a later week, the stored responses from earlier weeks are re-mapped in the same run. The student's earlier attendance and trivia score come back automatically. Nothing needs to be re-imported.

## Timezone handling

Poll Everywhere writes its export timezone into the column header, for example `Started At (CDT)`. The importer reads that label, converts to the course timezone, and stores timestamps with an explicit UTC offset.

The legacy single-question format states no timezone anywhere. Set `poll_export_timezone` in `config.json` to tell the importer what clock those files use.

Set `class_start` and `class_end` and every import is checked against them. If the median response time drifts outside the scheduled window, the importer says so and asks before continuing. This is what catches a changed Poll Everywhere account timezone, a mislabelled export, or a wrong `course_timezone`, any of which could otherwise file a lecture under a shifted clock or a shifted date without a word.

For ECS101 as configured (1:25 pm to 2:40 pm Eastern, exports in CDT), the one-hour offset never crosses midnight, so class dates were correct under v2.x as well. The check exists so that stays true.

## Google Sheet structure

### Canonical data

- **Roster** — Canvas student roster, including any login or e-mail alias
- **Participant Map** — persistent Poll Everywhere to Canvas identity mappings, keyed by e-mail where available
- **Questions** — question metadata, order, scoring status, and correct answer
- **Responses** — one effective response per identity per question
- **Import Log** — import history and duplicate protection

### Instructor input

- **Excused** — excused absences, entered by hand. The importer reads this tab
  and never writes to it.

### Rebuilt views

- **Attendance** — `P` if the student answered any question that day, `E` if
  the Excused tab records an excused absence
- **Attendance Review** — students with no matched Poll response, plus unresolved identities
- **Scores** — first-question score for each class date
- **Leaderboard** — cumulative correct answers, scored questions answered, and accuracy

`Attendance Review` deliberately says **No matched Poll response** rather than **Absent**, because the Poll data alone cannot prove physical absence. It lists only the students who need a look; the present ones are already in `Attendance`.

## Recording excused absences

Every rebuilt view is regenerated from scratch on each import, so an annotation
made directly on `Attendance` would be silently overwritten the next time the
importer runs. The `Excused` tab exists so that does not happen: it is the one
tab the importer reads but never writes.

It is created with a header row the first time the importer runs, and from then
on it belongs to the instructor. One row per excused absence:

```text
Date         Student Key      Student            Reason
2026-08-26   canvas:1487188   Imogen Vance       Varsity travel
2026-09-02   canvas:1160917   Colin Cashman      Illness
```

`Date` must be a class date that already has imported questions. Fill in either
`Student Key` or `Student`; giving both is safest, and the key wins if they
disagree. `Attendance Review` lists the date, key and name of everyone with no
matched response, which is the natural place to copy rows from. Any row the
importer cannot resolve is reported by row number at import time rather than
being quietly skipped.

The entries show up after the next import, or immediately by running:

```bash
python ecs101_poll_importer.py --refresh-views
```

In `Attendance`, an excused date shows `E`. `Classes Attended` stays a count of
classes actually attended and a separate `Excused` column counts the rest, so
how the two are weighted at the end of the semester remains a course decision.
A matched response always wins over an excused row: if the student answered,
they were there. `Scores` is deliberately unaffected, since an excused student
did not answer the scored question.

## Useful commands

Check Google access without changing data:

```bash
python ecs101_poll_importer.py --check-google
```

Dry run without writing to Google Sheets:

```bash
python ecs101_poll_importer.py polls/2026-08-26/Lecture2_Kamchatka.csv --dry-run
```

Dry run with a Canvas roster, no prompts at all:

```bash
python ecs101_poll_importer.py polls/2026-08-26/Lecture2_Kamchatka.csv \
  --dry-run --non-interactive \
  --roster-csv rosters/2026-08-25_canvas.csv
```

Rebuild derived views from existing Google Sheet data:

```bash
python ecs101_poll_importer.py --refresh-views
```

Re-apply the Participant Map to Responses after editing it by hand, then rebuild views:

```bash
python ecs101_poll_importer.py --remap-identities
```

`--refresh-views` rebuilds views only and never touches canonical data. `--remap-identities` is the one that rewrites `Responses`.

Replace a corrected question intentionally:

```bash
python ecs101_poll_importer.py /path/to/export.csv --replace
```

## Setup

Python 3.9 or newer. 3.10+ recommended.

```bash
python -m pip install -r requirements.txt
cp config.example.json config.json
```

### config.json

| Setting | Required | Meaning |
| --- | --- | --- |
| `spreadsheet_id` | yes | The long token in the Sheet's URL between `/d/` and `/edit` |
| `credentials_file` | no | Google Desktop OAuth client file. Default `credentials.json` |
| `authorized_user_file` | no | Cached OAuth token. Default `authorized_user.json` |
| `course_timezone` | no | IANA name for the course's own clock. Default `America/New_York` |
| `poll_export_timezone` | no | Timezone abbreviation for exports that do not label their own clock, e.g. `CDT`. A label in the file always wins |
| `class_start` / `class_end` | no | `HH:MM` in `course_timezone`. Set both or neither. Enables the timezone check |
| `backup_dir` | no | Where pre-write backups and resume journals go. Default `backups` |

Unknown settings are rejected by name, so a typo says what is wrong instead of surfacing as an OAuth error.

## When Google authorization expires

`invalid_grant: Token has been expired or revoked` means the cached
authorization is no longer valid. Delete the token file and run the command
again; a browser opens to reauthorize:

```bash
rm authorized_user.json
```

If it recurs weekly, the cause is the OAuth consent screen. A project set to
**External** with a publishing status of **Testing** issues refresh tokens that
expire after seven days, unless the only scopes requested are name, e-mail and
profile. This project requests the Sheets scope, so it is not exempt. Two ways
to remove the limit, in Google Cloud Console under APIs & Services then OAuth
consent screen:

- **Publish app** moves the status to *In production*. Sign-in then shows an
  "unverified app" warning that a handful of course staff can click past.
- **Internal** user type removes both the limit and the warning, but is only
  offered when the project sits inside a Google Workspace organization.

## Reliability

Every import backs the canonical tabs up to `backups/<timestamp>/` before writing anything.

All tabs are written together: the grids grow, then one `values.batchUpdate` writes every tab, then only the rows below the new data are cleared. A canonical tab is never empty at any point, and the whole import costs about four Google Sheets API calls rather than the roughly fifty-four that v2.x needed against a sixty-per-minute quota. Quota and transient errors are retried with backoff.

If a run does fail after writing began, the importer says so explicitly and points at the backup, rather than reporting that nothing was written.

## Development

```text
ecs101_poll_importer.py    entry point, unchanged interface
ecs101/
    normalize.py           text, name, answer, e-mail and timestamp normalization
    models.py              dataclasses and table schemas
    parsers.py             Canvas roster and both Poll Everywhere formats
    identity.py            participant matching and interactive review
    records.py             canonical record building, collapse, remap
    views.py               Attendance, Scores, Leaderboard, Attendance Review
    scoring.py             scored-question selection and the correct-answer guard
    sheets.py              the only module that imports gspread
    pipeline.py            orchestration
    cli.py                 argparse and dispatch
tools/compare_views.py     diff two view exports, for verifying an upgrade
tests/                     pytest, no network, no Google account
```

Only `sheets.py` and `pipeline.py` touch Google. Everything else takes plain data and returns plain data, which is what makes the tests possible.

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

`tools/compare_views.py` diffs two Attendance, Scores or Leaderboard CSV
exports and names the students whose records differ. Use it to see what an
upgrade or a re-import changes before trusting it:

```bash
python ecs101_poll_importer.py polls/ --dry-run \
    --roster-csv rosters/canvas.csv --output-dir preview
python tools/compare_views.py ~/Downloads/Attendance.csv preview/attendance_preview.csv
```

`tests/test_regressions.py` holds one test per defect found in the v2.3.1 review. Each of them fails against v2.3.1 and passes here. Run the suite before every change.

## Upgrading from v2.x

No manual migration is needed. The first run reads the existing sheet, upgrades every row in memory, and writes the new schema back.

- Participant Map rows keyed by a bare name are re-keyed and keep working.
- Response rows from v1 (`Student` column) and v2 (`Poll Participant` column) are both read.
- `Responses` drops the `Question` and `Source File` columns and stores a 12-character `File Hash` prefix. All three are reachable from `Questions` through `Question ID`, which already embeds the question text. On a full semester this removes roughly a third of what gets rewritten on every import.

**Recommended first step after upgrading.** Run `--import-roster` with your current Canvas CSV once. That reconciles every stored Poll identity against the roster and backfills any attendance that v2.x left unmatched.

## Privacy

Every person in `tests/` is invented, and the addresses use the `example.edu`
and `example.com` domains that RFC 2606 reserves. Real student names and
addresses must never enter the test fixtures, the docstrings or this README,
even as illustrative examples. The fixtures reproduce the *shapes* the importer
has to handle (hyphenated surnames, word-order variation, NetID versus
first.last addresses, one person with several addresses) using invented people.

Do not commit student data or Google credentials to GitHub. The repository excludes at least:

```text
credentials.json
authorized_user.json
config.json
*.csv
*.xlsx
polls/
rosters/
output_preview*/
backups/
data/
```

Before pushing changes:

```bash
git status
```

Only code, documentation, dependency files, and example configuration should be tracked.
