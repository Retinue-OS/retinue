"""Calendar triggers: "at a time of day, on these weekdays (or dates), in
this zone" — the clock the scheduler keeps beside ``interval_seconds``.

A job declares one with these fields (docs/scheduling.md, "Calendar
triggers"):

    at     "HH:MM", or a list of them — the local time(s) of day
    days   weekdays, as the focus schedule writes them: "mon-fri",
           "mon,wed,fri", "sat, sun", "weekend", a list of those; default
           every day. Not with ``date``.
    date   "YYYY-MM-DD", or a list of them: one-off dates instead of weekdays
    tz     an IANA zone ("Europe/Zurich"); default the instance's zone (the
           attention model's: ATTENTION_TZ, RETINUE_DISPLAY_TZ, TZ, local)
    lead   how late a slot may still fire ("30m", "2h", minutes as a number);
           default DEFAULT_LEAD_MINUTES. A slot the scheduler finds later than
           that — the container was down — is skipped, never fired late.

and, written by the framework rather than by hand (chamber apps' snooze and
skip, scripts/chamber_apps.py):

    extra       instants (ISO) that fire once besides the slots: a snooze
    skip_dates  local dates ("YYYY-MM-DD") on which nothing fires: "not today"
    skip_slots  single instants (ISO) that do not fire: "done ahead of time"

Wall-clock times are read in the zone, so a daily 07:00 stays 07:00 across a
daylight-saving change. A time that does not exist on the day the clocks go
forward (02:30 in most of Europe) fires when the clock would have shown it,
the length of the gap later (03:30); a time that exists twice on the day they
go back fires once, at its first occurrence.

Stdlib only.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import attention as _attention

DEFAULT_LEAD_MINUTES = 15.0
# The longest lead a trigger may declare: the scheduler looks back this far
# at most, and a reminder that may fire a week late is not a reminder.
MAX_LEAD_MINUTES = 7 * 24 * 60.0
# How far back latest() looks when nothing bounds it (a day-of-week trigger
# has a slot within a week; anything older is long missed).
_LOOKBACK = timedelta(days=8)

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

CALENDAR_FIELDS = ("at", "days", "date", "tz", "lead")


def is_calendar(job: dict) -> bool:
    """Whether a job is timed by the calendar (it names ``at``)."""
    return isinstance(job, dict) and job.get("at") not in (None, "", [])


def occurrence_key(when: datetime) -> str:
    """The one spelling of an instant every party uses to name an occurrence
    — the scheduler, the gateway's reminder items and the Android shell's
    delivery confirmations: UTC, to the second, with a Z."""
    return when.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_instant(value) -> datetime | None:
    """An ISO instant with a zone (a trailing Z included), as an aware
    datetime; None when it is not one."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def zone_named(name, default) -> ZoneInfo:
    """``name`` as a zone, or ``default`` when no name is given. ValueError
    for a name that is not an IANA zone."""
    if name in (None, ""):
        return default
    if not isinstance(name, str):
        raise ValueError(f"tz must be an IANA zone name, not {name!r}")
    try:
        return ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ValueError(f"unknown time zone {name!r} (use an IANA name such as Europe/Zurich)")


@dataclass
class Trigger:
    times: list[time]
    tz: object
    lead: timedelta
    weekdays: set[int] | None = None     # None: every day (or dates instead)
    dates: list[date] | None = None      # one-off dates instead of weekdays
    extra: list[datetime] = field(default_factory=list)
    skip_dates: set[date] = field(default_factory=set)
    skip_slots: set[str] = field(default_factory=set)

    # -- the slots ------------------------------------------------------------

    def _days_matching(self, first: date, last: date):
        if self.dates is not None:
            for d in self.dates:
                if first <= d <= last:
                    yield d
            return
        d = first
        while d <= last:
            if self.weekdays is None or d.weekday() in self.weekdays:
                yield d
            d += timedelta(days=1)

    def _skipped(self, when: datetime) -> bool:
        return (when.astimezone(self.tz).date() in self.skip_dates
                or occurrence_key(when) in self.skip_slots)

    def slots(self, start: datetime, end: datetime) -> list[datetime]:
        """Every firing instant in (start, end], oldest first, in the zone."""
        if end <= start:
            return []
        first = start.astimezone(self.tz).date() - timedelta(days=1)
        last = end.astimezone(self.tz).date() + timedelta(days=1)
        found = set()
        for day in sorted(set(self._days_matching(first, last))):
            for t in self.times:
                when = local_instant(day, t, self.tz)
                if start < when <= end:
                    found.add(when)
        for when in self.extra:
            if start < when <= end:
                found.add(when.astimezone(self.tz))
        return sorted(w for w in found if not self._skipped(w))

    def latest(self, now: datetime, floor: datetime | None = None) -> datetime | None:
        """The most recent instant at or before ``now`` and after ``floor``."""
        start = floor if floor is not None else now - _LOOKBACK
        start = max(start, now - _LOOKBACK - self.lead)
        hits = self.slots(start, now)
        return hits[-1] if hits else None

    def upcoming(self, after: datetime, horizon: timedelta = timedelta(days=2),
                 limit: int = 20) -> list[datetime]:
        """The instants after ``after`` within ``horizon``, at most ``limit``."""
        return self.slots(after, after + horizon)[:limit]

    def next_after(self, after: datetime, horizon: timedelta = timedelta(days=400)) -> datetime | None:
        # Day by day in weekly windows, so a far one-off date costs little.
        cursor = after
        end = after + horizon
        while cursor < end:
            window = min(end, cursor + timedelta(days=31))
            hits = self.slots(cursor, window)
            if hits:
                return hits[0]
            cursor = window
        return None


def local_instant(day: date, t: time, tz) -> datetime:
    """``day`` at the wall-clock time ``t`` in ``tz``. A time inside a
    spring-forward gap resolves to the instant the clock would have shown it
    (the gap's length later); a time that occurs twice resolves to the first."""
    naive = datetime.combine(day, t)
    aware = naive.replace(tzinfo=tz, fold=0)
    # Round-trip through UTC: a non-existent wall time comes back changed.
    # fold=0 there reads the time with the offset before the change, which
    # lands that much after the gap starts — 02:30 → 03:30 for a one-hour gap.
    return aware.astimezone(timezone.utc).astimezone(tz)


def parse(job: dict, default_tz) -> Trigger:
    """The calendar trigger a job declares. ValueError, with a message fit
    for an API answer or a log line, when a field is unusable."""
    times = []
    for raw in _as_list(job.get("at")):
        m = _TIME_RE.match(str(raw).strip()) if isinstance(raw, (str, int)) else None
        if not m:
            raise ValueError(f"at must be a time of day as HH:MM, not {raw!r}")
        times.append(time(int(m.group(1)), int(m.group(2))))
    if not times:
        raise ValueError("at is required: a time of day as HH:MM, or a list of them")
    if len(times) > 24:
        raise ValueError("at names more than 24 times")
    tz = zone_named(job.get("tz"), default_tz)

    lead_raw = job.get("lead")
    if lead_raw in (None, ""):
        lead_min = DEFAULT_LEAD_MINUTES
    else:
        lead_min = _attention.parse_lead(lead_raw)
        if lead_min is None or not (0 <= lead_min <= MAX_LEAD_MINUTES):
            raise ValueError(f"lead must be a span such as 30m, 2h or 1d (at most 7d), not {lead_raw!r}")

    dates = None
    weekdays = None
    if job.get("date") not in (None, "", []):
        if job.get("days") not in (None, "", []):
            raise ValueError("name either days or date, not both")
        dates = []
        for raw in _as_list(job.get("date")):
            if not isinstance(raw, str) or not _DATE_RE.match(raw.strip()):
                raise ValueError(f"date must be YYYY-MM-DD, not {raw!r}")
            try:
                dates.append(date.fromisoformat(raw.strip()))
            except ValueError:
                raise ValueError(f"no such date: {raw!r}")
        if len(dates) > 366:
            raise ValueError("date names more than 366 dates")
        dates.sort()
    elif job.get("days") not in (None, "", []):
        try:
            tokens = _attention.parse_days(job.get("days"))
        except ValueError as exc:
            raise ValueError(str(exc))
        tokens.discard("holiday")
        if not tokens:
            raise ValueError("days names no weekday")
        weekdays = {_WEEKDAYS.index(t) for t in tokens}

    extra = []
    for raw in _as_list(job.get("extra")):
        when = parse_instant(raw)
        if when is not None:
            extra.append(when)
    skip_dates = set()
    for raw in _as_list(job.get("skip_dates")):
        try:
            skip_dates.add(date.fromisoformat(str(raw)))
        except ValueError:
            continue
    skip_slots = set()
    for raw in _as_list(job.get("skip_slots")):
        when = parse_instant(raw)
        if when is not None:
            skip_slots.add(occurrence_key(when))
    return Trigger(times=sorted(set(times)), tz=tz, lead=timedelta(minutes=lead_min),
                   weekdays=weekdays, dates=dates, extra=extra,
                   skip_dates=skip_dates, skip_slots=skip_slots)


def describe(trigger: Trigger) -> str:
    """One line for a listing: "07:00 mon-fri Europe/Zurich, lead 15m"."""
    times = ",".join(t.strftime("%H:%M") for t in trigger.times)
    if trigger.dates is not None:
        when = ",".join(d.isoformat() for d in trigger.dates[:3]) + ("…" if len(trigger.dates) > 3 else "")
    elif trigger.weekdays is None:
        when = "daily"
    else:
        when = ",".join(_WEEKDAYS[i] for i in sorted(trigger.weekdays))
    lead = int(trigger.lead.total_seconds() // 60)
    return f"{times} {when} {getattr(trigger.tz, 'key', trigger.tz)}, lead {lead}m"


__all__ = ["DEFAULT_LEAD_MINUTES", "MAX_LEAD_MINUTES", "CALENDAR_FIELDS", "Trigger",
           "is_calendar", "occurrence_key", "parse_instant", "zone_named", "local_instant",
           "parse", "describe"]
