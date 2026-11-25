#!/usr/bin/env python3
"""Retracting a queued send, on every channel gateway with a pending store.

E-mail has had `email_client.py retract` all along; the messenger and calendar
gateways now take `POST /pending-sends/<id>/retract`, which the push CLIs call
through `--retract` (scripts/pending_retract.py). Each gateway is loaded with
the sandbox loader of its own policy test, served on a loopback port, and
driven through the CLI helper exactly as an agent would:

  * a pending send retracts to status "retracted", is never executed, leaves
    the pending list, and a later approve cannot revive it;
  * a send the user already approved cannot be retracted ("too late", exit 1);
  * a send the user already denied reports that nothing was sent (exit 0);
  * an unknown id is a 404 (exit 1), a malformed one never reaches the gateway;
  * the mail gateway's queue: a send that failed after it was allowed is "too
    late … failed; nothing was sent", and one whose outcome nobody knows is
    never worded as failed.

    python3 tests/test_pending_retract.py
"""
import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
TESTS_DIR = REPO_ROOT / "tests"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import pending_retract  # noqa: E402

_VERIFY = [{"number": "*", "category": "verify"}]


def _test_module(name):
    spec = importlib.util.spec_from_file_location(f"{name}_loader", TESTS_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _signal(tmp):
    gw = _test_module("test_signal_send_policy")._load_signal_gateway(_VERIFY, tmp)
    return gw, gw._PushHandler, "_execute_approved_send", lambda: gw._new_pending_send(
        "+15551234567", "hello", None, images=[], voice=False, category="verify")


def _whatsapp(tmp):
    gw = _test_module("test_whatsapp_send_policy")._load_whatsapp_gateway(_VERIFY, tmp)
    return gw, gw._PushHandler, "_execute_approved_send", lambda: gw._new_pending_send(
        "+15551234567", "hello", None, images=[], voice=False, category="verify")


def _telegram(tmp):
    gw = _test_module("test_telegram_send_policy")._load_telegram_gateway(_VERIFY, tmp)
    return gw, gw._PushHandler, "_execute_approved_send", lambda: gw._new_pending_send(
        "@someone", "hello", None, images=[], voice=False, category="verify")


def _sms(tmp):
    gw = _test_module("test_sms_gateway")._load(tmp, policy=_VERIFY)
    return gw, gw._Handler, "_execute_approved_send", lambda: gw._new_pending_send(
        "+41791112233", "hello", "verify")


def _caldav(tmp):
    gw = _test_module("test_caldav_send_policy")._load_caldav_gateway(_VERIFY, tmp)
    return gw, gw._PushHandler, "_execute_approved_event", lambda: gw._new_pending_send(
        "Dentist", "2026-10-01T14:00:00", "2026-10-01T14:30:00", False, "", None, "verify")


GATEWAYS = {"signal": _signal, "whatsapp": _whatsapp, "telegram": _telegram,
            "sms": _sms, "caldav": _caldav}
_TOKENS = ("SIGNAL_GATEWAY_TOKEN", "WHATSAPP_GATEWAY_TOKEN", "TELEGRAM_GATEWAY_TOKEN",
           "SMS_GATEWAY_TOKEN", "CALDAV_GATEWAY_TOKEN", "GATEWAY_TOKEN")


@contextlib.contextmanager
def _served(name):
    for var in _TOKENS:
        os.environ.pop(var, None)
    with tempfile.TemporaryDirectory() as tmp:
        gw, handler, executor, new_pending = GATEWAYS[name](tmp)
        executed = []
        # Record instead of sending: approval hands the entry to this worker.
        setattr(gw, executor, lambda path, entry: executed.append(entry["id"]))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/send"
            yield gw, new_pending, executed, url
        finally:
            server.shutdown()
            server.server_close()


def _retract(url, request_id):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = pending_retract.retract("test-push", url, request_id, "", 10)
    return code, out.getvalue() + err.getvalue()


def test_retract_pending_send():
    for name in GATEWAYS:
        with _served(name) as (gw, new_pending, executed, url):
            rid = new_pending()
            code, text = _retract(url, rid)
            assert code == 0, (name, text)
            assert "retracted" in text, (name, text)
            assert gw._get_pending_send_detail(rid)["status"] == "retracted", name
            assert [e["id"] for e in gw._list_pending_sends_store()] == [], name
            # The user's approval, arriving after the agent took it back,
            # finds nothing to send.
            assert gw._complete_pending_send(rid, approved=True)["status"] == "retracted", name
            assert executed == [], (name, executed)
            # Retracting twice is harmless.
            assert _retract(url, rid)[0] == 0, name
        print(f"ok: {name}: pending send retracted, never executed")


def test_retract_after_approval_is_too_late():
    for name in GATEWAYS:
        with _served(name) as (gw, new_pending, executed, url):
            rid = new_pending()
            gw._complete_pending_send(rid, approved=True)
            code, text = _retract(url, rid)
            assert code == 1, (name, text)
            assert "too late" in text, (name, text)
            assert gw._get_pending_send_detail(rid)["status"] == "sending", name
        print(f"ok: {name}: approved send cannot be retracted")


def test_retract_after_denial_reports_nothing_sent():
    for name in GATEWAYS:
        with _served(name) as (gw, new_pending, executed, url):
            rid = new_pending()
            gw._complete_pending_send(rid, approved=False)
            code, text = _retract(url, rid)
            assert code == 0, (name, text)
            assert "denied" in text, (name, text)
            assert gw._get_pending_send_detail(rid)["status"] == "rejected", name
        print(f"ok: {name}: denied send reports nothing sent")


def test_unknown_and_malformed_ids():
    for name in GATEWAYS:
        with _served(name) as (gw, new_pending, executed, url):
            code, text = _retract(url, "0" * 32)
            assert code == 1 and "no pending" in text, (name, text)
    # Malformed: refused before any request (the URL is unreachable on purpose).
    code, text = _retract("http://127.0.0.1:9/send", "../etc/passwd")
    assert code == 2 and "not a request id" in text, text
    print("ok: unknown id is 404, malformed id never sent")


def test_the_mail_gateways_outcomes():
    """The mail gateway tells a send that failed from one of which nobody
    knows whether it went out (``outcome_unknown``): the second is never
    reported as "failed" — whoever read that would send the mail again."""
    send = _test_module("test_mail_gateway_send")
    t = send.t
    w = send.world(policy="verify")
    try:
        gw, smtp = w.gw, w.smtp
        server, base = t.serve(gw)
        token = t.TOKEN

        def retract(request_id):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = pending_retract.retract("mail-push", base + "/send", request_id,
                                               token, 10)
            return code, out.getvalue(), err.getvalue()
        try:
            # Queued, and taken back: nothing is sent.
            _s, queued = w.send(to=["ann@x.org"], message="One.")
            code, out, err = retract(queued["request_id"])
            assert code == 0 and "retracted; nothing was sent" in out and err == ""
            assert gw.SENDS.get(queued["request_id"])["status"] == "retracted"
            # Allowed, and refused by the mail server: failed, for certain.
            _s, queued = w.send(to=["ann@x.org"], message="Two.")
            smtp.reject_data = "554 5.7.1 content rejected"
            gw._decide_send(queued["request_id"], "approve")
            smtp.reject_data = None
            code, out, err = retract(queued["request_id"])
            assert code == 1 and out == "" and "too late" in err, err
            assert "the send failed; nothing was sent" in err and "NOT KNOWN" not in err
            # Allowed, handed over, and the server's answer lost: nobody
            # knows — and that is what is said, not "failed".
            _s, queued = w.send(to=["ann@x.org"], message="Three.")
            smtp.drop = "dot"
            gw._decide_send(queued["request_id"], "approve")
            smtp.drop = None
            entry = gw.SENDS.get(queued["request_id"])
            assert entry["status"] == "error" and entry["outcome_unknown"] is True
            code, out, err = retract(queued["request_id"])
            assert code == 1 and out == "" and "too late" in err, err
            assert "NOT KNOWN whether it went out" in err and "Do not send it again" in err
            assert "failed" not in err, err
            # Dismissed from its chat, it is still that: over, not "failed".
            gw.SENDS.update(queued["request_id"], request_key="k-1")
            gw.SENDS.dismiss(queued["request_id"])
            assert gw.SENDS.get(queued["request_id"])["dismissed"] is True
            code, out, err = retract(queued["request_id"])
            assert code == 1 and "NOT KNOWN whether it went out" in err and "failed" not in err
            assert len(smtp.mails) == 1, "the one that was handed over"
        finally:
            server.shutdown()
    finally:
        send.done(w)
    # A gateway that does not say either way is read as before.
    assert pending_retract._OUTCOMES["error"][1].endswith("the send failed")
    print("ok: mail: a send of unknown outcome is never reported as failed")


def test_gateway_base():
    assert pending_retract.gateway_base("http://signal-gateway:8090/send") == \
        "http://signal-gateway:8090"
    assert pending_retract.gateway_base("http://caldav-gateway:8094/create-event/") == \
        "http://caldav-gateway:8094"
    print("ok: gateway base derived from the action URL")


if __name__ == "__main__":
    test_gateway_base()
    test_retract_pending_send()
    test_retract_after_approval_is_too_late()
    test_retract_after_denial_reports_nothing_sent()
    test_unknown_and_malformed_ids()
    test_the_mail_gateways_outcomes()
    print("all pending-retract tests passed")
