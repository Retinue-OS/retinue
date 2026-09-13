#!/usr/bin/env python3
"""Checks for the CalDAV read client's rendering (scripts/caldav-read.py --text).

The gateway's own tests cover the JSON it serves; these cover what the client
makes of it, which carries its own conventions: an all-day event's `end` is the
exclusive iCalendar DTEND (so a multi-day event must not print as one day), and
the window header trims sub-second noise without losing the UTC offset.

Runnable with no gateway and no network — only the rendering helpers are called.

    python3 tests/test_caldav_read_client.py
"""
import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_client():
    """Load scripts/caldav-read.py (its name is not importable as a module)."""
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "caldav_read_under_test", SCRIPTS_DIR / "caldav-read.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_short_keeps_the_offset():
    cr = _load_client()
    # Only the fractional seconds go: splitting at the "." would take a trailing
    # offset with them and misstate the window's timezone.
    assert cr._short("2026-09-20T00:00:00.123+02:00") == "2026-09-20T00:00:00+02:00"
    assert cr._short("2026-09-20T00:00:00.999999Z") == "2026-09-20T00:00:00Z"
    assert cr._short("2026-09-20T00:00:00+02:00") == "2026-09-20T00:00:00+02:00"
    assert cr._short("2026-09-20T00:00:00") == "2026-09-20T00:00:00"
    print("ok: the window header keeps its UTC offset")


def test_all_day_span_reads_the_exclusive_end():
    cr = _load_client()
    # DTEND is exclusive: a one-day event ends the following day and prints as a
    # single date; a longer one prints through its last covered day.
    assert cr._all_day_span({"start": "2026-09-23", "end": "2026-09-24"}) == "2026-09-23"
    assert cr._all_day_span({"start": "2026-09-23", "end": "2026-09-26"}) == "2026-09-23 – 2026-09-25"
    # Missing or unparseable end → just the start, no crash.
    assert cr._all_day_span({"start": "2026-09-23", "end": ""}) == "2026-09-23"
    assert cr._all_day_span({"start": "2026-09-23"}) == "2026-09-23"
    assert cr._all_day_span({"start": "2026-09-23", "end": "nonsense"}) == "2026-09-23"
    print("ok: an all-day span reads the exclusive DTEND")


def test_render_events():
    cr = _load_client()
    rendered = cr._render_events({
        "range": {"start": "2026-09-20T00:00:00.500+02:00", "end": "2026-09-28T00:00:00+02:00"},
        "total": 3, "count": 2, "truncated": True,
        "events": [
            {"start": "2026-09-21T14:00:00+02:00", "end": "2026-09-21T14:30:00+02:00",
             "summary": "Dentist", "location": "Bahnhofstrasse 1", "calendar": "Personal",
             "all_day": False, "recurring": False, "uid": "u1"},
            {"start": "2026-09-23", "end": "2026-09-26", "summary": "", "calendar": "Work",
             "all_day": True, "recurring": True, "uid": "u2"},
        ],
    })
    lines = rendered.splitlines()
    # The header states the window (sub-second noise trimmed, offset kept) and,
    # when the cap bit, how many of the total are shown.
    assert lines[0] == "2026-09-20T00:00:00+02:00 → 2026-09-28T00:00:00+02:00: 3 event(s) (showing 2)"
    assert "2026-09-21T14:00:00+02:00 – 2026-09-21T14:30:00+02:00" in lines[1]
    assert "Dentist" in lines[1] and "@ Bahnhofstrasse 1" in lines[1] and "[Personal]" in lines[1]
    assert lines[2].strip() == "uid: u1"
    # The multi-day all-day event spans its days, is flagged, and an event with
    # no title still says something.
    assert "2026-09-23 – 2026-09-25" in lines[3]
    assert "(no title)" in lines[3] and "(all day)" in lines[3] and "(recurring)" in lines[3]
    # A single event with no window (the --uid path) renders without a header.
    single = cr._render_events({"events": [
        {"start": "2026-09-21T14:00:00", "end": "2026-09-21T14:30:00", "summary": "Dentist"}]})
    assert single.splitlines()[0].strip().startswith("2026-09-21T14:00:00")
    # An empty answer is still legible.
    assert cr._render_events({"range": {"start": "a", "end": "b"}, "total": 0,
                              "events": []}) == "a → b: 0 event(s)"
    print("ok: events render compactly")


def test_render_calendars_surfaces_a_broken_write_target():
    cr = _load_client()
    rendered = cr._render_calendars({
        "account": "default",
        "write_target": {"id": "cal-1", "url": "https://dav/c1", "name": "Personal"},
        "calendars": [{"id": "cal-1", "url": "https://dav/c1", "name": "Personal"},
                      {"id": "cal-2", "url": "https://dav/c2", "name": ""}],
    })
    assert "account: default" in rendered
    assert "Personal ← writes land here" in rendered
    assert "(unnamed)" in rendered and "cal-2" in rendered
    # A misconfigured write target is a warning the operator can act on, not a
    # silently missing marker.
    broken = cr._render_calendars({
        "account": "default", "write_target": None,
        "write_target_error": "CALDAV_CALENDAR_ID: calendar 'cal-typo' not found",
        "calendars": [{"id": "cal-1", "url": "https://dav/c1", "name": "Personal"}],
    })
    assert "cal-typo" in broken and "⚠" in broken
    assert "← writes land here" not in broken
    print("ok: the calendar listing surfaces a broken write target")


def main():
    test_short_keeps_the_offset()
    test_all_day_span_reads_the_exclusive_end()
    test_render_events()
    test_render_calendars_surfaces_a_broken_write_target()
    print("\nAll CalDAV read-client checks passed.")


if __name__ == "__main__":
    main()
