#!/usr/bin/env python3
"""A queued send remembers the dashboard thread that asked for it, and the
user's Allow/Deny on /sends is noted back into that thread.

The thread travels RETINUE_THREAD_ID (session_env) → the push CLI's "thread"
(send_origin) → the gateway's pending entry → the web-gateway's report.

    python3 tests/test_send_decision_thread.py
"""
import argparse
import importlib.util
import io
import json
import os
import sys
import tempfile
import types
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import send_origin  # noqa: E402
import session_env  # noqa: E402

THREAD = "0123456789abcdef0123456789abcdef"


def test_send_origin():
    assert send_origin.valid_thread(THREAD.upper()) == THREAD
    assert send_origin.valid_thread("not-a-thread") is None
    assert send_origin.valid_thread(None) is None
    assert send_origin.stamp({}, THREAD) == {"thread": THREAD}
    assert send_origin.stamp({}, "../etc") == {}
    with patch.dict(os.environ, {"RETINUE_THREAD_ID": THREAD}):
        parser = argparse.ArgumentParser()
        send_origin.add_argument(parser)
        assert parser.parse_args([]).thread == THREAD
        assert parser.parse_args(["--thread", "x"]).thread == "x"


def test_session_env_thread_is_per_spawn():
    env = session_env.build({"RETINUE_THREAD_ID": "stale", "PATH": "/bin"})
    assert "RETINUE_THREAD_ID" not in env, "a stale thread id must not be inherited"
    env = session_env.build({"PATH": "/bin"}, thread_id=THREAD)
    assert env["RETINUE_THREAD_ID"] == THREAD


def test_signal_gateway_keeps_thread():
    if "langdetect" not in sys.modules:
        stub = types.ModuleType("langdetect")
        stub.detect = lambda *a, **k: "en"
        stub.detect_langs = lambda *a, **k: []
        stub.LangDetectException = type("LangDetectException", (Exception,), {})
        sys.modules["langdetect"] = stub
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["SIGNAL_SEND_POLICY"] = "[]"
        os.environ["SIGNAL_PENDING_SENDS_DIR"] = tmp
        os.environ["PIPER_DATA_DIR"] = str(Path(tmp) / "models")
        os.environ["SIGNAL_ATTACHMENTS_DIR"] = str(Path(tmp) / "attachments")
        spec = importlib.util.spec_from_file_location(
            "signal_gateway_thread_test", SCRIPTS_DIR / "signal-gateway.py")
        sg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sg)
        assert sg._origin_thread({"thread": THREAD}) == THREAD
        assert sg._origin_thread({"thread": "../x"}) is None
        assert sg._origin_thread({}) is None
        rid = sg._new_pending_send("+15550001111", "hi", None, [], False,
                                   "verify", thread=THREAD)
        stored = json.loads((Path(tmp) / f"{rid}.json").read_text())
        assert stored["thread"] == THREAD
        snapshot = sg._complete_pending_send(rid, approved=False)
        assert snapshot["status"] == "rejected" and snapshot["thread"] == THREAD


def _load_web_gateway(tmp: Path):
    os.environ["CONVERSATIONS_DIR"] = str(tmp / "convs")
    os.environ["CONVERSATION_DIR"] = str(tmp / "convlog")
    os.environ["CHAMBERS_DIR"] = str(tmp / "chambers")
    os.environ["WEB_GATEWAY_STATE"] = str(tmp / "state.json")
    os.environ["EMAIL_USER"] = "you@example.com"
    os.environ["EMAIL_PASS"] = "x"
    os.environ["IMAP_HOST"] = "imap.example.com"
    os.environ["SMTP_HOST"] = "smtp.example.com"
    (tmp / "chambers").mkdir(parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location(
        "web_gateway_send_decision_test", SCRIPTS_DIR / "web-gateway.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.SEND_DECISION_POLL_INTERVAL = 0.0
    return mod


def _agent_notes(wg, cid):
    return [m for m in wg._load_conv(cid)["messages"] if m["role"] == "agent"]


def test_channel_reject_is_noted(wg, gw):
    cid = wg._new_conv("user", "me", "Reply", "user", "answer Mara")["id"]
    entry = {"id": "a" * 32, "status": "rejected", "thread": cid,
             "recipient": "+15550001111", "message": "See you at eight!"}
    with redirect_stdout(io.StringIO()):
        wg._report_channel_send_action("signal-gateway", gw, "a" * 32, "reject", entry)
        # A second click answers with the same entry: noted once.
        wg._report_channel_send_action("signal-gateway", gw, "a" * 32, "reject", entry)
    notes = _agent_notes(wg, cid)
    assert len(notes) == 1, notes
    assert "denied" in notes[0]["text"] and "See you at eight!" in notes[0]["text"]
    assert "+15550001111" in notes[0]["text"]
    assert "a" * 32 in notes[0]["context"]


def test_channel_approve_waits_for_outcome(wg, gw):
    cid = wg._new_conv("user", "me", "Reply", "user", "answer Mara")["id"]
    rid = "b" * 32
    entry = {"id": rid, "status": "sending", "thread": cid,
             "recipient": "+15550001111", "message": "Yes"}
    polls = iter([(200, {**entry, "status": "sending"}),
                  (200, {**entry, "status": "approved"})])
    with patch.object(wg, "_gateway_hop", side_effect=lambda *a, **k: next(polls)):
        with redirect_stdout(io.StringIO()):
            wg._await_channel_send_outcome("signal-gateway", gw, rid, entry)
    notes = _agent_notes(wg, cid)
    assert len(notes) == 1 and "was sent" in notes[0]["text"], notes


def test_channel_approve_failure_wakes_thread(wg, gw):
    conv = wg._new_conv("user", "me", "Reply", "user", "answer Mara")
    cid = conv["id"]
    wg._conv_set_flags(cid, unread=False)
    rid = "c" * 32
    entry = {"id": rid, "status": "sending", "thread": cid, "recipient": "+1555"}
    with patch.object(wg, "_gateway_hop",
                      return_value=(200, {**entry, "status": "error",
                                          "error": "untrusted identity"})):
        with redirect_stdout(io.StringIO()):
            wg._await_channel_send_outcome("signal-gateway", gw, rid, entry)
    notes = _agent_notes(wg, cid)
    assert len(notes) == 1 and "untrusted identity" in notes[0]["text"], notes
    assert wg._load_conv(cid)["unread"] is True


def test_no_thread_no_note(wg, gw):
    cid = wg._new_conv("user", "me", "Reply", "user", "x")["id"]
    entry = {"id": "d" * 32, "status": "rejected", "recipient": "+1555"}
    with patch.object(wg.threading, "Thread") as thread:
        wg._report_channel_send_action("signal-gateway", gw, "d" * 32, "approve",
                                       {**entry, "status": "sending"})
        thread.assert_not_called()
    wg._report_channel_send_action("signal-gateway", gw, "d" * 32, "reject", entry)
    assert _agent_notes(wg, cid) == []


class _FakeConnection:
    def getsockname(self):
        return ("172.19.0.9", 8080)


class _FakeSendHandler:
    def __init__(self):
        self.html = None
        self.redirected_to = None
        self.client_address = ("172.19.0.4", 51234)
        self.connection = _FakeConnection()

    def _send_html(self, status, body):
        self.html = (status, body)

    def _redirect(self, location):
        self.redirected_to = location


def test_email_decisions_are_noted(wg):
    _FakeSendHandler._request_from_edge = wg.Handler._request_from_edge
    cid = wg._new_conv("user", "me", "Mail", "user", "reply to the landlord")["id"]
    approved = {"approved": "42", "sent": True, "to": ["landlord@example.com"],
                "subject": "Re: Rent", "saved_to_sent": True,
                "stripped_headers": [], "thread": cid}
    with patch.object(wg.ec, "approve_pending_send", return_value=approved):
        with redirect_stdout(io.StringIO()):
            wg.Handler._handle_send_action(_FakeSendHandler(), "default", "42", "approve")
    pending = {"request_id": "43", "to": "landlord@example.com",
               "subject": "Re: Keys", "thread": cid}
    with patch.object(wg.ec, "get_pending_send", return_value=pending), \
            patch.object(wg.ec, "delete_pending_draft") as delete:
        with redirect_stdout(io.StringIO()):
            wg.Handler._handle_send_action(_FakeSendHandler(), "default", "43", "reject")
    delete.assert_called_once()
    notes = [m["text"] for m in _agent_notes(wg, cid)]
    assert len(notes) == 2, notes
    assert "Re: Rent" in notes[0] and "was sent" in notes[0], notes
    assert "Re: Keys" in notes[1] and "denied" in notes[1], notes


def test_email_header_round_trip():
    spec = importlib.util.spec_from_file_location(
        "email_client_thread_test", SCRIPTS_DIR / "email_client.py")
    ec = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ec)
    assert ec.REQUEST_THREAD_HEADER in ec._REQUEST_HEADERS, \
        "the thread header must be stripped before the message goes out"
    captured = {}

    def fake_append(cfg, folder, msg, seen=False):
        captured["msg"] = msg
        return "7"

    with patch.object(ec, "_append", fake_append), \
            patch.dict(os.environ, {"RETINUE_THREAD_ID": THREAD}):
        msg = ec.EmailMessage()
        cfg = types.SimpleNamespace(drafts_folder="Drafts")
        assert ec.register_pending_send(cfg, msg, "verify") == "7"
    assert captured["msg"][ec.REQUEST_THREAD_HEADER] == THREAD


def main():
    test_send_origin()
    test_session_env_thread_is_per_spawn()
    test_signal_gateway_keeps_thread()
    test_email_header_round_trip()
    with tempfile.TemporaryDirectory() as td:
        wg = _load_web_gateway(Path(td))
        gw = {"base_url": "http://signal-gateway:8090", "token": "", "label": "Signal"}
        test_channel_reject_is_noted(wg, gw)
        test_channel_approve_waits_for_outcome(wg, gw)
        test_channel_approve_failure_wakes_thread(wg, gw)
        test_no_thread_no_note(wg, gw)
        test_email_decisions_are_noted(wg)
    print("all send-decision thread tests passed")


if __name__ == "__main__":
    main()
