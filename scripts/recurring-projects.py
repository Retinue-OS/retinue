#!/usr/bin/env python3
"""Wake projects whose date has come — store-gated, chamber-agnostic, credit-free.

Two kinds of project rest until a date, and both are woken here.

**Standing cadences.** Some projects demand an action on a fixed cadence (a day
each month, a day each quarter) and otherwise rest. We model "resting" as
``paused: true`` in the project's Markdown frontmatter (the dashboard project
card hides paused projects, but they stay alive and queryable — unlike
``status: done``, which retires them for good). A recurring project carries:

    recurring: monthly | quarterly
    due_day:   <1..28>       # informational: day of period the action is due
    next_due:  YYYY-MM-DD     # the date it should wake up
    paused:    true           # resting between cadences

**One-off deadlines.** A project can also rest against a single future date — a
filing due in five months, a statutory deadline two years out. That is what
``expected_by`` already means, so no new vocabulary is needed; it just has to be
acted on. Such a project carries no ``recurring`` cadence:

    expected_by:    YYYY-MM-DD   # the date this wants attention
    remind_before:  3m           # optional lead time: 10 / 10d / 2w / 3m
    paused:         true

By default it wakes *on* ``expected_by``, which is what that field means for the
common case — a follow-up date, "check on 29 August whether this happened". A
real deadline needs acting on before it arrives, and says so with
``remind_before``: days (bare number or ``Nd``), weeks (``Nw``) or calendar
months (``Nm``). A date already in the past wakes on the next run rather than
being skipped, so nothing is lost when this is first deployed — or when the
container was down on the day.

Two exclusions keep finished work asleep, matching ``agent-self-review.py``:
``resolved: true`` and ``status: done``. A project that is merely ``paused`` is
resting, not done, and is exactly what this looks for.

Optionally, the reminder shown when either kind wakes:

    reminder_title:   <short line>
    reminder_message: <one paragraph>

Design — why this lives in the framework and gates on the store
---------------------------------------------------------------
This is the same shape as ``agent-self-review.py``: a scheduler ``command`` job
(so the scheduler spends **no Claude credits**) whose gate is a plain SPARQL
``SELECT`` against the life store (also free). "Which recurring project is due?"
is a store question, not a per-chamber filesystem scan — every chamber's project
frontmatter is already in the life store (one named graph per file), so one
query covers notes, operations, and any chamber added later. An empty result
does nothing beyond one HTTP round-trip.

The store is read-only (no SPARQL UPDATE), so the two halves split cleanly:

  * **Detect** (chamber-agnostic, free): the SELECT returns each candidate
    project together with its named graph ``?g`` — which is ``file:<path-
    relative-to-chambers-root>``, exactly the mapping the dashboard uses to
    resolve a project URI back to its source file.
  * **Reactivate** (only for the actual matches): resolve ``?g`` to the file,
    flip it back to active (``paused: false``, ``waiting_since: today``), and
    open one dashboard conversation with the project's own reminder text. The
    file's existing ``current_actor`` is left untouched — a resting project
    rests with its owner already set, so no owner name is baked into this code.
    No blind scan of any chamber.

Cadence (monthly vs quarterly) makes no difference to this gate — ``next_due``
drives everything. Advancing ``next_due`` to the following period happens when
the human marks the cadence done (via Ara), not here: so an overdue period keeps
the project active until it is actually handled, instead of silently skipping.

The deadline half of the gate deliberately carries **no date filter**: the wake
date is ``expected_by`` minus a lead time, and the lead lives in the file (the
store need not carry ``remind_before``, and no chamber has to teach its
converter a new key for this to work). So the SELECT returns every paused
project that has an ``expected_by`` at all — a handful, in practice — and the
per-file check decides. The alternative, a fixed look-ahead horizon in the
query, would silently wake anything with a longer lead too late.

De-duplication needs no state file: the gate requires ``paused: true``, and the
first reactivation flips that to false, so the project no longer matches. The
file (not the lagging store) is re-read as the authority right before acting,
which closes any store-lag window under manual double-runs.

Usage:
    recurring-projects.py                      # scan + act
    recurring-projects.py --dry-run            # report only, change nothing
    recurring-projects.py --today 2026-09-08   # override "today", for testing
"""

from __future__ import annotations

import argparse
import calendar
import datetime as dt
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

KB = "https://w3id.org/retinue/kb#"
ENDPOINT = os.environ.get("SPARQL_ENDPOINT_LIFE", "http://qlever-life:7001")
CHAMBERS_ROOT = Path(os.environ.get("CHAMBERS_ROOT", "/workspace/chambers"))
CONVERSATION_PUSH = os.environ.get(
    "CONVERSATION_PUSH", "/workspace/scripts/conversation-push.py"
)
# How early a one-off deadline wakes when its project names no `remind_before`.
# Zero, deliberately: `expected_by` means "the date this wants attention", which
# for a follow-up is the date itself. Waking early is the exception a project
# opts into with `remind_before` — a statutory deadline that needs lead time
# says so, rather than every follow-up in every chamber shifting by default.
DEFAULT_LEAD_DAYS = int(os.environ.get("PROJECT_DEADLINE_LEAD_DAYS", "0"))

# The whole gate, in two halves: resting (paused) projects that are either a
# cadence whose next due date has arrived, or a one-off deadline (any date —
# the lead time lives in the file, see the module docstring). `?g` carries the
# file provenance so a match resolves back to its source Markdown. A plain
# string (not an f-string) so the SPARQL braces stay literal; `__KB__` and
# `__TODAY__` are substituted with .replace().
QUERY_TMPL = """
PREFIX kb: <__KB__>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
SELECT ?project ?g ?recurring ?nextDue ?expectedBy ?title WHERE {
  GRAPH ?g {
    ?project a kb:Project ;
             kb:paused true .
    OPTIONAL { ?project kb:recurring ?recurring }
    OPTIONAL { ?project kb:nextDue ?nextDue }
    OPTIONAL { ?project kb:expectedBy ?expectedBy }
    OPTIONAL { ?project kb:title ?title }
    FILTER NOT EXISTS { ?project kb:status "done" }
    FILTER NOT EXISTS { ?project kb:resolved true }
    FILTER (
      (BOUND(?recurring) && LCASE(STR(?recurring)) IN ("monthly", "quarterly")
       && BOUND(?nextDue) && ?nextDue <= "__TODAY__"^^xsd:date)
      || BOUND(?expectedBy)
    )
  }
}
ORDER BY ?project
"""


def build_query(today: dt.date) -> str:
    return QUERY_TMPL.replace("__KB__", KB).replace("__TODAY__", today.isoformat())

_FM_RE = re.compile(r"^---\n(.*?)\n---\s*\n", re.DOTALL)


def query(sparql: str) -> list[dict]:
    data = urllib.parse.urlencode({"query": sparql}).encode()
    req = urllib.request.Request(
        ENDPOINT,
        data=data,
        headers={
            "Accept": "application/sparql-results+json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.load(resp)
    rows = []
    for b in payload.get("results", {}).get("bindings", []):
        rows.append({k: v["value"] for k, v in b.items()})
    return rows


def graph_to_path(graph: str) -> Path | None:
    """Resolve a ``file:<relpath>`` named graph to an absolute chamber path."""
    if not graph.startswith("file:"):
        return None
    rel = graph[len("file:"):]
    return CHAMBERS_ROOT / rel


def parse_frontmatter(text: str):
    """Minimal YAML-frontmatter reader (scalars only, stdlib-only)."""
    m = _FM_RE.match(text)
    if not m:
        return {}, None
    fm = {}
    for line in m.group(1).splitlines():
        mm = re.match(r"^([A-Za-z0-9_]+):\s*(.*)$", line)
        if not mm:
            continue
        key, val = mm.group(1), mm.group(2).strip()
        if val.startswith(("'", '"')) and val.endswith(("'", '"')) and len(val) >= 2:
            val = val[1:-1]
        fm[key] = val
    return fm, m


def folded_scalar(block: str, key: str) -> str:
    """Read a folded/literal ('>-'/'|') scalar spanning indented lines."""
    out, grab = [], False
    for line in block.splitlines():
        if re.match(rf"^{re.escape(key)}:\s*[>|]", line):
            grab = True
            continue
        if grab:
            if re.match(r"^\s+\S", line):
                out.append(line.strip())
            else:
                break
    return " ".join(out)


def as_bool(v) -> bool:
    return str(v).strip().lower() in ("true", "yes", "1")


def is_finished(fm: dict) -> bool:
    """Is this project done, whatever its chamber calls done?

    The two store-side exclusions cannot be relied on alone: a chamber's
    Markdown→triples converter maps the keys *it* chose, so a project can be
    ``resolved: true`` in its file and carry no ``kb:resolved`` in the store at
    all. The query filters are an optimisation; this is the actual check.
    """
    return (as_bool(fm.get("resolved", "false"))
            or fm.get("status", "").strip().lower() in ("done", "resolved"))


_LEAD_RE = re.compile(r"^(\d+)\s*([dwm])?$", re.IGNORECASE)


def parse_lead(spec: str) -> tuple[str, int] | None:
    """Parse a lead time (``10``, ``10d``, ``2w``, ``3m``) into (unit, count).

    An empty spec means the default; a malformed one returns None so the caller
    can say so rather than silently reminding at some other time.
    """
    s = (spec or "").strip().lower()
    if not s:
        return ("d", DEFAULT_LEAD_DAYS)
    m = _LEAD_RE.match(s)
    if not m:
        return None
    return (m.group(2) or "d", int(m.group(1)))


def minus_lead(due: dt.date, unit: str, count: int) -> dt.date:
    """Subtract a lead time from a date; months are calendar months."""
    if unit == "d":
        return due - dt.timedelta(days=count)
    if unit == "w":
        return due - dt.timedelta(weeks=count)
    # Calendar months, clamped to the last valid day (31 March minus 1 month
    # is 28/29 February, not an invalid date).
    index = (due.year * 12 + due.month - 1) - count
    year, month = divmod(index, 12)
    month += 1
    return dt.date(year, month, min(due.day, calendar.monthrange(year, month)[1]))


def as_date(raw: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat((raw or "").strip())
    except ValueError:
        return None


def wake_plan(fm: dict, row: dict) -> tuple[str, dt.date, dt.date] | str | None:
    """Decide when a resting project wakes, from its own frontmatter.

    Returns ``(kind, wake_on, due_on)`` — kind being ``"cadence"`` or
    ``"deadline"`` — or a string describing a malformed field, or None when the
    project simply has no date to wake on (not an error: the store lags, so it
    can still match a file that has since dropped its cadence or deadline).

    A declared cadence wins: such a project wakes on ``next_due``, and any
    ``expected_by`` it also carries is the end of the whole standing
    arrangement, not the next occurrence.
    """
    recurring = fm.get("recurring", row.get("recurring", "")).strip().lower()
    if recurring in ("monthly", "quarterly"):
        raw = fm.get("next_due", row.get("nextDue", ""))
        due = as_date(raw)
        if due is None:
            return f"bad next_due {raw!r}"
        return ("cadence", due, due)

    raw = fm.get("expected_by", row.get("expectedBy", ""))
    due = as_date(raw)
    if due is None:
        return None
    lead = parse_lead(fm.get("remind_before", ""))
    if lead is None:
        return f"bad remind_before {fm.get('remind_before')!r}"
    return ("deadline", minus_lead(due, *lead), due)


def set_field(block: str, key: str, value: str) -> str:
    """Set/replace a top-level scalar frontmatter field in the raw block."""
    pat = re.compile(rf"^({re.escape(key)}):.*$", re.MULTILINE)
    if pat.search(block):
        return pat.sub(f"{key}: {value}", block, count=1)
    return block.rstrip("\n") + f"\n{key}: {value}\n"


def reminder_text(
    fm: dict, block: str, title: str, due: dt.date, kind: str, today: dt.date
) -> tuple[str, str]:
    """Reminder title + message, taken from the project frontmatter.

    Kept in the project file (not the code), so no chamber-specific or personal
    wording lives in this framework script. Falls back to a neutral, generic
    line when a project declares no reminder of its own.
    """
    # A folded/literal scalar ('>-', '>', '|', '|-') leaves only the marker in the
    # flat frontmatter dict — the body lives on the following indented lines, so
    # read it out of the raw block instead.
    _FOLD = (">", ">-", "|", "|-", "")

    r_title = fm.get("reminder_title", "").strip()
    if r_title in _FOLD:
        r_title = folded_scalar(block, "reminder_title").strip()
    r_title = r_title or f"Due: {title}"

    r_msg = fm.get("reminder_message", "").strip()
    if r_msg in _FOLD:
        r_msg = folded_scalar(block, "reminder_message").strip()
    if not r_msg:
        # Neutral English fallback; the real, localized wording is expected to
        # live in the project's reminder_message frontmatter, not in this code.
        if kind == "deadline":
            days = (due - today).days
            when = (
                f"in {days} day{'s' if days != 1 else ''}"
                if days > 0
                else "today" if days == 0 else f"{-days} day(s) ago"
            )
            r_msg = (
                f"The project “{title}” has a deadline of {due.isoformat()} "
                f"({when}) and is active again."
            )
        else:
            r_msg = (
                f"The recurring project “{title}” is due "
                f"({due.isoformat()}) and active again."
            )
    return r_title, r_msg


def push_conversation(title: str, message: str, dry_run: bool) -> bool:
    if dry_run:
        print(f"[dry-run] would push conversation: {title!r}")
        return True
    try:
        subprocess.run(
            ["python3", CONVERSATION_PUSH, "--title", title, message], check=True
        )
        return True
    except subprocess.CalledProcessError as exc:
        print(f"[recurring-projects] conversation push failed: {exc}", file=sys.stderr)
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report only; change nothing")
    ap.add_argument("--today", help="override today (YYYY-MM-DD), for testing")
    args = ap.parse_args()

    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()

    try:
        rows = query(build_query(today))
    except Exception as e:  # store slow/down -> skip this tick, never crash
        print(f"[recurring-projects] gate query failed, skipping: {e}", file=sys.stderr)
        return 0

    if not rows:
        print("[recurring-projects] nothing due.")
        return 0

    acted = 0
    for r in rows:
        proj = r["project"]
        path = graph_to_path(r.get("g", ""))
        if path is None or not path.is_file():
            print(f"[recurring-projects] {proj}: cannot resolve file from graph "
                  f"{r.get('g')!r}; skip", file=sys.stderr)
            continue

        text = path.read_text()
        fm, m = parse_frontmatter(text)
        if not m:
            print(f"[recurring-projects] {proj}: no frontmatter in {path}; skip",
                  file=sys.stderr)
            continue

        # File is the authority: the store lags behind the filesystem, so a
        # project reactivated on a prior run may still show paused=true in the
        # store. Trust the file.
        if not as_bool(fm.get("paused", "false")):
            continue
        if is_finished(fm):
            continue

        plan = wake_plan(fm, r)
        if plan is None:
            continue  # nothing to wake on any more
        if isinstance(plan, str):
            print(f"[recurring-projects] {proj}: {plan}; skip", file=sys.stderr)
            continue
        kind, wake_on, due = plan
        if today < wake_on:
            continue  # not yet — or the store said due but the file moved on

        title = fm.get("title", r.get("title", path.stem))
        r_title, r_msg = reminder_text(fm, m.group(1), title, due, kind, today)

        print(f"[recurring-projects] {proj}: {kind} due {due.isoformat()} "
              f"(wake {wake_on.isoformat()}, today {today}) -> reactivating")

        # Flip to active first: even if the reminder push later fails, the project
        # visibly reappears on the dashboard card (paused=false), so the worst
        # failure is a missing nudge, never a silently-skipped month. The file's
        # existing current_actor is left as-is — a resting project already has
        # its owner set, so no owner identity is hardcoded here.
        if not args.dry_run:
            block = m.group(1)
            block = set_field(block, "paused", "false")
            block = set_field(block, "waiting_since", today.isoformat())
            path.write_text(f"---\n{block}\n---\n" + text[m.end():])

        push_conversation(r_title, r_msg, args.dry_run)
        acted += 1

    if acted == 0:
        print("[recurring-projects] nothing due.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
