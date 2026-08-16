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
        # The device is still linked — re-pairing is NOT the remedy, so the
        # /gateways page must show the error, not a pairing QR.
        assert snap["needs_repair"] is False

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
        # Unlinked device: scanning a fresh QR IS the remedy.
        assert snap["needs_repair"] is True


class _ProbeClient:
    """Fake neonize client for the IQ-probe call-shape discovery.

    `accepts` picks which argument shape get_user_info tolerates; the other
    shape raises the protobuf error the real binding produces when handed the
    wrong one ("Parameter to initialize message field …"). `wedge` makes the
    accepted shape fail like a wedged bridge instead.
    """

    def __init__(self, accepts="scalar", wedge=None):
        self.accepts = accepts
        self.wedge = wedge
        self.calls = 0

    def get_me(self):
        import types as _t
        return _t.SimpleNamespace(JID="15551234567@s.whatsapp.net")

    def get_user_info(self, arg):
        shape = "list" if isinstance(arg, list) else "scalar"
        if shape != self.accepts:
            raise ValueError(
                "Parameter to initialize message field must be dict or instance "
                "of same class: expected <class 'Neonize_pb2.JID'> got <class 'list'>."
            )
        if self.wedge:
            raise RuntimeError(self.wedge)
        self.calls += 1


def test_iq_probe_discovers_call_shape():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_whatsapp_gateway(tmp)
        # This binding only accepts the list shape; the scalar attempt fails
        # with the protobuf shape error — which must be treated as "try the
        # next convention", never as a wedge (the bug behind the false
        # "disconnected" on a healthy bridge).
        client = _ProbeClient(accepts="list")
        wg._wa_client = client
        wg._iq_probe_once()
        assert client.calls == 1
        assert wg._iq_call == ("get_user_info", "list")
        # The discovered shape is cached and reused.
        wg._iq_probe_once()
        assert client.calls == 2


def test_iq_probe_shape_error_is_unsupported_not_down():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_whatsapp_gateway(tmp)

        class _NoShape(_ProbeClient):
            def get_user_info(self, arg):
                raise ValueError("Parameter to initialize message field must be dict")
            get_user_devices = get_user_info

        wg._wa_client = _NoShape()
        try:
            wg._iq_probe_once()
        except wg._IQProbeUnsupported:
            pass  # correct: disables the probe instead of flagging a wedge
        else:
            raise AssertionError("expected _IQProbeUnsupported")


def test_iq_probe_real_failure_still_raises():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_whatsapp_gateway(tmp)
        wg._wa_client = _ProbeClient(accepts="scalar",
                                     wedge="failed to send usync query: info query timed out")
        try:
            wg._iq_probe_once()
        except RuntimeError as exc:
            assert "usync" in str(exc)
        else:
            raise AssertionError("expected the wedge to raise")


# ── Outbound usync retry / LID fallback (issue #120) ──────────────────────────

class _Runner:
    """Fake op runner: fails usync-style for the JIDs in `bad`, else records."""

    def __init__(self, bad=()):
        self.bad = set(bad)
        self.sent = []  # (jid, op-kind) in execution order

    def __call__(self, jid, op):
        if jid in self.bad:
            raise RuntimeError("failed to get device list: failed to send usync query: "
                               "info query timed out")
        self.sent.append((jid, op["kind"]))


def test_usync_error_classification():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_whatsapp_gateway(tmp)
        assert wg._is_usync_error(RuntimeError(
            "failed to get device list: failed to send usync query: info query timed out"))
        assert wg._is_usync_error(RuntimeError("usync query rejected"))
        assert not wg._is_usync_error(ValueError("recipient not on WhatsApp"))


def test_send_falls_back_to_lid():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_whatsapp_gateway(tmp)
        runner = _Runner(bad={"pn-jid"})
        ops = wg._build_send_ops("hello", [Path(tmp) / "doc.pdf"])
        # The first media op carries the text as caption — no separate text op.
        assert [op["kind"] for op in ops] == ["media"]
        assert ops[0]["caption"] == "hello"
        # The phone-number JID stalls in usync; the cached-LID candidate — the
        # path that delivered in the issue-#120 repro — takes over.
        wg._send_ops_with_retry(["pn-jid", "lid-jid"], ops, runner, "+15551112222",
                                retries=1, backoff=0)
        assert runner.sent == [("lid-jid", "media")]
        assert wg._health_snapshot()["recipient_lookup_ok"] is True


def test_send_partial_failure_never_resends():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_whatsapp_gateway(tmp)

        sent = []
        fails = {"n": 0}

        def runner(jid, op):
            # The first part succeeds on the first candidate; the second part
            # (caption already consumed → "") stalls in usync once.
            if op["caption"] == "" and fails["n"] < 1:
                fails["n"] += 1
                raise RuntimeError("failed to send usync query: info query timed out")
            sent.append((jid, op["caption"]))

        ops = wg._build_send_ops("hello", [Path(tmp) / "a.pdf", Path(tmp) / "b.pdf"])
        assert [op["kind"] for op in ops] == ["media", "media"]
        wg._send_ops_with_retry(["a", "b"], ops, runner, "x", retries=1, backoff=0)
        # The first part went out exactly once (on the first candidate); only
        # the failed second part was re-attempted, on the fallback candidate.
        assert sent == [("a", "hello"), ("b", "")]


def test_send_exhausted_records_lookup_failure():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_whatsapp_gateway(tmp)
        runner = _Runner(bad={"pn-jid"})
        ops = wg._build_send_ops("hello", [])
        try:
            wg._send_ops_with_retry(["pn-jid"], ops, runner, "+15551112222",
                                    retries=1, backoff=0)
        except RuntimeError as exc:
            assert "device list" in str(exc) and "First-contact" in str(exc)
        else:
            raise AssertionError("expected the exhausted send to raise")
        snap = wg._health_snapshot()
        # Informational signal only: the link stays connected (the own-JID
        # probe is green), but the degradation is visible.
        assert snap["recipient_lookup_ok"] is False
        assert "usync" in snap["recipient_lookup_error"]
        # The evidence decays instead of being cleared by a probe.
        with wg._CONN_LOCK:
            wg._conn["recipient_lookup_at"] -= wg.WHATSAPP_RECIPIENT_LOOKUP_TTL + 1
        assert wg._health_snapshot()["recipient_lookup_ok"] is None


def test_send_non_usync_error_propagates():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_whatsapp_gateway(tmp)
        calls = []

        def runner(jid, op):
            calls.append(jid)
            raise ValueError("recipient not on WhatsApp")

        ops = wg._build_send_ops("hello", [])
        try:
            wg._send_ops_with_retry(["a", "b"], ops, runner, "x", retries=3, backoff=0)
        except ValueError:
            pass
        else:
            raise AssertionError("expected the non-usync error to propagate")
        assert calls == ["a"]  # no retry, no fallback


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
             test_iq_probe_discovers_call_shape,
             test_iq_probe_shape_error_is_unsupported_not_down,
             test_iq_probe_real_failure_still_raises,
             test_usync_error_classification,
             test_send_falls_back_to_lid,
             test_send_partial_failure_never_resends,
             test_send_exhausted_records_lookup_failure,
             test_send_non_usync_error_propagates,
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
