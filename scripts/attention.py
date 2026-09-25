#!/usr/bin/env python3
"""The attention model's policy: what an item's level is, whether it may break
through the current focus mode, what a breakpoint releases, what the sweep
escalates, and how a correction changes the profile.

Design: docs/attention-model.md; migration plan: docs/attention-migration.md;
the browser prototype this mirrors: examples/attention-prototype/engine.js.

This module decides and never acts: it takes plain dicts (the gateway's own
thread, chat and project documents carry an ``attention`` block in the shape
``item_from_doc`` reads) and returns decisions and effects. Sending pushes,
saving documents and writing the life-store emit are the gateway's and the
scheduler's business, so the policy stays testable without either.

Times are timezone-aware ``datetime`` objects; lead times are ``timedelta``.
Schedules work in minutes of the local day of the ``now`` they are given.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path

LEVELS = ["passive", "active", "time-sensitive", "critical"]
RANK = {name: i for i, name in enumerate(LEVELS)}

# rows: importance 4–5 / 2–3 / 0–1; columns: time left > lead / ≤ lead / ≤ ⅓ lead or overdue
LEVEL_TABLE = [
    ["active", "time-sensitive", "time-sensitive"],
    ["passive", "active", "active"],
    ["passive", "passive", "active"],
]

DEFAULT_IMPORTANCE = 2.5
DAY = 24 * 60

# Lead-time defaults per kind of item, in minutes. A deployment overrides these
# in the attention profile; a correction on any item of a kind rewrites its entry.
DEFAULT_LEADS = {
    "default": 3 * DAY,
    "customer request": 2 * DAY,
    "invitation": 2 * DAY,
    "family note": 3 * DAY,
    "appointment": 2 * 60,
    "tax filing": 14 * DAY,
    "admin chore": 3 * DAY,
    "system alert": 60,
    "group chatter": 3 * DAY,
    "acknowledgement": 3 * DAY,
    "invoice run": 1 * DAY,
}

# A mode is how interruptible the user is right now — the mood, not the
# subject or the hour. Each carries the threshold (the lowest level that may
# ring), the spheres that may reach it, and ``only_admitted``: the list shows
# only what the mode admits, the rest folding into a collapsed "Not now"
# (critical, permitted, pulled and the user's own items stay visible).
#
# Focused is the head-down mood, and it takes a *scope*: nothing (only
# critical rings — flow), one sphere ("all clients"), or one project ("this
# one") — the nesting the data already has, sphere ⊃ project ⊃ item, offered
# at either level. A schedule entry may name a sphere as the third element;
# by hand, the menu offers spheres and the projects on the list. The scope
# stands in for the rule's ``admits`` for that stint (see mode_at); what the
# rule lists is what Focused admits when no scope is given.
DEFAULT_MODES = {
    "rest":    {"id": "rest",    "name": "Rest & relax", "admits": [],                                                    "admit_tags": [],         "threshold": "critical",       "only_admitted": True,  "blurb": "no interruptions; the digest waits for the morning"},
    "focused": {"id": "focused", "name": "Focused",      "admits": [],                                                    "admit_tags": ["health"], "threshold": "time-sensitive", "only_admitted": True,  "blurb": "head down — only what is about the scope at hand may ring, and health", "with_subject": True},
    "chores":  {"id": "chores",  "name": "Chores",       "admits": ["customers", "admin", "health", "friends", "family", "system"], "admit_tags": [], "threshold": "time-sensitive", "only_admitted": False, "blurb": "interruptions welcome: anything urgent rings"},
    "social":  {"id": "social",  "name": "Social",       "admits": ["friends", "family"],                                 "admit_tags": ["health"], "threshold": "time-sensitive", "only_admitted": False, "blurb": "people, not subjects: friends and family may break through"},
}
# A day's schedule: minute of the local day → mode id, optionally the sphere
# Focused is on.
DEFAULT_SCHEDULE = [[0, "rest"], [7 * 60, "chores"], [8 * 60, "focused"], [12 * 60, "chores"], [13 * 60, "focused", "customers"], [17 * 60, "chores"], [18 * 60, "social"], [22 * 60, "rest"]]
DEFAULT_DIGEST_TIMES = [8 * 60, 12 * 60, 17 * 60, 21 * 60]
SWEEP_EVERY_MINUTES = 30

# The week is a few *day plans*, not seven schedules: each plan is one day's
# schedule with the days it rules, written compactly — ``mon-fri``, ``sat,
# sun`` — and every weekday belongs to exactly one plan. One plan may also
# claim ``holiday``: the dates in the document's ``holidays`` then follow it,
# whatever weekday they fall on, so a week off is a date range told to the
# system, not a new schedule. A plan may carry its own digest times (a day off
# that starts at nine wants its first digest at nine); without them it uses
# the document's. See day_plan and apply_rules.
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_WEEKDAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
HOLIDAY = "holiday"
DAY_ALIASES = {"weekdays": "mon-fri", "weekend": "sat-sun", "daily": "mon-sun", "holidays": HOLIDAY}
DEFAULT_DAY_OFF = [[0, "rest"], [9 * 60, "social"], [22 * 60, "rest"]]
DEFAULT_WEEK = [
    {"name": "Workday", "days": ["mon-fri"], "schedule": DEFAULT_SCHEDULE},
    {"name": "Day off", "days": ["sat", "sun", HOLIDAY], "schedule": DEFAULT_DAY_OFF, "digest_times": [9 * 60, 18 * 60]},
]


def default_focus() -> dict:
    """The focus document the gateway keeps (focus.json): modes, the week of
    day plans, the holidays, the default digest times, the manual override
    with its subject, its end (``manual_until``) and its suggested
    breakpoints (``breaks``)."""
    return {"manual": None, "subject": None, "manual_until": None, "breaks": [],
            "modes": json.loads(json.dumps(DEFAULT_MODES)),
            "week": json.loads(json.dumps(DEFAULT_WEEK)), "holidays": [],
            "digest_times": list(DEFAULT_DIGEST_TIMES)}


def upgrade_focus(doc: dict) -> dict:
    """A focus document from before the week had one ``schedule`` for every
    day. A deployment that had chosen its own keeps it for every day,
    holidays included, as a one-plan week; one still on the shipped default
    never chose, so it gets the shipped week. A document carrying both drops
    the stale schedule."""
    doc = dict(doc)
    if "schedule" in doc:
        legacy = doc.pop("schedule")
        if not doc.get("week") and legacy and [list(x) for x in legacy] != DEFAULT_SCHEDULE:
            doc["week"] = [{"name": "Every day", "days": ["mon-sun", HOLIDAY],
                            "schedule": [list(x) for x in legacy]}]
    return doc


def heal_focus(focus: dict) -> dict:
    """Make a focus document safe to read: every mode it names exists.

    A hand-edited file, or one written while the modes had other names (Off,
    Home, Deep work, Open, Work), can define modes without the ones the
    shipped week names, or name a mode it does not define — in a day plan or
    in the override. Every reading of the mode would then fail on the missing
    id, and with it the list, the inbound rail and the tick. So the shipped
    modes are put back where missing (a deployment renames and adds modes;
    the shipped week still relies on these), a mode missing a field it needs
    gets it, a schedule entry naming a mode that does not exist is dropped —
    the entry before it runs on, and a plan left with none takes the shipped
    workday — and an override to one is released."""
    modes = focus.get("modes")
    modes = {mid: m for mid, m in modes.items() if isinstance(m, dict)} if isinstance(modes, dict) else {}
    for mid, mode in DEFAULT_MODES.items():
        modes.setdefault(mid, json.loads(json.dumps(mode)))
    for mid, mode in modes.items():
        shipped = DEFAULT_MODES.get(mid, {})
        mode["id"] = mid
        mode.setdefault("name", shipped.get("name") or mid.replace("-", " ").capitalize())
        if not isinstance(mode.get("admits"), list):
            mode["admits"] = list(shipped.get("admits") or [])
        if mode.get("threshold") not in RANK:
            mode["threshold"] = shipped.get("threshold") or "time-sensitive"
    focus["modes"] = modes
    for plan in focus.get("week") or []:
        if not isinstance(plan, dict):
            continue
        entries = plan.get("schedule")
        kept = [list(e) for e in entries if isinstance(e, (list, tuple)) and len(e) >= 2 and e[1] in modes] \
            if isinstance(entries, list) else []
        plan["schedule"] = kept or [list(e) for e in DEFAULT_SCHEDULE]
    if focus.get("manual") and focus["manual"] not in modes:
        focus.update(manual=None, subject=None, manual_until=None, breaks=[])
    return focus


def default_profile() -> dict:
    """The attention profile (profile.json): importance priors, lead times, permits."""
    return {"priors": {}, "spheres": {}, "leads": dict(DEFAULT_LEADS), "permits": {mid: [] for mid in DEFAULT_MODES}, "learned": []}


def load_json(path: Path, default: dict) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return default
    merged = default
    merged.update({k: v for k, v in data.items() if v is not None})
    return merged


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


# ---- items ----------------------------------------------------------------------

def lead_for(kind: str | None, profile: dict) -> timedelta:
    leads = profile.get("leads") or {}
    minutes = leads.get(kind) if kind else None
    if minutes is None:
        minutes = leads.get("default", DEFAULT_LEADS["default"])
    return timedelta(minutes=float(minutes))


def parse_dt(value) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


# A bare date as a deadline means the end of that working day, not midnight:
# "due Friday" gives the whole of Friday, and the lead-time ratio then counts
# down to an hour people actually work towards.
DATE_DUE_HOUR = 17

_LEAD_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([mhdw]?)\s*$", re.IGNORECASE)
_LEAD_UNITS = {"": 1, "m": 1, "h": 60, "d": DAY, "w": 7 * DAY}


def parse_lead(text) -> float | None:
    """A lead time as agents write it — ``90`` or ``90m`` (minutes), ``2h``,
    ``3d``, ``2w`` — in minutes; None when it is not one."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text) if text >= 0 else None
    m = _LEAD_RE.match(str(text))
    if not m:
        return None
    return float(m.group(1)) * _LEAD_UNITS[m.group(2).lower()]


def parse_due(text, now: datetime) -> datetime | None:
    """A deadline as agents write it: an ISO date-time (a naive one is read in
    ``now``'s zone) or a bare ``YYYY-MM-DD`` (DATE_DUE_HOUR of that day). None
    when absent or unparseable."""
    if text is None or text == "":
        return None
    if isinstance(text, datetime):
        return text if text.tzinfo else text.replace(tzinfo=now.tzinfo)
    raw = str(text).strip()
    try:
        if len(raw) == 10:
            d = datetime.strptime(raw, "%Y-%m-%d")
            return d.replace(hour=DATE_DUE_HOUR, tzinfo=now.tzinfo)
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=now.tzinfo)


def item_from_doc(doc: dict, kind: str, profile: dict) -> dict:
    """Read the ``attention`` block of a gateway document into a policy item.

    ``kind`` is ``chat``, ``thread`` or ``project``; missing fields fall back to
    the mid importance and the kind's lead time, as the brief specifies.
    """
    a = doc.get("attention") or {}
    kind_label = a.get("kind")
    lead = a.get("lead")
    return {
        "id": doc.get("id"),
        "kind": kind,
        "title": doc.get("title") or doc.get("name") or "",
        "sphere": a.get("sphere") or doc.get("sphere") or "admin",
        "tags": list(a.get("tags") or []),
        "importance": float(a.get("importance", DEFAULT_IMPORTANCE)),
        "importance_from": a.get("importance_from") or ("agent" if "importance" in a else "default"),
        "due": parse_dt(a.get("due")),
        "lead": timedelta(minutes=float(lead)) if lead is not None else lead_for(kind_label, profile),
        "lead_from": a.get("lead_from") or ("set" if lead is not None else "kind default"),
        "kind_label": kind_label,
        "actor": a.get("actor") or doc.get("current_actor") or "you",
        "waiting_since": parse_dt(a.get("waiting_since")),
        "sender": a.get("sender") or doc.get("sender"),
        "critical": bool(a.get("critical")),
        "vip": bool(a.get("vip")),
        "state": a.get("state") or ("done" if doc.get("archived") else "open"),
        "released": bool(a.get("released", False)),
        "snoozed_until": parse_dt(a.get("snoozed_until")),
        "boost": int(a.get("boost", 0)),
        "last_level": a.get("last_level"),
        "pushed": [parse_dt(x) for x in a.get("pushed") or []],
        "pulled": bool(a.get("pulled", False)),
        "digest_at": parse_dt(a.get("digest_at")),
        "done_at": a.get("done_at"),
        "done_how": a.get("done_how"),
        "project": a.get("project") or doc.get("project") or None,
    }


def item_to_attention(item: dict) -> dict:
    """The ``attention`` block to store back on the document."""
    iso = lambda d: d.isoformat() if d else None  # noqa: E731
    return {
        "importance": item["importance"], "importance_from": item.get("importance_from"),
        "due": iso(item.get("due")), "lead": item["lead"].total_seconds() / 60, "lead_from": item.get("lead_from"),
        "sphere": item["sphere"], "tags": list(item.get("tags") or []), "kind": item.get("kind_label"),
        "actor": item.get("actor"), "waiting_since": iso(item.get("waiting_since")), "sender": item.get("sender"),
        "critical": bool(item.get("critical")), "vip": bool(item.get("vip")),
        "state": item.get("state", "open"), "released": bool(item.get("released")),
        "snoozed_until": iso(item.get("snoozed_until")), "boost": int(item.get("boost", 0)), "last_level": item.get("last_level"),
        "pushed": [iso(x) for x in item.get("pushed") or []], "pulled": bool(item.get("pulled", False)),
        "digest_at": iso(item.get("digest_at")),
        "done_at": item.get("done_at"), "done_how": item.get("done_how"),
        "project": item.get("project") or None,
    }


# ---- the three fields -------------------------------------------------------------

def urgency_band(item: dict, now: datetime) -> int:
    """0: more than the lead time left (or no deadline); 1: within it; 2: within a third, or overdue."""
    due = item.get("due")
    if due is None:
        return 0
    left = due - now
    if left <= timedelta(0):
        return 2
    lead = item["lead"] if item["lead"] > timedelta(0) else timedelta(minutes=1)
    u = left / lead
    return 2 if u <= 1 / 3 else 1 if u <= 1 else 0


def level(item: dict, now: datetime) -> str:
    if item.get("critical"):
        return "critical"
    imp = item["importance"]
    row = 0 if imp >= 3.5 else 1 if imp >= 1.5 else 2
    lvl = LEVEL_TABLE[row][urgency_band(item, now)]
    boost = int(item.get("boost", 0))
    if boost:
        lvl = LEVELS[min(RANK[lvl] + boost, 2)]
    return lvl


def fmt_duration(td: timedelta) -> str:
    minutes = int(round(td.total_seconds() / 60))
    if minutes < 60:
        return f"{minutes} min"
    if minutes < DAY:
        h = minutes / 60
        return f"{int(h) if abs(h - round(h)) < 0.05 else round(h, 1)} h"
    d = minutes / DAY
    return f"{int(d) if abs(d - round(d)) < 0.05 else round(d, 1)} d"


def urgency_text(item: dict, now: datetime) -> str:
    if item.get("critical"):
        return "critical, declared"
    due = item.get("due")
    if due is None:
        return "no deadline"
    left = due - now
    if left <= timedelta(0):
        return f"overdue by {fmt_duration(-left)}"
    return f"due {due.strftime('%a %H:%M')} · {fmt_duration(left)} left of a {fmt_duration(item['lead'])} lead"


# ---- modes --------------------------------------------------------------------------

def minute_of_day(now: datetime) -> int:
    return now.hour * 60 + now.minute


# ---- the week -----------------------------------------------------------------------

def _weekday(token: str) -> int:
    """mon / monday / tues → 0 / 0 / 1; ValueError when it is not a weekday."""
    tok = token.strip().lower().rstrip(".")
    for i, name in enumerate(_WEEKDAY_NAMES):
        if len(tok) >= 3 and name.startswith(tok):
            return i
    raise ValueError(f"not a day: {token!r} (mon … sun, a range like mon-fri, or holiday)")


def parse_days(spec) -> set[str]:
    """Days as written — ``"mon-fri"``, ``"sat, sun, holiday"``, ``"weekend"``,
    or a list of those — as a set of tokens (``mon`` … ``sun``, ``holiday``).
    A range may wrap: ``fri-mon`` is Friday to Monday. ValueError on a word
    that is not a day."""
    parts = spec if isinstance(spec, (list, tuple, set)) else [spec]
    out: set[str] = set()
    for part in parts:
        text = re.sub(r"\s*[-–—]\s*", "-", str(part or "").strip().lower())
        for tok in re.split(r"[,;\s]+", text):
            if not tok:
                continue
            tok = DAY_ALIASES.get(tok, tok)
            if tok == HOLIDAY:
                out.add(HOLIDAY)
            elif "-" in tok:
                a, _, b = tok.partition("-")
                i, j = _weekday(a), _weekday(b)
                while True:
                    out.add(WEEKDAYS[i])
                    if i == j:
                        break
                    i = (i + 1) % 7
            else:
                out.add(WEEKDAYS[_weekday(tok)])
    return out


def fmt_days(days: set[str]) -> list[str]:
    """The compact form of a set of days: runs of three or more weekdays as a
    range (``mon-fri``), the rest one by one, ``holiday`` last."""
    idx = sorted(WEEKDAYS.index(d) for d in days if d in WEEKDAYS)
    out: list[str] = []
    run: list[int] = []
    for i in idx + [None]:
        if i is not None and run and i == run[-1] + 1:
            run.append(i)
            continue
        if len(run) >= 3:
            out.append(f"{WEEKDAYS[run[0]]}-{WEEKDAYS[run[-1]]}")
        else:
            out.extend(WEEKDAYS[k] for k in run)
        run = [i] if i is not None else []
    if HOLIDAY in days:
        out.append(HOLIDAY)
    return out


def week_of(focus: dict) -> list[dict]:
    """The day plans; a document from before the week reads as one plan."""
    week = focus.get("week")
    if isinstance(week, list) and week:
        return week
    return [{"name": "Every day", "days": ["mon-sun", HOLIDAY],
             "schedule": focus.get("schedule") or DEFAULT_SCHEDULE}]


def plan_days(plan: dict) -> set[str]:
    try:
        return parse_days(plan.get("days") or [])
    except ValueError:
        return set()


def holiday_on(focus: dict, day: date) -> dict | None:
    """The holiday entry the date falls in, if any."""
    iso = day.isoformat()
    for h in focus.get("holidays") or []:
        start = str(h.get("from") or "")
        if start and start <= iso <= str(h.get("to") or start):
            return h
    return None


def day_plan(focus: dict, day: date) -> dict:
    """The plan that rules a date: the holiday plan on a holiday (where one
    claims holidays), else the plan that claims its weekday. Lenient about a
    hand-edited file — an unclaimed weekday falls to the first plan — since
    apply_rules is where a week is held to covering each day exactly once."""
    week = week_of(focus)
    if holiday_on(focus, day):
        plan = next((p for p in week if HOLIDAY in plan_days(p)), None)
        if plan is not None:
            return plan
    weekday = WEEKDAYS[day.weekday()]
    return next((p for p in week if weekday in plan_days(p)), week[0])


def _entries(plan: dict) -> list[list]:
    entries = sorted(list(e) for e in plan.get("schedule") or [] if isinstance(e, (list, tuple)) and len(e) >= 2)
    return entries or [[0, "rest"]]


def day_schedule(focus: dict, day: date) -> list[list]:
    """A date's schedule from midnight: its plan's entries, led — where the
    plan's first entry comes after 00:00 — by the mode the evening before
    ended in. The day starts where the night left off, and which night that
    is depends on the date, not on the plan."""
    entries = _entries(day_plan(focus, day))
    if entries[0][0] > 0:
        before = _entries(day_plan(focus, day - timedelta(days=1)))[-1]
        entries = [[0, *before[1:]]] + entries
    return entries


def digest_times_on(focus: dict, day: date) -> list[int]:
    """A date's digest times: its plan's own, else the document's."""
    plan = day_plan(focus, day)
    return sorted(plan.get("digest_times") or focus.get("digest_times") or DEFAULT_DIGEST_TIMES)


def _mode_changes(focus: dict, day: date) -> list[int]:
    """The minutes of a date at which the scheduled mode changes in a way that
    is a breakpoint: the entry differs from the one before it (at midnight,
    from the evening before), and the one before is not Rest — the step out
    of Rest is not a breakpoint, the morning digest opens the day."""
    before = _entries(day_plan(focus, day - timedelta(days=1)))[-1]
    out = []
    for entry in day_schedule(focus, day):
        if list(entry[1:]) != list(before[1:]) and before[1] != "rest":
            out.append(entry[0])
        before = entry
    return out


def scheduled_until(focus: dict, now: datetime) -> datetime | None:
    """When the schedule next puts a different mode (or scope) in force —
    looking past midnight, since an evening's Rest runs into the next
    day's; None when the week never changes it."""
    current = scheduled_entry(focus, now)[1:]
    m = minute_of_day(now)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for offset in range(8):
        for entry in day_schedule(focus, now.date() + timedelta(days=offset)):
            if offset == 0 and entry[0] <= m:
                continue
            if list(entry[1:]) != list(current):
                return start + timedelta(days=offset, minutes=entry[0])
    return None


def scheduled_entry(focus: dict, now: datetime) -> list:
    """The schedule entry in force: ``[minute, mode]`` or ``[minute, mode, sphere]``."""
    m = minute_of_day(now)
    current = None
    for entry in day_schedule(focus, now.date()):
        if m >= entry[0]:
            current = entry
    return list(current)


def scheduled_id(focus: dict, now: datetime) -> str:
    return scheduled_entry(focus, now)[1]


# ---- the mode set by hand -------------------------------------------------------------
#
# A mode set by hand may run for a while (``manual_until``) and then give way
# to the schedule again. A long one — over an hour — gets a suggested
# breakpoint every BREAK_EVERY minutes: a digest of what waited, and the
# natural moment to look up. While a timed mode runs, its breaks and its end
# are its breakpoints and the day's digest times wait: a stretch set aside by
# hand keeps its own rhythm. Switching into Focused by hand is no breakpoint
# at all: it is a decision to focus on something right now, which a reminder
# of everything else would only get in the way of. The schedule's own switch
# into Focused stays one — that stretch was planned, and the digest at its
# start clears the deck before it.

BREAK_EVERY = 55        # minutes between suggested breakpoints in a long hand-set mode
BREAK_MARGIN = 20       # after the first, none this close to the end: the end is a breakpoint itself
QUIET_ENTRY = {"focused"}   # modes whose entry by hand is no breakpoint


def suggests_breaks(mode_id: str, minutes: int | None) -> bool:
    """Whether a hand-set mode of this length is offered breakpoints: over an
    hour, and not Rest, where a breakpoint releases nothing."""
    return bool(minutes) and minutes > 60 and mode_id != "rest"


def minutes_until(value, now: datetime) -> int | None:
    """``"17:00"`` (today, or tomorrow once past) or an ISO moment, as minutes
    from ``now``; None when it is neither or not ahead."""
    minute = parse_minute(value)
    if minute is not None:
        at = now.replace(hour=minute // 60, minute=minute % 60, second=0, microsecond=0)
        if at <= now:
            at += timedelta(days=1)
    else:
        try:
            at = parse_dt(value)
        except (TypeError, ValueError):
            return None
        if at is None or at.tzinfo is None:
            return None
    left = int(round((at - now).total_seconds() / 60))
    return left if left > 0 else None


def set_manual(focus: dict, mode_id: str | None, subject: dict | None, now: datetime,
               minutes: int | None = None, breaks: bool | None = None) -> bool:
    """Set the mode by hand — for ``minutes``, or until released — or, with
    ``mode_id`` None, release it to the schedule. ``breaks`` takes the
    suggested breakpoints of a long one (by default, as suggested). Returns
    whether the change is a breakpoint: every change is, except a switch
    into Focused."""
    focus["manual_until"] = None
    focus["breaks"] = []
    if not mode_id:
        focus["manual"] = None
        focus["subject"] = None
        return True
    mode = focus["modes"][mode_id]
    focus["manual"] = mode_id
    focus["subject"] = subject if mode.get("with_subject") else None
    if minutes:
        start = now.replace(second=0, microsecond=0)
        until = start + timedelta(minutes=int(minutes))
        focus["manual_until"] = until.isoformat()
        if suggests_breaks(mode_id, minutes) and breaks is not False:
            # The first after 55 minutes, then every 55 while the end is not near.
            k = 1
            while True:
                at = start + timedelta(minutes=k * BREAK_EVERY)
                if at >= until or (k > 1 and at > until - timedelta(minutes=BREAK_MARGIN)):
                    break
                focus["breaks"].append(at.isoformat())
                k += 1
    return mode_id not in QUIET_ENTRY


def manual_until(focus: dict) -> datetime | None:
    return parse_dt(focus.get("manual_until")) if focus.get("manual") else None


def manual_breaks(focus: dict) -> list[datetime]:
    return [parse_dt(b) for b in focus.get("breaks") or []] if focus.get("manual") else []


def manual_expired(focus: dict, now: datetime) -> bool:
    """A timed hand-set mode whose time is up: the tick hands it back to the
    schedule, and that is a breakpoint."""
    until = manual_until(focus)
    return until is not None and now >= until


def scope_of(focus: dict, now: datetime) -> dict | None:
    """What Focused is on: by hand, the override's subject — a sphere
    (``{"kind": "sphere", "id": …}``) or a project (``{"kind": "project",
    "id": <uri>, "title": …}``; a bare string is a sphere); by schedule, the
    entry's third element as a sphere. None when the mode has no scope."""
    if focus.get("manual"):
        subject = focus.get("subject")
        if isinstance(subject, str) and subject:
            return {"kind": "sphere", "id": subject, "title": subject}
        if isinstance(subject, dict) and subject.get("id"):
            return {"kind": subject.get("kind") or "sphere", "id": subject["id"],
                    "title": subject.get("title") or subject["id"]}
        return None
    entry = scheduled_entry(focus, now)
    if len(entry) > 2 and entry[2]:
        return {"kind": "sphere", "id": entry[2], "title": entry[2]}
    return None


def mode_at(focus: dict, now: datetime) -> dict:
    """The mode in force. A mode that takes a scope (``with_subject``) and has
    one is returned as a copy: ``admits`` becomes that one sphere, or none
    with ``project`` set — the mood stays, the scope stands in for the rule's
    list — and ``subject`` carries the scope so the title can say so. The
    stored rule is never touched; releasing the override drops its scope."""
    mode = focus["modes"][focus.get("manual") or scheduled_id(focus, now)]
    scope = scope_of(focus, now) if mode.get("with_subject") else None
    if not scope:
        return mode
    if scope["kind"] == "project":
        return {**mode, "admits": [], "project": scope["id"], "subject": scope}
    return {**mode, "admits": [scope["id"]], "subject": scope}


def about_project(item: dict, uri: str) -> bool:
    return bool(uri) and (item.get("project") == uri or item.get("id") == uri)


def admitted(item: dict, mode: dict, profile: dict) -> bool:
    """A sphere in ``admits`` gets through; so does one in ``admit_tags``,
    whether the item carries it as its sphere or as a tag — "health may
    reach me" is about the subject, not about which slot it sits in — and,
    with Focused on a project, whatever is about that project. A VIP's
    message is admitted everywhere (see breaks_through)."""
    if item.get("vip"):
        return True
    tags = mode.get("admit_tags", [])
    if item["sphere"] in mode["admits"] or item["sphere"] in tags:
        return True
    if any(t in tags for t in item.get("tags") or []):
        return True
    if about_project(item, mode.get("project")):
        return True
    sender = item.get("sender")
    return bool(sender) and sender in (profile.get("permits", {}).get(mode["id"]) or [])


def admitted_by(item: dict, mode: dict) -> dict | None:
    """Which rule of the mode in force lets the item through, so the details
    sheet offers the switch that actually changes it — ``{"by", "what"}``:

    - ``vip`` — the sender is a VIP; no Focus rule to change;
    - ``project`` / ``scope`` — Focused is on this project or this sphere:
      the stint itself, not a rule;
    - ``sphere`` — the mode's rule lists the item's sphere (``admits``);
    - ``tag`` — the mode admits a word wherever it stands (``admit_tags``),
      as the item's sphere or as one of its tags: Focused lets *health*
      through this way, whatever the scope.

    None when no rule admits it (a permit is the sender's, not a rule of the
    mode, and the sheet has its own switch for it)."""
    if item.get("vip"):
        return {"by": "vip", "what": item.get("sender") or ""}
    if about_project(item, mode.get("project")):
        return {"by": "project", "what": mode["project"]}
    if item["sphere"] in mode["admits"]:
        return {"by": "scope" if mode.get("subject") else "sphere", "what": item["sphere"]}
    tags = mode.get("admit_tags") or []
    hit = next((t for t in [item["sphere"], *(item.get("tags") or [])] if t in tags), None)
    if hit is not None:
        return {"by": "tag", "what": hit}
    return None


def mode_label(mode: dict) -> str:
    """The mode as the reason line names it: "Focused on Müller AG"."""
    scope = mode.get("subject")
    return f"{mode['name']} on {scope['title']}" if scope else mode["name"]


def admission_reason(item: dict, mode: dict, profile: dict, now: datetime) -> str:
    if level(item, now) == "critical":
        return "critical rings in every mode"
    if item.get("vip"):
        return f"{item.get('sender') or 'the sender'} is a VIP"
    if about_project(item, mode.get("project")):
        return f"{mode_label(mode)}: this is about it"
    if item["sphere"] in mode["admits"]:
        return f"{mode['name']} admits {item['sphere']}"
    tag = next((t for t in [item["sphere"]] + list(item.get("tags") or []) if t in mode.get("admit_tags", [])), None)
    if tag:
        return f"{mode['name']} admits {tag} everywhere"
    if has_permit(item, mode, profile):
        return f"{item['sender']} holds a {mode['name']} permit"
    if mode["threshold"] == "critical":
        return f"{mode['name']} admits only critical"
    if mode.get("subject"):
        return f"{mode_label(mode)} — this is not"
    if mode.get("with_subject") and not mode["admits"]:
        return f"{mode['name']} on nothing admits only critical"
    return f"{mode['name']} does not admit {item['sphere']}"


def has_permit(item: dict, mode: dict, profile: dict) -> bool:
    sender = item.get("sender")
    return bool(sender) and sender in (profile.get("permits", {}).get(mode["id"]) or [])


def breaks_through(item: dict, mode: dict, profile: dict, now: datetime) -> bool:
    """Critical rings everywhere. Otherwise an item breaks through at or above
    the mode's threshold when its sphere or a tag is admitted — or, holding a
    permit, at *active* already: a permit admits the sender and lowers the bar
    for them (the brief), while importance still decides the level, so a
    trivial note from a permitted sender stays in the digest.

    A VIP rings in every mode, at every level, like critical: the delivery
    gate's sender flag (docs/triage-delivery-gate.md) is the user saying *I
    want to hear from this person the moment they write*. What keeps a VIP
    quiet is decided before an item exists — a muted chat, a quieted or
    ignored group."""
    lvl = level(item, now)
    if lvl == "critical" or item.get("vip"):
        return True
    if has_permit(item, mode, profile):
        return RANK[lvl] >= RANK["active"]
    return RANK[lvl] >= RANK[mode["threshold"]] and admitted(item, mode, profile)


def next_breakpoint(focus: dict, now: datetime) -> datetime:
    """The next digest time or scheduled mode change, on whichever day of the
    week it falls — each day by its own plan. A scheduled step out of Rest is
    not a breakpoint (the morning digest opens the day); a manual override
    suspends the schedule, so only digest times count until it is released —
    and a timed one brings its own: its suggested breaks and its end."""
    until = manual_until(focus)
    if until is not None:
        ahead = [b for b in manual_breaks(focus) + [until] if b > now]
        if ahead:
            return min(ahead)
    m = minute_of_day(now)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for offset in range(8):
        day = now.date() + timedelta(days=offset)
        candidates = list(digest_times_on(focus, day))
        if not focus.get("manual"):
            candidates += _mode_changes(focus, day)
        if offset == 0:
            candidates = [t for t in candidates if t > m]
        if candidates:
            return start_of_day + timedelta(days=offset, minutes=min(candidates))
    return start_of_day + timedelta(days=1)  # unreachable: every day has a digest time


def due_events(focus: dict, now: datetime) -> set[str]:
    """What this minute is due for: ``digest`` at a digest time of the day's
    plan, ``mode`` at a scheduled change that counts as a breakpoint (see
    next_breakpoint), ``break`` at a suggested breakpoint of a timed
    hand-set mode (which holds the other two back), and ``sweep`` every
    SWEEP_EVERY_MINUTES. The end of a timed mode is the tick's to notice
    (manual_expired)."""
    m = minute_of_day(now)
    events = set()
    until = manual_until(focus)
    if until is not None and now < until:
        # A timed hand-set mode: its suggested breaks, not the day's digests.
        stamp = now.replace(second=0, microsecond=0)
        if any(b.replace(second=0, microsecond=0) == stamp for b in manual_breaks(focus)):
            events.add("break")
    else:
        if m in digest_times_on(focus, now.date()):
            events.add("digest")
        if not focus.get("manual") and m in _mode_changes(focus, now.date()):
            events.add("mode")
    if m % SWEEP_EVERY_MINUTES == 0:
        events.add("sweep")
    return events


# ---- delivery -----------------------------------------------------------------------

def on_arrival(item: dict, focus: dict, profile: dict, now: datetime) -> dict:
    """Decide what happens to a newly arrived (or re-opened) item.

    Returns ``{"deliver": "push"|"list"|"hold", "level": ..., "reason": ...}`` and
    marks the item released where it is delivered or listed."""
    mode = mode_at(focus, now)
    lvl = level(item, now)
    item["last_level"] = lvl
    item["pulled"] = False
    item["digest_at"] = None
    if item.get("actor", "you") != "you":
        item["released"] = True
        return {"deliver": "waiting", "level": lvl, "reason": f"parked on {item['actor']}"}
    if breaks_through(item, mode, profile, now):
        item["released"] = True
        item.setdefault("pushed", []).append(now)
        return {"deliver": "push", "level": lvl, "reason": admission_reason(item, mode, profile, now), "urgency": "high"}
    if lvl == "passive":
        item["released"] = True
        return {"deliver": "list", "level": lvl, "reason": "passive is listed, never pushed"}
    item["released"] = False
    return {"deliver": "hold", "level": lvl, "reason": admission_reason(item, mode, profile, now), "until": next_breakpoint(focus, now)}


def breakpoint(items: list[dict], focus: dict, now: datetime) -> dict:
    """Release what was held and say what the digest carries — ranked as the
    list ranks (level, importance, the nearest deadline), and each item
    stamped ``digest_at`` so the home can show what this digest brought. In
    Rest nothing is released and no digest goes out; the morning digest
    carries it."""
    mode = mode_at(focus, now)
    due = [i for i in items if i.get("state", "open") == "open" and not i.get("released") and i.get("actor", "you") == "you"
           and (i.get("snoozed_until") is None or i["snoozed_until"] <= now)]
    if mode["id"] == "rest":
        return {"digest": None, "held": due, "reason": "Rest has no digest"}
    stamp = now.replace(second=0, microsecond=0)
    for i in due:
        i["released"] = True
        i["snoozed_until"] = None
        i["digest_at"] = stamp
    if not due:
        return {"digest": None, "held": [], "reason": "nothing was held"}
    due.sort(key=lambda i: rank_key(i, now))
    return {"digest": {"at": stamp, "items": due, "urgency": "normal", "topic": "digest"}, "held": [], "reason": "breakpoint"}


def fmt_when(when: datetime, now: datetime) -> str:
    """A moment as a notification says it, from ``now``: 17:00, tomorrow
    12:00, Fri 12:00, 3 Oct."""
    when = when.astimezone(now.tzinfo) if when.tzinfo and now.tzinfo else when
    days = (when.date() - now.date()).days
    if days == 0:
        return when.strftime("%H:%M")
    if days == 1:
        return when.strftime("tomorrow %H:%M")
    if 1 < days < 7:
        return when.strftime("%a %H:%M")
    return f"{when.day} {when:%b}"


def digest_line(item: dict, now: datetime) -> str:
    """One item of a digest push: its title, and why it is there — critical,
    overdue, its deadline, else the start of what it says."""
    title = item.get("title") or "Untitled"
    due = item.get("due")
    if item.get("critical"):
        why = "critical"
    elif due is not None:
        why = f"overdue since {fmt_when(due, now)}" if due <= now else f"due {fmt_when(due, now)}"
    else:
        preview = " ".join(str(item.get("preview") or "").split())
        if len(preview) > 60:
            cut = preview[:59]
            preview = (cut.rsplit(" ", 1)[0] if " " in cut else cut).rstrip(" ,;:—-") + "…"
        why = preview
    return f"{title} — {why}" if why else title


DIGEST_LINES = 5


def digest_text(digest: dict, now: datetime) -> tuple[str, str]:
    """The digest push: a title that says when and how much — or, at a break
    or the end of a hand-set mode, which (``label``) — and one line per item,
    most pressing first; past DIGEST_LINES, how many more."""
    items = digest["items"]
    n = len(items)
    label = digest.get("label") or f"Digest {digest['at'].strftime('%H:%M')}"
    title = f"{label} · {n} thing{'s' if n != 1 else ''} waited"
    lines = [digest_line(i, now) for i in items[:DIGEST_LINES]]
    if n > DIGEST_LINES:
        lines.append(f"… and {n - DIGEST_LINES} more")
    return title, "\n".join(lines)


def sweep(items: list[dict], focus: dict, profile: dict, now: datetime) -> list[dict]:
    """Re-evaluate held and released items; return push and climb effects."""
    mode = mode_at(focus, now)
    effects = []
    for item in items:
        if item.get("state", "open") != "open" or item.get("actor", "you") != "you":
            continue
        lvl = level(item, now)
        last = item.get("last_level")
        rose = last is not None and RANK[lvl] > RANK.get(last, 0)
        if not item.get("released"):
            if item.get("snoozed_until") is not None and item["snoozed_until"] > now:
                item["last_level"] = lvl
                continue
            if breaks_through(item, mode, profile, now):
                item["released"] = True
                item.setdefault("pushed", []).append(now)
                effects.append({"type": "push", "item": item, "level": lvl, "urgency": "high",
                                "reason": f"the sweep found it in the next urgency band ({last} → {lvl})" if rose else f"the sweep found it admitted now ({admission_reason(item, mode, profile, now)})"})
            elif rose:
                effects.append({"type": "climb", "item": item, "level": lvl, "reason": admission_reason(item, mode, profile, now)})
        elif rose:
            if breaks_through(item, mode, profile, now) and not item.get("pushed"):
                item.setdefault("pushed", []).append(now)
                effects.append({"type": "push", "item": item, "level": lvl, "urgency": "high", "reason": f"climbs to {lvl}; {admission_reason(item, mode, profile, now)}"})
            else:
                effects.append({"type": "climb", "item": item, "level": lvl, "reason": ""})
        item["last_level"] = lvl
    return effects


def reevaluate(item: dict, focus: dict, profile: dict, now: datetime, why: str) -> dict | None:
    """After a correction, a permit or a Focus-rule change: push if it now breaks through."""
    if item.get("state", "open") != "open":
        return None
    mode = mode_at(focus, now)
    lvl = level(item, now)
    before = item.get("last_level")
    item["last_level"] = lvl
    if not item.get("released") and (item.get("snoozed_until") is None or item["snoozed_until"] <= now):
        if breaks_through(item, mode, profile, now):
            item["released"] = True
            item.setdefault("pushed", []).append(now)
            return {"type": "push", "item": item, "level": lvl, "urgency": "high", "reason": f"after {why}: {lvl}; {admission_reason(item, mode, profile, now)}"}
        return {"type": "held", "item": item, "level": lvl, "reason": admission_reason(item, mode, profile, now)}
    if item.get("released") and breaks_through(item, mode, profile, now) and not item.get("pushed") and RANK[lvl] >= 2:
        item["pushed"] = [now]
        return {"type": "push", "item": item, "level": lvl, "urgency": "high", "reason": f"after {why}: {lvl}; {admission_reason(item, mode, profile, now)}"}
    if before and before != lvl:
        return {"type": "climb" if RANK[lvl] > RANK.get(before, 0) else "fall", "item": item, "level": lvl, "reason": f"{before} → {lvl} after {why}"}
    return None


def repeat_policy(item: dict, mode: dict) -> dict:
    """Per-class repeat policy: off by default, on for family in Rest (the repeated-caller case)."""
    if item["sphere"] == "family" and mode["id"] == "rest":
        return {"escalate": True, "reason": "a family repeat breaks through in Rest"}
    return {"escalate": False, "reason": ""}


# ---- what the user does -------------------------------------------------------------

def correct(item: dict, profile: dict, patch: dict, now: datetime) -> list[str]:
    """Apply a three-field correction; returns what the profile learned."""
    learned = []
    if patch.get("importance") is not None:
        item["importance"] = float(patch["importance"])
        item["importance_from"] = "you"
        key = item.get("sender") or item.get("kind_label") or item["kind"]
        profile.setdefault("priors", {})[key] = item["importance"]
        learned.append(f"importance prior for {key} → {item['importance']:g}")
    if patch.get("lead") is not None:
        minutes = float(patch["lead"])
        item["lead"] = timedelta(minutes=minutes)
        item["lead_from"] = "you"
        if item.get("kind_label"):
            profile.setdefault("leads", {})[item["kind_label"]] = minutes
            learned.append(f"lead time for “{item['kind_label']}” → {fmt_duration(item['lead'])}")
    if "due" in patch:
        item["due"] = parse_dt(patch["due"])
    if patch.get("sphere"):
        item["sphere"] = str(patch["sphere"])
        key = item.get("sender")
        if key:
            profile.setdefault("spheres", {})[key] = item["sphere"]
            learned.append(f"sphere for {key} → {item['sphere']}")
    if isinstance(patch.get("tags"), list):
        # A tag is a further sphere, so it is the same word the rules use.
        item["tags"] = list(dict.fromkeys(t for t in (sphere_id(x) for x in patch["tags"]) if t))
    if "critical" in patch:
        item["critical"] = bool(patch["critical"])
    for text in learned:
        profile.setdefault("learned", []).append({"at": now.isoformat(), "text": text})
    return learned


def set_permit(profile: dict, sender: str, mode_id: str, on: bool, now: datetime, modes: dict) -> bool:
    permits = profile.setdefault("permits", {}).setdefault(mode_id, [])
    has = sender in permits
    if on == has:
        return False
    if on:
        permits.append(sender)
    else:
        permits.remove(sender)
    profile.setdefault("learned", []).append({"at": now.isoformat(), "text": f"{sender} {'may interrupt' if on else 'may no longer interrupt'} in {modes[mode_id]['name']}"})
    return True


def parse_minute(value) -> int | None:
    """A time of day as the settings or an agent write it — ``"08:30"``, ``"8"``,
    or a minute count — as a minute of the day; None when it is not one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        m = int(value)
        return m if 0 <= m < DAY else None
    raw = str(value or "").strip()
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?$", raw)
    if not m:
        return None
    minute = int(m.group(1)) * 60 + int(m.group(2) or 0)
    return minute if 0 <= minute < DAY else None


def apply_rules(focus: dict, patch: dict, spheres: list[str], today: date | None = None) -> list[str]:
    """Change the focus rules from one patch — what the mode menu, the
    settings page and Ara all write. Returns what changed, in words; an
    empty list means nothing did. Unknown keys are ignored; a value that is
    not valid raises ValueError with the reason, and the week and the
    holidays are left as they were.

    Per mode (``{"mode": id, ...}``): ``only_admitted`` (bool), ``threshold``
    (a level), ``admits`` (the whole sphere list) or ``admit`` / ``deny``
    (spheres to add / remove), ``admit_tags`` or ``tag_on`` / ``tag_off``.

    The week: ``week`` replaces the whole list of day plans (``{name, days,
    schedule, digest_times?}``); ``{"plan": name, ...}`` changes one —
    ``days`` (the days move to it from whichever plan had them; a plan left
    with none is gone), ``schedule``, ``digest_times`` (``[]`` or null: the
    default again), ``rename`` — or adds it, given its days and schedule.
    Days are ``mon``…``sun``, ranges like ``mon-fri``, ``weekdays``,
    ``weekend``, ``daily`` and ``holiday``; a schedule is a list of ``[time,
    mode]`` / ``[time, mode, sphere]`` or the compact ``"07:00 chores, 08:00
    focused, 13:00 focused customers"``; a time is ``"HH:MM"`` or a minute.
    ``schedule`` without ``plan`` is the one plan's, while there is one.
    Every weekday must end up in exactly one plan.

    Holidays: ``holidays`` replaces the list, ``holiday_add`` and
    ``holiday_remove`` change it — a holiday is ``"2026-12-24"``,
    ``"2026-12-24..2027-01-02 Christmas"`` or ``{from, to?, name?}``; a
    removal names it or a date inside it. Holidays that are over (before
    ``today``) are dropped whenever the list changes. They follow the plan
    that claims ``holiday``, so one must.

    ``digest_times`` without ``plan``: the default digest times, for plans
    that name none.
    """
    changes: list[str] = []
    mode_id = patch.get("mode")
    if mode_id is not None:
        mode = focus["modes"].get(str(mode_id))
        if mode is None:
            raise ValueError("unknown mode")
        if "only_admitted" in patch and patch["only_admitted"] is not None:
            on = bool(patch["only_admitted"])
            if bool(mode.get("only_admitted")) != on:
                mode["only_admitted"] = on
                changes.append(f"{mode['name']} lists {'only what it admits' if on else 'everything'}")
        if patch.get("threshold") is not None:
            th = str(patch["threshold"])
            if th not in RANK or th == "passive":
                raise ValueError("threshold must be active, time-sensitive or critical")
            if mode["threshold"] != th:
                mode["threshold"] = th
                changes.append(f"{mode['name']} rings from {th}")
        for key, on_key, off_key, label in (("admits", "admit", "deny", "admits"),
                                            ("admit_tags", "tag_on", "tag_off", "admits the tag")):
            current = list(mode.get(key) or [])
            wanted = list(current)
            if isinstance(patch.get(key), list):
                wanted = [str(x).strip().lower() for x in patch[key] if str(x).strip()]
            for x in patch.get(on_key) or []:
                x = str(x).strip().lower()
                if x and x not in wanted:
                    wanted.append(x)
            for x in patch.get(off_key) or []:
                wanted = [w for w in wanted if w != str(x).strip().lower()]
            if key == "admits":
                unknown = [w for w in wanted if w not in spheres]
                if unknown:
                    raise ValueError(f"unknown sphere: {', '.join(unknown)}")
            if wanted != current:
                mode[key] = wanted
                for x in wanted:
                    if x not in current:
                        changes.append(f"{mode['name']} {label} {x}")
                for x in current:
                    if x not in wanted:
                        changes.append(f"{mode['name']} no longer {label} {x}")
    week = json.loads(json.dumps(week_of(focus)))
    if patch.get("week") is not None:
        if not isinstance(patch["week"], list) or not patch["week"]:
            raise ValueError("the week is a list of day plans")
        week = [_parse_plan(raw, focus, spheres) for raw in patch["week"]]
        _check_week(week)
        if week != week_of(focus):
            changes.append("the week: " + " · ".join(f"{p['name']} {', '.join(p['days'])}" for p in week))
    plan_name = patch.get("plan")
    own_digests = plan_name is not None
    if plan_name is None and patch.get("schedule") is not None:
        # Before the week there was one schedule; while there is one plan,
        # that is what a bare schedule means.
        if len(week) != 1:
            raise ValueError("the week has several day plans — say which: " + ", ".join(p["name"] for p in week))
        plan_name = week[0]["name"]
    if plan_name is not None:
        changes += _patch_plan(week, str(plan_name), patch, focus, spheres, own_digests)
        _check_week(week)

    holidays = [dict(h) for h in focus.get("holidays") or []]
    touched = any(patch.get(k) is not None for k in ("holidays", "holiday_add", "holiday_remove"))
    if patch.get("holidays") is not None:
        holidays = [parse_holiday(h) for h in _as_list(patch["holidays"])]
        changes.append("holidays: " + (", ".join(fmt_holiday(h) for h in holidays) or "none"))
    for raw in _as_list(patch.get("holiday_add")):
        h = parse_holiday(raw)
        holidays = [x for x in holidays if (x["from"], x["to"]) != (h["from"], h["to"])] + [h]
        changes.append(f"holiday {fmt_holiday(h)}")
    for raw in _as_list(patch.get("holiday_remove")):
        key = str(raw).strip()
        try:
            iso = date.fromisoformat(key).isoformat()
            gone = [x for x in holidays if x["from"] <= iso <= x["to"]]
        except ValueError:
            gone = [x for x in holidays if str(x.get("name") or "").casefold() == key.casefold()]
        if not gone:
            raise ValueError(f"no holiday {key}")
        holidays = [x for x in holidays if x not in gone]
        changes += [f"holiday {fmt_holiday(x)} removed" for x in gone]
    if touched and today is not None:
        holidays = [x for x in holidays if x["to"] >= today.isoformat()]
    holidays.sort(key=lambda x: (x["from"], x["to"]))
    if (touched or week != week_of(focus)) and holidays and not any(HOLIDAY in plan_days(p) for p in week):
        raise ValueError("no day plan takes holidays — give one the day “holiday” first")

    if week != week_of(focus):
        focus["week"] = week
        focus.pop("schedule", None)
    if touched and holidays != (focus.get("holidays") or []):
        focus["holidays"] = holidays
    if patch.get("digest_times") is not None and not own_digests:
        times = _parse_times(patch["digest_times"])
        if times != sorted(focus["digest_times"]):
            focus["digest_times"] = times
            changes.append("digest times → " + _fmt_times(times))
    return changes


def _as_list(value) -> list:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _fmt_times(times: list[int]) -> str:
    return ", ".join(f"{t // 60:02d}:{t % 60:02d}" for t in times)


def _parse_times(value) -> list[int]:
    raw = re.split(r"[,;\s]+", value.strip()) if isinstance(value, str) else _as_list(value)
    times = [parse_minute(t) for t in raw if str(t).strip()]
    if not times or None in times:
        raise ValueError("digest times are HH:MM")
    return sorted(set(times))


def _plan_name(value) -> str:
    name = str(value or "").strip()
    if not name or len(name) > 40:
        raise ValueError("a day plan needs a name of at most 40 characters")
    return name


def parse_schedule(value, focus: dict, spheres: list[str]) -> list[list]:
    """A day's schedule as written — ``[[time, mode], [time, mode, sphere]]``
    or ``"07:00 chores, 08:00 focused, 13:00 focused customers"`` — sorted,
    each time once. It need not start at 00:00: the day then begins in the
    mode the evening before ended in (see day_schedule)."""
    if isinstance(value, str):
        value = [part.split() for part in value.split(",") if part.strip()]
    if not isinstance(value, (list, tuple)):
        raise ValueError("a schedule is a list of [time, mode] entries")
    schedule = []
    for entry in value:
        if not isinstance(entry, (list, tuple)) or len(entry) not in (2, 3):
            raise ValueError("a schedule entry is [time, mode] or [time, mode, sphere]")
        minute = parse_minute(entry[0])
        if minute is None or str(entry[1]) not in focus["modes"]:
            raise ValueError(f"bad schedule entry {list(entry)!r}")
        row = [minute, str(entry[1])]
        if len(entry) == 3 and entry[2]:
            scope = sphere_id(entry[2])
            if scope is None or scope not in spheres:
                raise ValueError(f"unknown sphere in schedule entry {list(entry)!r}")
            if not focus["modes"][row[1]].get("with_subject"):
                raise ValueError(f"{focus['modes'][row[1]]['name']} takes no scope")
            row.append(scope)
        schedule.append(row)
    schedule.sort()
    if not schedule:
        raise ValueError("the schedule needs at least one entry")
    for a, b in zip(schedule, schedule[1:]):
        if a[0] == b[0]:
            raise ValueError(f"two entries at {_fmt_times([a[0]])}")
    return schedule


def _parse_plan(raw, focus: dict, spheres: list[str]) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("a day plan is {name, days, schedule, digest_times?}")
    name = _plan_name(raw.get("name"))
    days = parse_days(raw.get("days") or [])
    if not days:
        raise ValueError(f"{name} names no days")
    plan = {"name": name, "days": fmt_days(days), "schedule": parse_schedule(raw.get("schedule"), focus, spheres)}
    if raw.get("digest_times"):
        plan["digest_times"] = _parse_times(raw["digest_times"])
    return plan


def _check_week(week: list[dict]) -> None:
    """Every weekday in exactly one plan, ``holiday`` in at most one, no two
    plans of one name."""
    owner: dict[str, str] = {}
    names: set[str] = set()
    for plan in week:
        if plan["name"].casefold() in names:
            raise ValueError(f"two day plans are called {plan['name']}")
        names.add(plan["name"].casefold())
        for day in plan_days(plan):
            if day in owner:
                raise ValueError(f"{day} is in both {owner[day]} and {plan['name']}")
            owner[day] = plan["name"]
    missing = [d for d in WEEKDAYS if d not in owner]
    if missing:
        raise ValueError("no day plan for " + ", ".join(missing) + " — give it to one")


def _patch_plan(week: list[dict], name: str, patch: dict, focus: dict, spheres: list[str],
                own_digests: bool) -> list[str]:
    """Change (or add) one day plan of the working week in place."""
    changes: list[str] = []
    plan = next((p for p in week if p["name"].casefold() == name.strip().casefold()), None)
    created = plan is None
    if created:
        if patch.get("days") is None or patch.get("schedule") is None:
            raise ValueError(f"there is no day plan {name} — a new one needs its days and its schedule")
        plan = {"name": _plan_name(name), "days": [], "schedule": []}
        week.append(plan)
    if patch.get("schedule") is not None:
        schedule = parse_schedule(patch["schedule"], focus, spheres)
        if schedule != plan["schedule"]:
            plan["schedule"] = schedule
            if not created:
                changes.append(f"{plan['name']}: the schedule changed")
    if own_digests and "digest_times" in patch:
        value = patch["digest_times"]
        if value in (None, [], ""):
            if plan.pop("digest_times", None) is not None:
                changes.append(f"{plan['name']}: the default digest times")
        else:
            times = _parse_times(value)
            if times != plan.get("digest_times"):
                plan["digest_times"] = times
                changes.append(f"{plan['name']}: digests {_fmt_times(times)}")
    if patch.get("days") is not None:
        days = parse_days(patch["days"])
        if not days:
            raise ValueError(f"{plan['name']} needs at least one day")
        if days != plan_days(plan):
            # The days move here: whichever plan had them loses them, and a
            # plan left with no days is gone.
            gone = []
            for other in week:
                if other is not plan and plan_days(other) & days:
                    left = plan_days(other) - days
                    other["days"] = fmt_days(left)
                    if not left:
                        gone.append(other["name"])
            week[:] = [p for p in week if p is plan or p["name"] not in gone]
            plan["days"] = fmt_days(days)
            if not created:
                changes.append(f"{plan['name']}: {', '.join(plan['days'])}")
            changes += [f"{g} is gone — no days left" for g in gone]
    if patch.get("rename"):
        new = _plan_name(patch["rename"])
        if new != plan["name"]:
            changes.append(f"{plan['name']} → {new}")
            plan["name"] = new
    if created:
        changes.insert(0, f"new day plan {plan['name']}: {', '.join(plan['days'])}")
    return changes


def parse_holiday(value) -> dict:
    """``"2026-12-24"``, ``"2026-12-24..2027-01-02 Christmas"`` (``/`` or
    ``to`` also separate the dates), ``[from, to?, name?]`` or ``{from, to?,
    name?}`` as ``{"from", "to", "name"?}`` with ISO dates, ``to`` inclusive."""
    if isinstance(value, dict):
        start, end, name = value.get("from") or value.get("date"), value.get("to"), value.get("name")
    elif isinstance(value, (list, tuple)) and value:
        start, end, name = (list(value) + [None, None])[:3]
    else:
        m = re.match(r"^\s*(\d{4}-\d{2}-\d{2})(?:\s*(?:\.\.|/|–|—|\bto\b)\s*(\d{4}-\d{2}-\d{2}))?(?:\s+(.+?))?\s*$",
                     str(value or ""))
        if not m:
            raise ValueError(f"not a holiday: {value!r} (YYYY-MM-DD, or YYYY-MM-DD..YYYY-MM-DD and a name)")
        start, end, name = m.groups()
    try:
        first = date.fromisoformat(str(start))
        last = date.fromisoformat(str(end)) if end else first
    except ValueError:
        raise ValueError(f"not a holiday: {value!r} (dates are YYYY-MM-DD)") from None
    if last < first or (last - first).days > 366:
        raise ValueError(f"not a holiday: {value!r} (the end is before the start, or a year away)")
    out = {"from": first.isoformat(), "to": last.isoformat()}
    if name and str(name).strip():
        out["name"] = str(name).strip()[:60]
    return out


def fmt_holiday(h: dict) -> str:
    span = h["from"] if h["to"] == h["from"] else f"{h['from']} – {h['to']}"
    return f"{h['name']}, {span}" if h.get("name") else span


# A word in any script: letters and digits (Unicode), hyphens between them.
_SPHERE_RE = re.compile(r"^[^\W_](?:[^\W_]|-)*$")


def sphere_id(text) -> str | None:
    """A sphere as the user types it — "Board games", "Ökologie" — as the word
    the store and the rules use: lowercase, hyphens for spaces, letters and
    digits in any script, at most 32 characters. None when what is left is
    not a word."""
    raw = re.sub(r"[\s_]+", "-", str(text or "").strip().lower())
    raw = re.sub(r"-{2,}", "-", raw).strip("-")
    return raw if raw and len(raw) <= 32 and _SPHERE_RE.match(raw) else None


def add_sphere(focus: dict, text) -> str:
    """Add a sphere to the vocabulary; returns its id. Idempotent. Spheres are
    the user's subjects — a hobby, a client, a cause — so adding one must
    cost nothing: a word, and it exists. ValueError when it is not a word."""
    sid = sphere_id(text)
    if sid is None:
        raise ValueError("a sphere is a short word: letters, digits, hyphens")
    spheres = focus.setdefault("spheres", [])
    if sid not in spheres:
        spheres.append(sid)
    return sid


def remove_sphere(focus: dict, text) -> str:
    """Drop a sphere from the vocabulary. Refused while a mode still admits
    it — the rule would silently stop meaning anything — and for ``unknown``,
    which the model itself assigns. Items already in the sphere keep their
    word; the sheet still offers it on them. ValueError says why not."""
    sid = sphere_id(text)
    if sid is None or sid not in (focus.get("spheres") or []):
        raise ValueError("no such sphere")
    if sid == "unknown":
        raise ValueError("the model assigns “unknown” itself; it cannot be removed")
    admitting = [m["name"] for m in focus["modes"].values()
                 if sid in (m.get("admits") or []) or sid in (m.get("admit_tags") or [])]
    if admitting:
        raise ValueError(f"still admitted in {', '.join(admitting)} — change those rules first")
    focus["spheres"] = [x for x in focus["spheres"] if x != sid]
    return sid


def set_admission(focus: dict, sphere: str, mode_id: str, on: bool) -> bool:
    mode = focus["modes"][mode_id]
    has = sphere in mode["admits"]
    if on == has:
        return False
    if on:
        mode["admits"].append(sphere)
    else:
        mode["admits"].remove(sphere)
    return True


def snooze(item: dict, focus: dict, now: datetime, when: str) -> datetime:
    if when == "tomorrow":
        # Tomorrow's first digest, by tomorrow's plan.
        tomorrow = now.date() + timedelta(days=1)
        until = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1, minutes=digest_times_on(focus, tomorrow)[0])
    else:
        until = next_breakpoint(focus, now)
    item["released"] = False
    item["snoozed_until"] = until
    item["pulled"] = False
    item["digest_at"] = None
    return until


def pull(item: dict) -> bool:
    """Pull a held item onto the list ahead of the digest. It stays visible
    afterwards even where the mode folds the unadmitted away (``pulled``),
    until the next arrival or snooze judges it afresh."""
    if item.get("state", "open") != "open" or item.get("released"):
        return False
    item["released"] = True
    item["snoozed_until"] = None
    item["pulled"] = True
    item["digest_at"] = None
    return True


def reopen(item: dict) -> None:
    """Put a handled item back on the list — visibly, like a pull."""
    item["state"] = "open"
    item["released"] = True
    item["snoozed_until"] = None
    item["pulled"] = True
    item["digest_at"] = None
    item["done_at"] = None
    item["done_how"] = None


def shown_anyway(item: dict, mode: dict, profile: dict, now: datetime) -> bool:
    """What a mode that lists only the admitted still shows: critical, what
    the mode admits (sphere, tag or permit), what the user pulled or put
    back, and their own threads with Ara."""
    return (level(item, now) == "critical" or admitted(item, mode, profile)
            or bool(item.get("pulled")) or bool(item.get("own")))


# ---- views ------------------------------------------------------------------------------

def delivery_text(item: dict, focus: dict, profile: dict, now: datetime) -> str:
    mode = mode_at(focus, now)
    lvl = level(item, now)
    if item.get("state", "open") != "open":
        return "handled"
    if item.get("actor", "you") != "you":
        since = item.get("waiting_since")
        return f"waiting on {item['actor']}" + (f" since {fmt_duration(now - since)}" if since else "")
    if not item.get("released"):
        if item.get("snoozed_until") is not None and item["snoozed_until"] > now:
            return f"snoozed until {item['snoozed_until'].strftime('%a %H:%M')}"
        # Say which of the two held it. An admitted item waiting only because
        # it is not urgent enough must not be explained by "Work admits
        # customers" — that reads like a reason to ring.
        why = (admission_reason(item, mode, profile, now)
               if not (admitted(item, mode, profile) or has_permit(item, mode, profile))
               else f"{lvl} is below {mode['name']}’s bar")
        return f"held until {next_breakpoint(focus, now).strftime('%a %H:%M')} — {why}"
    if breaks_through(item, mode, profile, now):
        pushed = item.get("pushed") or []
        return (f"pushed {pushed[-1].strftime('%H:%M')}" if pushed else "in Now") + f" — {admission_reason(item, mode, profile, now)}"
    if lvl == "passive":
        return "listed — passive never pushes"
    if RANK[lvl] < RANK[mode["threshold"]]:
        return f"in Next — {lvl} is below {mode['name']}’s bar"
    return f"in Next — {lvl}, but {admission_reason(item, mode, profile, now)}"


def explain(item: dict, focus: dict, profile: dict, now: datetime) -> dict:
    """The three fields every row shows, each with its reason."""
    return {
        "importance": f"{item['importance']:g}/5 · {item.get('importance_from') or 'default'}",
        "urgency": urgency_text(item, now),
        "delivery": delivery_text(item, focus, profile, now),
        "level": level(item, now),
    }


def rank_key(item: dict, now: datetime) -> tuple:
    """How the list and the digest order items: level, then importance, then
    the nearest deadline, then the title."""
    due = item.get("due")
    return (-RANK[level(item, now)], -item["importance"], (due - now).total_seconds() if due else float("inf"),
            item.get("title") or "")


def sections(items: list[dict], focus: dict, profile: dict, now: datetime) -> dict:
    """Now · Next · Held · Waiting — and, in a mode that lists only what it
    admits (``only_admitted``), Not now: the released items the mode does not
    admit, folded away like Held. What is in Now breaks through, so it is
    admitted by definition; the fold only ever takes from Next."""
    mode = mode_at(focus, now)
    fold = bool(mode.get("only_admitted"))
    now_l, next_l, held, waiting, not_now = [], [], [], [], []
    for i in items:
        if i.get("state", "open") != "open":
            continue
        if i.get("actor", "you") != "you":
            waiting.append(i)
        elif not i.get("released"):
            held.append(i)
        elif breaks_through(i, mode, profile, now):
            now_l.append(i)
        elif fold and not shown_anyway(i, mode, profile, now):
            not_now.append(i)
        else:
            next_l.append(i)

    for lst in (now_l, next_l, held, not_now):
        lst.sort(key=lambda i: rank_key(i, now))
    waiting.sort(key=lambda i: (i.get("waiting_since") or now).timestamp())
    return {"now": now_l, "next": next_l, "held": held, "waiting": waiting, "not_now": not_now,
            "mode": mode, "next_breakpoint": next_breakpoint(focus, now)}


# ---- the life store -------------------------------------------------------------------

KB = "https://w3id.org/retinue/kb#"


def _lit(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


# What N-Triples forbids inside an IRI: controls, the space, and <>"{}|^`\.
_IRI_FORBIDDEN = re.compile(r'[\x00-\x20<>"{}|^`\\]')


def _iri_part(value) -> str:
    """A value spliced into an IRI, with what N-Triples forbids there
    percent-encoded, so one odd sphere or actor name cannot make the whole
    file unloadable. Letters in any script stay as they are, as an IRI
    allows; the words the model itself writes (sphere_id) pass unchanged."""
    return _IRI_FORBIDDEN.sub(lambda m: "".join(f"%{b:02X}" for b in m.group(0).encode("utf-8")), str(value))


def to_ntriples(items: list[dict], subject_for) -> str:
    """Deterministic, blank-node-free N-Triples for the four properties, so the
    dashboard's question — what wants attention, at which level — is a SELECT."""
    lines = []
    for item in items:
        s = _iri_part(subject_for(item))
        importance = "%g" % item["importance"]
        lead_minutes = int(item["lead"].total_seconds() // 60)
        lines.append(f"<{s}> <{KB}importance> {_lit(importance)}^^<http://www.w3.org/2001/XMLSchema#decimal> .")
        lines.append(f"<{s}> <{KB}leadTime> {_lit('PT%dM' % lead_minutes)}^^<http://www.w3.org/2001/XMLSchema#duration> .")
        lines.append(f"<{s}> <{KB}sphere> <urn:retinue:sphere:{_iri_part(item['sphere'])}> .")
        for tag in item.get("tags") or []:
            lines.append(f"<{s}> <{KB}tag> <urn:retinue:sphere:{_iri_part(tag)}> .")
        if item.get("due") is not None:
            lines.append(f"<{s}> <{KB}due> {_lit(item['due'].isoformat())}^^<http://www.w3.org/2001/XMLSchema#dateTime> .")
        actor = item.get("actor") or "you"
        lines.append(f"<{s}> <{KB}currentActor> <urn:retinue:actor:{_iri_part(actor.replace(' ', '-'))}> .")
    return "\n".join(sorted(set(lines))) + ("\n" if lines else "")


def write_if_changed(path: Path, text: str) -> bool:
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return True
