#!/usr/bin/env python3
"""email_client.py and the mail gateway (docs/mail-chats.md, "Writing").

Two things tie them together, and both are pinned here.

**The outbound note.** A mail sent through email_client.py reaches the mail
gateway's mirror as any client's does — a copy in Sent, recorded as "some
mail client". So after a successful send, direct or approved, email_client
tells the gateway whose mail it was: ``POST <MAIL_GATEWAY_BASE_URL>/
outbound-note {"message_id", "author": "agent", "account": <From address>}``.
Best-effort: only where a gateway is configured, never failing the send that
produced it, and a 404 (another account's gateway) is no error.

**The send helpers as a library.** The gateway sends through ``_smtp_send``
and ``_build_message``: two hooks mark the moments a send journal needs, the
refused addresses are returned, and a caller may mint the Message-ID. With
none of that asked for, the helpers do exactly what they did — and a send
writes no policy: the whitelist-on-send went with the e-mail whitelist.

    python3 tests/test_email_outbound_note.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import smtplib
import sys
import tempfile
import threading
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

_ENV = ("MAIL_GATEWAY_BASE_URL", "MAIL_GATEWAY_TOKEN", "EMAIL_SEND_POLICY")


def _load_email_client():
    spec = importlib.util.spec_from_file_location("email_client", SCRIPTS_DIR / "email_client.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Cfg:
    smtp_host = "smtp.invalid"
    smtp_port = 587
    user = "Me@Example.com"
    password = "x"
    from_name = "Me"
    save_sent = True
    sent_folder = "Sent"
    drafts_folder = "Drafts"


def _msg(message_id="<m1@example.com>"):
    msg = EmailMessage()
    msg["From"] = "Me <Me@Example.com>"
    msg["To"] = "ann@example.org"
    msg["Subject"] = "Hello"
    if message_id:
        msg["Message-Id"] = message_id
    msg.set_content("body")
    return msg


class Gateway:
    """A mail gateway's /outbound-note, recording what it is told."""

    def __init__(self, status=200):
        self.notes = []          # (path, body, authorization)
        self.status = status
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
                gateway.notes.append((self.path, json.loads(raw),
                                      self.headers.get("Authorization")))
                data = b'{"ok": true}' if gateway.status == 200 else b'{"error": "no"}'
                self.send_response(gateway.status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"


def _env(**values):
    for name in _ENV:
        os.environ.pop(name, None)
    os.environ.update(values)


def test_the_note_says_whose_mail_it_was(ec):
    gateway = Gateway()
    try:
        # A trailing slash on the base is not doubled; the token is the gateway's.
        _env(MAIL_GATEWAY_BASE_URL=gateway.base + "/", MAIL_GATEWAY_TOKEN="mail-secret")
        ec._note_outbound(_Cfg(), _msg())
        assert gateway.notes == [("/outbound-note", {
            "message_id": "<m1@example.com>", "author": "agent",
            "account": "Me@Example.com"}, "Bearer mail-secret")], gateway.notes
        # No token configured: none is sent (the gateway will refuse; that is
        # its answer to give).
        _env(MAIL_GATEWAY_BASE_URL=gateway.base)
        ec._note_outbound(_Cfg(), _msg("<m2@example.com>"))
        assert gateway.notes[-1][2] is None
        # A mail with no Message-ID has nothing to be noted by.
        ec._note_outbound(_Cfg(), _msg(None))
        assert len(gateway.notes) == 2
        # No gateway configured: nothing is sent anywhere.
        _env()
        ec._note_outbound(_Cfg(), _msg())
        assert len(gateway.notes) == 2
    finally:
        gateway.server.shutdown()
        _env()
    print("PASS test_the_note_says_whose_mail_it_was")


def test_the_note_never_fails_a_send(ec):
    import contextlib
    import io
    # Another account's gateway answers 404: no error, and not a word.
    for status, noisy in ((404, False), (401, True), (503, True)):
        gateway = Gateway(status)
        try:
            _env(MAIL_GATEWAY_BASE_URL=gateway.base, MAIL_GATEWAY_TOKEN="t")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                ec._note_outbound(_Cfg(), _msg())
            assert len(gateway.notes) == 1
            assert bool(err.getvalue()) is noisy, (status, err.getvalue())
            assert not noisy or str(status) in err.getvalue()
        finally:
            gateway.server.shutdown()
    # Nobody listening, a base that is no URL: a warning, never an exception.
    for base in ("http://127.0.0.1:9", "not a url", "ftp://example.org"):
        _env(MAIL_GATEWAY_BASE_URL=base)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ec._note_outbound(_Cfg(), _msg())
        assert "warning" in err.getvalue(), (base, err.getvalue())
    _env()
    print("PASS test_the_note_never_fails_a_send")


def test_a_direct_send_is_noted_and_a_queued_one_is_not(ec):
    gateway = Gateway()
    calls = []
    real_send, real_append, real_register = ec._smtp_send, ec._append, ec.register_pending_send
    ec._smtp_send = lambda cfg, msg, recipients, **kw: calls.append("smtp")
    ec._append = lambda cfg, folder, msg, seen=True: calls.append("append") or "7"
    ec.register_pending_send = lambda cfg, msg, category: calls.append("draft") or "12"
    try:
        _env(MAIL_GATEWAY_BASE_URL=gateway.base, MAIL_GATEWAY_TOKEN="t",
             EMAIL_SEND_POLICY='[{"address": "me@example.com", "category": "allow"}]')
        msg = ec._build_message(_Cfg(), ["ann@example.org"], "Hello", "body")
        result = ec._dispatch_message(_Cfg(), msg, ["ann@example.org"])
        assert result["sent"] is True and calls == ["smtp", "append"]
        assert [n[1] for n in gateway.notes] == [{
            "message_id": msg["Message-Id"], "author": "agent", "account": "Me@Example.com"}]
        # Queued for approval: nothing was sent, so nothing is noted. (The
        # approval notes it, when it sends — approve_pending_send.)
        _env(MAIL_GATEWAY_BASE_URL=gateway.base, MAIL_GATEWAY_TOKEN="t",
             EMAIL_SEND_POLICY='[{"address": "me@example.com", "category": "verify"}]')
        result = ec._dispatch_message(_Cfg(), _msg("<q@example.com>"), ["ann@example.org"])
        assert result["pending"] is True and calls[-1] == "draft" and len(gateway.notes) == 1
        # A send that fails is not noted either.
        def refuse(*a, **kw):
            ec.die("SMTP send failed: nope")
        ec._smtp_send = refuse
        _env(MAIL_GATEWAY_BASE_URL=gateway.base,
             EMAIL_SEND_POLICY='[{"address": "*", "category": "allow"}]')
        try:
            ec._dispatch_message(_Cfg(), _msg("<f@example.com>"), ["ann@example.org"])
            raise AssertionError("no error")
        except ec.EmailError:
            pass
        assert len(gateway.notes) == 1
        import inspect
        approve = inspect.getsource(ec.approve_pending_send)
        assert approve.index("_smtp_send(") < approve.index("_note_outbound(") \
            < approve.index("_append("), "an approved send is noted once it is out"
    finally:
        ec._smtp_send, ec._append, ec.register_pending_send = real_send, real_append, real_register
        gateway.server.shutdown()
        _env()
    print("PASS test_a_direct_send_is_noted_and_a_queued_one_is_not")


class _FakeSMTP:
    """An SMTP session as smtplib runs one: the envelope, then the message
    through ``data`` — which is where the hooks are."""
    log: list = []
    refuse: set = set()
    answer = (250, b"queued")

    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        type(self).log.append("quit")
        return False

    def ehlo(self):
        pass

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        type(self).log.append("login")

    def data(self, payload):
        type(self).log.append("data")
        return type(self).answer

    def send_message(self, msg, from_addr=None, to_addrs=None):
        refused = {a: (550, b"no such user") for a in to_addrs if a in self.refuse}
        type(self).log.append("envelope")
        code, text = self.data(msg.as_bytes())
        if code != 250:
            raise smtplib.SMTPDataError(code, text)
        return refused


def test_the_send_helpers_as_a_library(ec, tp):
    real = smtplib.SMTP
    smtplib.SMTP = _FakeSMTP
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "email-policy.nt"
            os.environ["TRIAGE_EMAIL_WHITELIST_PATH"] = str(path)
            try:
                # With nothing asked for: the mail goes out, and the refused
                # addresses come back.
                _FakeSMTP.log, _FakeSMTP.refuse = [], {"gone@example.org"}
                refused = ec._smtp_send(_Cfg(), _msg(), ["ann@example.org", "gone@example.org"])
                assert list(refused) == ["gone@example.org"]
                assert _FakeSMTP.log == ["login", "envelope", "data", "quit"]
                # A send is no statement about its recipients: it writes no
                # policy, in the CLI's process as in the gateway's. (Sending
                # to an address used to whitelist it for e-mail triage.)
                calls = []
                real_mutate = tp._mutate_email
                tp._mutate_email = lambda **kw: calls.append(kw)
                try:
                    _FakeSMTP.refuse = set()
                    assert ec._smtp_send(_Cfg(), _msg(), ["new@example.org"]) == {}
                finally:
                    tp._mutate_email = real_mutate
                assert calls == [] and not path.exists(), "a send wrote policy"
                assert not hasattr(ec, "_whitelist_recipients")
                assert not hasattr(ec, "WHITELIST_ON_SEND")

                # The hooks: just before the message is handed over, and as
                # soon as the server said it took it — before the session ends.
                _FakeSMTP.log = []
                log = _FakeSMTP.log
                ec._smtp_send(_Cfg(), _msg(), ["ann@example.org"],
                              on_data=lambda: log.append("on_data"),
                              on_taken=lambda: log.append("on_taken"))
                assert log == ["login", "envelope", "on_data", "data", "on_taken", "quit"], log
                # Refused by the server: handed over, never taken.
                _FakeSMTP.log, _FakeSMTP.answer = [], (554, b"rejected")
                log = _FakeSMTP.log
                try:
                    ec._smtp_send(_Cfg(), _msg(), ["ann@example.org"],
                                  on_data=lambda: log.append("on_data"),
                                  on_taken=lambda: log.append("on_taken"))
                    raise AssertionError("no error")
                except ec.EmailError as exc:
                    # The gateway tells a refusal from a lost answer by this.
                    assert isinstance(exc.__context__, smtplib.SMTPDataError)
                assert "on_data" in log and "on_taken" not in log
                _FakeSMTP.answer = (250, b"queued")
                # What on_data raises stops the send before the message goes.
                _FakeSMTP.log = []

                def stop():
                    raise OSError("the journal cannot be written")
                try:
                    ec._smtp_send(_Cfg(), _msg(), ["ann@example.org"], on_data=stop)
                    raise AssertionError("no error")
                except OSError:
                    pass
                assert "data" not in _FakeSMTP.log
            finally:
                os.environ.pop("TRIAGE_EMAIL_WHITELIST_PATH", None)
    finally:
        smtplib.SMTP = real
    # A caller may mint the Message-ID; by default one is made.
    minted = ec._build_message(_Cfg(), ["ann@example.org"], "S", "b",
                               message_id="<minted@example.com>")
    assert minted["Message-Id"] == "<minted@example.com>"
    made = ec._build_message(_Cfg(), ["ann@example.org"], "S", "b")
    assert made["Message-Id"].startswith("<") and made["Message-Id"] != minted["Message-Id"]
    print("PASS test_the_send_helpers_as_a_library")


def main() -> int:
    ec = _load_email_client()
    import triage_policy as tp
    test_the_note_says_whose_mail_it_was(ec)
    test_the_note_never_fails_a_send(ec)
    test_a_direct_send_is_noted_and_a_queued_one_is_not(ec)
    test_the_send_helpers_as_a_library(ec, tp)
    print("all email outbound-note tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
