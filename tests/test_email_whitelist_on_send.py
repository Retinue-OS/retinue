#!/usr/bin/env python3
"""Mail sent through email_client.py whitelists its recipients at once.

The triage gate's frequent tick only works whitelisted senders; its daily
refresh from the Sent folder is the backstop for other clients. A reply to mail
sent from here must not wait for that refresh, so `_smtp_send` whitelists every
recipient once the send has succeeded — and never fails the send if it cannot.

Standalone, no third-party deps:

    python3 tests/test_email_whitelist_on_send.py
"""
from __future__ import annotations

import importlib.util
import os
import smtplib
import sys
import tempfile
from email.message import EmailMessage
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))


def _load_email_client():
    spec = importlib.util.spec_from_file_location(
        "email_client", SCRIPTS_DIR / "email_client.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeSMTP:
    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        pass

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        pass

    # Addresses the scripted server refuses: send_message() returns them,
    # keyed as passed, instead of raising (the mail went to the others).
    refuse = set()

    def send_message(self, msg, from_addr=None, to_addrs=None):
        return {a: (550, b"no such user") for a in to_addrs if a in self.refuse}


class _Cfg:
    smtp_host = "smtp.invalid"
    smtp_port = 587
    user = "me@example.com"
    password = "x"


def _msg():
    msg = EmailMessage()
    msg["From"] = "me@example.com"
    msg["To"] = "New.Person@Gmail.com"
    msg["Subject"] = "Re: hello"
    msg.set_content("body")
    return msg


def test_recipients_whitelisted_after_send(ec, tp):
    ec._smtp_send(_Cfg(), _msg(), ["New.Person@Gmail.com", "cc@example.org"])
    addresses, wildcards = tp.load_email_whitelist()
    assert "new.person@gmail.com" in addresses, addresses
    assert "cc@example.org" in addresses, addresses
    # Never a domain: one freemail recipient does not trust all of gmail.com.
    assert not wildcards, wildcards


def test_refused_recipient_not_whitelisted(ec, tp):
    _FakeSMTP.refuse = {"gone@example.org"}
    try:
        ec._smtp_send(_Cfg(), _msg(), ["kept@example.org", "gone@example.org"])
    finally:
        _FakeSMTP.refuse = set()
    addresses, _ = tp.load_email_whitelist()
    assert "kept@example.org" in addresses, addresses
    assert "gone@example.org" not in addresses, addresses


def test_policy_failure_does_not_fail_send(ec, tp):
    orig = tp._mutate_email

    def boom(**_kw):
        raise OSError("read-only")
    tp._mutate_email = boom
    try:
        ec._smtp_send(_Cfg(), _msg(), ["other@example.net"])  # must not raise
    finally:
        tp._mutate_email = orig


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["TRIAGE_EMAIL_WHITELIST_PATH"] = str(Path(tmp) / "wl.nt")
        import triage_policy as tp
        ec = _load_email_client()
        real_smtp = smtplib.SMTP
        smtplib.SMTP = _FakeSMTP
        try:
            test_recipients_whitelisted_after_send(ec, tp)
            test_refused_recipient_not_whitelisted(ec, tp)
            test_policy_failure_does_not_fail_send(ec, tp)
        finally:
            smtplib.SMTP = real_smtp
            os.environ.pop("TRIAGE_EMAIL_WHITELIST_PATH", None)
    print("all email whitelist-on-send tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
