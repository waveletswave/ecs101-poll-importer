"""Poll Everywhere identity to Canvas student matching.

Matching order, strictest first. Fuzzy similarity is used only to rank
candidates for a human; it never confirms a match on its own.

  1. a saved Participant Map entry (by e-mail key, then by name key)
  2. an exact Canvas login / e-mail match
  3. a 'first.last@example.edu' address whose reconstructed name matches exactly
     one active Canvas student
  4. a unique exact normalized-name match
  5. human review
"""

from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .models import PARTICIPANT_MAP_HEADERS, ResponseRow
from .normalize import (
    clean_space,
    email_local_part,
    name_from_email_local_part,
    normalize_email,
    normalize_person_name,
    now_iso,
    poll_identity_key,
)

__all__ = [
    "name_similarity",
    "roster_candidates",
    "RosterIndex",
    "make_map_row",
    "participant_map_index",
    "resolve_participants",
    "MatchJournal",
    "MATCH_TYPES",
]

MATCH_TYPES = ("saved", "login", "email-name", "exact", "confirmed", "non-student", "unresolved")

# Match types that carry a real Canvas student key.
_RESOLVED_TYPES = {"saved", "login", "email-name", "exact", "confirmed"}


# ---------------------------------------------------------------------------
# Similarity (ranking only)
# ---------------------------------------------------------------------------

def name_similarity(a: str, b: str) -> float:
    """Ranking heuristic for HUMAN REVIEW only; never used for auto-match.

    v2.3.1 gave a surname bonus only when the *last* token of both names
    agreed. Poll Everywhere lets students type their own display name, so
    'Lin Wei' against Canvas's 'Wei Lin' lost the bonus and could fall
    out of the candidate list entirely. Token overlap is now order-invariant
    and the surname bonus accepts a match at either end.
    """
    na, nb = normalize_person_name(a), normalize_person_name(b)
    if not na or not nb:
        return 0.0

    ratio = difflib.SequenceMatcher(None, na, nb).ratio()
    ta, tb = na.split(), nb.split()
    sa, sb = set(ta), set(tb)

    overlap = len(sa & sb) / max(len(sa | sb), 1)
    ends_a = {ta[0], ta[-1]}
    ends_b = {tb[0], tb[-1]}
    surname_bonus = 0.18 if ends_a & ends_b else 0.0
    subset_bonus = 0.12 if (sa <= sb or sb <= sa) else 0.0
    # Reward a shared rare token even when word order is completely different.
    token_bonus = 0.10 if len(sa & sb) >= 2 else 0.0

    return min(1.0, 0.60 * ratio + 0.25 * overlap + surname_bonus + subset_bonus + token_bonus)


def roster_candidates(
    poll_name: str,
    roster_rows: Sequence[Dict[str, str]],
    limit: int = 8,
) -> List[Tuple[float, Dict[str, str]]]:
    scored: List[Tuple[float, Dict[str, str]]] = []
    for row in roster_rows:
        name = clean_space(row.get("Student Name"))
        if not name:
            continue
        scored.append((name_similarity(poll_name, name), row))
    scored.sort(key=lambda x: (-x[0], x[1].get("Student Name", "").casefold()))
    return scored[:limit]


# ---------------------------------------------------------------------------
# Roster indexes
# ---------------------------------------------------------------------------

class RosterIndex:
    """Lookup structures over roster rows, built once per run."""

    def __init__(self, roster_rows: Sequence[Dict[str, str]]):
        self.rows = [dict(r) for r in roster_rows]
        self.by_key: Dict[str, Dict[str, str]] = {}
        self.by_name: Dict[str, Optional[Dict[str, str]]] = {}
        self.by_login: Dict[str, Optional[Dict[str, str]]] = {}
        self.by_canvas_id: Dict[str, Dict[str, str]] = {}

        for row in self.rows:
            key = clean_space(row.get("Student Key"))
            if not key:
                continue
            self.by_key[key] = row
            if key.startswith("canvas:"):
                self.by_canvas_id[key.split(":", 1)[1]] = row

            norm = normalize_person_name(row.get("Student Name"))
            if norm:
                self.by_name[norm] = None if norm in self.by_name else row

            for alias in self._login_aliases(row):
                self.by_login[alias] = None if alias in self.by_login else row

    @staticmethod
    def _login_aliases(row: Dict[str, str]) -> List[str]:
        """Every string this student could be identified by in an e-mail column.

        A purely numeric value is skipped: Canvas SIS User IDs are numbers and
        can never legitimately equal an e-mail local part, so letting them in
        would only create false matches.
        """
        aliases: List[str] = []
        raw = clean_space(row.get("Login"))
        for piece in raw.split("|"):
            value = clean_space(piece).casefold()
            if not value or value.isdigit():
                continue
            aliases.append(value)
            email = normalize_email(value)
            if email:
                local = email_local_part(email)
                if local and not local.isdigit():
                    aliases.append(local)
        return aliases

    def active_rows(self) -> List[Dict[str, str]]:
        return [
            r for r in self.rows
            if str(r.get("Active", "")).strip().upper() != "FALSE"
            and clean_space(r.get("Student Key"))
        ]

    def match_login(self, email: str) -> Optional[Dict[str, str]]:
        normalized = normalize_email(email)
        if not normalized:
            return None
        for probe in (normalized, email_local_part(normalized)):
            if not probe or probe.isdigit():
                continue
            hit = self.by_login.get(probe)
            if hit is not None:
                return hit
        return None

    def match_email_name(self, email: str) -> Optional[Dict[str, str]]:
        reconstructed = name_from_email_local_part(email)
        if not reconstructed:
            return None
        return self.by_name.get(reconstructed) or None

    def match_exact_name(self, poll_name: str) -> Optional[Dict[str, str]]:
        norm = normalize_person_name(poll_name)
        if not norm:
            return None
        return self.by_name.get(norm) or None

    def search(self, text: str, limit: int = 12) -> List[Dict[str, str]]:
        """Substring search over names and logins, for the interactive prompt."""
        needle = normalize_person_name(text)
        raw = clean_space(text).casefold()
        if not needle and not raw:
            return []
        hits: List[Dict[str, str]] = []
        for row in self.rows:
            name = normalize_person_name(row.get("Student Name"))
            login = clean_space(row.get("Login")).casefold()
            if (needle and needle in name) or (raw and raw in login):
                hits.append(row)
            if len(hits) >= limit:
                break
        return hits


# ---------------------------------------------------------------------------
# Participant map rows
# ---------------------------------------------------------------------------

def make_map_row(
    poll_name: str,
    email: str,
    student: Optional[Dict[str, str]],
    match_type: str,
    notes: str = "",
) -> Dict[str, str]:
    return {
        "Poll Key": poll_identity_key(poll_name, email),
        "Poll Participant": clean_space(poll_name),
        "Poll Email": normalize_email(email),
        "Student Key": "" if student is None else clean_space(student.get("Student Key")),
        "Student Name": "" if student is None else clean_space(student.get("Student Name")),
        "Match Type": match_type,
        "Updated At": now_iso(),
        "Notes": notes,
    }


def participant_map_index(rows: Sequence[Dict[str, str]]) -> Dict[str, Dict[str, str]]:
    """Index saved mappings by Poll Key, plus name and e-mail aliases.

    Pre-v3 sheets stored a bare normalized name as the Poll Key. Those rows are
    re-keyed to 'name:<normalized>' on read so an old map keeps working, and
    both the e-mail and name forms of every entry resolve to it.
    """
    idx: Dict[str, Dict[str, str]] = {}
    for raw in rows:
        row = dict(raw)
        participant = clean_space(row.get("Poll Participant"))
        email = normalize_email(row.get("Poll Email"))
        key = clean_space(row.get("Poll Key"))

        if not key:
            key = poll_identity_key(participant, email)
        elif ":" not in key:
            key = f"name:{normalize_person_name(key)}"

        if not key or key in (":", "name:", "email:"):
            continue

        row["Poll Key"] = key
        row["Poll Email"] = email
        idx[key] = row

        # Alias so a lookup by either identity form finds this entry.
        if email:
            idx.setdefault(f"email:{email}", row)
        name_key = normalize_person_name(participant)
        if name_key:
            idx.setdefault(f"name:{name_key}", row)
    return idx


def _canonical_map_rows(idx: Dict[str, Dict[str, str]]) -> List[Dict[str, str]]:
    """Collapse the alias index back to one row per stored Poll Key."""
    unique: Dict[str, Dict[str, str]] = {}
    for row in idx.values():
        key = clean_space(row.get("Poll Key"))
        if key:
            unique[key] = row
    return sorted(
        ({h: clean_space(r.get(h)) for h in PARTICIPANT_MAP_HEADERS} for r in unique.values()),
        key=lambda r: (r.get("Poll Participant", "").casefold(), r.get("Poll Key", "")),
    )


# ---------------------------------------------------------------------------
# Resume journal
# ---------------------------------------------------------------------------

class MatchJournal:
    """Append-only record of interactive matching decisions.

    Each confirmation is written the moment it is made, so an interrupted
    review resumes where it stopped instead of discarding twenty answers.
    """

    def __init__(self, path: Optional[Path]):
        self.path = Path(path) if path else None

    def load(self) -> Dict[str, Dict[str, str]]:
        if not self.path or not self.path.exists():
            return {}
        out: Dict[str, Dict[str, str]] = {}
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = clean_space(row.get("Poll Key"))
            if key:
                out[key] = row
        return out

    def append(self, row: Dict[str, str]) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def clear(self) -> None:
        if self.path and self.path.exists():
            self.path.unlink()


# ---------------------------------------------------------------------------
# Interactive review
# ---------------------------------------------------------------------------

def _print_candidates(candidates: Sequence[Tuple[float, Dict[str, str]]]) -> None:
    if not candidates:
        print("\n  (no similar Canvas names found)")
        return
    print("\nPossible Canvas students:")
    for i, (score, row) in enumerate(candidates, start=1):
        active = str(row.get("Active", "")).strip().upper() != "FALSE"
        login = clean_space(row.get("Login")).split("|")[0].strip()
        detail = f"{'active' if active else 'inactive'}, similarity {score:.2f}"
        if login:
            detail += f", {login}"
        print(f"  {i}. {clean_space(row.get('Student Name'))}  ({detail})")


def prompt_participant_match(
    poll_name: str,
    email: str,
    index: RosterIndex,
    input_fn=input,
) -> Dict[str, str]:
    print("\n" + "-" * 72)
    print("Participant match review")
    print(f"Poll Everywhere: {poll_name}")
    if normalize_email(email):
        print(f"E-mail:          {normalize_email(email)}")

    candidates = roster_candidates(poll_name, index.rows, limit=8)
    _print_candidates(candidates)

    print("\n  F <text>   search the roster by name or login")
    print("  #<id>      match by Canvas ID directly")
    print("  N          mark as non-student / staff / guest")
    print("  S          leave unresolved for now")

    while True:
        raw = clean_space(input_fn("Match [number / F text / #id / N / S]: "))
        lowered = raw.casefold()

        if lowered == "n":
            # Marking a real student non-student silently removes them from
            # attendance for the whole semester, and nothing ever asks again.
            # It is the one irreversible choice here, so it is confirmed.
            confirm = clean_space(input_fn(
                f"  Confirm {poll_name!r} is NOT an enrolled student? [y/N]: "
            )).casefold()
            if confirm in {"y", "yes"}:
                return make_map_row(poll_name, email, None, "non-student")
            print("  Cancelled.")
            continue

        if lowered == "s":
            return make_map_row(poll_name, email, None, "unresolved")

        if lowered.startswith("f"):
            needle = clean_space(raw[1:])
            if not needle:
                print("  Usage: F followed by part of a name or login, e.g. 'F pham'.")
                continue
            hits = index.search(needle)
            if not hits:
                print(f"  No roster entry matches {needle!r}.")
                continue
            candidates = [(name_similarity(poll_name, h.get("Student Name", "")), h) for h in hits]
            _print_candidates(candidates)
            continue

        if raw.startswith("#"):
            canvas_id = clean_space(raw[1:])
            hit = index.by_canvas_id.get(canvas_id)
            if hit is None:
                print(f"  No roster entry with Canvas ID {canvas_id!r}.")
                continue
            print(f"  -> {clean_space(hit.get('Student Name'))}")
            return make_map_row(poll_name, email, hit, "confirmed")

        try:
            choice = int(raw)
        except ValueError:
            choice = -1
        if 1 <= choice <= len(candidates):
            chosen = candidates[choice - 1][1]
            print(f"  -> {clean_space(chosen.get('Student Name'))}")
            return make_map_row(poll_name, email, chosen, "confirmed")

        print("  Please choose a candidate number, F <text>, #<canvas id>, N, or S.")


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def resolve_participants(
    identities: Iterable[ResponseRow] | Iterable[Tuple[str, str]],
    roster_rows: Sequence[Dict[str, str]],
    participant_map_rows: Sequence[Dict[str, str]],
    interactive: bool = True,
    journal: Optional[MatchJournal] = None,
    input_fn=input,
    output_fn=print,
) -> Tuple[Dict[str, Dict[str, str]], List[Dict[str, str]], Dict[str, int]]:
    """Resolve Poll Everywhere identities to Canvas students.

    ``identities`` accepts ResponseRow objects or (poll_name, email) pairs.
    Returns (index, canonical rows, stats). The index is keyed by Poll Key and
    also carries name/e-mail aliases, so a caller can look an identity up by
    whichever form it has.
    """
    index = RosterIndex(roster_rows)
    map_idx = participant_map_index(participant_map_rows)
    stats: Dict[str, int] = {}

    def bump(name: str) -> None:
        stats[name] = stats.get(name, 0) + 1

    if journal:
        for key, row in journal.load().items():
            map_idx[key] = row
            bump("resumed")
        if stats.get("resumed"):
            output_fn(
                f"\nResumed {stats['resumed']} confirmation(s) from an interrupted run."
            )

    pairs: List[Tuple[str, str]] = []
    for item in identities:
        if isinstance(item, ResponseRow):
            pairs.append((item.student_name, item.normalized_email))
        else:
            name, email = item
            pairs.append((clean_space(name), normalize_email(email)))

    seen: set = set()
    unique_pairs: List[Tuple[str, str]] = []
    for name, email in sorted(pairs, key=lambda p: (p[0].casefold(), p[1])):
        key = poll_identity_key(name, email)
        if not key or key in seen:
            continue
        seen.add(key)
        unique_pairs.append((name, email))

    for poll_name, email in unique_pairs:
        pkey = poll_identity_key(poll_name, email)
        name_key = f"name:{normalize_person_name(poll_name)}"

        # 1. saved mapping, by e-mail key then by name key
        existing = map_idx.get(pkey) or map_idx.get(name_key)
        if existing:
            mtype = clean_space(existing.get("Match Type"))
            skey = clean_space(existing.get("Student Key"))
            if mtype == "non-student":
                map_idx[pkey] = existing
                bump("saved_nonstudent")
                continue
            if skey and skey in index.by_key:
                refreshed = dict(existing)
                refreshed["Poll Key"] = pkey
                refreshed["Poll Participant"] = clean_space(poll_name)
                refreshed["Poll Email"] = email
                refreshed["Student Name"] = clean_space(
                    index.by_key[skey].get("Student Name")
                )
                map_idx[pkey] = refreshed
                if name_key != pkey:
                    map_idx.setdefault(name_key, refreshed)
                bump("saved")
                continue
            if mtype != "unresolved" and skey:
                output_fn(
                    f"\nSaved mapping for {poll_name!r} points to a student no "
                    "longer present in the stored roster. Reviewing again."
                )

        # 2. exact Canvas login / e-mail match
        hit = index.match_login(email)
        if hit is not None:
            row = make_map_row(poll_name, email, hit, "login")
            map_idx[pkey] = row
            bump("login")
            continue

        # 3. first.last@ address reconstructed to a unique Canvas name
        hit = index.match_email_name(email)
        if hit is not None:
            row = make_map_row(poll_name, email, hit, "email-name")
            map_idx[pkey] = row
            bump("email_name")
            continue

        # 4. unique exact normalized-name match
        hit = index.match_exact_name(poll_name)
        if hit is not None:
            row = make_map_row(poll_name, email, hit, "exact")
            map_idx[pkey] = row
            bump("exact")
            continue

        # 5. human review
        if interactive:
            row = prompt_participant_match(poll_name, email, index, input_fn=input_fn)
            map_idx[pkey] = row
            if journal:
                journal.append(row)
            mtype = row["Match Type"]
            bump({"confirmed": "confirmed", "non-student": "nonstudent"}.get(mtype, "unresolved"))
        else:
            row = make_map_row(poll_name, email, None, "unresolved")
            map_idx[pkey] = row
            bump("unresolved")

    return map_idx, _canonical_map_rows(map_idx), stats


def lookup_mapping(
    mapping: Dict[str, Dict[str, str]],
    poll_name: str,
    email: str = "",
) -> Dict[str, str]:
    """Find a mapping entry by e-mail key, then by name key."""
    pkey = poll_identity_key(poll_name, email)
    name_key = f"name:{normalize_person_name(poll_name)}"
    return mapping.get(pkey) or mapping.get(name_key) or {}
