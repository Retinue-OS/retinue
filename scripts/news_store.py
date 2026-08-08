#!/usr/bin/env python3
"""The news store: one JSON file of link-sized news items, ranked at read time.

Design in one paragraph. A news item here is a *reference*, never a copy: a
title, the source it came from, a short excerpt the feed itself published, and
the URL to read it at. Ranking is deliberately one number per item — an
`importance` the news agent (see `.claude/agents/herald.md`) assigns, decayed by
the item's age with a per-item half-life, and pinned (no decay) while a dated
item has not lapsed yet. That is the whole model: no keyframe curves, no
interpolation, no second representation to keep in sync.

Why a plain JSON file rather than the life store: news is high-churn, disposable
data with no long-term value — a few hundred rows rewritten every hour. Putting
it in a chamber would mean a git commit and a QLever index rebuild per fetch, to
store facts nobody will query in a month. Dashboard conversations already set
this precedent (a JSON store on the persistent /root volume), and this follows
it. What *is* durable — what the user likes and dislikes — lives in
`preferences.md`, prose the agent maintains and anyone can read.

Files under NEWS_DIR (default /root/.retinue/news, on the persistent volume):

    items.json       every known item, newest first
    preferences.md   the agent's memory of what the user cares about
    feedback.jsonl   append-only log of user signals, consumed by the curator
    state.json       cursors (how much feedback the curator has folded in)
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

NEWS_DIR = Path(os.environ.get("NEWS_DIR", "/root/.retinue/news"))

# An item nobody has scored yet still has to rank somewhere: mid-scale, so a
# fresh unscored item outranks a stale low-importance one but loses to anything
# the agent actually flagged as interesting.
DEFAULT_IMPORTANCE = 2.5
MAX_IMPORTANCE = 5.0

# How fast an undated item loses relevance. Two days halves it, so a week-old
# blurb sits at an eighth of its original weight — present, but below anything
# from today. Feeds and the agent override it per item.
DEFAULT_HALF_LIFE_HOURS = float(os.environ.get("NEWS_HALF_LIFE_HOURS", "48"))

# Housekeeping bounds, applied on every fetch.
MAX_AGE_DAYS = int(os.environ.get("NEWS_MAX_AGE_DAYS", "30"))
MAX_ITEMS = int(os.environ.get("NEWS_MAX_ITEMS", "500"))

# A user tap on 👍/👎 nudges that one item right away, so the feed reacts before
# the curator next runs. The durable half of the same signal is the feedback log
# the agent later generalizes into preferences.md.
FEEDBACK_NUDGE = 1.0
# Something the user has opened is not gone, but it should not keep occupying
# the top of the feed either.
READ_FACTOR = 0.25

VALID_SIGNALS = ("up", "down", "read", "hide", "note")


# ── paths (functions, not constants, so tests can repoint NEWS_DIR) ───────────

def items_file() -> Path:
    return NEWS_DIR / "items.json"


def preferences_file() -> Path:
    return NEWS_DIR / "preferences.md"


def feedback_file() -> Path:
    return NEWS_DIR / "feedback.jsonl"


def state_file() -> Path:
    return NEWS_DIR / "state.json"


def curation_payload_file() -> Path:
    return NEWS_DIR / "pending-curation.json"


def _ensure_dir() -> None:
    NEWS_DIR.mkdir(parents=True, exist_ok=True)


def _write_atomic(path: Path, text: str) -> None:
    _ensure_dir()
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class _Lock:
    """Cross-process lock around a read-modify-write of items.json.

    The fetcher, the scorer and the gateway all mutate the same file from
    different processes; without this, an hourly fetch landing while the user
    taps 👍 would drop one of the two writes."""

    def __init__(self) -> None:
        _ensure_dir()
        self._path = NEWS_DIR / ".lock"
        self._fh = None

    def __enter__(self):
        self._fh = open(self._path, "a+")
        fcntl.flock(self._fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *_exc):
        try:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None
        return False


# ── time helpers ─────────────────────────────────────────────────────────────

def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_time(value) -> datetime | None:
    """Parse an ISO-8601 stamp (with or without zone, with or without time).

    Anything unparseable is None — a missing date must never crash a fetch."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    for candidate in (text, text + "T00:00:00+00:00"):
        try:
            dt = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


# ── items ────────────────────────────────────────────────────────────────────

def load_items() -> list[dict]:
    try:
        raw = items_file().read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return data.get("items", []) if isinstance(data, dict) else []


def save_items(items: list[dict]) -> None:
    _write_atomic(items_file(), json.dumps(
        {"generated": iso(now()), "items": items}, ensure_ascii=False, indent=1))


def add_items(new_items: list[dict]) -> int:
    """Merge freshly fetched items in, keeping what we already know about them.

    Re-fetching a feed re-delivers everything in it, so an item already scored,
    read or hidden must survive unchanged — only genuinely new ids are added.
    Returns the number of new items."""
    with _Lock():
        items = load_items()
        known = {i.get("id") for i in items}
        added = [i for i in new_items if i.get("id") and i["id"] not in known]
        if added:
            items = added + items
            items = _prune(items)
            save_items(items)
        return len(added)


def _prune(items: list[dict]) -> list[dict]:
    """Drop what nobody will look at again: long-expired dated items, anything
    past MAX_AGE_DAYS, and the tail beyond MAX_ITEMS."""
    cutoff = now() - timedelta(days=MAX_AGE_DAYS)
    lapsed = now() - timedelta(days=2)
    kept = []
    for item in items:
        expires = parse_time(item.get("expires"))
        if expires and expires < lapsed:
            continue
        seen = parse_time(item.get("published")) or parse_time(item.get("fetched"))
        if seen and seen < cutoff:
            continue
        kept.append(item)
    kept.sort(key=lambda i: parse_time(i.get("published"))
              or parse_time(i.get("fetched")) or datetime.min.replace(tzinfo=timezone.utc),
              reverse=True)
    return kept[:MAX_ITEMS]


def update_item(item_id: str, **fields) -> dict | None:
    """Patch one item in place. Returns the updated item, or None if unknown."""
    with _Lock():
        items = load_items()
        for item in items:
            if item.get("id") == item_id:
                item.update(fields)
                save_items(items)
                return item
        return None


# ── ranking ──────────────────────────────────────────────────────────────────

def item_age_hours(item: dict, at: datetime) -> float:
    seen = parse_time(item.get("published")) or parse_time(item.get("fetched"))
    if seen is None:
        return 0.0
    return max(0.0, (at - seen).total_seconds() / 3600.0)


def score(item: dict, at: datetime | None = None) -> float:
    """Relevance of one item at one instant.

    Three rules, in order:
      1. A dated item (`expires`) that has lapsed scores 0 — the announcement of
         an event on the 30th is worthless on the 31st.
      2. A dated item that has not lapsed keeps its full importance. It is
         *pending*, not ageing, so it stays put until the date passes and then
         drops out in one step.
      3. Everything else decays by its half-life: half the weight per
         `half_life_hours` elapsed since publication.
    Read items are damped rather than removed, so a mis-tap does not lose an
    item and a re-read is still one scroll away."""
    at = at or now()
    if item.get("hidden"):
        return 0.0
    importance = item.get("importance")
    if not isinstance(importance, (int, float)):
        importance = DEFAULT_IMPORTANCE
    importance = max(0.0, min(float(importance), MAX_IMPORTANCE))

    expires = parse_time(item.get("expires"))
    if expires is not None:
        if at > expires:
            return 0.0
        value = importance
    else:
        half_life = item.get("half_life_hours") or DEFAULT_HALF_LIFE_HOURS
        try:
            half_life = float(half_life)
        except (TypeError, ValueError):
            half_life = DEFAULT_HALF_LIFE_HOURS
        if half_life <= 0:
            half_life = DEFAULT_HALF_LIFE_HOURS
        value = importance * (0.5 ** (item_age_hours(item, at) / half_life))

    if item.get("read"):
        value *= READ_FACTOR
    return value


def ranked(scope: str = "feed", limit: int | None = None,
           at: datetime | None = None) -> list[dict]:
    """The feed: every item that still scores above zero, best first.

    scope: "feed" (unread, unhidden), "read", "hidden", or "all"."""
    at = at or now()
    out = []
    for item in load_items():
        if scope == "feed" and (item.get("read") or item.get("hidden")):
            continue
        if scope == "read" and not item.get("read"):
            continue
        if scope == "hidden" and not item.get("hidden"):
            continue
        value = score(item, at)
        if value <= 0 and scope != "hidden":
            continue
        entry = dict(item)
        entry["score"] = round(value, 4)
        out.append(entry)
    out.sort(key=lambda i: (-i["score"], i.get("title") or ""))
    return out[:limit] if limit else out


# ── preferences (the agent's durable memory) ─────────────────────────────────

PREFERENCES_TEMPLATE = """# News preferences

What Reto cares about, as learned from feedback on the news feed. Maintained by
the Herald agent — every line here should be traceable to something the user
did (a 👍, a 👎, a note), not to a guess.

## Interested in

_(nothing learned yet)_

## Not interested in

_(nothing learned yet)_

## Notes
"""


def load_preferences() -> str:
    try:
        return preferences_file().read_text(encoding="utf-8")
    except OSError:
        return PREFERENCES_TEMPLATE


def save_preferences(text: str) -> None:
    _write_atomic(preferences_file(), text)


# ── feedback (the transient half of the same signal) ─────────────────────────

def append_feedback(entry: dict) -> dict:
    _ensure_dir()
    entry = dict(entry)
    entry.setdefault("ts", iso(now()))
    with open(feedback_file(), "a", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        fcntl.flock(fh, fcntl.LOCK_UN)
    return entry


def all_feedback() -> list[dict]:
    try:
        lines = feedback_file().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def load_state() -> dict:
    try:
        return json.loads(state_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    _write_atomic(state_file(), json.dumps(state, ensure_ascii=False, indent=1))


def pending_feedback() -> tuple[list[dict], int]:
    """Feedback the curator has not folded into preferences.md yet, plus the
    cursor to store once it has. The cursor is a plain count because the log is
    append-only — no ids to track, no state to reconcile."""
    entries = all_feedback()
    consumed = int(load_state().get("feedback_consumed", 0) or 0)
    consumed = max(0, min(consumed, len(entries)))
    return entries[consumed:], len(entries)


def ack_feedback(cursor: int) -> None:
    state = load_state()
    state["feedback_consumed"] = int(cursor)
    state["feedback_consumed_at"] = iso(now())
    save_state(state)


def record_feedback(item_id: str | None, signal: str, note: str = "") -> dict:
    """Apply one user signal: log it for the curator, and nudge the item now.

    The log is what teaches the agent; the nudge is what makes the feed feel
    alive between curation runs. Both halves matter — an agent-only loop feels
    broken until the next run, and a nudge-only loop forgets everything."""
    if signal not in VALID_SIGNALS:
        raise ValueError(f"unknown signal: {signal}")
    item = None
    if item_id:
        if signal == "read":
            item = update_item(item_id, read=True, read_at=iso(now()))
        elif signal == "hide":
            item = update_item(item_id, hidden=True, hidden_at=iso(now()))
        elif signal in ("up", "down"):
            with _Lock():
                items = load_items()
                for candidate in items:
                    if candidate.get("id") == item_id:
                        base = candidate.get("importance")
                        if not isinstance(base, (int, float)):
                            base = DEFAULT_IMPORTANCE
                        delta = FEEDBACK_NUDGE if signal == "up" else -FEEDBACK_NUDGE
                        candidate["importance"] = max(0.0, min(MAX_IMPORTANCE,
                                                               float(base) + delta))
                        if signal == "down":
                            candidate["read"] = True
                        item = candidate
                        break
                if item is not None:
                    save_items(items)
        else:
            items = load_items()
            item = next((i for i in items if i.get("id") == item_id), None)
        if item is None and signal != "note":
            raise KeyError(item_id)

    entry = {"signal": signal, "note": note}
    if item_id:
        entry["item"] = item_id
    if item:
        entry["title"] = item.get("title")
        entry["source"] = item.get("source")
        entry["url"] = item.get("url")
        entry["tags"] = item.get("tags") or []
    return append_feedback(entry)
