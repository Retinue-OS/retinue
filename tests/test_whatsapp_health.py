#!/usr/bin/env python3
"""Checks for the WhatsApp gateway's health state machine (issue #115).

The bridge can hold a live, linked websocket while outbound info queries (IQ /
usync) are wedged — every send to a recipient without a cached device list then
fails while a socket-only health check still says "connected". These tests
exercise the state machine that fixes that: _note_iq_result() folding probe
results into _conn, _health_snapshot() deriving `connected` = "can actually
send", and the reconnect backoff.

Runnable without neonize (the bridge library is only imported inside the
bridge-adapter functions).

    python3 tests/test_whatsapp_health.py
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_whatsapp_gateway(tmp: str):
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    os.environ["WHATSAPP_SEND_POLICY"] = json.dumps([])
    os.environ["WHATSAPP_ACCOUNT"] = "+15551234567"
    os.environ["WHATSAPP_PENDING_SENDS_DIR"] = str(Path(tmp) / "pending")
    os.environ["WHATSAPP_DATA_DIR"] = str(Path(tmp) / "data")
    os.environ["WHATSAPP_TMP_DIR"] = str(Path(tmp) / "tmp")
    spec = importlib.util.spec_from_file_location(
        "whatsapp_gateway_health_under_test", SCRIPTS_DIR / "whatsapp-gateway.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeClient:
    def __init__(self):
        self.disconnects = 0

    def disconnect(self):
        self.disconnects += 1


def test_iq_wedge_flips_health_after_debounce():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_whatsapp_gateway(tmp)
        # Link comes up; no IQ verdict yet — None must not count against health.
        wg._set_conn(connected=True, linked=True)
        snap = wg._health_snapshot()
        assert snap["connected"] is True and snap["iq_ok"] is None

        # One failed probe is a blip, not a wedge (threshold 2).
        assert wg._note_iq_result(False, "info query timed out") is False
        assert wg._health_snapshot()["connected"] is True

        # The second consecutive failure confirms the wedge: /health flips even
        # though the socket/link state is still "up", and the error says what
        # actually breaks (sends needing a device-list lookup).
        assert wg._note_iq_result(False, "info query timed out") is True
        snap = wg._health_snapshot()
        assert snap["connected"] is False and snap["iq_ok"] is False
        assert "usync" in snap["error"] and "info query timed out" in snap["error"]

        # Past the threshold every further failure keeps reporting the wedge,
        # so backoff-limited reconnect attempts keep retrying.
        assert wg._note_iq_result(False, "still wedged") is True

        # Only a successful probe clears it.
        assert wg._note_iq_result(True) is False
        snap = wg._health_snapshot()
        assert snap["connected"] is True and snap["iq_ok"] is True and snap["error"] is None


def test_logged_out_wins_over_iq_state():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_whatsapp_gateway(tmp)
        wg._set_conn(connected=True, linked=True)
        wg._note_iq_result(True)
        wg._set_conn(connected=False, linked=False, logged_out=True, error=None)
        snap = wg._health_snapshot()
        assert snap["connected"] is False
        assert "re-pairing" in snap["error"]


def test_reconnect_backoff():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_whatsapp_gateway(tmp)
        fake = _FakeClient()
        wg._wa_client = fake
        wg._maybe_iq_reconnect()
        wg._maybe_iq_reconnect()  # inside the backoff window — must not fire
        assert fake.disconnects == 1
        wg._last_iq_reconnect = 0.0  # backoff elapsed
        wg._maybe_iq_reconnect()
        assert fake.disconnects == 2


def main() -> int:
    tests = [test_iq_wedge_flips_health_after_debounce,
             test_logged_out_wins_over_iq_state,
             test_reconnect_backoff]
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
