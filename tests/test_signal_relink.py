#!/usr/bin/env python3
"""Regression checks for the Signal gateway's re-link (GET /qr) state machine.

Covers the race found in the PR #57 review: after a successful `signal-cli
link`, /health must report the link as up IMMEDIATELY (the pairing is the
proof), not only once the parked receive loop completes its next poll —
otherwise a page-driven GET /qr landing in that window starts a second link
attempt, parking the receive loop again and showing a fresh QR to a user who
just scanned.

Runs without signal-cli: the link subprocess is stubbed to behave like a
successful pairing. langdetect is stubbed as in the other Signal tests.

    python3 tests/test_signal_relink.py
"""
import importlib.util
import os
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
        "signal_gateway_relink_under_test", SCRIPTS_DIR / "signal-gateway.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def test_successful_relink_marks_link_up_immediately():
    with tempfile.TemporaryDirectory() as tmp:
        sg = _load_signal_gateway(tmp)
        sg.subprocess.Popen = _FakeLinkProc  # stub only the link subprocess
        # QR rendering needs segno, which the test env may not have — the
        # worker treats a render failure as non-fatal, so stub it out too.
        sg._qr_png_bytes = lambda uri: b"png"

        # The link is down and the receive loop has recorded why.
        sg._note_receive_result(False, "receive failed")
        assert sg._health_snapshot()["connected"] is False

        # First GET /qr starts a relink attempt.
        status, body, _ = sg._relink_qr_response()
        assert status == 202 and body["status"] == "starting"

        # Let the worker run to completion (it pairs successfully).
        for _ in range(200):
            if not sg._RELINK_ACTIVE.is_set():
                break
            sg.time.sleep(0.01)
        assert not sg._RELINK_ACTIVE.is_set(), "relink worker did not finish"

        # The pairing itself proves connectivity: /health is up NOW, before the
        # (parked) receive loop has had a chance to poll again …
        assert sg._health_snapshot()["connected"] is True
        # … so a page-driven refresh of the same <img> cannot start a second
        # link attempt.
        status, body, _ = sg._relink_qr_response()
        assert status == 409 and body["status"] == "connected"
        assert not sg._RELINK_ACTIVE.is_set()


def main() -> int:
    try:
        test_successful_relink_marks_link_up_immediately()
        print("PASS test_successful_relink_marks_link_up_immediately")
    except AssertionError as exc:
        print(f"FAIL test_successful_relink_marks_link_up_immediately: {exc}")
        return 1
    print("\nAll Signal relink checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
