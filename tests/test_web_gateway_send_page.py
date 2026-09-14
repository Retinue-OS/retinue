#!/usr/bin/env python3
"""Checks for the channel-send status/approval page rendering.

Covers the UX contract of the send status page (issue #116 follow-up): a
"sending" entry renders a spinner and client-side polling (no full-page
meta-refresh), success renders the green check and auto-advance, and the
next-request button appears only when a next request actually exists — on the
approval page's Skip too.

Also covers what a pending *calendar* event renders as: the approval card has
to say which event would be written, since a card showing an empty message box
asks the user to approve something they cannot see.

    python3 tests/test_web_gateway_send_page.py
"""
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
        assert "default calendar" in out
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
        out = wg._render_channel_send_html(
            _event("pending", start="2026-09-10", end="2026-09-12", all_day=True),
            "caldav-gateway", "a" * 32, None)
        assert "Thu 10 Sep 2026 \u2013 Sat 12 Sep 2026 (all day)" in out


def test_named_calendar_is_shown():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp))
        out = wg._render_channel_send_html(_event("pending", calendar_id="reminders"),
                                           "caldav-gateway", "a" * 32, None)
        assert "<th>Calendar</th><td>reminders</td>" in out


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
