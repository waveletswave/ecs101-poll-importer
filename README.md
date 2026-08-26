# ECS101 Poll Everywhere Importer

A small Python tool for maintaining the ECS101 **attendance, daily trivia score, and leaderboard** in a shared Google Sheet.

**Current version: v2.3.1**

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

Canvas is the authoritative roster. Poll Everywhere participant names are matched to Canvas students before they count toward attendance or scores.

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
2. propose question 1 as the scored question;
3. ask the TA to confirm it;
4. ask for the correct answer to question 1;
5. treat all later questions as attendance-only;
6. match participants to the Canvas roster; and
7. update the shared Google Sheet.

## Canvas roster

Import or refresh the authoritative roster with:

```bash
python ecs101_poll_importer.py --import-roster rosters/final_canvas.csv
```

Name matching follows this order:

1. reuse a saved `Participant Map` entry;
2. accept a unique exact normalized-name match;
3. ask for human review when the identity is ambiguous.

Fuzzy matching only suggests candidates. It never confirms a match automatically.

During review:

```text
1-5  choose a Canvas student
N    mark as non-student / staff / guest
S    leave unresolved for now
```

Confirmed mappings are saved for future imports. Students missing from a later Canvas roster are marked inactive rather than deleted.

## Google Sheet structure

### Canonical data

- **Roster** — Canvas student roster
- **Participant Map** — persistent Poll Everywhere → Canvas identity mappings
- **Questions** — question metadata, order, scoring status, and correct answer
- **Responses** — one effective response per identity per question
- **Import Log** — import history and duplicate protection

### Rebuilt views

- **Attendance** — `P` if the student answered any question that day
- **Attendance Review** — students with no matched Poll response plus unresolved identities
- **Scores** — first-question score for each class date
- **Leaderboard** — cumulative correct answers, scored questions answered, and accuracy

`Attendance Review` deliberately uses **No matched Poll response** rather than **Absent**, because the Poll data alone cannot prove physical absence.

## Useful commands

Check Google access without changing data:

```bash
python ecs101_poll_importer.py --check-google
```

Dry run without writing to Google Sheets:

```bash
python ecs101_poll_importer.py polls/2026-08-26/Lecture2_Kamchatka.csv --dry-run
```

Dry run with a Canvas roster:

```bash
python ecs101_poll_importer.py polls/2026-08-26/Lecture2_Kamchatka.csv \
  --dry-run \
  --roster-csv rosters/2026-08-25_canvas.csv
```

Rebuild derived views from existing Google Sheet data:

```bash
python ecs101_poll_importer.py --refresh-views
```

Replace a corrected question intentionally:

```bash
python ecs101_poll_importer.py /path/to/export.csv --replace
```

## Setup

Python 3.10+ is recommended.

```bash
python -m pip install -r requirements.txt
cp config.example.json config.json
```

`config.json` should contain the shared spreadsheet ID and local OAuth filenames. The project uses a Google Desktop OAuth client with Google Sheets API access.

## Privacy

Do not commit student data or Google credentials to GitHub. The repository should exclude at least:

```text
credentials.json
authorized_user.json
config.json
*.csv
*.xlsx
polls/
rosters/
output_preview*/
data/
```

Before pushing changes:

```bash
git status
```

Only code, documentation, dependency files, and example configuration should be tracked.
