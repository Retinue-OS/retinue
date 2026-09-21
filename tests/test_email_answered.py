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
            "in_reply_to": "",
            "references": "",
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


def test_an_untracked_candidate_that_threads_elsewhere_does_not_count(ec):
    # Same correspondent, same subject, two distinct mails: a reply to the
    # first, sent after the second arrived, passes the recipient, subject and
    # time checks for the second. It cites the first, though -- and a reply
    # that cites a message and was not found by the threaded search cites a
    # different one, so it must not settle this anchor. A reply with no
    # threading headers at all is what the fallback exists for, and counts.
    anchor = {
        "uid": "b",
        "from": "Sender <sender@example.com>",
        "subject": "Project status",
        "date": "2026-09-16T10:00:00+00:00",
    }
    rc, payload = _run_case(
        ec,
        anchor=anchor,
        untracked=[
            {"to": "sender@example.com", "subject": "Re: Project status",
             "date": "2026-09-16T12:00:00+00:00",
             "in_reply_to": "<a@example.com>"},
        ],
    )
    assert rc == 3 and payload["answered"] is False, payload
    rc, payload = _run_case(
        ec,
        anchor=anchor,
        untracked=[
            {"to": "sender@example.com", "subject": "Re: Project status",
             "date": "2026-09-16T12:00:00+00:00",
             "references": "<a@example.com> <x@example.com>"},
        ],
    )
    assert rc == 3 and payload["answered"] is False, payload
    rc, payload = _run_case(
        ec,
        anchor=anchor,
        untracked=[
            {"to": "sender@example.com", "subject": "Re: Project status",
             "date": "2026-09-16T12:00:00+00:00"},
        ],
    )
    assert rc == 0 and payload["answered"] is True, payload
    print("PASS an untracked candidate threaded to another message does not count")


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


def test_base_subject_strips_the_same_prefixes_the_gate_nominates_on(ec):
    # The gate nominates on this prefix set; a reply the gate nominates but
    # this check cannot pair (`Antw: Budget` vs `Budget`) would be rejected
    # here every time, and answered mail would stay in the INBOX.
    for prefix in ("Re:", "AW:", "Fwd:", "WG:", "TR:", "Antw:", "SV:", "VS:",
                   "RE[2]:", "Re: AW:"):
        assert ec._base_subject(f"{prefix} Budget") == "budget", prefix
    print("PASS base subject strips every prefix the gate nominates on")


def test_untracked_search_covers_cc_and_bcc(ec):
    # A reply-all that reaches the sender only via Cc is no less an answer;
    # a TO-only search would never return it, so the exact check never sees
    # it, so the mail is proposed again on every sweep.
    class _M:
        def __init__(self):
            self.criteria = None

        def uid(self, verb, charset, *criteria):
            self.criteria = criteria
            return "OK", [b"7 8"]

    ec = _load_email_client()  # earlier cases stub _search_sent_to on the module
    m = _M()
    assert ec._search_sent_to(m, "sender@example.com", "2026-09-16T10:00:00+00:00") == [b"7", b"8"]
    crit = list(m.criteria)
    for header in ("TO", "CC", "BCC"):
        assert header in crit and crit[crit.index(header) + 1] == '"sender@example.com"', crit
    assert crit.count("OR") == 2 and "SINCE" in crit, crit
    print("PASS untracked search covers To, Cc and Bcc")


def main():
    ec = _load_email_client()
    test_threaded_replies_still_need_same_correspondent_subject_and_later_time(ec)
    test_untracked_replies_apply_the_exact_timestamp_check(ec)
    test_without_the_anchor_mail_the_answer_is_conservatively_unanswered(ec)
    test_an_untracked_candidate_that_threads_elsewhere_does_not_count(ec)
    test_base_subject_strips_the_same_prefixes_the_gate_nominates_on(ec)
    test_untracked_search_covers_cc_and_bcc(ec)
    print("all email answered tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
