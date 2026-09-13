#!/usr/bin/env python3
"""Checks for the CalDAV gateway's read API (GET /calendars, /events, /event).

Runnable without the `caldav` package or network access, like its sibling
tests/test_caldav_send_policy.py: the gateway's `caldav` import is guarded, the
CalDAV principal is a stub, and the iCalendar serialization is exercised against
plain dicts — the gateway's component helpers only ever call `.get()`, and
icalendar normalizes property names to upper case, so an upper-cased dict stands
in for a VEVENT component exactly.

    python3 tests/test_caldav_read.py
"""
import datetime
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_caldav_gateway(pending_dir, calendar_id="", send_policy=None):
    """Load scripts/caldav-gateway.py with the given read-relevant config."""
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    os.environ["CALDAV_PENDING_SENDS_DIR"] = str(pending_dir)
    os.environ["CALDAV_CALENDAR_ID"] = calendar_id
    # Reads are deliberately NOT governed by the send policy; every test here
    # runs with the strictest one (verify) to prove a read is never gated by it.
    os.environ["CALDAV_SEND_POLICY"] = json.dumps(
        send_policy if send_policy is not None else [{"account": "*", "category": "verify"}])
    os.environ.pop("SEND_APPROVAL_SLUG", None)
    spec = importlib.util.spec_from_file_location(
        "caldav_gateway_read_under_test", SCRIPTS_DIR / "caldav-gateway.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── Stubs standing in for the caldav library's objects ────────────────────────

class _Prop:
    """An icalendar date/time property: the value lives in `.dt`."""

    def __init__(self, dt):
        self.dt = dt


class _Event:
    """A caldav event object exposing one parsed VEVENT component."""

    def __init__(self, component):
        self.icalendar_component = component


class NotFoundError(Exception):
    """Stands in for caldav.lib.error.NotFoundError (the package is not installed).

    The gateway recognizes it by name when the real class cannot be imported,
    which is exactly the situation here.
    """


class _Calendar:
    def __init__(self, cal_id, url, name, events=(), by_uid=None):
        self.id = cal_id
        self.url = url
        self.name = name
        self._events = list(events)
        self._by_uid = by_uid or {}

    def search(self, start=None, end=None, event=None, expand=None):
        return list(self._events)

    def get_event_by_uid(self, uid):
        if uid not in self._by_uid:
            raise NotFoundError(f"{uid} not found on server")
        return _Event(self._by_uid[uid])


class _LegacyCalendar(_Calendar):
    """A caldav release exposing only the deprecated alias, not the current name."""

    get_event_by_uid = None  # not callable, so the finder falls through

    def event_by_uid(self, uid):
        return _Calendar.get_event_by_uid(self, uid)


class _GenericLookupCalendar(_Calendar):
    """A caldav release shipping only the generic object lookup."""

    get_event_by_uid = None
    event_by_uid = None

    def get_object_by_uid(self, uid):
        return _Calendar.get_event_by_uid(self, uid)


class _UnreachableCalendar(_Calendar):
    """A calendar the gateway cannot reach at all (transport/auth failure)."""

    def get_event_by_uid(self, uid):
        raise RuntimeError("connection reset by peer")


class _Principal:
    def __init__(self, calendars, default=None):
        self._calendars = list(calendars)
        self._default = default if default is not None else self._calendars[0]

    def calendars(self):
        return list(self._calendars)

    def calendar(self):
        return self._default


def _timed(summary, start, end=None, **extra):
    component = {"UID": f"uid-{summary}", "SUMMARY": summary, "DTSTART": _Prop(start)}
    if end is not None:
        component["DTEND"] = _Prop(end)
    component.update(extra)
    return component


# ── Window parsing ───────────────────────────────────────────────────────────

def test_read_window_defaults_to_now_plus_default_days():
    with tempfile.TemporaryDirectory() as tmp:
        cg = _load_caldav_gateway(tmp)
        before = datetime.datetime.now()
        start, end = cg._parse_read_window("", "", "")
        assert before <= start <= datetime.datetime.now()
        assert abs((end - start).days - cg.READ_DEFAULT_DAYS) <= 1
        # --days overrides the default span.
        start, end = cg._parse_read_window("2026-09-20", "", "7")
        assert start == datetime.datetime(2026, 9, 20)
        assert end == datetime.datetime(2026, 9, 27)
    print("ok: read window defaults to now + READ_DEFAULT_DAYS")


def test_read_window_is_half_open_and_a_bare_end_date_covers_its_day():
    with tempfile.TemporaryDirectory() as tmp:
        cg = _load_caldav_gateway(tmp)
        # The window is [start, end), as a CalDAV time-range query is. A bare end
        # date therefore becomes the FOLLOWING midnight, so start == end still
        # reads the whole day — with no gap in its last second, which an
        # inclusive 23:59:59 bound would leave (CalDAV bounds are whole seconds).
        start, end = cg._parse_read_window("2026-09-20", "2026-09-20", "")
        assert start == datetime.datetime(2026, 9, 20, 0, 0, 0)
        assert end == datetime.datetime(2026, 9, 21, 0, 0, 0)
        # An explicit time is taken as given, exclusive like the rest.
        _, end = cg._parse_read_window("2026-09-20", "2026-09-20T12:00:00", "")
        assert end == datetime.datetime(2026, 9, 20, 12, 0, 0)
    print("ok: the window is half-open and a bare end date covers its whole day")


def test_read_window_rejects_bad_input():
    with tempfile.TemporaryDirectory() as tmp:
        cg = _load_caldav_gateway(tmp)
        for start, end, days in (
            ("not-a-date", "", ""),          # unparseable start
            ("2026-09-20", "nonsense", ""),  # unparseable end
            ("2026-09-20", "", "soon"),      # non-numeric days
            ("2026-09-20", "", "0"),         # non-positive days
            ("2026-09-20", "2026-09-19", ""),  # end date before the start date
            ("2026-09-20", "2026-09-18", ""),  # … and further before it
            # The window is half-open, so an end AT the start is empty, which is
            # a bad request rather than a silently empty answer.
            ("2026-09-20T12:00:00", "2026-09-20T12:00:00", ""),
            # float() accepts these, and timedelta then raises something the
            # handler would not map to a 400 — so reject them here instead.
            ("2026-09-20", "", "inf"),
            ("2026-09-20", "", "nan"),
            ("2026-09-20", "", "1e10"),
            ("9999-12-31", "", "365"),       # window past datetime.max
            # A bare end date is advanced by a day, which overflows here — and
            # must answer 400 like every other bad window, not crash the read.
            ("9999-12-30", "9999-12-31", ""),
        ):
            try:
                cg._parse_read_window(start, end, days)
            except ValueError:
                continue
            raise AssertionError(f"expected ValueError for {(start, end, days)!r}")
    print("ok: read window rejects bad input")


def test_read_window_aligns_mixed_timezones_without_moving_the_instant():
    with tempfile.TemporaryDirectory() as tmp:
        cg = _load_caldav_gateway(tmp)
        # One aware bound and one naive one must still be comparable: the naive
        # side is read in this container's timezone rather than raising.
        start, end = cg._parse_read_window("2026-09-20T00:00:00Z", "2026-09-27", "")
        assert start.tzinfo is not None and end.tzinfo is not None
        assert end > start
        start, end = cg._parse_read_window("2026-09-20", "2026-09-27T00:00:00+02:00", "")
        assert start.tzinfo is not None and end > start
        # Crucially it is a CONVERSION, not a restamping: the naive bound keeps
        # the instant it named. Stamping the other bound's offset onto it would
        # shift the window by that offset — which, with the start omitted and
        # defaulting to now(), silently moved "now" hours into the past.
        naive = datetime.datetime(2026, 9, 20, 13, 30)
        aware = datetime.datetime(2026, 9, 27, tzinfo=datetime.timezone(datetime.timedelta(hours=2)))
        aligned_start, _ = cg._align_timezones(naive, aware)
        assert aligned_start == naive.astimezone(), aligned_start
        assert aligned_start.astimezone(datetime.timezone.utc) == \
            naive.astimezone(datetime.timezone.utc)
        _, aligned_end = cg._align_timezones(aware, naive)
        assert aligned_end == naive.astimezone()
        # An omitted start with an offset-bearing end: the window must still
        # start at the present instant, not at a shifted wall clock.
        before = datetime.datetime.now(datetime.timezone.utc)
        start, end = cg._parse_read_window("", "2999-09-27T00:00:00+02:00", "")
        after = datetime.datetime.now(datetime.timezone.utc)
        assert before <= start.astimezone(datetime.timezone.utc) <= after, start
    print("ok: mixed-timezone bounds are aligned without moving the instant")


def test_read_limit_clamped_to_maximum():
    with tempfile.TemporaryDirectory() as tmp:
        cg = _load_caldav_gateway(tmp)
        assert cg._parse_read_limit("") == cg.READ_MAX_EVENTS
        assert cg._parse_read_limit("10") == 10
        assert cg._parse_read_limit(str(cg.READ_MAX_EVENTS * 10)) == cg.READ_MAX_EVENTS
        for bad in ("many", "0", "-3"):
            try:
                cg._parse_read_limit(bad)
            except ValueError:
                continue
            raise AssertionError(f"expected ValueError for {bad!r}")
    print("ok: read limit clamped to READ_MAX_EVENTS")


# ── Event serialization ──────────────────────────────────────────────────────

def test_unusable_read_tunables_fall_back_to_their_defaults():
    for name, value in (("CALDAV_READ_MAX_EVENTS", "-1"),   # matched[:-1] ≈ everything
                        ("CALDAV_READ_MAX_EVENTS", "0"),    # matched[:0] = nothing
                        ("CALDAV_READ_MAX_EVENTS", "lots"),
                        ("CALDAV_READ_DEFAULT_DAYS", "inf"),
                        ("CALDAV_READ_DEFAULT_DAYS", "-7"),
                        ("CALDAV_READ_DEFAULT_DAYS", "soon"),
                        # int() parses this happily; math.isfinite() then
                        # overflows converting it to float, at import time.
                        ("CALDAV_READ_MAX_EVENTS", "1" * 400),
                        # Positive and finite, yet no window can be built from
                        # it — so every no-end read would 400 at request time.
                        ("CALDAV_READ_DEFAULT_DAYS", "1e10")):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ[name] = value
            try:
                cg = _load_caldav_gateway(tmp)
                # A cap that would defeat its own purpose is refused, not served.
                assert cg.READ_MAX_EVENTS == 500, (name, value, cg.READ_MAX_EVENTS)
                assert cg.READ_DEFAULT_DAYS == 30.0, (name, value, cg.READ_DEFAULT_DAYS)
            finally:
                os.environ.pop(name, None)
    print("ok: unusable read tunables fall back to their defaults")


def test_serialize_timed_event():
    with tempfile.TemporaryDirectory() as tmp:
        cg = _load_caldav_gateway(tmp)
        tz = datetime.timezone(datetime.timedelta(hours=2))
        component = {
            "UID": "abc@example.com",
            "SUMMARY": "Dentist",
            "DESCRIPTION": "annual checkup",
            "LOCATION": "Bahnhofstrasse 1",
            "STATUS": "CONFIRMED",
            "DTSTART": _Prop(datetime.datetime(2026, 9, 3, 14, 0, tzinfo=tz)),
            "DTEND": _Prop(datetime.datetime(2026, 9, 3, 14, 30, tzinfo=tz)),
        }
        entry = cg._serialize_event(component, {"id": "cal-1", "url": "https://dav/c1", "name": "Personal"})
        assert entry["uid"] == "abc@example.com"
        assert entry["summary"] == "Dentist"
        assert entry["start"] == "2026-09-03T14:00:00+02:00"
        assert entry["end"] == "2026-09-03T14:30:00+02:00"
        assert entry["all_day"] is False
        assert entry["description"] == "annual checkup"
        assert entry["location"] == "Bahnhofstrasse 1"
        assert entry["status"] == "CONFIRMED"
        assert entry["recurring"] is False
        assert entry["calendar"] == "Personal"
        assert entry["calendar_id"] == "cal-1"
    print("ok: timed event serialized with its timezone")


def test_serialize_all_day_event_round_trips_into_the_write_path():
    with tempfile.TemporaryDirectory() as tmp:
        cg = _load_caldav_gateway(tmp)
        component = {"UID": "x", "SUMMARY": "Conference",
                     "DTSTART": _Prop(datetime.date(2026, 9, 10)),
                     "DTEND": _Prop(datetime.date(2026, 9, 12))}
        entry = cg._serialize_event(component)
        assert entry["all_day"] is True
        # The DTEND is reported as iCalendar has it (exclusive) — the same value
        # /create-event takes, so a read result can be written back unchanged.
        assert (entry["start"], entry["end"]) == ("2026-09-10", "2026-09-12")
        assert cg._parse_event_datetime(entry["start"], True) == datetime.date(2026, 9, 10)
        assert cg._parse_event_datetime(entry["end"], True) == datetime.date(2026, 9, 12)
        # No calendar identity given → stable shape, empty strings.
        assert entry["calendar"] == "" and entry["calendar_id"] == ""
    print("ok: all-day event round-trips into the write path")


def test_serialize_derives_a_missing_end():
    with tempfile.TemporaryDirectory() as tmp:
        cg = _load_caldav_gateway(tmp)
        # DURATION instead of DTEND.
        entry = cg._serialize_event({
            "DTSTART": _Prop(datetime.datetime(2026, 9, 3, 14, 0)),
            "DURATION": _Prop(datetime.timedelta(minutes=45)),
        })
        assert entry["end"] == "2026-09-03T14:45:00"
        # Neither: RFC 5545 makes a timed event instantaneous …
        entry = cg._serialize_event({"DTSTART": _Prop(datetime.datetime(2026, 9, 3, 14, 0))})
        assert entry["end"] == "2026-09-03T14:00:00"
        # … and an all-day event one day long.
        entry = cg._serialize_event({"DTSTART": _Prop(datetime.date(2026, 9, 3))})
        assert entry["end"] == "2026-09-04"
        # A component without any start still serializes (no crash on bad data).
        entry = cg._serialize_event({"SUMMARY": "orphan"})
        assert entry["start"] == "" and entry["end"] == "" and entry["summary"] == "orphan"
    print("ok: a missing end is derived per RFC 5545")


def test_serialize_flags_recurrence():
    with tempfile.TemporaryDirectory() as tmp:
        cg = _load_caldav_gateway(tmp)
        base = {"DTSTART": _Prop(datetime.datetime(2026, 9, 3, 9, 0))}
        assert cg._serialize_event(dict(base, RRULE="FREQ=WEEKLY"))["recurring"] is True
        assert cg._serialize_event(dict(base, RDATE="20260910T090000"))["recurring"] is True
        # One expanded instance carries RECURRENCE-ID rather than the rule.
        assert cg._serialize_event(dict(base, **{"RECURRENCE-ID": _Prop(
            datetime.datetime(2026, 9, 10, 9, 0))}))["recurring"] is True
        assert cg._serialize_event(base)["recurring"] is False
    print("ok: recurrence flagged for series and expanded instances")


def test_event_components_walks_a_calendar_instance():
    with tempfile.TemporaryDirectory() as tmp:
        cg = _load_caldav_gateway(tmp)

        class _Instance:
            def walk(self, name):
                assert name == "VEVENT"
                return [{"SUMMARY": "master"}, {"SUMMARY": "override"}]

        class _Both:
            icalendar_instance = _Instance()
            icalendar_component = {"SUMMARY": "master"}

        assert [c["SUMMARY"] for c in cg._event_components(_Both())] == ["master", "override"]
        # Only a single component available → that one.
        assert cg._event_components(_Event({"SUMMARY": "solo"})) == [{"SUMMARY": "solo"}]
        # Neither → nothing, rather than an exception.
        assert cg._event_components(object()) == []
    print("ok: event components walked from a calendar instance")


# ── Calendar selection and identity ──────────────────────────────────────────

def test_calendar_identity_survives_an_unreadable_property():
    with tempfile.TemporaryDirectory() as tmp:
        cg = _load_caldav_gateway(tmp)

        class _Hostile:
            id = "cal-1"
            url = "https://dav/c1"

            @property
            def name(self):
                raise RuntimeError("server said no")

        identity = cg._calendar_identity(_Hostile())
        assert identity == {"id": "cal-1", "url": "https://dav/c1", "name": ""}
        # Matching works on any of the three, and never on the empty one.
        assert cg._match_calendar(_Hostile(), "cal-1") is True
        assert cg._match_calendar(_Hostile(), "https://dav/c1") is True
        assert cg._match_calendar(_Hostile(), "") is False
    print("ok: calendar identity survives an unreadable property")


def test_pick_read_calendars():
    work = _Calendar("cal-work", "https://dav/work", "Work")
    home = _Calendar("cal-home", "https://dav/home", "Home")
    principal = _Principal([work, home])

    with tempfile.TemporaryDirectory() as tmp:
        cg = _load_caldav_gateway(tmp)  # CALDAV_CALENDAR_ID unset
        # Nothing configured, nothing requested → the whole account, because
        # "what is on my agenda" spans it (a write, by contrast, needs one).
        assert cg._pick_read_calendars(principal, None) == [work, home]
        # An explicit name/id/URL narrows to one.
        assert cg._pick_read_calendars(principal, "Home") == [home]
        assert cg._pick_read_calendars(principal, "cal-work") == [work]
        assert cg._pick_read_calendars(principal, "https://dav/home") == [home]
        # A calendar the CALLER named and that does not exist is a bad request
        # (ValueError → 400): a typo must not read as a CalDAV outage.
        try:
            cg._pick_read_calendars(principal, "nope")
        except ValueError as exc:
            assert "not found" in str(exc)
        else:
            raise AssertionError("expected ValueError for an unknown requested calendar")

    with tempfile.TemporaryDirectory() as tmp:
        # A CONFIGURED calendar that does not exist is a deployment fault
        # (RuntimeError → 502), not something the caller can fix.
        cg = _load_caldav_gateway(tmp, calendar_id="cal-typo")
        try:
            cg._pick_read_calendars(principal, None)
        except RuntimeError as exc:
            assert "CALDAV_CALENDAR_ID" in str(exc)
        else:
            raise AssertionError("expected RuntimeError for a misconfigured calendar")

    with tempfile.TemporaryDirectory() as tmp:
        cg = _load_caldav_gateway(tmp, calendar_id="cal-work")
        # Configured for one calendar → reads default to it …
        assert cg._pick_read_calendars(principal, None) == [work]
        # … "*" opens the whole account anyway, and a request still wins.
        assert cg._pick_read_calendars(principal, "*") == [work, home]
        assert cg._pick_read_calendars(principal, "Home") == [home]
    print("ok: read calendar selection")


def test_a_duplicate_display_name_is_ambiguous_not_a_coin_flip():
    # Display names are not unique on a CalDAV server: two calendars can answer
    # to "Work", and picking the first would make the answer depend on server
    # ordering — reading, or writing to, whichever came back first.
    first = _Calendar("cal-a", "https://dav/a", "Work")
    second = _Calendar("cal-b", "https://dav/b", "Work")
    principal = _Principal([first, second])
    with tempfile.TemporaryDirectory() as tmp:
        cg = _load_caldav_gateway(tmp)
        try:
            cg._pick_read_calendars(principal, "Work")
        except cg.BadRequest as exc:
            # A 400 naming both URLs, so the caller can pick one.
            assert "ambiguous" in str(exc) and "https://dav/a" in str(exc)
        else:
            raise AssertionError("expected BadRequest for an ambiguous calendar name")
        # An id or URL still resolves it, and so does the write path.
        assert cg._pick_read_calendars(principal, "cal-b") == [second]
        cg._connect_principal = lambda: principal
        assert cg._resolve_calendar("https://dav/a") is first
        try:
            cg._resolve_calendar("Work")
        except cg.BadRequest as exc:
            assert "ambiguous" in str(exc)
        else:
            raise AssertionError("expected BadRequest on the write path too")
    with tempfile.TemporaryDirectory() as tmp:
        # Configured ambiguously: a deployment fault (502), and /calendars says so.
        cg = _load_caldav_gateway(tmp, calendar_id="Work")
        cg._connect_principal = lambda: principal
        try:
            cg._pick_read_calendars(principal, None)
        except RuntimeError as exc:
            assert "CALDAV_CALENDAR_ID" in str(exc) and "ambiguous" in str(exc)
        else:
            raise AssertionError("expected RuntimeError for an ambiguous configured name")
        snapshot = cg._calendars_snapshot()
        assert snapshot["write_target"] is None
        assert "ambiguous" in snapshot["write_target_error"]
    print("ok: a duplicate display name is ambiguous, not a coin flip")


# ── Listing, filtering, uid lookup ───────────────────────────────────────────

def test_list_events_sorted_across_calendars():
    work = _Calendar("cal-work", "https://dav/work", "Work", events=[
        _Event(_timed("standup", datetime.datetime(2026, 9, 21, 9, 0),
                      datetime.datetime(2026, 9, 21, 9, 15))),
    ])
    home = _Calendar("cal-home", "https://dav/home", "Home", events=[
        _Event(_timed("dinner", datetime.datetime(2026, 9, 20, 19, 0),
                      datetime.datetime(2026, 9, 20, 21, 0))),
        _Event({"UID": "trip", "SUMMARY": "trip", "DTSTART": _Prop(datetime.date(2026, 9, 21)),
                "DTEND": _Prop(datetime.date(2026, 9, 23))}),
    ])
    with tempfile.TemporaryDirectory() as tmp:
        cg = _load_caldav_gateway(tmp)
        cg._connect_principal = lambda: _Principal([work, home])
        start, end = cg._parse_read_window("2026-09-20", "2026-09-27", "")
        events = cg._list_events_on_server(start, end)
        # Chronological across calendars; the all-day event of the 21st sorts
        # before that day's timed one (its start is a bare date).
        assert [e["summary"] for e in events] == ["dinner", "trip", "standup"]
        assert [e["calendar"] for e in events] == ["Home", "Home", "Work"]
        # A read is never gated by CALDAV_SEND_POLICY (verify, here): it
        # answered instead of registering anything pending.
        assert cg._outbound_policy_category() == "verify"
        assert cg._list_pending_sends_store() == []
    print("ok: events listed chronologically across calendars, ungated by send policy")


def test_events_sort_on_the_instant_not_the_string():
    tz_zurich = datetime.timezone(datetime.timedelta(hours=2))
    tz_utc = datetime.timezone.utc
    # 09:00+02:00 is 07:00 UTC, so it happens BEFORE 08:00+00:00 — while sorting
    # the ISO strings would put it after.
    early = _Calendar("cal-a", "https://dav/a", "A", events=[
        _Event(_timed("zurich-morning", datetime.datetime(2026, 9, 21, 9, 0, tzinfo=tz_zurich))),
    ])
    later = _Calendar("cal-b", "https://dav/b", "B", events=[
        _Event(_timed("utc-morning", datetime.datetime(2026, 9, 21, 8, 0, tzinfo=tz_utc))),
    ])
    with tempfile.TemporaryDirectory() as tmp:
        cg = _load_caldav_gateway(tmp)
        cg._connect_principal = lambda: _Principal([later, early])
        start, end = cg._parse_read_window("2026-09-20", "2026-09-27", "")
        events = cg._list_events_on_server(start, end)
        assert [e["summary"] for e in events] == ["zurich-morning", "utc-morning"]
        # An unparseable start sorts last rather than breaking the read.
        assert cg._event_sort_key({"start": "nonsense"}) == \
            datetime.datetime.max.replace(tzinfo=datetime.timezone.utc)
        assert cg._event_sort_key({}) == datetime.datetime.max.replace(tzinfo=datetime.timezone.utc)
    print("ok: events sort on the instant, not the ISO string")


def test_expanded_search_falls_back_when_unsupported():
    calls = []

    class _PickyCalendar(_Calendar):
        def search(self, start=None, end=None, event=None, expand=None):
            calls.append(expand)
            if expand:
                raise RuntimeError("expand not supported by this server")
            return list(self._events)

    cal = _PickyCalendar("c", "https://dav/c", "C", events=[
        _Event(_timed("weekly", datetime.datetime(2026, 9, 21, 9, 0))),
    ])
    with tempfile.TemporaryDirectory() as tmp:
        cg = _load_caldav_gateway(tmp)
        cg._connect_principal = lambda: _Principal([cal])
        start, end = cg._parse_read_window("2026-09-20", "2026-09-27", "")
        events = cg._list_events_on_server(start, end)
        # Expansion was tried first, then the plain time-range query — a server
        # that cannot expand still answers the read.
        assert calls == [True, None]
        assert [e["summary"] for e in events] == ["weekly"]
    print("ok: unsupported expansion falls back to a plain time-range search")


def test_query_filter_matches_text_fields():
    with tempfile.TemporaryDirectory() as tmp:
        cg = _load_caldav_gateway(tmp)
        entry = {"uid": "u-1", "summary": "Dentist", "description": "annual Checkup",
                 "location": "Zürich", "calendar": "Personal"}
        for needle in ("dentist", "checkup", "zürich", "u-1", "personal", ""):
            assert cg._matches_query(entry, needle) is True, needle
        assert cg._matches_query(entry, "plumber") is False
    print("ok: query filter matches an event's text fields")


def test_find_event_by_uid_across_calendars():
    wanted = _timed("Dentist", datetime.datetime(2026, 9, 3, 14, 0),
                    datetime.datetime(2026, 9, 3, 14, 30))
    empty = _Calendar("cal-work", "https://dav/work", "Work")
    holder = _Calendar("cal-home", "https://dav/home", "Home", by_uid={"uid-Dentist": wanted})
    with tempfile.TemporaryDirectory() as tmp:
        cg = _load_caldav_gateway(tmp)
        cg._connect_principal = lambda: _Principal([empty, holder])
        entry = cg._find_event_by_uid("uid-Dentist")
        assert entry["summary"] == "Dentist" and entry["calendar"] == "Home"
        # A uid no calendar holds is None (mapped to 404 by the handler), not an
        # error — the first calendar's "not found" must not abort the search.
        assert cg._find_event_by_uid("uid-nothing") is None
    with tempfile.TemporaryDirectory() as tmp:
        # A library release without the current method name still resolves,
        # through its own deprecated alias.
        cg = _load_caldav_gateway(tmp)
        legacy = _LegacyCalendar("cal-old", "https://dav/old", "Old", by_uid={"uid-Dentist": wanted})
        cg._connect_principal = lambda: _Principal([legacy])
        assert cg._find_event_by_uid("uid-Dentist")["summary"] == "Dentist"
    with tempfile.TemporaryDirectory() as tmp:
        # … and one that ships only the generic object lookup.
        cg = _load_caldav_gateway(tmp)
        generic = _GenericLookupCalendar("cal-gen", "https://dav/gen", "Generic",
                                        by_uid={"uid-Dentist": wanted})
        cg._connect_principal = lambda: _Principal([generic])
        assert cg._find_event_by_uid("uid-Dentist")["summary"] == "Dentist"
    print("ok: uid lookup spans the calendars a read covers")


def test_unreachable_calendar_is_not_reported_as_a_missing_event():
    wanted = _timed("Dentist", datetime.datetime(2026, 9, 3, 14, 0))
    broken = _UnreachableCalendar("cal-work", "https://dav/work", "Work")
    holder = _Calendar("cal-home", "https://dav/home", "Home", by_uid={"uid-Dentist": wanted})
    with tempfile.TemporaryDirectory() as tmp:
        cg = _load_caldav_gateway(tmp)
        cg._connect_principal = lambda: _Principal([broken, holder])
        # A transport/auth/server failure must NOT be swallowed into a None the
        # handler reports as 404 — an outage is a 502, and only a genuine
        # "not found" moves on to the next calendar.
        try:
            cg._find_event_by_uid("uid-Dentist")
        except RuntimeError as exc:
            assert "connection reset" in str(exc)
        else:
            raise AssertionError("a transport failure must not read as 'not found'")
        # The name-based recognition that makes that distinction work without
        # the caldav package installed.
        assert cg._is_not_found(NotFoundError("gone")) is True
        assert cg._is_not_found(RuntimeError("connection reset by peer")) is False
    print("ok: an unreachable calendar is not a missing event")


def test_calendars_snapshot_names_the_write_target():
    work = _Calendar("cal-work", "https://dav/work", "Work")
    home = _Calendar("cal-home", "https://dav/home", "Home")
    with tempfile.TemporaryDirectory() as tmp:
        cg = _load_caldav_gateway(tmp, calendar_id="cal-home")
        cg._connect_principal = lambda: _Principal([work, home], default=work)
        snapshot = cg._calendars_snapshot()
        assert [c["name"] for c in snapshot["calendars"]] == ["Work", "Home"]
        # Configured CALDAV_CALENDAR_ID → that is where a write lands.
        assert snapshot["write_target"]["id"] == "cal-home"
    with tempfile.TemporaryDirectory() as tmp:
        cg = _load_caldav_gateway(tmp)  # nothing configured
        cg._connect_principal = lambda: _Principal([work, home], default=work)
        snapshot = cg._calendars_snapshot()
        # … unset → the account's default calendar.
        assert snapshot["write_target"]["id"] == "cal-work"
        assert "write_target_error" not in snapshot
    with tempfile.TemporaryDirectory() as tmp:
        cg = _load_caldav_gateway(tmp, calendar_id="cal-typo")
        cg._connect_principal = lambda: _Principal([work, home], default=work)
        snapshot = cg._calendars_snapshot()
        # A configured calendar that matches nothing is a misconfiguration —
        # /create-event rejects it too — so discovery says so rather than
        # reporting "no write target" as a healthy state. The list still comes
        # back: it is what shows how to fix the setting.
        assert snapshot["write_target"] is None
        assert "cal-typo" in snapshot["write_target_error"]
        assert [c["name"] for c in snapshot["calendars"]] == ["Work", "Home"]
    print("ok: calendars snapshot names the write target")


def test_serialize_a_real_icalendar_component_when_available():
    """The stubs above assume how icalendar presents a VEVENT; verify it for real.

    Skipped where the package is absent (CI installs neither caldav nor its
    icalendar dependency — the gateway guards that import for exactly this
    reason), but it pins the assumptions the stubs encode: case-insensitive
    property access, `.dt` payloads, decoded TEXT escapes, preserved timezones.
    """
    try:
        import icalendar
    except ImportError:
        print("skip: icalendar not installed (the real-component check)")
        return
    raw = b"""BEGIN:VCALENDAR\r
VERSION:2.0\r
PRODID:-//retinue//test//EN\r
BEGIN:VEVENT\r
UID:real-1@example.com\r
SUMMARY:Zahnarzt\r
DESCRIPTION:Kontrolle\\nund Reinigung\r
LOCATION:Bahnhofstrasse 1\r
STATUS:CONFIRMED\r
RRULE:FREQ=YEARLY\r
DTSTART;TZID=Europe/Zurich:20260903T140000\r
DTEND;TZID=Europe/Zurich:20260903T143000\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:real-2@example.com\r
SUMMARY:Konferenz\r
DTSTART;VALUE=DATE:20260910\r
DURATION:P2D\r
END:VEVENT\r
END:VCALENDAR\r
"""
    with tempfile.TemporaryDirectory() as tmp:
        cg = _load_caldav_gateway(tmp)

        class _Parsed:
            icalendar_instance = icalendar.Calendar.from_ical(raw)

        components = cg._event_components(_Parsed())
        assert len(components) == 2
        timed, all_day = (cg._serialize_event(c) for c in components)
        assert timed["summary"] == "Zahnarzt"
        # The event's own timezone survives, rather than being flattened to UTC.
        assert timed["start"] == "2026-09-03T14:00:00+02:00"
        assert timed["end"] == "2026-09-03T14:30:00+02:00"
        # TEXT escapes are decoded by icalendar, so the JSON carries real newlines.
        assert timed["description"] == "Kontrolle\nund Reinigung"
        assert timed["status"] == "CONFIRMED" and timed["recurring"] is True
        assert timed["all_day"] is False
        # A DATE start with a DURATION: all-day, end derived from the duration.
        assert (all_day["all_day"], all_day["start"], all_day["end"]) == (True, "2026-09-10", "2026-09-12")
    print("ok: a real icalendar component serializes as the stubs assume")


# ── Request routing ──────────────────────────────────────────────────────────

def test_route_and_params():
    with tempfile.TemporaryDirectory() as tmp:
        cg = _load_caldav_gateway(tmp)
        route, params = cg._route_and_params("/events?start=2026-09-20&days=7&query=a%20b")
        assert route == "/events"
        assert cg._param(params, "start") == "2026-09-20"
        assert cg._param(params, "days") == "7"
        assert cg._param(params, "query") == "a b"
        assert cg._param(params, "missing") == ""
        # A trailing slash and a query string never hide a route.
        assert cg._route_and_params("/calendars/")[0] == "/calendars"
        assert cg._route_and_params("/health?x=1")[0] == "/health"
        assert cg._route_and_params("/")[0] == ""
        # Repeated parameters: the first wins, whitespace stripped.
        _, params = cg._route_and_params("/events?limit=%2010%20&limit=99")
        assert cg._param(params, "limit") == "10"
    print("ok: routes and query parameters parsed")


# ── The read endpoints over real HTTP ────────────────────────────────────────

def test_read_endpoints_over_http():
    """Drive /calendars, /events and /event through _PushHandler itself.

    The checks above exercise the helpers; this one covers the HTTP boundary the
    outside world actually meets — the token gate, the status mapping (200 / 400
    / 404 / 502) and the promise that a read never touches the send policy, which
    is `verify` here (the same in-process pattern as
    test_signal_send_policy.py's handler test).
    """
    import http.client
    import threading
    from http.server import ThreadingHTTPServer

    tz = datetime.timezone(datetime.timedelta(hours=2))
    personal = _Calendar("cal-1", "https://dav/c1", "Personal", events=[
        _Event(_timed("Dentist", datetime.datetime(2026, 9, 21, 14, 0, tzinfo=tz),
                      datetime.datetime(2026, 9, 21, 14, 30, tzinfo=tz),
                      LOCATION="Bahnhofstrasse 1")),
    ], by_uid={"uid-Dentist": _timed("Dentist", datetime.datetime(2026, 9, 21, 14, 0, tzinfo=tz))})

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["CALDAV_GATEWAY_TOKEN"] = "s3cret"
        try:
            cg = _load_caldav_gateway(tmp)
        finally:
            os.environ.pop("CALDAV_GATEWAY_TOKEN", None)
        cg._connect_principal = lambda: _Principal([personal])
        server = ThreadingHTTPServer(("127.0.0.1", 0), cg._PushHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            def get(path, token="s3cret"):
                conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1])
                conn.request("GET", path, headers={"Authorization": f"Bearer {token}"} if token else {})
                resp = conn.getresponse()
                body = json.loads(resp.read().decode("utf-8"))
                conn.close()
                return resp.status, body

            # The token gates every read; /health stays open, as the monitor needs.
            for path in ("/calendars", "/events", "/event?uid=uid-Dentist"):
                assert get(path, token=None)[0] == 401, path
                assert get(path, token="wrong")[0] == 401, path
            assert get("/health", token=None)[0] == 200

            status, body = get("/events?start=2026-09-20&end=2026-09-27")
            assert status == 200, body
            assert [e["summary"] for e in body["events"]] == ["Dentist"]
            assert body["events"][0]["location"] == "Bahnhofstrasse 1"
            assert (body["count"], body["total"], body["truncated"]) == (1, 1, False)
            # The range echoes the half-open window actually queried.
            assert body["range"] == {"start": "2026-09-20T00:00:00", "end": "2026-09-28T00:00:00"}
            # A read under a verify policy answers instead of queueing anything.
            assert cg._outbound_policy_category() == "verify"
            assert get("/pending-sends")[1]["pending"] == []

            # The text filter and the response cap, over the wire.
            assert get("/events?days=30&query=plumber")[1]["total"] == 0
            status, body = get("/events?days=30&limit=1")
            assert status == 200 and body["count"] == 1

            # Malformed parameters are client errors with a JSON body.
            for path in ("/events?days=soon", "/events?days=inf", "/events?days=1e10",
                         "/events?start=nonsense", "/events?limit=0",
                         "/events?start=2026-09-20&end=2026-09-19",
                         "/events?start=9999-12-30&end=9999-12-31",
                         # A calendar the caller named and that does not exist:
                         # their typo, so a 400 — never a 502 about the server.
                         "/events?days=7&calendar_id=nope",
                         "/event?uid=uid-Dentist&calendar_id=nope"):
                status, body = get(path)
                assert status == 400 and "error" in body, (path, status, body)

            # /event: found, missing uid (404), no uid at all (400).
            status, body = get("/event?uid=uid-Dentist")
            assert status == 200 and body["summary"] == "Dentist" and body["calendar"] == "Personal"
            assert get("/event?uid=nope")[0] == 404
            assert get("/event")[0] == 400
            assert get("/calendars")[1]["write_target"]["name"] == "Personal"
            assert get("/nope")[0] == 404

            # A ValueError raised INSIDE the CalDAV library is the server's
            # problem, not the caller's: only BadRequest may become a 400.
            def _library_value_error(*largs, **kwargs):
                raise ValueError("malformed response from the calendar server")

            saved_search = cg._search_calendar
            cg._search_calendar = _library_value_error
            try:
                status, body = get("/events?days=7")
                assert status == 502, (status, body)
                assert "malformed response" in body["error"]
            finally:
                cg._search_calendar = saved_search

            # A backend that fails is a 502, never a 200 with nothing in it.
            # (The gateway logs the traceback as it does in production — the
            # noise below this line in the test output is that log.)
            def _unreachable():
                raise RuntimeError("server unreachable")

            cg._connect_principal = _unreachable
            for path in ("/events?days=7", "/event?uid=uid-Dentist", "/calendars"):
                status, body = get(path)
                assert status == 502 and "unreachable" in body["error"], (path, status, body)
        finally:
            server.shutdown()
    print("ok: the read endpoints answer correctly over HTTP")


def main():
    test_read_window_defaults_to_now_plus_default_days()
    test_read_window_is_half_open_and_a_bare_end_date_covers_its_day()
    test_read_window_rejects_bad_input()
    test_read_window_aligns_mixed_timezones_without_moving_the_instant()
    test_read_limit_clamped_to_maximum()
    test_unusable_read_tunables_fall_back_to_their_defaults()
    test_serialize_timed_event()
    test_serialize_all_day_event_round_trips_into_the_write_path()
    test_serialize_derives_a_missing_end()
    test_serialize_flags_recurrence()
    test_event_components_walks_a_calendar_instance()
    test_calendar_identity_survives_an_unreadable_property()
    test_pick_read_calendars()
    test_a_duplicate_display_name_is_ambiguous_not_a_coin_flip()
    test_list_events_sorted_across_calendars()
    test_events_sort_on_the_instant_not_the_string()
    test_expanded_search_falls_back_when_unsupported()
    test_query_filter_matches_text_fields()
    test_find_event_by_uid_across_calendars()
    test_unreachable_calendar_is_not_reported_as_a_missing_event()
    test_calendars_snapshot_names_the_write_target()
    test_serialize_a_real_icalendar_component_when_available()
    test_route_and_params()
    test_read_endpoints_over_http()
    print("\nAll CalDAV read checks passed.")


if __name__ == "__main__":
    main()
