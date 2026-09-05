#!/usr/bin/env python3
"""Checks for the Claude sign-in monitor (scripts/claude-auth-monitor.py).

Drives the incident state machine with synthetic credential_status() verdicts
and a fake notifier — no files, no HTTP, no Claude session.

    python3 tests/test_claude_auth_monitor.py
"""
import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS_DIR))
spec = importlib.util.spec_from_file_location(
    "claude_auth_monitor_under_test", SCRIPTS_DIR / "claude-auth-monitor.py")
cam = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cam)


class FakeNotifier:
    def __init__(self, fail=False):
        self.fail = fail
        self.opened = []    # (title, message)
        self.appended = []  # (thread_id, message)

    def open_thread(self, title, message, attention=None):
        if self.fail:
            return None
        self.opened.append((title, message))
        return f"thread-{len(self.opened)}"

    def append(self, thread_id, message, attention=None):
        if self.fail:
            return False
        self.appended.append((thread_id, message))
        return True


OK = {"state": "ok", "reason": "Signed in."}
EXPIRING = {"state": "expiring", "reason": "The sign-in expires in about 2.0 day(s)."}
BROKEN = {"state": "needs_login", "reason": "A fresh sign-in is required."}
STALE = {"state": "stale", "reason": "The access token has not been refreshed."}


def _engine(notifier=None, **kwargs):
    return cam.AuthMonitorEngine(notifier or FakeNotifier(), state={},
                                 fail_threshold=2, remind_broken=6 * 3600,
                                 remind_warn=24 * 3600, **kwargs)


def test_levels():
    assert cam.level_of(OK) == "ok"
    assert cam.level_of(EXPIRING) == "warn"
    assert cam.level_of(STALE) == "warn"
    assert cam.level_of(BROKEN) == "broken"


def test_single_blip_never_notifies():
    n = FakeNotifier()
    e = _engine(n)
    e.step(BROKEN, now=0)
    e.step(OK, now=60)
    assert n.opened == [] and n.appended == []


def test_debounce_then_notify_broken():
    n = FakeNotifier()
    e = _engine(n)
    e.step(BROKEN, now=0)
    assert n.opened == []
    e.step(BROKEN, now=300)
    assert len(n.opened) == 1
    title, message = n.opened[0]
    assert title == "Claude sign-in broken"
    assert "/claude-auth" in message and "fresh sign-in is required" in message
    # No bare URL: the link is a Markdown label (dashboard renders Markdown).
    assert "](" in message


def test_warning_uses_own_title_and_cadence():
    n = FakeNotifier()
    e = _engine(n)
    e.step(EXPIRING, now=0)
    e.step(EXPIRING, now=300)
    assert n.opened[0][0] == "Claude sign-in expires soon"
    # Below the warn reminder cadence: silent.
    e.step(EXPIRING, now=6 * 3600)
    assert n.appended == []
    # Past it: one reminder in the same thread.
    e.step(EXPIRING, now=25 * 3600)
    assert len(n.appended) == 1 and n.appended[0][0] == "thread-1"


def test_escalation_appends_immediately():
    n = FakeNotifier()
    e = _engine(n)
    e.step(EXPIRING, now=0)
    e.step(EXPIRING, now=300)
    # The predicted outage arrives: same thread, immediate message, and the
    # broken cadence takes over.
    e.step(BROKEN, now=600)
    assert len(n.opened) == 1
    assert len(n.appended) == 1 and "escalated" in n.appended[0][1]
    e.step(BROKEN, now=600 + 7 * 3600)
    assert len(n.appended) == 2 and "Reminder" in n.appended[1][1]


def test_recovery_reports_in_same_thread_and_resets():
    n = FakeNotifier()
    e = _engine(n)
    e.step(BROKEN, now=0)
    e.step(BROKEN, now=300)
    e.step(OK, now=600)
    assert len(n.appended) == 1 and "healthy again" in n.appended[0][1]
    # A later incident is a new thread with fresh debounce.
    e.step(BROKEN, now=1000)
    assert len(n.opened) == 1
    e.step(BROKEN, now=1300)
    assert len(n.opened) == 2


def test_notify_retries_after_backend_failure():
    n = FakeNotifier(fail=True)
    e = _engine(n)
    e.step(BROKEN, now=0)
    e.step(BROKEN, now=300)
    assert n.opened == []
    n.fail = False
    e.step(BROKEN, now=600)
    assert len(n.opened) == 1


def test_recovery_without_notification_is_silent():
    n = FakeNotifier(fail=True)
    e = _engine(n)
    e.step(BROKEN, now=0)
    e.step(BROKEN, now=300)   # notification could not be delivered
    n.fail = False
    e.step(OK, now=600)       # nothing was ever posted — no recovery message
    assert n.opened == [] and n.appended == []


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception as exc:  # noqa: BLE001 - report and continue
            failed += 1
            print(f"FAIL {test.__name__}: {exc!r}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
