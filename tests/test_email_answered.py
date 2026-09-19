#!/usr/bin/env python3
"""Focused checks for `email_client answered`.

The triage gate uses `email_client answered` as its final authority for whether
an INBOX mail has already been replied to. Threading alone is not enough: the
Sent copy must still match the anchor mail's correspondent, base subject and
exactly-later timestamp.

    python3 tests/test_email_answered.py
"""
import importlib.util
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_email_client():
    spec = importlib.util.spec_from_file_location(
        "email_client_answered_under_test", SCRIPTS_DIR / "email_client.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeMailbox:
    def __init__(self):
        self.selected = []

    def logout(self):
        return None


def _run_case(ec, *, anchor, threaded=(), untracked=()):
    mailbox = _FakeMailbox()
    ec.imap_connect = lambda cfg: mailbox
    ec.imap_select = lambda M, folder, readonly=True: M.selected.append(folder)
    ec._search_by_message_id = lambda M, mid: [b"anchor"] if anchor else []
    ec._search_replies_to = lambda M, mid: [f"thr{i}".encode() for i, _ in enumerate(threaded, 1)]
    ec._search_sent_to = lambda M, addr, since: [f"unt{i}".encode() for i, _ in enumerate(untracked, 1)]
    reply_lookup = {
        f"thr{i}": msg for i, msg in enumerate(threaded, 1)
    } | {
        f"unt{i}": msg for i, msg in enumerate(untracked, 1)
    }

    def fake_summary(M, uid):
        uid = uid.decode() if isinstance(uid, bytes) else uid
        if uid == "anchor":
            return dict(anchor)
        return None

    def fake_reply_summary(M, uid):
        uid = uid.decode() if isinstance(uid, bytes) else uid
        msg = reply_lookup.get(uid)
        if msg is None:
            return None
        return {
            "uid": uid,
            "to": "",
            "cc": "",
            "bcc": "",
            "subject": "",
            "date": "",
            **msg,
        }

    ec._summary = fake_summary
    ec._reply_summary = fake_reply_summary
    args = SimpleNamespace(message_id="<m@example.com>", folder="Sent", in_folder="INBOX")
    out = io.StringIO()
    with redirect_stdout(out):
        rc = ec.cmd_answered(SimpleNamespace(sent_folder="Sent"), args)
    return rc, json.loads(out.getvalue())


def test_threaded_replies_still_need_same_correspondent_subject_and_later_time(ec):
    anchor = {
        "uid": "a1",
        "from": "Sender <sender@example.com>",
        "subject": "Project status",
        "date": "2026-09-16T10:00:00+00:00",
    }
    rc, payload = _run_case(
        ec,
        anchor=anchor,
        threaded=[
            {"to": "elsewhere@example.com", "subject": "Re: Project status",
             "date": "2026-09-16T11:00:00+00:00"},
            {"to": "sender@example.com", "subject": "Re: Something else",
             "date": "2026-09-16T11:00:00+00:00"},
            {"to": "sender@example.com", "subject": "Re: Project status",
             "date": "2026-09-16T09:00:00+00:00"},
        ],
    )
    assert rc == 3, rc
    assert payload["answered"] is False, payload
    assert payload["reply_count"] == 0, payload
    print("PASS threaded replies must still match recipient, subject and timestamp")


def test_untracked_replies_apply_the_exact_timestamp_check(ec):
    anchor = {
        "uid": "a1",
        "from": "Sender <sender@example.com>",
        "subject": "Project status",
        "date": "2026-09-16T10:00:00+00:00",
    }
    rc, payload = _run_case(
        ec,
        anchor=anchor,
        untracked=[
            {"to": "sender@example.com", "subject": "Re: Project status",
             "date": "2026-09-16T09:30:00+00:00"},
            {"to": "sender@example.com", "subject": "Re: Project status",
             "date": "2026-09-16T10:30:00+00:00"},
        ],
    )
    assert rc == 0, rc
    assert payload["answered"] is True, payload
    assert payload["reply_count"] == 1, payload
    assert payload["basis"] == ["untracked (1)"], payload
    print("PASS untracked replies must postdate the anchor exactly")


def test_without_the_anchor_mail_the_answer_is_conservatively_unanswered(ec):
    rc, payload = _run_case(
        ec,
        anchor=None,
        threaded=[{"to": "sender@example.com", "subject": "Re: Whatever",
                   "date": "2026-09-16T11:00:00+00:00"}],
    )
    assert rc == 3, rc
    assert payload["answered"] is False, payload
    print("PASS missing anchor yields a conservative unanswered result")


def main():
    ec = _load_email_client()
    test_threaded_replies_still_need_same_correspondent_subject_and_later_time(ec)
    test_untracked_replies_apply_the_exact_timestamp_check(ec)
    test_without_the_anchor_mail_the_answer_is_conservatively_unanswered(ec)
    print("all email answered tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
