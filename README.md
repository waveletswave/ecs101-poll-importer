# ECS101 Poll Everywhere Importer

A small Python tool for turning Poll Everywhere CSV exports into an automatically maintained attendance and question-score tracker for Duke ECS101.

## Workflow

```text
Poll Everywhere CSV exports
            ↓
      Python importer
            ↓
  cleaned canonical records
            ↓
Roster / Questions / Responses / Import Log
            ↓
    automatically rebuilt
            ↓
Attendance / Scores / Leaderboard
            ↓
     shared Google Sheet
```

The original Poll Everywhere CSV files remain the raw source data. The Google Sheet stores cleaned response records and automatically generated course summaries.

## What it does

For each class meeting:

- **Attendance:** a student is marked present if they answered **any** imported Poll Everywhere question that day.
- **Question scores:**
  - `1` = answered correctly
  - `0` = answered incorrectly
  - blank = did not answer that question
- Each Poll Everywhere CSV is treated as one question.
- The TA selects the correct answer interactively because the current Poll Everywhere CSV exports do not identify the correct answer.
- Enter `S` for a poll that should count toward attendance but should **not** be scored.
- The script automatically rebuilds attendance, scores, and the semester leaderboard after each import.
- Exact duplicate CSV files are detected using a **SHA-256 file hash**, even if the file has been renamed.
- If a student submitted multiple responses to the same question, the latest response is used as the effective response.

## Google Sheet structure

The v2 importer separates canonical data from automatically generated views.

### Canonical tables

These are the source of truth used by the program.

#### `Roster`

One row per observed student.

| Student Key | Student Name | Active | Notes |
|---|---|---|---|

Students are automatically added when they first appear in Poll Everywhere data.

`Student Key` is currently generated from the registered participant name. The program does not attempt to guess whether differently spelled names belong to the same student.

#### `Questions`

One row per imported question.

Includes:

- Question ID
- class date
- question / source-file name
- available answer choices
- correct answer
- whether the question is scored
- source-file hash
- import timestamp

#### `Responses`

One row represents one student's **effective response to one question**.

Includes:

- class date
- Question ID
- student key and name
- Poll Everywhere screen name
- response
- correctness
- timestamp
- source file
- file hash

If a student submitted multiple responses to one question, only the latest response is stored here.

The original Poll Everywhere CSV remains the raw archive.

#### `Import Log`

Records completed imports, including:

- source file
- SHA-256 file hash
- Question ID
- number of imported responses
- import timestamp

This prevents the same CSV from accidentally being imported twice.

### Automatically generated views

These tables can be completely rebuilt from the canonical data.

#### `Attendance`

One row per student and one column per class date.

`P` means the student answered at least one Poll Everywhere question that day.

#### `Scores`

One row per student and one column per scored question.

Values are:

- `1` = correct
- `0` = incorrect
- blank = no response

The final columns summarize total correct answers, number of questions answered, and accuracy.

#### `Leaderboard`

Semester ranking based primarily on total correct answers.

It also reports questions answered and accuracy.

## Files in this repository

- `ecs101_poll_importer.py` — main program
- `requirements.txt` — Python dependency
- `config.example.json` — example local Google Sheet configuration
- `.gitignore` — prevents credentials and student data from being committed

Local OAuth credentials, real Poll Everywhere CSVs, and course records are **not** stored in GitHub.

## Installation

Python 3.10+ is recommended.

Install the dependency:

```bash
python -m pip install -r requirements.txt
```

## Google Sheets setup

The importer uses Google's standard OAuth browser authentication through `gspread`.

### One-time setup

1. Create or choose the shared ECS101 Google Sheet.
2. Enable the **Google Sheets API** in a Google Cloud project.
3. Configure the Google Auth consent screen.
4. Create a **Desktop app OAuth client**.
5. Download the OAuth client JSON as:

```text
credentials.json
```

6. Copy:

```text
config.example.json
```

to:

```text
config.json
```

and enter the shared spreadsheet ID.

Example:

```json
{
  "spreadsheet_id": "YOUR_GOOGLE_SHEET_ID",
  "credentials_file": "credentials.json",
  "authorized_user_file": "authorized_user.json"
}
```

7. Test the connection:

```bash
python ecs101_poll_importer.py --check-google
```

On the first run, a browser window opens for Google authorization.

The resulting local OAuth token is stored as:

```text
authorized_user.json
```

Do **not** commit or share this file. Each TA should authorize their own Google account.

## Normal class workflow

Put all Poll Everywhere CSV files from one class meeting into a folder:

```text
2026-08-26/
    question1.csv
    question2.csv
    question3.csv
```

Run:

```bash
python ecs101_poll_importer.py 2026-08-26
```

For each question, the importer displays the Poll Everywhere answer choices:

```text
Question 1 of 3
File: question1.csv

  1. Answer A
  2. Answer B
  3. Answer C
  4. Answer D

Correct answer [number(s) / S]: 3
```

Use:

- a number such as `3` for one correct answer
- `1,3` if multiple choices should be treated as correct
- `S` if the question should count for attendance but not scoring

After all questions are processed, the shared Google Sheet is updated automatically.

## Dry run

To inspect results without modifying the Google Sheet:

```bash
python ecs101_poll_importer.py /path/to/csvs --dry-run
```

The program writes preview files to `output_preview_v2/`:

- `roster_preview.csv`
- `questions_preview.csv`
- `responses_preview.csv`
- `import_log_preview.csv`
- `attendance_preview.csv`
- `scores_preview.csv`
- `leaderboard_preview.csv`

## Duplicate protection

Each source CSV is hashed using SHA-256.

If the exact same file has already been imported, the program skips it automatically even if the filename has changed.

Example:

```text
Exact duplicate file(s) already imported; skipped:
  - Lecture1_Yellowstone.csv [b31de62f8031]
```

## Replacing a question

Question IDs are based on:

```text
class date + CSV filename stem
```

If a corrected export needs to replace an existing question, run:

```bash
python ecs101_poll_importer.py /path/to/csvs --replace
```

Use `--replace` intentionally because it replaces the existing records for the matching Question ID.

## Student identity

The current Poll Everywhere exports provide a `Registered participant` field, which the importer uses as the student identity.

If that field is missing, the response is labeled:

```text
[UNREGISTERED] ...
```

rather than guessing the student's identity.

The `Roster` tab can later be used for human-reviewed identity cleanup if needed, especially during the add/drop period.

## v1.1 migration

If the Google Sheet was previously populated by v1.1, v2 can migrate the old `Raw Responses` data automatically.

On the first v2 run:

```text
Raw Responses
      ↓
Responses
```

and an `Import Log` is created from the existing imports.

The old `Raw Responses` tab is intentionally left untouched as a backup. After v2 has been verified with additional class meetings, it can be hidden or removed manually.

## Privacy and GitHub safety

Poll Everywhere exports may contain identifiable student information.

The repository therefore excludes:

- `credentials.json`
- `authorized_user.json`
- `config.json`
- Poll Everywhere CSV files
- generated preview CSV files
- other local course-data folders

Before every push, check:

```bash
git status
```

No student CSV, roster export, OAuth credential, or local configuration file should ever be staged for GitHub.

For this course workflow, the repository should remain private.