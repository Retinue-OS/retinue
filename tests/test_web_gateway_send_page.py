#!/usr/bin/env python3
"""Checks for the channel-send status/approval page rendering.

Covers the UX contract of the send status page (issue #116 follow-up): a
"sending" entry renders a spinner and client-side polling (no full-page
meta-refresh), success renders the green check and auto-advance, and the
next-request button appears only when a next request actually exists — on the
approval page's Skip too.

Also covers what a pending *calendar* event renders as: the approval card has
to say which event would be written — and what is already in the calendar on
those days — since a card showing an empty message box asks the user to approve
something they cannot see.

    python3 tests/test_web_gateway_send_page.py
"""
import contextlib
import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_gateway(tmp: Path):
    os.environ["CONVERSATIONS_DIR"] = str(tmp / "convs")
    os.environ["CONVERSATION_DIR"] = str(tmp / "convlog")
    os.environ["CHAMBERS_DIR"] = str(tmp / "chambers")
    os.environ["WEB_GATEWAY_STATE"] = str(tmp / "state.json")
    (tmp / "chambers").mkdir(parents=True, exist_ok=True)
    if "markdown_it" not in sys.modules:
        try:
            import markdown_it  # noqa: F401
        except ImportError:
            stub = types.ModuleType("markdown_it")
            stub.MarkdownIt = object
            sys.modules["markdown_it"] = stub
    sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "web_gateway_send_page_under_test", SCRIPTS_DIR / "web-gateway.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@contextlib.contextmanager
def _display_zone(name):
    """Pin RETINUE_DISPLAY_TZ for one test, then put back what was there.

    Without it these assertions only hold where the host happens to run on UTC:
    _display_tz() falls back to TZ, so a developer (or runner) in
    Europe/Zurich would see 14:15+02:00 render as 14:15 and the test fail for
    no fault of the code.
    """
    previous = os.environ.get("RETINUE_DISPLAY_TZ")
    os.environ["RETINUE_DISPLAY_TZ"] = name
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("RETINUE_DISPLAY_TZ", None)
        else:
            os.environ["RETINUE_DISPLAY_TZ"] = previous


def _detail(status, **extra):
    return {"status": status, "recipient": "+15551112222", "category": "verify",
            "message": "hello", **extra}


def test_sending_page_polls_with_spinner():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp))
        out = wg._render_channel_send_html(_detail("sending"), "whatsapp-gateway", "a" * 32, None)
        assert 'class="spin"' in out                      # rotating symbol
        assert "/sends/whatsapp-gateway/" + "a" * 32 + "/status" in out  # client-side poll
        # No unconditional full-page refresh — only the no-JS fallback.
        assert out.count('http-equiv="refresh"') == 1 and "<noscript>" in out
        # No next request → the next button stays hidden and nextUrl is null.
        assert "var nextUrl=null;" in out
        assert 'id="st-next"' in out and "display:none" in out


def test_sending_page_with_next_shows_button():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp))
        out = wg._render_channel_send_html(_detail("sending"), "whatsapp-gateway", "a" * 32,
                                           "/sends/signal-gateway/bbb")
        assert 'var nextUrl="/sends/signal-gateway/bbb";' in out
        assert 'id="st-next" href="/sends/signal-gateway/bbb"' in out
        assert 'style=""' in out  # button visible


def test_approved_page_shows_check_and_advances():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp))
        out = wg._render_channel_send_html(_detail("approved"), "whatsapp-gateway", "a" * 32, None)
        assert 'class="check"' in out and "✓" in out       # green check
        assert "setTimeout(advance,1500)" in out           # brief pause, then close/advance
        assert "window.close()" in out


def test_error_page_shows_gateway_error():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp))
        out = wg._render_channel_send_html(
            _detail("error", error="usync query timed out"), "whatsapp-gateway", "a" * 32, None)
        assert 'class="cross"' in out
        assert "usync query timed out" in out


def test_approval_page_skip_only_with_next():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp))
        without = wg._render_channel_send_html(_detail("pending"), "whatsapp-gateway", "a" * 32, None)
        # No skip anchor is rendered (the lockButtons script may still name the
        # id defensively — only the element matters).
        assert 'id="btn-skip"' not in without
        with_next = wg._render_channel_send_html(_detail("pending"), "whatsapp-gateway", "a" * 32,
                                                 "/sends/signal-gateway/bbb")
        assert 'id="btn-skip"' in with_next and "/sends/signal-gateway/bbb" in with_next


def _event(status, **extra):
    """A pending calendar event as the caldav-gateway stores it: no "message"
    anywhere, the event itself in its own fields."""
    return {"status": status, "kind": "event", "to": "default", "category": "verify",
            "subject": "Dentist", "summary": "Dentist",
            "start": "2026-09-03T14:00:00", "end": "2026-09-03T14:30:00",
            "all_day": False, "description": "Bring the insurance card",
            "calendar_id": None, **extra}


def test_event_approval_page_shows_the_event():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp))
        out = wg._render_channel_send_html(_event("pending"), "caldav-gateway", "a" * 32, None)
        # What is being approved: title, when, target calendar, notes.
        assert "<h1>Approve Caldav-Gateway Event</h1>" in out   # not "Send"
        assert "<th>Event</th><td>Dentist</td>" in out
        assert "<th>When</th><td>Thu 03 Sep 2026, 14:00 \u2013 14:30</td>" in out
        assert "the gateway's configured calendar" in out
        assert "Bring the insurance card" in out
        # Never the empty message box the messenger renderer would leave.
        assert '<pre class="msg-body"></pre>' not in out


def test_event_without_description_says_so():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp))
        out = wg._render_channel_send_html(_event("pending", description=""),
                                           "caldav-gateway", "a" * 32, None)
        assert "No description." in out
        assert '<pre class="msg-body"></pre>' not in out


def test_all_day_event_reads_as_a_span_of_days():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp))
        # DTEND is exclusive (RFC 5545), so this covers the 10th and the 11th —
        # the card must not promise a day the calendar will not carry.
        out = wg._render_channel_send_html(
            _event("pending", start="2026-09-10", end="2026-09-12", all_day=True),
            "caldav-gateway", "a" * 32, None)
        assert "Thu 10 Sep 2026 \u2013 Fri 11 Sep 2026 (all day)" in out
        one_day = wg._render_channel_send_html(
            _event("pending", start="2026-09-10", end="2026-09-11", all_day=True),
            "caldav-gateway", "a" * 32, None)
        assert "<th>When</th><td>Thu 10 Sep 2026 (all day)</td>" in one_day


def _agenda_event(start, end, summary, **extra):
    return {"start": start, "end": end, "summary": summary, "all_day": False,
            "uid": summary, "calendar": "Personal", **extra}


def test_agenda_lists_the_day_and_flags_the_clash():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp))
        agenda = {"events": [
            dict(_agenda_event("2026-09-03T09:00:00", "2026-09-03T09:15:00", "Standup"),
                 overlaps=False),
            dict(_agenda_event("2026-09-03T14:15:00", "2026-09-03T15:00:00", "Call with Mara",
                               location="Zoom"), overlaps=True),
        ], "error": None, "truncated": False}
        out = wg._render_channel_send_html(_event("pending"), "caldav-gateway", "a" * 32, None,
                                           agenda)
        assert "Already in the calendar" in out
        assert "Standup" in out and "Call with Mara" in out
        assert "@ Zoom" in out and "[Personal]" in out
        # The clash marker sits on the overlapping event only.
        assert out.count('<span class="clash">overlaps</span>') == 1
        assert out.index("Call with Mara") < out.index('<span class="clash">')
        assert ".clash{" in out  # the marker's style rides along


def test_agenda_says_when_the_days_are_empty_or_unreadable():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp))
        empty = wg._render_channel_send_html(_event("pending"), "caldav-gateway", "a" * 32, None,
                                             {"events": [], "error": None, "truncated": False})
        assert "Nothing else on these days." in empty
        broken = wg._render_channel_send_html(_event("pending"), "caldav-gateway", "a" * 32, None,
                                              {"events": [], "error": "timed out",
                                               "truncated": False})
        assert "could not be loaded" in broken and "timed out" in broken
        # A failed read never costs the user the decision itself.
        assert 'id="btn-approve"' in broken and 'id="btn-reject"' in broken


def test_overlap_rules():
    with tempfile.TemporaryDirectory() as tmp, _display_zone("UTC"):
        wg = _load_gateway(Path(tmp))
        proposed = wg._event_interval({"start": "2026-09-03T14:00:00", "end": "2026-09-03T14:30:00"})
        def clashes(start, end, **extra):
            return wg._events_overlap(proposed, wg._event_interval(
                {"start": start, "end": end, **extra}))
        assert clashes("2026-09-03T14:15:00", "2026-09-03T15:00:00")      # starts inside
        assert clashes("2026-09-03T13:00:00", "2026-09-03T18:00:00")      # contains it
        assert clashes("2026-09-03T14:10:00", "2026-09-03T14:20:00")      # inside it
        assert not clashes("2026-09-03T13:00:00", "2026-09-03T14:00:00")  # ends as it starts
        assert not clashes("2026-09-03T14:30:00", "2026-09-03T15:00:00")  # starts as it ends
        # An offset is converted into the display zone, not dropped: with no
        # zone configured that is UTC, so 14:15+02:00 is 12:15 and misses the
        # proposed 14:00-14:30 entirely, while 16:15+02:00 lands inside it.
        assert not clashes("2026-09-03T14:15:00+02:00", "2026-09-03T15:00:00+02:00")
        assert clashes("2026-09-03T16:15:00+02:00", "2026-09-03T17:00:00+02:00")
        # An all-day event covers the whole day, an unparsable one nothing.
        assert clashes("2026-09-03", "2026-09-04", all_day=True)
        assert not clashes("whenever", "whenever")


def test_offsets_are_read_in_the_configured_display_zone():
    """The bug this guards: 16:00+00:00 and 16:00+02:00 rendering alike.

    An event stored in UTC is two hours off the Zurich wall clock its owner
    reads. Rendering both as a bare "16:00" made a mis-zoned calendar entry
    look correct on its own approval page, and hid the clash between the two.
    """
    with tempfile.TemporaryDirectory() as tmp, _display_zone("Europe/Zurich"):
        wg = _load_gateway(Path(tmp))
        if True:
            in_utc = wg._format_event_when("2026-09-18T16:00:00+00:00",
                                           "2026-09-18T16:45:00+00:00", False)
            local = wg._format_event_when("2026-09-18T16:00:00+02:00",
                                          "2026-09-18T16:45:00+02:00", False)
            assert in_utc != local
            assert "18:00" in in_utc and "CEST" in in_utc
            assert "16:00" in local and "CEST" in local
            # Same day on both ends: the date is not repeated after the dash.
            assert local == "Fri 18 Sep 2026, 16:00 CEST – 16:45 CEST"
            # A naive time has no offset to convert from and names no zone.
            naive = wg._format_event_when("2026-09-18T16:00:00",
                                          "2026-09-18T16:45:00", False)
            assert naive == "Fri 18 Sep 2026, 16:00 – 16:45"
            # Converting across midnight still reads as two days.
            crossing = wg._format_event_when("2026-09-18T23:30:00+00:00",
                                             "2026-09-19T00:15:00+00:00", False)
            assert "Sat 19 Sep 2026, 01:30" in crossing
            # The two now genuinely clash: 16:00 UTC is 18:00 Zurich.
            proposed = wg._event_interval({"start": "2026-09-18T18:00:00",
                                           "end": "2026-09-18T18:45:00"})
            assert wg._events_overlap(proposed, wg._event_interval(
                {"start": "2026-09-18T16:00:00+00:00", "end": "2026-09-18T16:45:00+00:00"}))
            # An all-day entry is that zone's whole day, and still comparable
            # with an offset-carrying one.
            assert wg._events_overlap(
                wg._event_interval({"start": "2026-09-18", "end": "2026-09-19", "all_day": True}),
                wg._event_interval({"start": "2026-09-18T16:00:00+00:00",
                                    "end": "2026-09-18T16:45:00+00:00"}))


def test_an_unknown_display_zone_falls_back_to_utc():
    """A typo in the setting must not take the approval page down."""
    with tempfile.TemporaryDirectory() as tmp, _display_zone("Europe/Nowhere"):
        wg = _load_gateway(Path(tmp))
        assert wg._format_event_when("2026-09-18T16:00:00+02:00",
                                     "2026-09-18T16:45:00+02:00", False).startswith(
                                         "Fri 18 Sep 2026, 14:00")


def test_a_span_across_the_dst_fold_is_not_backwards():
    """Converting into the display zone and then comparing bare wall clocks
    loses what the conversion established: across the autumn fold 02:30+02:00
    and 02:15+01:00 are 45 minutes apart, in that order, while their local
    clocks read 02:30 then 02:15. Treated as backwards, the card silently
    dropped the end of a perfectly valid span."""
    with tempfile.TemporaryDirectory() as tmp, _display_zone("Europe/Zurich"):
        wg = _load_gateway(Path(tmp))
        start, end = "2026-10-25T02:30:00+02:00", "2026-10-25T02:15:00+01:00"
        assert not wg._ends_before_it_starts(start, end, False)
        assert "02:15" in wg._format_event_when(start, end, False)
        # A genuinely backwards pair is still caught.
        assert wg._ends_before_it_starts("2026-10-25T02:30:00+02:00",
                                         "2026-10-25T02:00:00+02:00", False)


def test_agenda_window_follows_the_rendered_day():
    """The window must name the days the card shows. A pending 00:30+02:00 is
    rendered on the previous day where the display zone is UTC, so asking the
    gateway for the raw date would fetch a day the user never sees and leave
    the day whose clashes they are being asked about unfetched."""
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp))
        event = {"start": "2026-09-18T00:30:00+02:00", "end": "2026-09-18T01:30:00+02:00"}
        with _display_zone("UTC"):
            assert "Thu 17 Sep 2026, 22:30" in wg._format_event_when(
                event["start"], event["end"], False)
            assert wg._agenda_window(event) == ("2026-09-17", "2026-09-17")
        with _display_zone("Europe/Zurich"):
            assert wg._agenda_window(event) == ("2026-09-18", "2026-09-18")
        # Midnight still belongs to the day before — in the rendered zone.
        with _display_zone("UTC"):
            assert wg._agenda_window({"start": "2026-09-18T20:00:00+00:00",
                                      "end": "2026-09-19T00:00:00+00:00"}) == ("2026-09-18",
                                                                               "2026-09-18")
        # A date names the same day everywhere: an all-day window is unmoved.
        with _display_zone("Pacific/Kiritimati"):
            assert wg._agenda_window({"start": "2026-09-10", "end": "2026-09-12",
                                      "all_day": True}) == ("2026-09-10", "2026-09-11")


def test_agenda_window_covers_only_the_days_the_event_touches():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp))
        assert wg._agenda_window({"start": "2026-09-03T14:00:00",
                                  "end": "2026-09-03T14:30:00"}) == ("2026-09-03", "2026-09-03")
        # Ending exactly at midnight belongs to the start day alone — the read
        # window's bare end date is inclusive, so naming the 4th would list a
        # day the event never touches.
        assert wg._agenda_window({"start": "2026-09-03T23:00:00",
                                  "end": "2026-09-04T00:00:00"}) == ("2026-09-03", "2026-09-03")
        # A minute past midnight really does reach into the next day.
        assert wg._agenda_window({"start": "2026-09-03T23:00:00",
                                  "end": "2026-09-04T00:01:00"}) == ("2026-09-03", "2026-09-04")
        # All-day DTEND is exclusive.
        assert wg._agenda_window({"start": "2026-09-10", "end": "2026-09-12",
                                  "all_day": True}) == ("2026-09-10", "2026-09-11")
        # An end before the start never yields a window running backwards.
        assert wg._agenda_window({"start": "2026-09-10T14:00:00",
                                  "end": "2026-09-09T14:00:00"}) == ("2026-09-10", "2026-09-10")


def test_agenda_read_is_bounded_in_time_and_size():
    """urlopen's timeout bounds each socket read, not the exchange: a gateway
    that accepts the connection and never finishes the body must cost a note on
    the card, not a page that never renders."""
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Dripping(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "100000")
            self.end_headers()
            for _ in range(40):          # a trickle, each write well inside the timeout
                try:
                    self.wfile.write(b" " * 8)
                    self.wfile.flush()
                except OSError:
                    return
                time.sleep(0.2)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Dripping)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            wg = _load_gateway(Path(tmp))
            started = time.monotonic()
            body, error = wg._fetch_json_bounded(
                f"http://127.0.0.1:{srv.server_port}/events", {}, 1.0, wg._AGENDA_MAX_BYTES)
            elapsed = time.monotonic() - started
            assert body is None and error and "1s" in error
            assert elapsed < 3, f"the caller waited {elapsed:.1f}s for a 1s deadline"
            # An oversized body is refused rather than read into memory.
            _body, big = wg._fetch_json_bounded(
                f"http://127.0.0.1:{srv.server_port}/events", {}, 1.0, 4)
            assert big
    finally:
        srv.shutdown()


def test_agenda_read_asks_the_gateway_for_the_right_window():
    """The read goes to the calendar gateway's own /events endpoint, over the
    days the pending event covers, and tags each answer with its overlap."""
    import json as _json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            seen["path"] = self.path
            seen["auth"] = self.headers.get("Authorization")
            body = _json.dumps({"events": [
                _agenda_event("2026-09-03T09:00:00", "2026-09-03T09:15:00", "Standup"),
                _agenda_event("2026-09-03T14:15:00", "2026-09-03T15:00:00", "Call"),
            ], "truncated": False}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            wg = _load_gateway(Path(tmp))
            gw = {"base_url": f"http://127.0.0.1:{srv.server_port}", "token": "t0ken"}
            agenda = wg._calendar_agenda(gw, _event("pending"))
            assert "/events?" in seen["path"]
            assert "start=2026-09-03" in seen["path"] and "end=2026-09-03" in seen["path"]
            # Explicitly account-wide: without it the endpoint narrows to
            # CALDAV_CALENDAR_ID wherever a deployment configures one.
            assert "calendar_id=%2A" in seen["path"] or "calendar_id=*" in seen["path"]
            assert seen["auth"] == "Bearer t0ken"
            assert [e["overlaps"] for e in agenda["events"]] == [False, True]
            assert agenda["error"] is None
            # An unreachable gateway is reported, not raised.
            dead = wg._calendar_agenda({"base_url": "http://127.0.0.1:1", "token": ""},
                                       _event("pending"))
            assert dead["error"] and dead["events"] == []
    finally:
        srv.shutdown()


def test_named_calendar_is_shown():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp))
        out = wg._render_channel_send_html(
            _event("pending", calendar_id="reminders", calendar_target="reminders"),
            "caldav-gateway", "a" * 32, None)
        assert "<th>Calendar</th><td>reminders</td>" in out
        # A request without its own target still lands in the gateway's
        # configured calendar, which the card names rather than claiming the
        # server's default.
        configured = wg._render_channel_send_html(
            _event("pending", calendar_target="Work"), "caldav-gateway", "a" * 32, None)
        assert "<th>Calendar</th><td>Work</td>" in configured
        unset = wg._render_channel_send_html(_event("pending", calendar_target=""),
                                             "caldav-gateway", "a" * 32, None)
        assert "the gateway's configured calendar" in unset


def test_a_span_never_runs_backwards():
    """Nothing validates end > start before an event is queued, so the card
    must not describe a range that cannot exist."""
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp))
        same = wg._render_channel_send_html(
            _event("pending", start="2026-09-10", end="2026-09-10", all_day=True),
            "caldav-gateway", "a" * 32, None)
        assert "<th>When</th><td>Thu 10 Sep 2026 (all day)</td>" in same
        assert "Wed 09 Sep 2026" not in same
        backwards = wg._render_channel_send_html(
            _event("pending", start="2026-09-10T14:00:00", end="2026-09-09T14:00:00"),
            "caldav-gateway", "a" * 32, None)
        assert "<th>When</th><td>Thu 10 Sep 2026, 14:00</td>" in backwards


def test_event_status_page_talks_about_the_calendar():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp))
        out = wg._render_channel_send_html(_event("approved"), "caldav-gateway", "a" * 32, None)
        assert "Added to the calendar." in out
        assert "Sent." not in out
        err = wg._render_channel_send_html(_event("error", error="calendar is read-only"),
                                           "caldav-gateway", "a" * 32, None)
        assert "could not create the event: calendar is read-only" in err


def test_unparsable_times_fall_back_to_the_raw_value():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp))
        out = wg._render_channel_send_html(_event("pending", start="whenever", end="whenever"),
                                           "caldav-gateway", "a" * 32, None)
        assert "whenever" in out


def test_index_row_names_the_event_time():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp))
        out = wg._render_sends_index_html([
            {"account": "caldav-gateway", "request_id": "a" * 32, "subject": "Dentist",
             "to": "default", "category": "verify", "kind": "event",
             "start": "2026-09-03T14:00:00", "end": "2026-09-03T14:30:00", "all_day": False},
        ])
        assert "Thu 03 Sep 2026, 14:00 \u2013 14:30" in out
        assert "Dentist" in out


def test_messenger_page_is_unchanged():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp))
        out = wg._render_channel_send_html(_detail("pending"), "whatsapp-gateway", "a" * 32, None)
        assert "<h1>Approve Whatsapp-Gateway Send</h1>" in out
        assert "<th>To</th><td>+15551112222</td>" in out
        assert '<pre class="msg-body">hello</pre>' in out
        assert "<th>Event</th>" not in out


def main() -> int:
    tests = [test_sending_page_polls_with_spinner,
             test_sending_page_with_next_shows_button,
             test_approved_page_shows_check_and_advances,
             test_error_page_shows_gateway_error,
             test_approval_page_skip_only_with_next,
             test_event_approval_page_shows_the_event,
             test_event_without_description_says_so,
             test_all_day_event_reads_as_a_span_of_days,
             test_named_calendar_is_shown,
             test_event_status_page_talks_about_the_calendar,
             test_agenda_lists_the_day_and_flags_the_clash,
             test_agenda_says_when_the_days_are_empty_or_unreadable,
             test_overlap_rules,
             test_offsets_are_read_in_the_configured_display_zone,
             test_an_unknown_display_zone_falls_back_to_utc,
             test_a_span_across_the_dst_fold_is_not_backwards,
             test_agenda_window_follows_the_rendered_day,
             test_a_span_never_runs_backwards,
             test_agenda_window_covers_only_the_days_the_event_touches,
             test_agenda_read_is_bounded_in_time_and_size,
             test_agenda_read_asks_the_gateway_for_the_right_window,
             test_unparsable_times_fall_back_to_the_raw_value,
             test_index_row_names_the_event_time,
             test_messenger_page_is_unchanged]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
    print(f"{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
