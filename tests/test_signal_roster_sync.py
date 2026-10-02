#!/usr/bin/env python3
"""Checks for the Signal gateway's roster sync request.

A linked device learns the account's contacts and groups only when the
primary device (the phone) sends a sync, and the phone sends one only when
asked. The gateway asks with `signal-cli sendSyncRequest` on start, after a
successful (re)link, and — throttled — when a group id turns up that the
roster does not know. Without the ask the roster stays empty for the life of
the link and every group chat is titled by its raw id.

Runs without signal-cli: `_run` is stubbed to record the command it was
given, the link subprocess is stubbed as in test_signal_relink.py, and
langdetect is stubbed as in the other Signal tests.

    python3 tests/test_signal_roster_sync.py
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_signal_gateway(tmp: str):
    if "langdetect" not in sys.modules:
        stub = types.ModuleType("langdetect")
        stub.detect = lambda *a, **k: "en"
        stub.detect_langs = lambda *a, **k: []
        stub.LangDetectException = type("LangDetectException", (Exception,), {})
        sys.modules["langdetect"] = stub
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    os.environ["SIGNAL_ACCOUNT"] = "+15551234567"
    os.environ["SIGNAL_PENDING_SENDS_DIR"] = str(Path(tmp) / "pending")
    os.environ["PIPER_DATA_DIR"] = str(Path(tmp) / "models")
    os.environ["SIGNAL_ATTACHMENTS_DIR"] = str(Path(tmp) / "attachments")
    spec = importlib.util.spec_from_file_location(
        "signal_gateway_roster_sync_under_test", SCRIPTS_DIR / "signal-gateway.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stub_run(sg, calls: list, returncode: int = 0, stderr: str = ""):
    """Replace the subprocess runner with one that records signal-cli calls."""

    def _fake_run(cmd, check=True, timeout=None):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr=stderr)

    sg._run = _fake_run


def _join_sync_workers(sg) -> None:
    """Wait for miss-triggered sync requests, which run on daemon threads."""
    for t in sg.threading.enumerate():
        if t.name == "roster-sync":
            t.join(timeout=5)


def _sync_calls(calls: list) -> list:
    return [c for c in calls if "sendSyncRequest" in c]


def test_sync_request_command_and_throttle():
    with tempfile.TemporaryDirectory() as tmp:
        sg = _load_signal_gateway(tmp)
        calls = []
        _stub_run(sg, calls)

        assert sg._request_roster_sync("start", force=True) is True
        assert _sync_calls(calls) == [
            ["signal-cli", "-a", "+15551234567", "sendSyncRequest"]
        ]
        # Within the interval, an unforced request is swallowed …
        assert sg._request_roster_sync("miss") is False
        assert len(_sync_calls(calls)) == 1
        # … a forced one is not …
        assert sg._request_roster_sync("relink", force=True) is True
        assert len(_sync_calls(calls)) == 2
        # … and once the interval has passed, an unforced one goes out again.
        sg._roster_sync_at -= sg.SIGNAL_ROSTER_SYNC_INTERVAL + 1
        assert sg._request_roster_sync("miss") is True
        assert len(_sync_calls(calls)) == 3
    print("ok: sync request uses sendSyncRequest and is throttled unless forced")


def test_sync_request_failure_is_non_fatal_and_throttled():
    with tempfile.TemporaryDirectory() as tmp:
        sg = _load_signal_gateway(tmp)
        calls = []
        _stub_run(sg, calls, returncode=1, stderr="not a linked device")
        assert sg._request_roster_sync("start", force=True) is False
        # The attempt is stamped, so the failure is not retried on every miss.
        assert sg._request_roster_sync("miss") is False
        assert len(_sync_calls(calls)) == 1

        # A runner that raises (signal-cli missing, timed out) is contained too.
        def _boom(cmd, check=True, timeout=None):
            raise subprocess.TimeoutExpired(cmd, timeout or 0)

        sg._run = _boom
        assert sg._request_roster_sync("start", force=True) is False
    print("ok: a failing sync request is logged, throttled and never raises")


def test_unknown_group_id_asks_for_a_sync_once():
    with tempfile.TemporaryDirectory() as tmp:
        sg = _load_signal_gateway(tmp)
        calls = []
        _stub_run(sg, calls)
        sg._signal_cli_json = lambda args: [{"id": "g-1", "name": "Family"}]

        # A known group: no sync request.
        assert sg._resolve_group_name("g-1") == "Family"
        assert _sync_calls(calls) == []
        # An unknown one: the roster was read fine but lacks it — ask the phone.
        sg._group_names_at -= sg._GROUP_NAMES_MISS_RETRY + 1
        assert sg._resolve_group_name("g-new") is None
        _join_sync_workers(sg)
        assert len(_sync_calls(calls)) == 1
        # Repeated misses inside the sync interval do not ask again.
        sg._group_names_at -= sg._GROUP_NAMES_MISS_RETRY + 1
        assert sg._resolve_group_name("g-new") is None
        sg._group_names_at -= sg._GROUP_NAMES_MISS_RETRY + 1
        assert sg._resolve_group_name("g-other") is None
        _join_sync_workers(sg)
        assert len(_sync_calls(calls)) == 1
    print("ok: an unknown group id triggers one throttled sync request")


def test_miss_does_not_wait_on_a_busy_signal_cli():
    with tempfile.TemporaryDirectory() as tmp:
        sg = _load_signal_gateway(tmp)
        calls = []
        _stub_run(sg, calls)
        sg._signal_cli_json = lambda args: []
        # Another signal-cli call holds the lock: the lookup must still return
        # at once, and the sync request go out once the lock is free.
        with sg.SIGNAL_CLI_LOCK:
            start = sg.time.monotonic()
            assert sg._resolve_group_name("g-new") is None
            assert sg.time.monotonic() - start < 1
            assert _sync_calls(calls) == []
        _join_sync_workers(sg)
        assert len(_sync_calls(calls)) == 1
    print("ok: a group-name miss never waits on the sync request")


def test_failing_roster_read_keeps_names_and_does_not_ask():
    with tempfile.TemporaryDirectory() as tmp:
        sg = _load_signal_gateway(tmp)
        calls = []
        _stub_run(sg, calls)
        roster = [{"id": "g-1", "name": "Family"}]

        def _fake(args):
            if roster is None:
                raise RuntimeError("signal-cli down")
            return roster

        sg._signal_cli_json = _fake
        assert sg._resolve_group_name("g-1") == "Family"
        sg._group_names_at -= sg._GROUP_NAMES_TTL + 1
        roster = None
        # signal-cli is failing: the last good name survives, and no sync
        # request is stacked on top of the failing call.
        assert sg._resolve_group_name("g-1") == "Family"
        assert sg._resolve_group_name("g-other") is None
        _join_sync_workers(sg)
        assert _sync_calls(calls) == []
    print("ok: a failing roster read keeps the last names and asks for nothing")


class _FakeLinkProc:
    """Stands in for a `signal-cli link` subprocess that pairs successfully."""

    def __init__(self, *args, **kwargs):
        self.stdout = iter(["sgnl://linkdevice?uuid=abc&pub_key=def\n"])
        self.stderr = types.SimpleNamespace(read=lambda: "")
        self.returncode = 0

    def wait(self):
        return self.returncode

    def poll(self):
        return self.returncode

    def kill(self):
        pass


def test_successful_relink_requests_a_sync():
    with tempfile.TemporaryDirectory() as tmp:
        sg = _load_signal_gateway(tmp)
        calls = []
        _stub_run(sg, calls)
        sg.subprocess.Popen = _FakeLinkProc
        sg._qr_png_bytes = lambda uri: b"png"
        # Pretend a sync went out just now: the post-link request must be
        # forced past the throttle, since a fresh link has an empty roster.
        sg._roster_sync_at = sg.time.monotonic()

        sg._note_receive_result(False, "receive failed")
        status, body, _ = sg._relink_qr_response()
        assert status == 202 and body["status"] == "starting"
        for _ in range(200):
            if not sg._RELINK_ACTIVE.is_set():
                break
            sg.time.sleep(0.01)
        assert not sg._RELINK_ACTIVE.is_set(), "relink worker did not finish"
        assert sg._health_snapshot()["connected"] is True
        assert len(_sync_calls(calls)) == 1, calls
    print("ok: a successful relink asks the phone for the roster")


def main() -> int:
    tests = [
        test_sync_request_command_and_throttle,
        test_sync_request_failure_is_non_fatal_and_throttled,
        test_unknown_group_id_asks_for_a_sync_once,
        test_miss_does_not_wait_on_a_busy_signal_cli,
        test_failing_roster_read_keeps_names_and_does_not_ask,
        test_successful_relink_requests_a_sync,
    ]
    failed = 0
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
    if failed:
        return 1
    print("\nAll Signal roster sync checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
