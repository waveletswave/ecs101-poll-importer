"""Interactive setup before an import.

Two decisions are the TA's: which participants who started a lecture's poll on
another day count on its class date, and the one scored question per date.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set

from .models import OffDateParticipant, PollFile, effective_poll_responses
from .normalize import clean_space, normalize_answer_text

__all__ = [
    "prompt_correct_answer",
    "configure_daily_scoring",
    "day_poll_order",
    "count_answer_hits",
    "review_off_date_participants",
    "admit_off_date_participants",
]


def _display_time(timestamp: str) -> str:
    raw = clean_space(timestamp)
    if not raw:
        return "time unavailable"
    if "T" in raw:
        return raw.split("T", 1)[1][:8]
    if " " in raw:
        return raw.split(" ", 1)[1][:8]
    return raw


def count_answer_hits(poll: PollFile, answers: Set[str]) -> int:
    """How many effective responses would be scored correct by this answer set."""
    normalized = {normalize_answer_text(a) for a in answers}
    return sum(
        1 for r in effective_poll_responses(poll)
        if normalize_answer_text(r.response) in normalized
    )


def prompt_correct_answer(
    poll: PollFile,
    input_fn=input,
    output_fn=print,
) -> None:
    """Ask for the correct answer to the one scored question for a class date.

    The selection is rejected when it matches zero student responses. That
    single guard is what turns a silent all-zero class into a question the TA
    has to answer, and it catches any future normalization mismatch between how
    an option is displayed and how a response is stored, not just the one that
    was found in v2.3.1.
    """
    responses = effective_poll_responses(poll)
    output_fn("\n" + "-" * 72)
    output_fn(f"SCORED question for {poll.class_date}")
    output_fn(f"Question: {poll.question_name}")
    output_fn(f"Source: {poll.path.name}")
    output_fn(f"Responses: {len(responses)}")

    if not poll.summary:
        poll.summary = poll.answer_counts()

    if poll.summary:
        output_fn("\nObserved answer choices:")
        for i, (answer, count) in enumerate(poll.summary, start=1):
            output_fn(f"  {i}. {answer}  ({count})")
    else:
        output_fn("\nNo nonblank answers were observed for this question.")

    output_fn("\nEnter the correct option number.")
    output_fn("Multiple correct options: use commas, e.g. 1,3.")
    output_fn("Enter T to type the correct answer text manually.")

    while True:
        raw = clean_space(input_fn("Correct answer [number(s) / T]: "))

        candidate: Optional[Set[str]] = None
        if raw.casefold() == "t":
            manual = normalize_answer_text(input_fn("Correct answer text: "))
            if not manual:
                output_fn("Please enter nonblank answer text.")
                continue
            candidate = {manual}
        else:
            try:
                choices = [int(x.strip()) for x in raw.split(",") if x.strip()]
            except ValueError:
                choices = []
            if choices and poll.summary and all(1 <= x <= len(poll.summary) for x in choices):
                candidate = {poll.summary[x - 1][0] for x in sorted(set(choices))}

        if candidate is None:
            if poll.summary:
                output_fn(f"Please enter 1-{len(poll.summary)}, a comma-separated list, or T.")
            else:
                output_fn("Please enter T and type the correct answer text.")
            continue

        hits = count_answer_hits(poll, candidate)
        if hits == 0:
            output_fn("")
            output_fn("  !! WARNING: that answer matches ZERO student responses.")
            output_fn("     Every student would be scored 0 for this question.")
            output_fn(f"     Selected: {'; '.join(sorted(candidate))}")
            output_fn("     This usually means the option text and the stored response")
            output_fn("     text differ. Pick an option number from the list above, or")
            output_fn("     re-enter the text exactly as it appears there.")
            again = clean_space(input_fn("     Use it anyway? [y/N]: ")).casefold()
            if again not in {"y", "yes"}:
                continue

        poll.correct_answers = candidate
        output_fn(
            f"  -> Correct answer(s): {'; '.join(sorted(candidate))}  "
            f"({hits} of {len(responses)} responses correct)"
        )
        return


def day_poll_order(day_polls: Sequence[PollFile]) -> List[PollFile]:
    """Order a day's questions: lecture CSV column order, else response time."""
    if any(p.source_format == "lecture-wide" for p in day_polls):
        return sorted(
            day_polls,
            key=lambda p: (
                0 if p.source_format == "lecture-wide" else 1,
                p.question_order if p.source_format == "lecture-wide" else 10 ** 6,
                p.first_response_at,
                p.question_name.casefold(),
            ),
        )
    return sorted(day_polls, key=lambda p: (p.first_response_at, p.path.name.casefold()))


def configure_daily_scoring(
    polls: Sequence[PollFile],
    flagged_questions: Optional[Dict[str, str]] = None,
    input_fn=input,
    output_fn=print,
) -> None:
    """Choose exactly one scored question per class date."""
    flagged = flagged_questions or {}
    by_date: Dict[str, List[PollFile]] = {}
    for poll in polls:
        by_date.setdefault(poll.class_date, []).append(poll)

    for class_date in sorted(by_date):
        day_polls = day_poll_order(by_date[class_date])
        for poll in day_polls:
            poll.correct_answers = None
        selected = day_polls[0]
        is_lecture_wide = selected.source_format == "lecture-wide"

        output_fn("\n" + "=" * 72)
        output_fn(f"Daily scoring setup: {class_date}")
        output_fn("=" * 72)
        if is_lecture_wide:
            output_fn("Question order detected from lecture CSV columns:")
        else:
            output_fn("Legacy export: question order detected from response timestamps:")

        for i, poll in enumerate(day_polls, start=1):
            marker = "  [proposed scored question]" if i == 1 else "  [attendance only]"
            label = poll.question_name if is_lecture_wide else (
                f"{poll.path.name} ({_display_time(poll.first_response_at)})"
            )
            output_fn(f"  {i}. {label}{marker}")
            if poll.question_name in flagged:
                output_fn(f"     !! {flagged[poll.question_name]}")

        # A flagged question 1 means the column may not be a question at all, so
        # the confirmation stops defaulting to yes and an explicit answer is
        # required before anything gets scored on it.
        proposal_flagged = day_polls[0].question_name in flagged

        if len(day_polls) > 1:
            if proposal_flagged:
                output_fn(
                    "\nQuestion 1 does not look like a multiple-choice question. "
                    "Confirm explicitly before it is scored."
                )
                hint = "Use question 1 as today's scored question? [y/n]: "
                accept = {"y", "yes"}
            else:
                hint = "\nUse question 1 as today's scored question? [Y/n]: "
                accept = {"", "y", "yes"}

            while True:
                raw = clean_space(input_fn(hint)).casefold()
                if raw in accept:
                    break
                if raw in {"n", "no"}:
                    while True:
                        choice_raw = clean_space(input_fn(f"Scored question [1-{len(day_polls)}]: "))
                        try:
                            choice = int(choice_raw)
                        except ValueError:
                            choice = 0
                        if 1 <= choice <= len(day_polls):
                            selected = day_polls[choice - 1]
                            break
                        output_fn(f"Please enter a number from 1 to {len(day_polls)}.")
                    break
                output_fn("Please enter Y or N.")

        prompt_correct_answer(selected, input_fn=input_fn, output_fn=output_fn)

        attendance_only = [p for p in day_polls if p is not selected]
        if attendance_only:
            output_fn("\nAttendance-only question(s):")
            for poll in attendance_only:
                output_fn(f"  - {poll.question_name}")
        else:
            output_fn("\nNo additional attendance-only questions for this date.")


# ---------------------------------------------------------------------------
# Participants who started on another day
# ---------------------------------------------------------------------------

def _month_day(iso_date: str) -> str:
    try:
        d = datetime.fromisoformat(iso_date)
    except ValueError:
        return iso_date
    return f"{d.month}/{d.day}"


def _clock(iso: str) -> str:
    """'2026-09-21T13:52:00-04:00' -> '9/21 1:52 PM'."""
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    return f"{dt.month}/{dt.day} {dt.strftime('%I:%M %p').lstrip('0')}"


def admit_off_date_participants(
    polls: Sequence[PollFile],
    chosen: Sequence[OffDateParticipant],
) -> None:
    """File the chosen participants' answers under their export's class date.

    ``polls`` are the questions of one export. Each answer keeps the
    participant's real start time as its timestamp; only the date it counts
    toward changes.
    """
    by_order = {p.question_order: p for p in polls}
    for person in chosen:
        for order, response in person.responses.items():
            poll = by_order.get(order)
            if poll is not None:
                poll.responses.append(response)
    for poll in polls:
        poll.summary = poll.answer_counts()


def review_off_date_participants(
    polls: Sequence[PollFile],
    input_fn=input,
    output_fn=print,
) -> None:
    """Ask which participants who started on another day count on the class date.

    Poll Everywhere stamps each row of a lecture export with the time of its
    first answer. When one of a lecture's questions is opened in an earlier
    class, the answers given then start a row dated that earlier day. Most
    participants get a fresh row on the class date and count normally, and the
    review only mentions them. A participant with no such row has every answer
    under the earlier date, including any given on the class date itself. A
    row cannot be split by day, so the importer shows what each of them
    answered, marks the questions nobody else answered on that earlier day,
    and lets the TA decide. Enter counts nobody, as earlier releases did.
    """
    by_file: Dict[Path, List[PollFile]] = {}
    for poll in polls:
        by_file.setdefault(poll.path, []).append(poll)

    for path, file_polls in by_file.items():
        held = sorted(file_polls[0].off_date, key=lambda p: p.name.casefold())
        if not held:
            continue
        class_date = file_polls[0].class_date
        titles = {p.question_order: p.question_name for p in file_polls}
        counted = {r.poll_key for p in file_polls for r in p.responses}
        covered = [
            p for p in held if next(iter(p.responses.values())).poll_key in counted
        ]
        undecided = [p for p in held if p not in covered]

        output_fn("\n" + "=" * 72)
        output_fn(f"{path.name}: {len(held)} participant(s) started on another day")
        output_fn("=" * 72)
        if covered:
            output_fn(
                f"Already counted on {class_date} through a row started that day: "
                + ", ".join(p.name for p in covered)
            )
        if not undecided:
            file_polls[0].off_date.clear()
            continue

        output_fn(f"\nClass date: {class_date}. Poll Everywhere stamps each row with the")
        output_fn("time of its first answer, so every answer in the rows below carries")
        output_fn(f"the earlier day, even any given in class on {_month_day(class_date)}.")
        output_fn("")

        width = max(len(p.name) for p in undecided)
        for number, person in enumerate(undecided, start=1):
            answered = " ".join(f"Q{o}" for o in person.orders)
            output_fn(
                f"  {number}. {person.name:<{width}}  started {_clock(person.started_at)}"
                f"  answered {answered}"
            )
            peers = [
                q for q in held
                if q is not person and q.started_date == person.started_date
            ]
            if peers:
                seen = {o for q in peers for o in q.orders}
                only = [o for o in person.orders if o not in seen]
                if only:
                    output_fn(
                        f"     {' '.join(f'Q{o}' for o in only)}: nobody else who started "
                        f"on {_month_day(person.started_date)} answered "
                        + ("this" if len(only) == 1 else "these")
                    )

        output_fn("")
        for order in sorted({o for p in undecided for o in p.orders}):
            output_fn(f"  Q{order}  {titles.get(order, '')}")

        while True:
            raw = clean_space(input_fn(
                f"\nCount which of them as present on {class_date}? "
                "[numbers such as 2 or 1,3; Enter for none]: "
            ))
            if not raw:
                chosen: List[OffDateParticipant] = []
                break
            try:
                numbers = sorted({int(x) for x in re.split(r"[,\s]+", raw) if x})
            except ValueError:
                numbers = []
            if numbers and all(1 <= x <= len(undecided) for x in numbers):
                chosen = [undecided[x - 1] for x in numbers]
                break
            output_fn(
                f"Please enter numbers from 1 to {len(undecided)}, separated by commas, "
                "or press Enter for none."
            )

        admit_off_date_participants(file_polls, chosen)
        if chosen:
            output_fn(f"  -> Counted on {class_date}: {', '.join(p.name for p in chosen)}")
        else:
            output_fn(f"  -> Nobody added to {class_date}.")
        file_polls[0].off_date.clear()      # decided; never asked twice
