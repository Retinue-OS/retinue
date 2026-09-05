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
from datetime import datetime, timedelta
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

DEFAULT_MODES = {
    "off":    {"id": "off",    "name": "Off",       "admits": [],                                                    "admit_tags": [],         "threshold": "critical",       "blurb": "only critical; the digest waits for the morning"},
    "home":   {"id": "home",   "name": "Home",      "admits": ["family", "health"],                                  "admit_tags": [],         "threshold": "time-sensitive", "blurb": "family and health may break through"},
    "deep":   {"id": "deep",   "name": "Deep work", "admits": [],                                                    "admit_tags": [],         "threshold": "critical",       "blurb": "only critical breaks through"},
    "open":   {"id": "open",   "name": "Open",      "admits": ["customers", "admin", "health", "friends", "family", "system"], "admit_tags": [], "threshold": "time-sensitive", "blurb": "every sphere admitted; time-sensitive rings"},
    "work":   {"id": "work",   "name": "Work",      "admits": ["customers", "admin", "health"],                      "admit_tags": ["health"], "threshold": "time-sensitive", "blurb": "customers, admin and health may break through"},
    "social": {"id": "social", "name": "Social",    "admits": ["friends", "family"],                                 "admit_tags": ["health"], "threshold": "time-sensitive", "blurb": "friends and family may break through"},
}
# minute of the local day → mode id
DEFAULT_SCHEDULE = [[0, "off"], [7 * 60, "home"], [8 * 60, "deep"], [12 * 60, "open"], [13 * 60, "work"], [17 * 60, "open"], [18 * 60, "social"], [22 * 60, "off"]]
DEFAULT_DIGEST_TIMES = [8 * 60, 12 * 60, 17 * 60, 21 * 60]
SWEEP_EVERY_MINUTES = 30


def default_focus() -> dict:
    """The focus document the gateway keeps (focus.json): modes, schedule, override."""
    return {"manual": None, "modes": json.loads(json.dumps(DEFAULT_MODES)), "schedule": [list(x) for x in DEFAULT_SCHEDULE], "digest_times": list(DEFAULT_DIGEST_TIMES)}


def default_profile() -> dict:
    """The attention profile (profile.json): importance priors, lead times, permits."""
    return {"priors": {}, "leads": dict(DEFAULT_LEADS), "permits": {mid: [] for mid in DEFAULT_MODES}, "learned": []}


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
        "state": a.get("state") or ("done" if doc.get("archived") else "open"),
        "released": bool(a.get("released", False)),
        "snoozed_until": parse_dt(a.get("snoozed_until")),
        "boost": int(a.get("boost", 0)),
        "last_level": a.get("last_level"),
        "pushed": [parse_dt(x) for x in a.get("pushed") or []],
    }


def item_to_attention(item: dict) -> dict:
    """The ``attention`` block to store back on the document."""
    iso = lambda d: d.isoformat() if d else None  # noqa: E731
    return {
        "importance": item["importance"], "importance_from": item.get("importance_from"),
        "due": iso(item.get("due")), "lead": item["lead"].total_seconds() / 60, "lead_from": item.get("lead_from"),
        "sphere": item["sphere"], "tags": list(item.get("tags") or []), "kind": item.get("kind_label"),
        "actor": item.get("actor"), "waiting_since": iso(item.get("waiting_since")), "sender": item.get("sender"),
        "critical": bool(item.get("critical")), "state": item.get("state", "open"), "released": bool(item.get("released")),
        "snoozed_until": iso(item.get("snoozed_until")), "boost": int(item.get("boost", 0)), "last_level": item.get("last_level"),
        "pushed": [iso(x) for x in item.get("pushed") or []],
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


def scheduled_id(focus: dict, now: datetime) -> str:
    m = minute_of_day(now)
    current = focus["schedule"][0][1]
    for start, mid in focus["schedule"]:
        if m >= start:
            current = mid
    return current


def mode_at(focus: dict, now: datetime) -> dict:
    return focus["modes"][focus.get("manual") or scheduled_id(focus, now)]


def admitted(item: dict, mode: dict, profile: dict) -> bool:
    if item["sphere"] in mode["admits"]:
        return True
    if any(t in mode.get("admit_tags", []) for t in item.get("tags") or []):
        return True
    sender = item.get("sender")
    return bool(sender) and sender in (profile.get("permits", {}).get(mode["id"]) or [])


def admission_reason(item: dict, mode: dict, profile: dict, now: datetime) -> str:
    if level(item, now) == "critical":
        return "critical rings in every mode"
    if item["sphere"] in mode["admits"]:
        return f"{mode['name']} admits {item['sphere']}"
    tag = next((t for t in item.get("tags") or [] if t in mode.get("admit_tags", [])), None)
    if tag:
        return f"{mode['name']} admits the tag {tag}"
    sender = item.get("sender")
    if sender and sender in (profile.get("permits", {}).get(mode["id"]) or []):
        return f"{sender} holds a {mode['name']} permit"
    if mode["threshold"] == "critical":
        return f"{mode['name']} admits only critical"
    return f"{mode['name']} does not admit {item['sphere']}"


def breaks_through(item: dict, mode: dict, profile: dict, now: datetime) -> bool:
    lvl = level(item, now)
    return lvl == "critical" or (RANK[lvl] >= RANK[mode["threshold"]] and admitted(item, mode, profile))


def next_breakpoint(focus: dict, now: datetime) -> datetime:
    """The next digest time or scheduled mode change. A scheduled step out of Off
    is not a breakpoint (the morning digest opens the day); a manual override
    suspends the schedule, so only digest times count until it is released."""
    m = minute_of_day(now)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    candidates = [t for t in focus["digest_times"] if t > m]
    if not focus.get("manual"):
        schedule = focus["schedule"]
        for k, (start, _mid) in enumerate(schedule):
            if start > m and (k == 0 or schedule[k - 1][1] != "off"):
                candidates.append(start)
    if candidates:
        return start_of_day + timedelta(minutes=min(candidates))
    return start_of_day + timedelta(days=1, minutes=min(focus["digest_times"]))


# ---- delivery -----------------------------------------------------------------------

def on_arrival(item: dict, focus: dict, profile: dict, now: datetime) -> dict:
    """Decide what happens to a newly arrived (or re-opened) item.

    Returns ``{"deliver": "push"|"list"|"hold", "level": ..., "reason": ...}`` and
    marks the item released where it is delivered or listed."""
    mode = mode_at(focus, now)
    lvl = level(item, now)
    item["last_level"] = lvl
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
    """Release what was held and say what the digest carries. In Off nothing is
    released and no digest goes out; the morning digest carries it."""
    mode = mode_at(focus, now)
    due = [i for i in items if i.get("state", "open") == "open" and not i.get("released") and i.get("actor", "you") == "you"
           and (i.get("snoozed_until") is None or i["snoozed_until"] <= now)]
    if mode["id"] == "off":
        return {"digest": None, "held": due, "reason": "Off has no digest"}
    for i in due:
        i["released"] = True
        i["snoozed_until"] = None
    if not due:
        return {"digest": None, "held": [], "reason": "nothing was held"}
    return {"digest": {"at": now, "items": due, "urgency": "normal", "topic": "digest"}, "held": [], "reason": "breakpoint"}


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
    """Per-class repeat policy: off by default, on for family in Off (the repeated-caller case)."""
    if item["sphere"] == "family" and mode["id"] == "off":
        return {"escalate": True, "reason": "a family repeat breaks through in Off"}
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
        until = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1, minutes=min(focus["digest_times"]))
    else:
        until = next_breakpoint(focus, now)
    item["released"] = False
    item["snoozed_until"] = until
    return until


def pull(item: dict) -> bool:
    if item.get("state", "open") != "open" or item.get("released"):
        return False
    item["released"] = True
    item["snoozed_until"] = None
    return True


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
        return f"held until {next_breakpoint(focus, now).strftime('%a %H:%M')} — {admission_reason(item, mode, profile, now)}"
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


def sections(items: list[dict], focus: dict, profile: dict, now: datetime) -> dict:
    mode = mode_at(focus, now)
    now_l, next_l, held, waiting = [], [], [], []
    for i in items:
        if i.get("state", "open") != "open":
            continue
        if i.get("actor", "you") != "you":
            waiting.append(i)
        elif not i.get("released"):
            held.append(i)
        elif breaks_through(i, mode, profile, now):
            now_l.append(i)
        else:
            next_l.append(i)

    def key(i):
        due = i.get("due")
        return (-RANK[level(i, now)], -i["importance"], (due - now).total_seconds() if due else float("inf"), i.get("title") or "")

    for lst in (now_l, next_l, held):
        lst.sort(key=key)
    waiting.sort(key=lambda i: (i.get("waiting_since") or now).timestamp())
    return {"now": now_l, "next": next_l, "held": held, "waiting": waiting, "mode": mode, "next_breakpoint": next_breakpoint(focus, now)}


# ---- the life store -------------------------------------------------------------------

KB = "https://w3id.org/retinue/kb#"


def _lit(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def to_ntriples(items: list[dict], subject_for) -> str:
    """Deterministic, blank-node-free N-Triples for the four properties, so the
    dashboard's question — what wants attention, at which level — is a SELECT."""
    lines = []
    for item in items:
        s = subject_for(item)
        importance = "%g" % item["importance"]
        lead_minutes = int(item["lead"].total_seconds() // 60)
        lines.append(f"<{s}> <{KB}importance> {_lit(importance)}^^<http://www.w3.org/2001/XMLSchema#decimal> .")
        lines.append(f"<{s}> <{KB}leadTime> {_lit('PT%dM' % lead_minutes)}^^<http://www.w3.org/2001/XMLSchema#duration> .")
        lines.append(f"<{s}> <{KB}sphere> <urn:retinue:sphere:{item['sphere']}> .")
        for tag in item.get("tags") or []:
            lines.append(f"<{s}> <{KB}tag> <urn:retinue:sphere:{tag}> .")
        if item.get("due") is not None:
            lines.append(f"<{s}> <{KB}due> {_lit(item['due'].isoformat())}^^<http://www.w3.org/2001/XMLSchema#dateTime> .")
        actor = item.get("actor") or "you"
        lines.append(f"<{s}> <{KB}currentActor> <urn:retinue:actor:{actor.replace(' ', '-')}> .")
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
