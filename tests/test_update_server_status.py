#!/usr/bin/env python3
"""Checks that GET /status can never describe a stale, previous run right
after POST /update dispatches a new one (updater/update-server.py).

`POST /update` used to start `_run_update` in a background thread while
holding `_lock`, but the *worker* thread only set `_state["running"] = True`
(and reset `returncode`/`failed_step`) after it separately reacquired that
lock -- so a caller could receive the 202 and, on its very first `GET
/status`, read `running: false` together with the *previous* run's
`returncode`/`failed_step`, misreporting a brand-new update as an
already-finished one (see PR #223 review, closing out #46). The fix moves
that initialisation -- including a fresh, monotonically increasing `run_id`
-- into `do_POST` itself, inside the same lock acquisition that starts the
worker and before the 202 goes out, so there is no window left in which a
poll can observe stale state.

Drives the real `Handler`/`ThreadingHTTPServer` from updater/update-server.py
(not a fake) against a scripted `UPDATE_COMMAND` that takes just long enough
to still be `running` when polled immediately afterwards, and repeats a
dispatch-then-poll-immediately cycle many times so a regression in the
synchronisation (state written from the worker thread instead of `do_POST`)
gets a realistic chance to show up as a flake, rather than asserting a
timing-dependent race exactly once.

    python3 tests/test_update_server_status.py
"""
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOKEN = "test-token"
HEADERS = {"X-Update-Token": TOKEN}

failures = []


def check(label, got, want):
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
        failures.append(label)


def _load_update_server(tmp: Path):
    """Import updater/update-server.py fresh, configured for the test:
    UPDATE_COMMAND is a fixed, fast shell recipe (no real git/docker calls),
    and PROJECT_DIR/UPDATE_LOG_PATH point inside the temp dir so the run's log
    lands somewhere writable and disposable."""
    os.environ["UPDATER_TOKEN"] = TOKEN
    os.environ["PROJECT_DIR"] = str(tmp)
    os.environ["UPDATE_LOG_PATH"] = str(tmp / "update.log")
    # Long enough to still be `running` when polled immediately after
    # dispatch (a local HTTP round trip is far faster than this), short
    # enough that a whole test run of many iterations stays quick.
    os.environ["UPDATE_COMMAND"] = "sleep 0.2"
    # Generous relative to the 0.2s recipe so a slow/loaded CI host cannot
    # make a legitimate run look like a per-step timeout.
    os.environ["UPDATE_TIMEOUT"] = "30"
    os.environ.pop("GITHUB_TOKEN", None)
    spec = importlib.util.spec_from_file_location(
        "update_server_under_test", REPO_ROOT / "updater" / "update-server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # Handler.log_message writes one line per request to stderr in
    # production, which is exactly what a real deployment wants but just
    # noise across dozens of requests in a tight test loop.
    mod.Handler.log_message = lambda self, fmt, *args: None
    return mod


class _RealUpdater:
    """Runs the actual Handler from updater/update-server.py on a real
    ThreadingHTTPServer (the class production uses -- a plain single-threaded
    HTTPServer would itself serialise the POST and the immediate follow-up
    GET, hiding the very race this file exists to catch)."""

    def __init__(self, mod):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
        self.port = self.server.server_address[1]

    def __enter__(self):
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def _post_update(base_url: str):
    request = urllib.request.Request(f"{base_url}/update", data=b"", headers=HEADERS, method="POST")
    with urllib.request.urlopen(request, timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _get_status(base_url: str) -> dict:
    request = urllib.request.Request(f"{base_url}/status", headers=HEADERS, method="GET")
    with urllib.request.urlopen(request, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def test_status_never_stale_immediately_after_dispatch(base_url):
    print("GET /status right after POST /update never shows a previous run's state")
    iterations = 25
    prior_run_id = None
    for i in range(iterations):
        status_code, body = _post_update(base_url)
        check(f"[{i}] POST /update accepted", status_code, 202)
        dispatched_run_id = body.get("run_id")
        check(f"[{i}] 202 body carries an integer run_id", isinstance(dispatched_run_id, int), True)
        if prior_run_id is not None:
            check(f"[{i}] run_id strictly increases on every dispatch",
                  dispatched_run_id > prior_run_id, True)

        # The critical read: no artificial delay here at all -- this mirrors
        # a client's very first poll straight after dispatch, which is
        # exactly where the pre-fix code could still show `running: false`
        # plus the previous run's leftover returncode/failed_step.
        state = _get_status(base_url)
        check(f"[{i}] /status run_id matches the dispatch that was just accepted",
              state.get("run_id"), dispatched_run_id)
        check(f"[{i}] running is true -- never a stale false right after dispatch",
              state.get("running"), True)
        check(f"[{i}] returncode is reset, not a leftover from a previous run",
              state.get("returncode"), None)
        check(f"[{i}] failed_step is reset, not a leftover from a previous run",
              state.get("failed_step"), None)

        # Let this run finish before the next dispatch -- POST /update
        # rejects a concurrent one with 409 while running is true.
        deadline = time.monotonic() + 5
        while True:
            state = _get_status(base_url)
            if not state.get("running"):
                break
            if time.monotonic() >= deadline:
                raise AssertionError(f"[{i}] run did not finish inside the test's own wait")
            time.sleep(0.02)
        check(f"[{i}] the finished run's own outcome is its own (scripted recipe always succeeds)",
              state.get("returncode"), 0)
        check(f"[{i}] the finished state still carries this run's run_id",
              state.get("run_id"), dispatched_run_id)
        prior_run_id = dispatched_run_id


def test_concurrent_dispatch_rejected_with_409(base_url):
    print("a second POST /update while one is running is rejected, not queued or raced")
    status_code, body = _post_update(base_url)
    check("first dispatch accepted", status_code, 202)
    try:
        _post_update(base_url)
        rejected = False
        code = None
    except urllib.error.HTTPError as exc:
        rejected = True
        code = exc.code
    check("a concurrent dispatch is rejected", rejected, True)
    check("...with 409, not started or queued", code, 409)
    # Drain the first run so it doesn't bleed into a later test.
    deadline = time.monotonic() + 5
    while _get_status(base_url).get("running"):
        if time.monotonic() >= deadline:
            raise AssertionError("run did not finish draining after the 409 check")
        time.sleep(0.02)


def test_status_correct_even_with_a_slow_worker_thread_start():
    print("GET /status is correct immediately after dispatch even when the worker "
          "thread is slow to actually start running")
    # The sharpest form of Finding 1's race: do_POST starts the worker thread
    # and returns 202, but nothing guarantees the OS schedules that thread
    # promptly -- under load, it may not run for a while. A correct fix must
    # not depend on prompt scheduling at all, so artificially delaying the
    # worker's start (well past any real HTTP round trip) must not change the
    # answer. Unlike the repeated-dispatch test above, this does not rely on
    # timing luck: if `do_POST`'s own synchronous state init were ever moved
    # back into `_run_update` (the original bug), this reliably fails, since
    # the delay below is far larger than a local HTTP round trip.
    with tempfile.TemporaryDirectory() as td:
        mod = _load_update_server(Path(td))
        original_run_update = mod._run_update

        def _delayed_run_update(run_id):
            time.sleep(0.3)
            return original_run_update(run_id)

        mod._run_update = _delayed_run_update
        with _RealUpdater(mod) as updater:
            status_code, body = _post_update(updater.base_url)
            check("POST /update accepted", status_code, 202)
            dispatched_run_id = body.get("run_id")
            # No sleep here at all: read /status as fast as urllib can
            # manage -- certain to land well inside the artificial 0.3s
            # delay above.
            state = _get_status(updater.base_url)
            check("run_id matches despite the slow worker thread",
                  state.get("run_id"), dispatched_run_id)
            check("running is true despite the slow worker thread",
                  state.get("running"), True)
            check("returncode is reset despite the slow worker thread",
                  state.get("returncode"), None)
            check("failed_step is reset despite the slow worker thread",
                  state.get("failed_step"), None)
            # Drain so the background run doesn't outlive this `with` block.
            deadline = time.monotonic() + 5
            while _get_status(updater.base_url).get("running"):
                if time.monotonic() >= deadline:
                    raise AssertionError("run did not finish inside the test's own wait")
                time.sleep(0.02)


def main():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        mod = _load_update_server(tmp)
        with _RealUpdater(mod) as updater:
            test_status_never_stale_immediately_after_dispatch(updater.base_url)
            test_concurrent_dispatch_rejected_with_409(updater.base_url)
    test_status_correct_even_with_a_slow_worker_thread_start()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {failures}")
        sys.exit(1)
    print("all updater /status staleness tests passed")


if __name__ == "__main__":
    main()
