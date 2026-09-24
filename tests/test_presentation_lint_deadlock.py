#!/usr/bin/env python3
"""Checks that the presentation lint can never hang the request it runs inside.

The lint is a `claude` subprocess, so it must be bounded — but it is bounded
*separately* from the session pool, and its wait for a slot is capped. Both
properties are correctness requirements, not tuning:

  * The lint runs inside an HTTP request, and that request's caller is regularly
    a session the gateway itself spawned — every `conversation-push.py` from an
    agent session is that case, and such a session already holds one of the
    `WEB_GATEWAY_MAX_CONCURRENCY` session slots for as long as its turn runs.
    While the lint shared `_worker_pool`, that caller waited for a slot it was
    itself holding: with the default bound of 2, one other busy session hung the
    push forever. The client timed out, the gateway thread stayed queued on the
    semaphore, and the thread never appeared in the dashboard at all.
  * Even on its own pool a lint can find it full, and an unbounded wait there is
    the same bug one level down. A lint that cannot get a slot is skipped, and
    the message goes out unlinted — form is never worth withholding the message.

    python3 tests/test_presentation_lint_deadlock.py
"""
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import types
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO / "scripts"

FAILURES = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def _load(module_name: str, script: str):
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS_DIR / script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stub_langdetect():
    if "langdetect" not in sys.modules:
        stub = types.ModuleType("langdetect")
        stub.detect = lambda *a, **k: "en"
        stub.detect_langs = lambda *a, **k: []
        stub.LangDetectException = type("LangDetectException", (Exception,), {})
        sys.modules["langdetect"] = stub


# The message the "agent" pushes, and what a lint would hand back. Both are
# comfortably inside the drift guards, so a successful lint is visible in the
# stored thread and a skipped one is visible by its absence.
RAW = "Your parcel is waiting. Collect it, or have it returned to the sender?"
LINTED = (RAW + " [[chip: Collect | I will collect the parcel.]] "
          "[[chip: Return | Send it back to the sender.]]")


def _fake_claude(record):
    """Stand in for `_run_claude`: no subprocess, no token refresh, no model."""
    def run(cmd, **kwargs):
        record.append(cmd)
        return types.SimpleNamespace(
            returncode=0, stdout=json.dumps({"result": LINTED}), stderr="")
    return run


def main():
    _stub_langdetect()
    conv_dir = tempfile.mkdtemp(prefix="lint-deadlock-convs-")
    os.environ.update({
        "CONVERSATIONS_DIR": conv_dir,
        "CONVERSATION_BACKEND_TOKEN": "test-token",
        "PRESENTATION_LINT": "1",
        # One session slot and one lint slot: the smallest configuration in
        # which the two pools can be told apart at all.
        "WEB_GATEWAY_MAX_CONCURRENCY": "1",
        "PRESENTATION_LINT_CONCURRENCY": "1",
        # Short enough that a hung wait is a visible test failure rather than a
        # test that takes half a minute.
        "PRESENTATION_LINT_WAIT": "2",
    })
    wg = _load("web_gateway_lint_deadlock", "web-gateway.py")

    spawned = []
    wg._run_claude = _fake_claude(spawned)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), wg.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def push(message, key, timeout):
        """POST a thread the way conversation-push.py does. Returns (status, body)."""
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/internal/conversations",
            data=json.dumps({"message": message, "key": key,
                             "title": "Parcel"}).encode(),
            headers={"Content-Type": "application/json",
                     "X-Conversation-Backend-Token": "test-token"},
            method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())

    def stored_text(cid):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/conversations/{cid}", timeout=10) as r:
            return json.loads(r.read().decode())["messages"][0]["text"]

    try:
        # ── 1. The pools are distinct ────────────────────────────────────────
        print("the lint does not share the session pool")
        check("_lint_pool is not _worker_pool",
              wg._lint_pool is not wg._worker_pool)

        # ── 2. The deadlock itself ───────────────────────────────────────────
        # Every session slot taken, exactly as when the pushing agent's own
        # turn holds one. Before the fix this POST never returned.
        print("a full session pool does not block a lint")
        wg._worker_pool.acquire()
        try:
            t0 = time.monotonic()
            try:
                status, body = push(RAW, "parcel-1", timeout=15)
                elapsed = time.monotonic() - t0
                check("the push returns while every session slot is held",
                      status == 201, f"status {status}")
                check("it returns promptly, not after a timeout",
                      elapsed < 5, f"{elapsed:.1f}s")
                check("and it was actually linted, not merely let through",
                      stored_text(body["id"]) == LINTED)
                check("the lint spawned exactly one claude process",
                      len(spawned) == 1, f"{len(spawned)} spawns")
            except (urllib.error.URLError, TimeoutError) as exc:
                check("the push returns while every session slot is held",
                      False, f"{type(exc).__name__}: {exc} — the deadlock is back")
        finally:
            wg._worker_pool.release()

        # ── 3. The capped wait on the lint's own pool ────────────────────────
        # The lint pool full is the same bug one level down. The message must
        # still be delivered — unlinted, and after at most the configured wait.
        print("a full lint pool skips the lint instead of queueing forever")
        spawned.clear()
        wg._lint_pool.acquire()
        try:
            t0 = time.monotonic()
            try:
                status, body = push(RAW, "parcel-2", timeout=20)
                elapsed = time.monotonic() - t0
                check("the push still returns", status == 201, f"status {status}")
                check("it waited roughly PRESENTATION_LINT_WAIT, then gave up",
                      1.5 <= elapsed < 10, f"{elapsed:.1f}s")
                check("the message went out unlinted rather than being withheld",
                      stored_text(body["id"]) == RAW)
                check("no claude process was spawned for the skipped lint",
                      not spawned, f"{len(spawned)} spawns")
            except (urllib.error.URLError, TimeoutError) as exc:
                check("the push still returns", False,
                      f"{type(exc).__name__}: {exc} — the wait is unbounded")
        finally:
            wg._lint_pool.release()

        # ── 4. The slot is returned, every time ─────────────────────────────
        # A lint pool that leaks a slot degrades into case 3 permanently: the
        # first failing lint would silently disable linting for good.
        print("the lint slot survives a failing lint")
        spawned.clear()

        def boom(cmd, **kwargs):
            spawned.append(cmd)
            raise OSError("claude is not on the path")

        wg._run_claude = boom
        status, body = push(RAW, "parcel-3", timeout=15)
        check("a crashing lint still delivers the message",
              status == 201 and stored_text(body["id"]) == RAW, f"status {status}")

        wg._run_claude = _fake_claude(spawned)
        t0 = time.monotonic()
        status, body = push(RAW, "parcel-4", timeout=15)
        elapsed = time.monotonic() - t0
        check("the next lint gets a slot immediately",
              elapsed < 1.5, f"{elapsed:.1f}s — the crashed lint leaked its slot")
        check("and it lints normally again", stored_text(body["id"]) == LINTED)
    finally:
        srv.shutdown()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: " + ", ".join(FAILURES))
        return 1
    print("all presentation-lint deadlock checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
