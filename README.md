# ECS101 Poll Everywhere Importer

A small Python tool for turning Poll Everywhere CSV exports into a shared Google Sheets tracker for **attendance, question scores, and the semester leaderboard**.

**Current version: v2.1.1**

## Workflow

```text
Canvas roster CSV ───────────────→ Roster
                                     ↓
Poll Everywhere CSVs → Participant matching → Responses
                                     ↓
                    Attendance / Attendance Review
                         Scores / Leaderboard
                                     ↓
                           shared Google Sheet
```

Canvas is the authoritative student roster. Poll Everywhere names are matched to Canvas students before they count toward attendance or scores.

## Core rules

- A student is **present** if they answered at least one imported Poll Everywhere question that day.
- For scored questions:
  - `1` = correct
  - `0` = incorrect
  - blank = no response
- Enter `S` for a poll that counts toward attendance but should not be scored.
- If a student submits multiple responses to one question, the latest response is used.
- Exact duplicate CSVs are detected by SHA-256 hash, even if renamed.
- Fuzzy name matching only suggests candidates. It never confirms a student automatically.
- Staff and guests can be saved as non-students.
- Students missing from a later Canvas roster are marked inactive rather than deleted.

## Google Sheet structure

### Canonical data

- **Roster** — authoritative Canvas roster
- **Participant Map** — persistent Poll Everywhere name → Canvas student mappings
- **Questions** — imported questions and correct answers
- **Responses** — one effective response per identity per question
- **Import Log** — imported-file history and duplicate protection

### Rebuilt views

- **Attendance** — one student per row, one class date per column
- **Attendance Review** — class summary, students with no matched Poll response, and unresolved identities
- **Scores** — `1`, `0`, or blank for each scored question
- **Leaderboard** — cumulative correct answers, questions answered, and accuracy

The original Poll Everywhere CSV files remain the raw archive.

## Installation

Python 3.10+ is recommended.

```bash
python -m pip install -r requirements.txt
```

Copy the example configuration:

```bash
cp config.example.json config.json
```

Add the shared Google Sheet ID to `config.json`:

```json
{
  "spreadsheet_id": "YOUR_GOOGLE_SHEET_ID",
  "credentials_file": "credentials.json",
  "authorized_user_file": "authorized_user.json"
}
```

The project uses a Google **Desktop OAuth client** with Google Sheets API access. Save its client file locally as `credentials.json`.

Test the connection:

```bash
python ecs101_poll_importer.py --check-google
```

The first authorization creates a local `authorized_user.json`.

## 1. Import or update the Canvas roster

Download the Canvas gradebook/roster CSV and run:

```bash
python ecs101_poll_importer.py --import-roster CanvasGrades.csv
```

Only roster-identifying fields are read. Assignment and grade columns are ignored.

The importer:

1. updates the authoritative roster;
2. automatically accepts unique exact name matches;
3. reuses previously confirmed mappings;
4. asks for human review when a Poll Everywhere name is ambiguous.

During review:

```text
1-5  choose a suggested Canvas student
N    mark as non-student / staff / guest
S    leave unresolved for now
```

Re-import an updated Canvas roster after add/drop. Students no longer listed are retained historically but marked inactive.

## 2. Import Poll Everywhere results after class

Put that day's Poll Everywhere CSVs in one folder:

```text
2026-08-27/
    question1.csv
    question2.csv
    question3.csv
```

Run:

```bash
python ecs101_poll_importer.py 2026-08-27
```

For each question, select its correct answer:

```text
Correct answer [number(s) / S]: 3
```

Use `1,3` for multiple accepted answers, or `S` for an unscored poll.

The Google Sheet is then updated automatically.

## Useful commands

Check Google access without changing data:

```bash
python ecs101_poll_importer.py --check-google
```

Rebuild Attendance, Attendance Review, Scores, and Leaderboard without importing new files:

```bash
python ecs101_poll_importer.py --refresh-views
```

Preview Poll CSV processing without writing to Google Sheets:

```bash
python ecs101_poll_importer.py /path/to/polls --dry-run
```

For a dry run with a Canvas roster:

```bash
python ecs101_poll_importer.py /path/to/polls --dry-run --roster-csv CanvasGrades.csv
```

Replace an existing question intentionally:

```bash
python ecs101_poll_importer.py /path/to/polls --replace
```

## Student identity and attendance review

Poll Everywhere participant names are resolved in this order:

1. saved `Participant Map` entry;
2. unique exact normalized-name match;
3. human-reviewed candidate suggestions.

Unresolved participants do **not** get assigned to a student automatically.

`Attendance Review` uses **No matched Poll response** rather than **Absent**, because the data can only establish that no matched Poll response was found. A student may have been present but not answered, had a technical issue, or still have an unresolved identity.

## Privacy

Do not commit course data or Google credentials to GitHub.

The repository `.gitignore` should exclude at least:

```text
credentials.json
authorized_user.json
config.json
*.csv
*.xlsx
output_preview*/
data/
```

Before pushing:

```bash
git status
```

Only code, documentation, and example configuration should be tracked.
