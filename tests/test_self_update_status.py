#!/usr/bin/env python3
"""Checks for the self-update client's status polling (scripts/self-update.py).

`POST /update` only dispatches the rebuild -- the updater sidecar answers 202
before the recipe even runs -- so the exit code used to reflect the dispatch,
never the actual outcome. This client now polls the sidecar's own `GET
/status` (derived from the same URL, not a second variable) until the run
finishes, then exits non-zero when it failed. Covered here against a small
fake HTTP server standing in for the updater: the URL derivation, a run that
finishes successfully, one that fails (naming the failed step), and a run that
never finishes inside the bounded wait.

Also covered (PR #223 review, closing out part of #46): the `run_id` the
updater now echoes is checked on every poll, so a *different* run taking
over mid-poll is reported as RunSuperseded rather than silently attributed
to the run this client dispatched; and the bounded wait is actually
bounded -- a request or a sleep can no longer carry the total wait
meaningfully past poll_timeout just because request_timeout or
poll_interval individually happen to be larger.

    python3 tests/test_self_update_status.py
"""
import importlib.util
import json
import sys
import threading
import time
import traceback
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_self_update():
    spec = importlib.util.spec_from_file_location(
        "self_update_under_test", SCRIPTS_DIR / "self-update.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


failures = []


def check(label, got, want):
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
        failures.append(label)


class _FakeUpdater:
    """A stand-in for the updater sidecar's GET /status: serves a scripted
    sequence of states (one per request) and records the auth header of every
    request it was asked. An optional `delay` makes every response wait that
    many seconds first, standing in for a slow or hanging updater.
    """

    def __init__(self, states, delay: float = 0.0):
        self._states = list(states)
        self._delay = delay
        self.requests: list[str] = []
        sink = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if sink._delay:
                    time.sleep(sink._delay)
                sink.requests.append(self.headers.get("X-Update-Token") or "")
                state = sink._states[min(len(sink._states) - 1, len(sink.requests) - 1)]
                body = json.dumps(state).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):  # keep test output clean
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        # A client that hits its own capped timeout closes the connection
        # before this handler's delayed write goes out, so the server sees a
        # routine BrokenPipeError/ConnectionResetError -- expected, and not
        # worth the default traceback-to-stderr noise; anything else still
        # prints, so a genuine bug in the handler is not hidden.
        self.server.handle_error = self._handle_error
        self.port = self.server.server_address[1]

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    @staticmethod
    def _handle_error(request, client_address):
        import sys as _sys
        exc = _sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            return
        traceback.print_exc()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/status"


def test_status_url_derivation(su):
    print("status_url")
    check("default /update -> /status",
          su.status_url("http://updater:9000/update"), "http://updater:9000/status")
    check("preserves host/port with no /update suffix",
          su.status_url("http://updater:9000/"), "http://updater:9000/status")
    check("appends after a non-standard path with no /update suffix",
          su.status_url("https://agents.example.com/hooks/rebuild"),
          "https://agents.example.com/hooks/rebuild/status")


def test_poll_until_done_success(su):
    print("poll_until_done: reports success once running goes false")
    states = [
        {"running": True, "returncode": None, "failed_step": None},
        {"running": True, "returncode": None, "failed_step": None},
        {"running": False, "returncode": 0, "failed_step": None},
    ]
    with _FakeUpdater(states) as fake:
        final = su.poll_until_done(fake.url, {"X-Update-Token": "tok"},
                                    request_timeout=5, poll_timeout=10, poll_interval=0.05)
    check("final state has returncode 0", final.get("returncode"), 0)
    check("every poll carried the token", all(t == "tok" for t in fake.requests), True)
    check("polled more than once before completion", len(fake.requests) >= 3, True)


def test_poll_until_done_reports_failure(su):
    print("poll_until_done: surfaces a failed run's failed_step")
    states = [
        {"running": True, "returncode": None, "failed_step": None},
        {"running": False, "returncode": 1, "failed_step": "docker compose build"},
    ]
    with _FakeUpdater(states) as fake:
        final = su.poll_until_done(fake.url, {}, request_timeout=5,
                                    poll_timeout=10, poll_interval=0.05)
    check("failed run's returncode", final.get("returncode"), 1)
    check("failed run's failed_step", final.get("failed_step"), "docker compose build")


def test_poll_until_done_times_out(su):
    print("poll_until_done: gives up after poll_timeout if still running")
    states = [{"running": True, "returncode": None, "failed_step": None}]
    with _FakeUpdater(states) as fake:
        final = su.poll_until_done(fake.url, {}, request_timeout=5,
                                    poll_timeout=0.2, poll_interval=0.05)
    check("None signals a bounded-wait timeout, not a verdict", final, None)


def test_poll_until_done_respects_deadline_despite_long_poll_interval(su):
    print("poll_until_done: a poll_interval longer than poll_timeout does not "
          "delay the timeout verdict")
    # Before the fix, the sleep between polls was never capped: a fast-but-
    # perpetually-running response would still sleep the *full*
    # poll_interval before the deadline was checked again, so a poll_timeout
    # of 0.3s with a poll_interval of 5s took upwards of 5s to give up, not
    # ~0.3s. Pin the fix by measuring wall-clock time, not just the outcome.
    states = [{"running": True, "returncode": None, "failed_step": None}]
    with _FakeUpdater(states) as fake:
        started = time.monotonic()
        final = su.poll_until_done(fake.url, {}, request_timeout=5,
                                    poll_timeout=0.3, poll_interval=5)
        elapsed = time.monotonic() - started
    check("still a bounded-wait timeout, not a verdict", final, None)
    check("elapsed time tracked poll_timeout, not poll_interval", elapsed < 2.0, True)


def test_poll_until_done_caps_request_timeout_to_remaining(su):
    print("poll_until_done: a slow response is cut off by the deadline, not by "
          "request_timeout")
    # Before the fix, each request's own timeout was the full request_timeout
    # regardless of how little of poll_timeout was left, so a response slower
    # than the remaining budget but faster than request_timeout would still be
    # awaited in full. Here the fake updater takes 1.5s to answer, request_timeout
    # is a generous 5s, but only 0.3s of poll_timeout remains -- the fix must cut
    # the wait to ~0.3s rather than let the 1.5s response come back normally.
    states = [{"running": True, "returncode": None, "failed_step": None}]
    with _FakeUpdater(states, delay=1.5) as fake:
        started = time.monotonic()
        try:
            su.poll_until_done(fake.url, {}, request_timeout=5,
                                poll_timeout=0.3, poll_interval=5)
            raised_promptly = False
        except (urllib.error.URLError, OSError):
            raised_promptly = True
        elapsed = time.monotonic() - started
    check("the capped request timeout surfaced as a network error, "
          "not a clean result", raised_promptly, True)
    check("elapsed time tracked the remaining budget, not the full response delay",
          elapsed < 1.0, True)


def test_poll_until_done_matching_run_id_reports_normally(su):
    print("poll_until_done: a run_id that matches throughout reports that run's own outcome")
    states = [
        {"running": True, "returncode": None, "failed_step": None, "run_id": 7},
        {"running": False, "returncode": 1, "failed_step": "docker compose build", "run_id": 7},
    ]
    with _FakeUpdater(states) as fake:
        final = su.poll_until_done(fake.url, {}, request_timeout=5, poll_timeout=10,
                                    poll_interval=0.05, run_id=7)
    check("matching run_id throughout: reports the real outcome", final.get("returncode"), 1)


def test_poll_until_done_ignores_run_id_when_updater_omits_it(su):
    print("poll_until_done: an updater too old to send run_id is not treated as a mismatch")
    states = [
        {"running": True, "returncode": None, "failed_step": None},
        {"running": False, "returncode": 0, "failed_step": None},
    ]
    with _FakeUpdater(states) as fake:
        final = su.poll_until_done(fake.url, {}, request_timeout=5, poll_timeout=10,
                                    poll_interval=0.05, run_id=7)
    check("no run_id in the response is not treated as a mismatch",
          final.get("returncode"), 0)


def test_poll_until_done_detects_run_superseded(su):
    print("poll_until_done: a different run_id appearing mid-poll raises RunSuperseded")
    # Simulates a second `POST /update` landing on the sidecar while this
    # client is still polling for its own run (run_id 5): the updater's
    # single-slot _state now describes run 6, and there is no way left to
    # learn run 5's outcome, so this must be reported rather than silently
    # attributed to run 5.
    states = [
        {"running": True, "returncode": None, "failed_step": None, "run_id": 5},
        {"running": True, "returncode": None, "failed_step": None, "run_id": 6},
        {"running": False, "returncode": 0, "failed_step": None, "run_id": 6},
    ]
    with _FakeUpdater(states) as fake:
        try:
            su.poll_until_done(fake.url, {}, request_timeout=5, poll_timeout=10,
                                poll_interval=0.05, run_id=5)
            message = None
        except su.RunSuperseded as exc:
            message = str(exc)
    check("a run_id change mid-poll raises RunSuperseded", message is not None, True)
    check("the message names both the expected and the newly-seen run_id",
          bool(message) and "5" in message and "6" in message, True)


def test_default_poll_timeout_covers_the_whole_recipe(su):
    print("DEFAULT_POLL_TIMEOUT accounts for all three steps of the built-in recipe")
    # updater/update-server.py applies its UPDATE_TIMEOUT (default 1800s) per
    # step, and the built-in recipe is three steps (git pull, docker compose
    # build, docker compose up -d) -- so the client's own default wait must
    # cover all three, or a legitimate run can report a timeout while it is
    # still going (see the module docstring's Configuration section).
    check("default poll timeout is 3x the updater's default per-step UPDATE_TIMEOUT",
          su.DEFAULT_POLL_TIMEOUT, 3 * 1800)


def main():
    su = _load_self_update()
    test_status_url_derivation(su)
    test_poll_until_done_success(su)
    test_poll_until_done_reports_failure(su)
    test_poll_until_done_times_out(su)
    test_poll_until_done_respects_deadline_despite_long_poll_interval(su)
    test_poll_until_done_caps_request_timeout_to_remaining(su)
    test_poll_until_done_matching_run_id_reports_normally(su)
    test_poll_until_done_ignores_run_id_when_updater_omits_it(su)
    test_poll_until_done_detects_run_superseded(su)
    test_default_poll_timeout_covers_the_whole_recipe(su)
    if failures:
        print(f"FAILED: {len(failures)} check(s): {failures}")
        sys.exit(1)
    print("all self-update status-polling tests passed")


if __name__ == "__main__":
    main()
