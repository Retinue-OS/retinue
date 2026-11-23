#!/usr/bin/env python3
"""The mail gateway's mirror (scripts/mail-gateway.py), against a fake mailbox.

Everything that speaks IMAP is the gateway's mailbox adapter, so the mirror is
exercised here with a mailbox made of dicts: threads across INBOX and Sent,
out-of-order arrival, merges, one record for two folder copies, tombstones,
the archive followed, a UIDVALIDITY change, the backfill (order, resume, the
one read of ``\\Seen``), the news exclusion and its join rule, the index
rebuilt from the ledger, the rail's queue, health and the HTTP API.

The MIME shapes and size caps are in tests/test_mail_gateway_mime.py, the
adapter itself (against a scripted IMAP server) in
tests/test_mail_gateway_imap.py; both load this file for its helpers.

    python3 tests/test_mail_gateway.py
"""
import email.utils
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

ACCOUNT = "me@example.org"
ME = "Me Myself <me@example.org>"
DAY = 86400.0
# A fixed instant; mails are dated in days after it and the mirror's clock
# stands at day 40, so the default ninety-day backfill sees them all.
T0 = 1_760_000_000.0
NOW = T0 + 40 * DAY
RAIL_URL = "http://rail.invalid/internal/chats/inbound"

_ENV_PREFIXES = ("MAIL_", "EMAIL_", "IMAP_", "SMTP_", "TRIAGE_")
_ENV_NAMES = ("SENT_FOLDER", "DRAFTS_FOLDER", "INBOUND_STORE_DIR")


def load(tmp, *, token="", days=90, configured=True, rail=True, **env):
    """Import a fresh gateway module whose state lives under ``tmp``."""
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    root = Path(tmp)
    for name in list(os.environ):
        if name.startswith(_ENV_PREFIXES) or name in _ENV_NAMES:
            del os.environ[name]
    os.environ.update({
        "MAIL_DATA_DIR": str(root / "data"),
        "INBOUND_STORE_DIR": str(root / "inbound"),
        "MAIL_GATEWAY_TOKEN": token,
        "MAIL_BACKFILL_DAYS": str(days),
        "MAIL_OWN_ADDRESSES": "alias@example.org",
    })
    if configured:
        # Mixed case on purpose: the account identity is the address lowercased.
        os.environ.update({"EMAIL_USER": "Me@Example.org", "EMAIL_PASS": "app-password",
                           "IMAP_HOST": "imap.example.org", "SMTP_HOST": "smtp.example.org"})
    os.environ.update({key: str(value) for key, value in env.items()})
    spec = importlib.util.spec_from_file_location("mail_gateway_under_test",
                                                  SCRIPTS_DIR / "mail-gateway.py")
    gw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gw)
    # chat_ingest is imported once per process; its endpoint is per test.
    gw._chats.CHATS_INGEST_URL = RAIL_URL if rail else ""
    gw._chats.CHATS_INGEST_TOKEN = "rail-token"
    return gw


def rail(gw, outcome="ok"):
    """Capture what the gateway offers the rail; returns the list it fills."""
    events = []

    def send(kind, payload):
        events.append((kind, payload))
        return outcome(kind, payload) if callable(outcome) else outcome
    gw._rail_send = send
    return events


def msg(mid="m1@x.org", *, sender="Ann Example <ann@x.org>", to=ACCOUNT, cc=None,
        subject="Quarterly numbers", body="The numbers are in.\n", refs=None, irt=None,
        day=1, list_id=None, html=None, files=(), headers=()) -> bytes:
    """One mail as bytes. ``mid=None`` leaves the Message-ID header out."""
    mail = EmailMessage()
    if mid:
        mail["Message-ID"] = f"<{mid}>"
    mail["From"] = sender
    mail["To"] = to
    if cc:
        mail["Cc"] = cc
    mail["Subject"] = subject
    mail["Date"] = email.utils.formatdate(T0 + day * DAY)
    if refs:
        mail["References"] = " ".join(f"<{r}>" for r in refs)
    if irt:
        mail["In-Reply-To"] = f"<{irt}>"
    if list_id:
        mail["List-Id"] = list_id
    for name, value in headers:
        mail[name] = value
    mail.set_content(body)
    if html:
        mail.add_alternative(html, subtype="html")
    for name, content_type, data in files:
        maintype, subtype = content_type.split("/", 1)
        mail.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return mail.as_bytes()


class FakeMailbox:
    """The mailbox adapter's surface over dicts — what the mirror is given in
    place of ``gw.Mailbox``. ``put`` / ``remove`` / ``move_mail`` / ``renumber``
    are what a mail client, or the server, does behind the mirror's back."""

    def __init__(self, gw, folders=("INBOX", "Sent", "Archive")):
        self.gw = gw
        self.boxes = {name: {"validity": 100 + i, "next": 1, "mails": {}}
                      for i, name in enumerate(folders)}
        self.roles = {"inbox": "INBOX", "sent": "Sent", "archive": "Archive", "trash": "Trash"}
        self.fetched = []     # (folder, uid, limit) of every fetch
        self.found = []       # (folder, message id) of every find
        self.listed = []      # (folder, since, first_uid, dates) of every listing
        self.fail = None      # raised by the next adapter call, once

    # -- what happens to the mailbox ------------------------------------------

    def put(self, folder, raw, *, flags=(), date=None) -> int:
        box = self.boxes[folder]
        uid, box["next"] = box["next"], box["next"] + 1
        if date is None:
            stamp = email.message_from_bytes(raw).get("Date")
            date = email.utils.parsedate_to_datetime(stamp).timestamp() if stamp else NOW
        box["mails"][uid] = {"raw": raw, "flags": frozenset(flags), "date": date}
        return uid

    def remove(self, folder, uid) -> dict:
        return self.boxes[folder]["mails"].pop(uid)

    def move_mail(self, folder, uid, to) -> int:
        mail = self.remove(folder, uid)
        return self.put(to, mail["raw"], flags=mail["flags"], date=mail["date"])

    def renumber(self, folder, offset=50) -> None:
        """A UIDVALIDITY change: the same mails under new UIDs."""
        box = self.boxes[folder]
        box["validity"] += 1
        box["mails"] = {uid + offset: mail for uid, mail in box["mails"].items()}
        box["next"] += offset

    def uid_of(self, folder, message_id) -> int | None:
        for uid, mail in sorted(self.boxes[folder]["mails"].items(), reverse=True):
            header = email.message_from_bytes(mail["raw"]).get("Message-ID") or ""
            if header.strip().strip("<>") == message_id:
                return uid
        return None

    # -- the adapter's surface ------------------------------------------------

    def _check(self):
        if self.fail is not None:
            failure, self.fail = self.fail, None
            raise failure

    def ensure(self):
        self._check()

    def close(self):
        pass

    def folders(self):
        self._check()
        return {role: name if name in self.boxes else None
                for role, name in self.roles.items()}

    def list_uids(self, folder, *, since=None, first_uid=None, dates=False):
        self._check()
        self.listed.append((folder, since, first_uid, dates))
        box = self.boxes.get(folder)
        if box is None:
            return None
        uids = {}
        for uid, mail in sorted(box["mails"].items()):
            # Like the server's SINCE as the adapter asks it: whole days, a day early.
            if since is not None and mail["date"] < since - DAY:
                continue
            if first_uid and uid < first_uid:
                continue
            uids[uid] = mail["date"] if dates else None
        return self.gw.Listing(box["validity"], box["next"], uids)

    def fetch(self, folder, uid, limit=None):
        self._check()
        self.fetched.append((folder, uid, limit))
        mail = self.boxes.get(folder, {"mails": {}})["mails"].get(uid)
        if mail is None:
            return None
        raw = mail["raw"]
        return self.gw.Fetched(raw[:limit] if limit else raw, len(raw),
                               mail["flags"], mail["date"])

    def find(self, folder, message_id):
        self._check()
        self.found.append((folder, message_id))
        if folder not in self.boxes:
            return None
        return self.uid_of(folder, message_id)


def records(gw) -> list:
    """Every ledger record's fields, in timeline order."""
    return [fields for _path, fields in gw._ibstore.iter_records(gw.INBOUND_STORE_DIR)]


def by_id(gw) -> dict:
    return {fields["message_id"]: fields for fields in records(gw)}


def blob(gw, reference) -> bytes:
    return gw._ibstore.load_media(gw.INBOUND_STORE_DIR, gw._ibstore.media_id_of(reference))[0]


def started(gw, box=None):
    """A mirror that has seen its (still empty) folders once, so what is put
    into the mailbox afterwards is new mail, not backfill."""
    box = box or FakeMailbox(gw)
    mirror = gw.Mirror(box, clock=lambda: NOW)
    assert mirror.poll() is False
    return mirror, box


def inbound(events) -> list:
    return [payload for kind, payload in events if kind == "inbound"]


# --------------------------------------------------------------------------
# Threads
# --------------------------------------------------------------------------

def test_a_thread_across_inbox_and_sent_is_one_chat():
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp)
        events = rail(gw)
        mirror, box = started(gw)
        assert gw.ACCOUNT == ACCOUNT
        box.put("INBOX", msg("a@x.org", cc="Bob <bob@y.org>", day=1))
        box.put("Sent", msg("b@example.org", sender=ME, to="ann@x.org", cc="bob@y.org",
                            subject="Re: Quarterly numbers", body="Thanks, looks right.\n",
                            refs=["a@x.org"], irt="a@x.org", day=2))
        box.put("INBOX", msg("c@x.org", subject="RE: Quarterly numbers",
                             body="Good.\n\n> Thanks, looks right.\n",
                             refs=["a@x.org", "b@example.org"], irt="b@example.org", day=3))
        mirror.poll()
        recs = by_id(gw)
        assert set(recs) == {"a@x.org", "b@example.org", "c@x.org"}, recs.keys()
        assert {r["chat"] for r in recs.values()} == {"a@x.org"}
        assert {r["account"] for r in recs.values()} == {ACCOUNT}
        assert {r["channel"] for r in recs.values()} == {"email"}
        a, b, c = recs["a@x.org"], recs["b@example.org"], recs["c@x.org"]
        assert a["type"] == gw._ibstore.T_INBOUND and b["type"] == gw._ibstore.T_OUTBOUND
        # Nothing is owed for a mirrored mail in this phase.
        assert a["delivered"] is True and c["delivered"] is True
        assert a["sender"] == "ann@x.org" and a["sender_name"] == "Ann Example"
        assert a["subject_line"] == "Quarterly numbers"
        assert a["recipients"] == [ACCOUNT] and a["cc"] == ["bob@y.org"]
        assert a["received_at"] == gw._ibstore._iso(T0 + DAY)
        # The user's own mail, whichever client sent it: the own-device echo,
        # with its From on record.
        assert b["author"] == "device" and b["sender"] == ACCOUNT
        assert b["sender_name"] == "Me Myself" and b["sent_at"] == gw._ibstore._iso(T0 + 2 * DAY)
        assert b["in_reply_to"] == "a@x.org" and b["references"] == "a@x.org"
        assert c["references"] == "a@x.org b@example.org" and c["in_reply_to"] == "b@example.org"
        # kb:text is the trimmed text; the parts are the original, as blobs.
        assert c["text"] == "Good." and c["trim"] == "deterministic", c["text"]
        assert blob(gw, c["body_part"]).decode() == "Good.\n\n> Thanks, looks right.\n"
        assert b"Subject: RE: Quarterly numbers" in blob(gw, c["header_part"])
        assert c["html_part"] is None and c["attachments"] == []
        assert c["attachment_meta"][c["body_part"]]["content_type"] == "text/plain; charset=utf-8"

        got = inbound(events)
        assert [e["message_id"] for e in got] == ["b@example.org", "a@x.org", "c@x.org"]
        for event in got:
            assert event["channel"] == "email" and event["account"] == ACCOUNT
            assert event["chat"] == "a@x.org" and event["chat_name"] == "Quarterly numbers"
            assert event["gate"] == {"forward": False, "vip": False, "instructed": False,
                                     "reason": "mirror"}
            # No handover is offered, and new mail is not a backfill.
            assert "handover" not in event and "backfill" not in event and "seen" not in event
        sent, first, last = got
        assert sent["direction"] == "out" and sent["author"] == "device"
        assert sent["sender"] == ACCOUNT and sent["sender_name"] == "Me Myself"
        assert first["direction"] == "in" and first["author"] is None
        assert first["ts"] == T0 + DAY and first["subject"] == "Quarterly numbers"
        assert last["text"] == "Good." and last["subject"] == "RE: Quarterly numbers"
        assert last["group"] is True and last["peer"] == {"address": "ann@x.org",
                                                          "name": "Ann Example"}
        assert last["participants"] == [{"address": "ann@x.org", "name": "Ann Example"},
                                        {"address": "bob@y.org", "name": "Bob"}]

        thread = mirror.thread("a@x.org")
        assert thread["messages"] == 3 and thread["live"] == 3 and thread["group"] is True
        assert thread["name"] == "Quarterly numbers" and thread["last_ts"] == T0 + 3 * DAY
        # A second poll finds nothing new and reads nothing again.
        fetched = len(box.fetched)
        mirror.poll()
        assert len(records(gw)) == 3 and len(box.fetched) == fetched and len(got) == 3
    print("ok: a thread across INBOX and Sent is one chat, in both directions")


def test_out_of_order_arrival_needs_no_merge():
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp)
        events = rail(gw)
        mirror, box = started(gw)
        # The reply arrives first; it names its root.
        box.put("INBOX", msg("reply@x.org", subject="Re: Plan", refs=["root@x.org"],
                             irt="root@x.org", day=5))
        mirror.poll()
        assert by_id(gw)["reply@x.org"]["chat"] == "root@x.org"
        box.put("INBOX", msg("root@x.org", subject="Plan", day=4))
        mirror.poll()
        assert {r["chat"] for r in records(gw)} == {"root@x.org"}
        assert [kind for kind, _p in events] == ["inbound", "inbound"]
        # The chat is named by its earliest mail, whenever that arrived.
        assert events[0][1]["chat_name"] == "Plan" and mirror.thread("root@x.org")["name"] == "Plan"
        assert mirror.thread("root@x.org")["messages"] == 2
    print("ok: a reply that arrives before its parent starts the chat its root names")


def test_a_merge_rekeys_the_records_and_tells_the_rail():
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp)
        events = rail(gw)
        mirror, box = started(gw)
        box.put("INBOX", msg("a@x.org", subject="Offer", day=1))
        # A client that sent only In-Reply-To, to a mail never mirrored: its own chat.
        box.put("INBOX", msg("b@x.org", subject="Re: Offer", irt="lost@x.org", day=2))
        mirror.poll()
        assert {r["message_id"]: r["chat"] for r in records(gw)} == {
            "a@x.org": "a@x.org", "b@x.org": "lost@x.org"}
        # The rekey fails once (a full disk, say): no merge is announced.
        real_rekey = gw._ibstore.rekey_chat
        gw._ibstore.rekey_chat = lambda *a, **kw: {"messages": 0, "errors": 1}
        try:
            box.put("INBOX", msg("c@x.org", subject="Re: Offer",
                                 refs=["a@x.org", "lost@x.org"], irt="lost@x.org", day=3))
            mirror.poll()
            assert [kind for kind, _p in events] == ["inbound"] * 3
            assert by_id(gw)["b@x.org"]["chat"] == "lost@x.org"
            assert by_id(gw)["c@x.org"]["chat"] == "a@x.org"
        finally:
            gw._ibstore.rekey_chat = real_rekey
        # The next poll finishes it: records re-keyed first, then the rail told.
        mirror.poll()
        assert {r["chat"] for r in records(gw)} == {"a@x.org"}
        assert events[-1] == ("merge", {"channel": "email", "account": ACCOUNT,
                                        "from": ["lost@x.org"], "into": "a@x.org"})
        assert mirror.thread("lost@x.org") is None
        assert mirror.thread("a@x.org")["messages"] == 3
        mirror.poll()
        assert len(events) == 4

        # In the ordinary case the merge is announced before the mail that caused it.
        box.put("INBOX", msg("p@x.org", subject="Trip", day=4))
        box.put("INBOX", msg("q@x.org", subject="Re: Trip", irt="gone@x.org", day=5))
        mirror.poll()
        box.put("INBOX", msg("r@x.org", subject="Re: Trip", refs=["p@x.org", "gone@x.org"], day=6))
        mirror.poll()
        tail = events[-2:]
        assert tail[0] == ("merge", {"channel": "email", "account": ACCOUNT,
                                     "from": ["gone@x.org"], "into": "p@x.org"})
        assert tail[1][0] == "inbound" and tail[1][1]["chat"] == "p@x.org"
    print("ok: a merge re-keys the absorbed chat's records, then tells the rail")


def test_a_half_done_merge_is_finished_after_a_restart():
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp)
        events = rail(gw)
        mirror, box = started(gw)
        box.put("INBOX", msg("a@x.org", day=1))
        box.put("INBOX", msg("b@x.org", irt="lost@x.org", day=2))
        mirror.poll()
        real_rekey = gw._ibstore.rekey_chat
        gw._ibstore.rekey_chat = lambda *a, **kw: {"messages": 0, "errors": 1}
        try:
            box.put("INBOX", msg("c@x.org", refs=["a@x.org", "lost@x.org"], day=3))
            mirror.poll()
        finally:
            gw._ibstore.rekey_chat = real_rekey
        assert by_id(gw)["b@x.org"]["chat"] == "lost@x.org"
        events.clear()
        # The process ends here, the merge half done; the next one finishes it.
        mirror = gw.Mirror(box, clock=lambda: NOW)
        mirror.poll()
        assert {r["chat"] for r in records(gw)} == {"a@x.org"}
        assert events == [("merge", {"channel": "email", "account": ACCOUNT,
                                     "from": ["lost@x.org"], "into": "a@x.org"})]
    print("ok: a merge a crash interrupted is finished at the next start")


# --------------------------------------------------------------------------
# One record per mail, tombstones, the archive
# --------------------------------------------------------------------------

def test_two_folder_copies_are_one_record():
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp)
        events = rail(gw)
        mirror, box = started(gw)
        note = msg("self@example.org", sender=ME, to=ACCOUNT, subject="Note to self", day=1)
        sent_uid = box.put("Sent", note)
        inbox_uid = box.put("INBOX", note)
        mirror.poll()
        recs = records(gw)
        assert len(recs) == 1 and recs[0]["type"] == gw._ibstore.T_OUTBOUND
        assert len(inbound(events)) == 1 and events[0][1]["direction"] == "out"
        # While one copy remains, the mail remains.
        box.remove("INBOX", inbox_uid)
        mirror.poll()
        assert records(gw)[0]["removed_at"] is None and len(events) == 1
        box.remove("Sent", sent_uid)
        mirror.poll()
        assert records(gw)[0]["removed_at"] and len(records(gw)) == 1
        assert events[-1] == ("removed", {"channel": "email", "account": ACCOUNT,
                                          "chat": "self@example.org",
                                          "message_ids": ["self@example.org"], "live": 0})
    print("ok: a mail in INBOX and in Sent is one record, gone when both copies are")


def test_the_sent_copy_arriving_late_makes_it_the_users_own_mail():
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp)
        events = rail(gw)
        mirror, box = started(gw)
        post = msg("post@example.org", sender=ME, to="list@lists.x.org", cc=ACCOUNT, day=1,
                   files=[("a.txt", "text/plain", b"attached")])
        # The list's copy reaches INBOX before the client has saved its Sent copy.
        box.put("INBOX", post)
        mirror.poll()
        first = records(gw)
        assert len(first) == 1 and first[0]["type"] == gw._ibstore.T_INBOUND
        box.put("Sent", post)
        mirror.poll()
        recs = records(gw)
        assert len(recs) == 1 and recs[0]["type"] == gw._ibstore.T_OUTBOUND
        assert recs[0]["author"] == "device" and recs[0]["chat"] == "post@example.org"
        assert [e["direction"] for e in inbound(events)] == ["in", "out"]
        # The first record's blobs went with it: only the second record's are left.
        media = [p for p in gw._ibstore.media_dir(gw.INBOUND_STORE_DIR).iterdir()
                 if "." not in p.name]
        named = {gw._ibstore.media_id_of(ref) for ref in gw._ibstore._media_refs(recs[0])}
        assert {p.name for p in media} == named and len(named) == 3
        assert mirror.thread("post@example.org")["messages"] == 1
    print("ok: a mail first seen in INBOX becomes the user's own once its Sent copy shows")


def test_a_move_to_the_archive_leaves_the_record_alone():
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp)
        events = rail(gw)
        mirror, box = started(gw)
        uid = box.put("INBOX", msg("a@x.org", day=1))
        other = box.put("INBOX", msg("b@x.org", refs=["a@x.org"], day=2))
        mirror.poll()
        before = Path(gw._ibstore.messages_dir(gw.INBOUND_STORE_DIR)).glob("*.nt")
        before = {p.name: p.read_text() for p in before}
        archived = box.move_mail("INBOX", uid, "Archive")
        fetched = len(box.fetched)
        mirror.poll()
        after = {p.name: p.read_text() for p in
                 Path(gw._ibstore.messages_dir(gw.INBOUND_STORE_DIR)).glob("*.nt")}
        assert after == before, "an archived mail's record must not change"
        assert len(events) == 2 and len(box.fetched) == fetched   # found, not read again
        assert ("Archive", "a@x.org") in box.found
        # The archive is followed, never read: a mail that only ever was there is nobody's.
        box.put("Archive", msg("old@x.org", day=1))
        mirror.poll()
        assert set(by_id(gw)) == {"a@x.org", "b@x.org"}
        # Deleted from the archive, the mail is gone; its thread is still alive.
        box.remove("Archive", archived)
        mirror.poll()
        gone = by_id(gw)["a@x.org"]
        assert gone["removed_at"] and gone["text"] == "" and gone["body_part"] is None
        assert gone["sender"] == "ann@x.org" and gone["chat"] == "a@x.org"
        assert events[-1] == ("removed", {"channel": "email", "account": ACCOUNT,
                                          "chat": "a@x.org", "message_ids": ["a@x.org"],
                                          "live": 1})
        thread = mirror.thread("a@x.org")
        assert thread["messages"] == 2 and thread["live"] == 1
        box.remove("INBOX", other)
        mirror.poll()
        assert events[-1][1]["live"] == 0 and mirror.thread("a@x.org")["live"] == 0
        # Tombstones are not looked for again.
        found = len(box.found)
        mirror.poll()
        assert len(box.found) == found
    print("ok: a move to the archive is followed; a mail in no folder leaves a tombstone")


def test_a_mail_that_comes_back_replaces_its_tombstone():
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp)
        events = rail(gw)
        mirror, box = started(gw)
        uid = box.put("INBOX", msg("a@x.org", day=1))
        mirror.poll()
        mail = box.remove("INBOX", uid)       # to the Trash, which is not mirrored
        mirror.poll()
        assert records(gw)[0]["removed_at"]
        box.put("INBOX", mail["raw"], date=mail["date"])   # and back again
        mirror.poll()
        recs = records(gw)
        assert len(recs) == 1 and recs[0]["removed_at"] is None
        assert recs[0]["text"] == "The numbers are in." and recs[0]["body_part"]
        assert [kind for kind, _p in events] == ["inbound", "removed", "inbound"]
        assert mirror.thread("a@x.org")["live"] == 1
    print("ok: a mail moved back into a mirrored folder replaces its tombstone")


def test_a_clients_seen_changes_are_ignored():
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp)
        events = rail(gw)
        mirror, box = started(gw)
        uid = box.put("INBOX", msg("a@x.org", day=1), flags=["\\Seen"])
        mirror.poll()
        # New mail: the flag is not even reported.
        assert "seen" not in events[0][1] and "backfill" not in events[0][1]
        record = next(Path(gw._ibstore.messages_dir(gw.INBOUND_STORE_DIR)).glob("*.nt"))
        text = record.read_text()
        box.boxes["INBOX"]["mails"][uid]["flags"] = frozenset()
        mirror.poll()
        box.boxes["INBOX"]["mails"][uid]["flags"] = frozenset(["\\Seen", "\\Flagged"])
        mirror.poll()
        assert record.read_text() == text and len(events) == 1
    print("ok: a client's \\Seen changes change nothing in the mirror")


# --------------------------------------------------------------------------
# UIDVALIDITY
# --------------------------------------------------------------------------

def test_a_uidvalidity_change_finds_the_mails_again():
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp)
        events = rail(gw)
        mirror, box = started(gw)
        box.put("INBOX", msg("a@x.org", day=1))
        box.put("INBOX", msg("b@x.org", refs=["a@x.org"], day=2))
        # A mail with no Message-ID cannot be searched for; it is still found.
        box.put("INBOX", msg(None, subject="No id", day=3))
        box.put("Sent", msg("s@example.org", sender=ME, to="ann@x.org", day=2))
        mirror.poll()
        assert len(records(gw)) == 4 and len(events) == 4
        snapshot = {p.name: p.read_text() for p in
                    Path(gw._ibstore.messages_dir(gw.INBOUND_STORE_DIR)).glob("*.nt")}
        box.renumber("INBOX")
        box.fetched.clear()
        mirror.poll()
        mirror.poll()
        after = {p.name: p.read_text() for p in
                 Path(gw._ibstore.messages_dir(gw.INBOUND_STORE_DIR)).glob("*.nt")}
        assert after == snapshot, "no record is rewritten, added or tombstoned"
        assert len(events) == 4
        # Found by Message-ID; only the id-less mail's headers were read.
        assert ("INBOX", "a@x.org") in box.found and ("INBOX", "b@x.org") in box.found
        assert box.fetched and all(limit == gw.HEADER_PEEK_BYTES for _f, _u, limit in box.fetched)
        assert mirror.backfill_status()["done"] is True
        # The folder is followed under its new numbering.
        box.put("INBOX", msg("c@x.org", refs=["a@x.org"], day=4))
        mirror.poll()
        assert len(records(gw)) == 5 and events[-1][1]["message_id"] == "c@x.org"
        assert "backfill" not in events[-1][1]
        box.remove("INBOX", box.uid_of("INBOX", "a@x.org"))
        mirror.poll()
        assert by_id(gw)["a@x.org"]["removed_at"] and events[-1][0] == "removed"
        assert len(records(gw)) == 5
    print("ok: after a UIDVALIDITY change mails are found by Message-ID, not ingested again")


def test_a_mail_gone_during_a_uidvalidity_change_is_a_tombstone():
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp)
        events = rail(gw)
        mirror, box = started(gw)
        uid = box.put("INBOX", msg("a@x.org", day=1))
        box.put("INBOX", msg("b@x.org", day=2))
        mirror.poll()
        box.remove("INBOX", uid)
        box.renumber("INBOX")
        # A mail that arrived while the folder was being renumbered is still mirrored.
        box.put("INBOX", msg("c@x.org", day=3))
        mirror.poll()
        mirror.poll()
        recs = by_id(gw)
        assert recs["a@x.org"]["removed_at"] and not recs["b@x.org"]["removed_at"]
        assert set(recs) == {"a@x.org", "b@x.org", "c@x.org"}
        assert [kind for kind, _p in events].count("removed") == 1
    print("ok: what a renumbered folder no longer holds is gone; what it gained is mirrored")


# --------------------------------------------------------------------------
# Backfill
# --------------------------------------------------------------------------

def test_backfill_is_oldest_first_across_folders_and_resumes():
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp)
        events = rail(gw)
        box = FakeMailbox(gw)
        # UID order is not date order, and the two folders interleave.
        box.put("INBOX", msg("d5@x.org", refs=["d1@x.org", "d3@example.org"], day=5),
                flags=["\\Seen"])
        box.put("INBOX", msg("d1@x.org", day=1), flags=["\\Seen", "\\Answered"])
        box.put("Sent", msg("d3@example.org", sender=ME, to="ann@x.org", refs=["d1@x.org"],
                            day=3), flags=["\\Seen"])
        box.put("INBOX", msg("d7@x.org", refs=["d1@x.org"], day=7))
        # Older than the window: not the mirror's.
        box.put("INBOX", msg("ancient@x.org", day=-120))
        mirror = gw.Mirror(box, clock=lambda: NOW)
        assert mirror.backfill_status() == {"done": False, "pending": None, "days": 90}
        assert mirror.poll(backfill_limit=2) is True
        assert [r["message_id"] for r in records(gw)] == ["d1@x.org", "d3@example.org"]
        assert mirror.backfill_status() == {"done": False, "pending": 2, "days": 90}
        # Mail that arrives while the backfill runs is new mail.
        box.put("INBOX", msg("live@x.org", day=39))
        assert mirror.poll(backfill_limit=0) is True
        assert "live@x.org" in by_id(gw)

        # A restart continues where the backfill stood.
        mirror = gw.Mirror(box, clock=lambda: NOW)
        fetched = len(box.fetched)
        assert mirror.poll() is False
        assert mirror.backfill_status() == {"done": True, "pending": 0, "days": 90}
        assert [f[1] for f in box.fetched[fetched:]] == [1, 3]   # d5, then d7; nothing twice
        order = [e["message_id"] for e in inbound(events)]
        assert order == ["d1@x.org", "d3@example.org", "live@x.org", "d5@x.org", "d7@x.org"]
        assert sorted(by_id(gw)) == sorted(order) and len(records(gw)) == 5
        assert {r["chat"] for r in records(gw) if r["message_id"] != "live@x.org"} == {"d1@x.org"}
        # Old mail says so, and carries the mailbox's \Seen — once, here only.
        flags = {e["message_id"]: (e.get("backfill"), e.get("seen")) for e in inbound(events)}
        assert flags == {"d1@x.org": (True, True), "d3@example.org": (True, True),
                         "d5@x.org": (True, True), "d7@x.org": (True, False),
                         "live@x.org": (None, None)}
        mirror.poll()
        assert len(records(gw)) == 5 and len(events) == 5
    print("ok: backfill reads INBOX and Sent merged, oldest first, resumes, and seeds \\Seen")


def test_no_backfill_mirrors_only_new_mail():
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp, days=0)
        rail(gw)
        box = FakeMailbox(gw)
        box.put("INBOX", msg("old@x.org", day=30))
        mirror = gw.Mirror(box, clock=lambda: NOW)
        assert mirror.poll() is False
        assert records(gw) == [] and mirror.backfill_status()["done"] is True
        box.put("INBOX", msg("new@x.org", day=39))
        mirror.poll()
        assert list(by_id(gw)) == ["new@x.org"]
    print("ok: MAIL_BACKFILL_DAYS=0 mirrors only what arrives from then on")


# --------------------------------------------------------------------------
# What never becomes a chat
# --------------------------------------------------------------------------

def _write_policy(gw, **sets):
    policy = gw._triage.EmailPolicy(frozenset(), frozenset())._replace(
        **{key: frozenset(values) for key, values in sets.items()})
    gw.POLICY_PATH.parent.mkdir(parents=True, exist_ok=True)
    gw.POLICY_PATH.write_text(gw._triage.render_email_policy(policy), encoding="utf-8")


def test_news_starts_no_chat_but_joins_one():
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp)
        # Read raw off the ledger volume, where the retinue side writes it.
        assert gw.POLICY_PATH == Path(tmp) / "inbound" / "policy" / "email-policy.nt"
        _write_policy(gw, news={"news@shop.example", "dev.lists.x.org"},
                      news_wildcards={"*@letters.example"})
        events = rail(gw)
        mirror, box = started(gw)
        box.put("INBOX", msg("n1@shop.example", sender="Shop <news@shop.example>", day=1))
        box.put("INBOX", msg("n2@letters.example", sender="weekly@letters.example", day=1))
        box.put("INBOX", msg("l1@x.org", sender="Dee <dee@x.org>", subject="[dev] Release",
                             list_id="Dev list <dev.lists.x.org>", day=2))
        box.put("INBOX", msg("ok@x.org", day=2))
        mirror.poll()
        assert list(by_id(gw)) == ["ok@x.org"] and len(events) == 1
        assert mirror.thread("l1@x.org") is None
        # Left alone entirely: not read a second time either.
        fetched = len(box.fetched)
        mirror.poll()
        assert len(box.fetched) == fetched
        # The user writes in that list thread …
        box.put("Sent", msg("mine@example.org", sender=ME, to="dev@lists.x.org",
                            subject="Re: [dev] Release", refs=["l1@x.org"], irt="l1@x.org",
                            day=3))
        mirror.poll()
        assert by_id(gw)["mine@example.org"]["chat"] == "l1@x.org"
        # … so the list's answers to that thread are shown in its chat,
        box.put("INBOX", msg("l2@x.org", sender="Dee <dee@x.org>", subject="Re: [dev] Release",
                             list_id="<dev.lists.x.org>", refs=["l1@x.org", "mine@example.org"],
                             irt="mine@example.org", day=4))
        # while another thread of the same list still starts none.
        box.put("INBOX", msg("l3@x.org", sender="Dee <dee@x.org>", subject="[dev] Other",
                             list_id="<dev.lists.x.org>", day=4))
        mirror.poll()
        recs = by_id(gw)
        assert set(recs) == {"ok@x.org", "mine@example.org", "l2@x.org"}
        assert recs["l2@x.org"]["chat"] == "l1@x.org"
        assert recs["l2@x.org"]["list_id"] == "dev.lists.x.org"
        assert mirror.thread("l1@x.org")["messages"] == 2
        # A news mail leaving the mailbox is nobody's tombstone.
        box.remove("INBOX", box.uid_of("INBOX", "n1@shop.example"))
        mirror.poll()
        assert not any(kind == "removed" for kind, _p in events)
    print("ok: a news sender or list starts no chat; a news mail in a mirrored thread joins it")


def test_an_unreadable_policy_mirrors_everything():
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp)
        rail(gw)
        gw.POLICY_PATH.parent.mkdir(parents=True)
        gw.POLICY_PATH.write_bytes(b"\xff\xfe not text")
        mirror, box = started(gw)
        box.put("INBOX", msg("n1@shop.example", sender="news@shop.example", day=1))
        mirror.poll()
        assert list(by_id(gw)) == ["n1@shop.example"]
    print("ok: with no readable policy, mail is mirrored rather than dropped")


def test_the_policy_path_follows_the_deployment():
    with tempfile.TemporaryDirectory() as tmp:
        elsewhere = Path(tmp) / "elsewhere" / "email-policy.nt"
        gw = load(tmp, TRIAGE_EMAIL_WHITELIST_PATH=elsewhere)
        assert gw.POLICY_PATH == elsewhere
    print("ok: TRIAGE_EMAIL_WHITELIST_PATH names the policy file when set")


# --------------------------------------------------------------------------
# The index is a cache
# --------------------------------------------------------------------------

def test_the_index_is_rebuilt_from_the_ledger():
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp)
        _write_policy(gw, news={"news@shop.example"})
        events = rail(gw)
        mirror, box = started(gw)
        box.put("INBOX", msg("a@x.org", day=1))
        box.put("Sent", msg("b@example.org", sender=ME, to="ann@x.org", refs=["a@x.org"], day=2))
        box.put("INBOX", msg("c@x.org", refs=["a@x.org", "b@example.org"], day=3))
        box.put("INBOX", msg("solo@y.org", sender="Cy <cy@y.org>", subject="Lunch", day=4))
        box.put("INBOX", msg(None, sender="cron@y.org", subject="No id", day=5))
        box.put("INBOX", msg("n@shop.example", sender="news@shop.example", day=5))
        archived = box.put("INBOX", msg("arch@x.org", day=6))
        mirror.poll()
        box.move_mail("INBOX", archived, "Archive")
        mirror.poll()
        assert len(records(gw)) == 6
        chats_before = {r["message_id"]: r["chat"] for r in records(gw)}
        thread_before = mirror.thread("a@x.org")
        mirror.index.close()
        mirror.threads.close()

        # The state volume is lost. Meanwhile one mail is deleted, one arrives.
        shutil.rmtree(Path(tmp) / "data")
        box.remove("INBOX", box.uid_of("INBOX", "solo@y.org"))
        box.put("INBOX", msg("new@x.org", refs=["a@x.org"], day=7))
        box.fetched.clear()
        events.clear()
        mirror = gw.Mirror(box, clock=lambda: NOW)
        # Known before a single IMAP command: the threads are back.
        assert mirror.thread("a@x.org")["messages"] == 3
        while mirror.poll():
            pass
        recs = by_id(gw)
        assert len(records(gw)) == 7, "no mail is recorded a second time"
        assert {m: r["chat"] for m, r in recs.items() if m != "new@x.org"} == chats_before
        assert recs["new@x.org"]["chat"] == "a@x.org"
        assert recs["solo@y.org"]["removed_at"]
        assert not any(r["removed_at"] for m, r in recs.items() if m != "solo@y.org")
        # Each mail's place was found again by Message-ID, the archive's too …
        assert ("INBOX", "a@x.org") in box.found and ("Archive", "arch@x.org") in box.found
        # … and only what the mirror had no record of was read in full: the new
        # mail and the news mail (skipped again).
        whole = sorted(uid for folder, uid, limit in box.fetched
                       if limit != gw.HEADER_PEEK_BYTES)
        assert whole == sorted([box.uid_of("INBOX", "new@x.org"),
                                box.uid_of("INBOX", "n@shop.example")]), box.fetched
        assert sorted(kind for kind, _p in events) == ["inbound", "removed"]
        after = mirror.thread("a@x.org")
        assert after["messages"] == 4 and after["name"] == thread_before["name"]
        assert after["peer"] == thread_before["peer"]
        # And the mirror goes on as before.
        box.remove("Archive", box.uid_of("Archive", "arch@x.org"))
        mirror.poll()
        assert by_id(gw)["arch@x.org"]["removed_at"]
    print("ok: with the state volume lost, both indexes are rebuilt from the ledger")


def test_an_index_behind_or_ahead_of_the_ledger_is_reconciled():
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp)
        _write_policy(gw, news={"news@shop.example"})
        events = rail(gw)
        mirror, box = started(gw)
        box.put("INBOX", msg("a@x.org", day=1))
        box.put("INBOX", msg("n@shop.example", sender="news@shop.example", day=1))
        mirror.poll()
        mirror.index.close()
        mirror.threads.close()
        backup = Path(tmp) / "backup"
        shutil.copytree(Path(tmp) / "data", backup)
        mirror = gw.Mirror(box, clock=lambda: NOW)
        box.put("INBOX", msg("b@x.org", refs=["a@x.org"], day=2))
        box.put("Sent", msg("c@example.org", sender=ME, to="ann@x.org", refs=["a@x.org"], day=3))
        mirror.poll()
        assert len(records(gw)) == 3
        mirror.index.close()
        mirror.threads.close()

        # The state volume comes back from an older backup: the ledger knows
        # two mails the indexes have never heard of.
        shutil.rmtree(Path(tmp) / "data")
        shutil.copytree(backup, Path(tmp) / "data")
        events.clear()
        mirror = gw.Mirror(box, clock=lambda: NOW)
        assert mirror.thread("a@x.org")["messages"] == 3
        mirror.poll()
        mirror.poll()
        assert sorted(r["message_id"] for r in records(gw)) == [
            "a@x.org", "b@x.org", "c@example.org"], "nothing is recorded twice"
        assert not any(r["removed_at"] for r in records(gw)) and events == []

        # A record is gone from the ledger (deleted by hand, or the ledger
        # restored from a backup): its mail is still in the mailbox, so it is
        # mirrored again — once, and quietly.
        mirror.index.close()
        mirror.threads.close()
        gone = next(path for path, fields in gw._ibstore.iter_records(gw.INBOUND_STORE_DIR)
                    if fields["message_id"] == "b@x.org")
        gone.unlink()
        mirror = gw.Mirror(box, clock=lambda: NOW)
        assert mirror.thread("a@x.org")["messages"] == 2
        fetched = len(box.fetched)
        while mirror.poll():
            pass
        recs = by_id(gw)
        assert sorted(recs) == ["a@x.org", "b@x.org", "c@example.org"]
        assert recs["b@x.org"]["chat"] == "a@x.org" and recs["b@x.org"]["text"]
        assert [(e["message_id"], e.get("backfill")) for e in inbound(events)] == [
            ("b@x.org", True)]
        # Only that mail was read again; the news mail stays set aside.
        assert [uid for _f, uid, _l in box.fetched[fetched:]] == [box.uid_of("INBOX", "b@x.org")]
        assert mirror.backfill_status()["done"] is True
    print("ok: an index older or newer than the ledger is made to agree with it at start")


def test_skipped_mail_is_not_read_again_after_a_uidvalidity_change():
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp)
        _write_policy(gw, news={"news@shop.example"})
        rail(gw)
        mirror, box = started(gw)
        box.put("INBOX", msg("n@shop.example", sender="news@shop.example", day=1))
        box.put("INBOX", msg("a@x.org", day=2))
        mirror.poll()
        box.renumber("INBOX")
        box.fetched.clear()
        mirror.poll()
        mirror.poll()
        assert box.fetched == [] and list(by_id(gw)) == ["a@x.org"]
        assert ("INBOX", "n@shop.example") in box.found
    print("ok: a mail set aside as news is found again, not read again, when UIDs change")


def test_an_interrupted_ingest_is_not_recorded_twice():
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp)
        events = rail(gw)
        mirror, box = started(gw)
        box.put("INBOX", msg("a@x.org", day=1))
        # The record cannot be written: nothing is left behind, the poll fails.
        real = gw._ibstore.write_message

        def refuse(*args, **kwargs):
            raise OSError("No space left on device")
        gw._ibstore.write_message = refuse
        try:
            try:
                mirror.poll()
                raise AssertionError("a failed ledger write must fail the poll")
            except OSError:
                pass
        finally:
            gw._ibstore.write_message = real
        media = gw._ibstore.media_dir(gw.INBOUND_STORE_DIR)
        assert records(gw) == [] and (not media.exists() or not list(media.iterdir()))
        assert mirror.thread("a@x.org") is None and events == []
        mirror.poll()
        assert len(records(gw)) == 1 and len(events) == 1

        # A crash after the ledger write and before the index knew of it: the
        # claim the ingest left is matched to the record at the next start.
        box.put("INBOX", msg("b@x.org", day=2))
        row = dict(mirror.index.one("SELECT * FROM mails WHERE message_id = 'a@x.org'"))
        gw._ibstore.write_message(gw.INBOUND_STORE_DIR, channel="email", sender="ann@x.org",
                                  text="x", chat="b@x.org", account=ACCOUNT,
                                  message_id="b@x.org", timestamp=T0 + 2 * DAY, delivered=True)
        mirror.index.run(
            "INSERT INTO mails (message_id, record, direction, chat, ts, sender, sender_name, "
            "recipients, cc) VALUES ('b@x.org', '', 'in', 'b@x.org', ?, 'ann@x.org', NULL, "
            "'[]', '[]')", (row["ts"] + DAY,))
        # And a claim whose record never made it to disk is simply dropped.
        mirror.index.run(
            "INSERT INTO mails (message_id, record, direction, chat, ts, sender, sender_name, "
            "recipients, cc) VALUES ('never@x.org', '', 'in', 'never@x.org', 1, '', NULL, "
            "'[]', '[]')")
        mirror = gw.Mirror(box, clock=lambda: NOW)
        assert mirror.thread("never@x.org") is None
        mirror.poll()
        assert sorted(r["message_id"] for r in records(gw)) == ["a@x.org", "b@x.org"]
    print("ok: a failed or interrupted ingest never leaves a second record")


def test_a_failing_mailbox_costs_no_mail():
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp)
        events = rail(gw)
        mirror, box = started(gw)
        for n in range(1, 4):
            box.put("INBOX", msg(f"m{n}@x.org", day=n))
        real = box.fetch
        calls = {"n": 0}

        def flaky(folder, uid, limit=None):
            calls["n"] += 1
            if calls["n"] == 2:
                raise gw.MailboxError("the IMAP connection failed: timed out")
            return real(folder, uid, limit)
        box.fetch = flaky
        try:
            mirror.poll()
            raise AssertionError("the poll must report the failure")
        except gw.MailboxError:
            pass
        assert [r["message_id"] for r in records(gw)] == ["m1@x.org"]
        mirror.poll()
        assert [r["message_id"] for r in records(gw)] == ["m1@x.org", "m2@x.org", "m3@x.org"]
        assert [e["message_id"] for e in inbound(events)] == ["m1@x.org", "m2@x.org", "m3@x.org"]
    print("ok: a poll the mailbox fails midway is resumed without loss or repeat")


# --------------------------------------------------------------------------
# Thread answers, notes
# --------------------------------------------------------------------------

def test_thread_answers():
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp)
        rail(gw)
        mirror, box = started(gw)
        # A thread the user started, to two people, from an alias.
        box.put("Sent", msg("s1@example.org", sender="Me <alias@example.org>",
                            to="Ann Example <ann@x.org>, bob@y.org", cc=ACCOUNT,
                            subject="Fwd: Venue", day=1))
        mirror.poll()
        thread = mirror.thread("s1@example.org")
        assert thread == {
            "chat": "s1@example.org", "name": "Venue", "group": True,
            "participants": [{"address": "ann@x.org", "name": "Ann Example"},
                             {"address": "bob@y.org", "name": None}],
            # Nobody has written back: the first person the user wrote to.
            "peer": {"address": "ann@x.org", "name": "Ann Example"},
            "last_ts": T0 + DAY, "messages": 1, "live": 1}
        box.put("INBOX", msg("r1@y.org", sender="Bob B <bob@y.org>", to="alias@example.org",
                             subject="Re: Venue", refs=["s1@example.org"], day=2))
        box.put("INBOX", msg("solo@z.org", sender="zed@z.org", subject="", day=3))
        mirror.poll()
        thread = mirror.thread("s1@example.org")
        # The most recent sender leads; the user's own addresses are never listed.
        assert thread["participants"] == [{"address": "bob@y.org", "name": "Bob B"},
                                          {"address": "ann@x.org", "name": "Ann Example"}]
        assert thread["peer"] == {"address": "bob@y.org", "name": "Bob B"}
        # The user's own mail arriving in INBOX (a copy to themselves, sent
        # from elsewhere) does not make them their own counterpart.
        box.put("INBOX", msg("r2@example.org", sender="Me <alias@example.org>", to="bob@y.org",
                             cc=ACCOUNT, subject="Re: Venue", refs=["s1@example.org"], day=4))
        mirror.poll()
        thread = mirror.thread("s1@example.org")
        assert thread["messages"] == 3 and thread["last_ts"] == T0 + 4 * DAY
        assert thread["peer"] == {"address": "bob@y.org", "name": "Bob B"}
        assert [p["address"] for p in thread["participants"]] == ["bob@y.org", "ann@x.org"]
        solo = mirror.thread("solo@z.org")
        assert solo["name"] == "" and solo["group"] is False
        assert solo["participants"] == [{"address": "zed@z.org", "name": None}]
        # Newest activity first.
        recent = mirror.recent_chats()
        assert [t["chat"] for t in recent] == ["s1@example.org", "solo@z.org"]
        assert recent[0] == thread
        assert [t["chat"] for t in mirror.recent_chats(limit=1)] == ["s1@example.org"]
        assert mirror.thread("nope@x.org") is None
    print("ok: a thread answers its name, participants, peer and counts")


def test_an_outbound_note_names_the_author():
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp)
        events = rail(gw)
        mirror, box = started(gw)
        # Told before the mail shows up in Sent …
        mirror.note_author("early@example.org", "agent")
        box.put("Sent", msg("early@example.org", sender=ME, to="ann@x.org", day=1))
        box.put("Sent", msg("late@example.org", sender=ME, to="ann@x.org", day=2))
        mirror.poll()
        recs = by_id(gw)
        assert recs["early@example.org"]["author"] == "agent"
        assert recs["late@example.org"]["author"] == "device"
        assert [e["author"] for e in inbound(events)] == ["agent", "device"]
        # … or after it was recorded as the own-device echo.
        mirror.note_author("late@example.org", "user")
        assert by_id(gw)["late@example.org"]["author"] == "user"
        # An inbound mail has no author to correct.
        box.put("INBOX", msg("in@x.org", day=3))
        mirror.poll()
        mirror.note_author("in@x.org", "agent")
        assert by_id(gw)["in@x.org"]["author"] is None
        # The notes are bounded.
        gw.MAIL_NOTES_MAX = 3
        for n in range(6):
            mirror.note_author(f"n{n}@example.org", "agent")
        assert mirror.index.one("SELECT COUNT(*) AS n FROM notes")["n"] == 3
    print("ok: an outbound note names a Sent mail's author, before or after it is recorded")


# --------------------------------------------------------------------------
# The rail
# --------------------------------------------------------------------------

def test_the_rail_queue_keeps_what_was_not_taken():
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp)
        state = {"answer": "down"}
        events = rail(gw, lambda kind, payload: state["answer"])
        mirror, box = started(gw)
        box.put("INBOX", msg("a@x.org", day=1))
        box.put("INBOX", msg("b@x.org", day=2))
        mirror.poll()
        # The web gateway is down: the mails are mirrored, the events wait.
        assert len(records(gw)) == 2
        assert mirror.index.one("SELECT COUNT(*) AS n FROM outbox")["n"] == 2
        assert {p["message_id"] for _k, p in events} == {"a@x.org"}, "no hammering when down"
        events.clear()
        state["answer"] = "ok"
        # A restart does not lose the queue; delivery is in order.
        mirror = gw.Mirror(box, clock=lambda: NOW)
        mirror.poll()
        assert [p["message_id"] for _k, p in events] == ["a@x.org", "b@x.org"]
        assert mirror.index.one("SELECT COUNT(*) AS n FROM outbox")["n"] == 0
        # An event the web gateway refuses as malformed is not retried for ever,
        events.clear()
        state["answer"] = "drop"
        box.put("INBOX", msg("c@x.org", day=3))
        mirror.poll()
        assert mirror.index.one("SELECT COUNT(*) AS n FROM outbox")["n"] == 0
        # and one it cannot take yet (a merge, on an older web gateway) waits
        # without holding the mails behind it back.
        events.clear()
        state["answer"] = "ok"
        gw._rail_send = lambda kind, payload: (
            events.append((kind, payload)), "later" if kind == "merge" else "ok")[1]
        box.put("INBOX", msg("d@x.org", irt="lost@x.org", day=4))
        mirror.poll()
        box.put("INBOX", msg("e@x.org", refs=["a@x.org", "lost@x.org"], day=5))
        mirror.poll()
        assert [k for k, _p in events] == ["inbound", "merge", "inbound"]
        assert mirror.index.one("SELECT kind FROM outbox")["kind"] == "merge"
    print("ok: rail events wait for a web gateway that is down, in order, across restarts")


def test_no_rail_configured_queues_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp, rail=False)
        events = rail(gw)
        mirror, box = started(gw)
        box.put("INBOX", msg("a@x.org", day=1))
        mirror.poll()
        assert len(records(gw)) == 1 and events == []
        assert mirror.index.one("SELECT COUNT(*) AS n FROM outbox")["n"] == 0
    print("ok: without a rail endpoint the ledger alone is written")


class _RailStub(BaseHTTPRequestHandler):
    seen: list = []
    status = 200

    def log_message(self, fmt, *args):
        return

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        type(self).seen.append((self.path, self.headers.get("X-Conversation-Backend-Token"),
                                json.loads(body)))
        self.send_response(type(self).status)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")


def test_rail_posts_and_their_outcomes():
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp)
        server = ThreadingHTTPServer(("127.0.0.1", 0), _RailStub)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        gw._chats.CHATS_INGEST_URL = f"{base}/internal/chats/inbound"
        _RailStub.seen = []
        try:
            _RailStub.status = 200
            assert gw._rail_send("inbound", {"chat": "a"}) == "ok"
            assert gw._rail_send("merge", {"into": "a"}) == "ok"
            assert gw._rail_send("removed", {"chat": "a"}) == "ok"
            # The two new endpoints are the inbound URL with its last segment replaced.
            assert [(path, token) for path, token, _b in _RailStub.seen] == [
                ("/internal/chats/inbound", "rail-token"),
                ("/internal/chats/merge", "rail-token"),
                ("/internal/chats/removed", "rail-token")]
            assert _RailStub.seen[0][2] == {"chat": "a"}
            for status, kind, outcome in ((400, "inbound", "drop"), (413, "inbound", "drop"),
                                          (500, "inbound", "down"), (404, "inbound", "down"),
                                          (404, "merge", "later"), (503, "removed", "later"),
                                          (401, "inbound", "down"), (403, "merge", "down")):
                _RailStub.status = status
                assert gw._rail_send(kind, {}) == outcome, (status, kind)
        finally:
            server.shutdown()
        gw._chats.CHATS_INGEST_URL = "http://127.0.0.1:9/internal/chats/inbound"
        assert gw._rail_send("inbound", {}) == "down"
    print("ok: the rail's three endpoints, its token, and what each answer means")


def test_a_long_text_is_capped_on_the_rail_only():
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp)
        events = rail(gw)
        gw.MAIL_RAIL_TEXT_CHARS = 50
        mirror, box = started(gw)
        box.put("INBOX", msg("a@x.org", body="word " * 200, day=1))
        mirror.poll()
        assert len(events[0][1]["text"]) == 50
        assert len(records(gw)[0]["text"]) > 900
    print("ok: an event carries a bounded text; the record carries all of it")


# --------------------------------------------------------------------------
# Health and the loop
# --------------------------------------------------------------------------

def test_health_states():
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp, configured=False)
        snap = gw._health_snapshot()
        assert snap["status"] == "ok" and snap["configured"] is False
        assert snap["connected"] is False and snap["account"] is None
        assert "EMAIL_USER" in snap["error"] and snap["needs_repair"] is False
        assert snap["mode"] == "inbox" and snap["read_only"] is True
        json.dumps(snap)
    with tempfile.TemporaryDirectory() as tmp:
        # A named account reads its own suffixed variables and no others.
        gw = load(tmp, configured=False, MAIL_ACCOUNT_NAME="work", EMAIL_USER="other@example.org",
                  EMAIL_PASS="x", IMAP_HOST="imap.example.org", SMTP_HOST="smtp.example.org")
        assert gw._health_snapshot()["configured"] is False
        assert "EMAIL_USER_WORK" in gw._health_snapshot()["error"]
        gw = load(tmp, configured=False, MAIL_ACCOUNT_NAME="work", EMAIL_USER_WORK="W@Example.org",
                  EMAIL_PASS_WORK="x", IMAP_HOST_WORK="imap.example.org",
                  SMTP_HOST_WORK="smtp.example.org")
        assert gw._health_snapshot()["account"] == "w@example.org"
    with tempfile.TemporaryDirectory() as tmp:
        # An address with the rest missing is a fault, and says which part.
        gw = load(tmp, configured=False, EMAIL_USER="me@example.org")
        snap = gw._health_snapshot()
        assert snap["configured"] is True and snap["connected"] is False
        assert "EMAIL_PASS" in snap["error"], snap
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp, MAIL_POLICY_WAIT_SECONDS=0)
        rail(gw)
        snap = gw._health_snapshot()
        assert snap["configured"] is True and snap["connected"] is False
        assert snap["account"] == ACCOUNT and "not been polled" in snap["error"]
        assert snap["backfill"] == {"done": False, "pending": None, "days": 90}
        box = FakeMailbox(gw)
        box.put("INBOX", msg("a@x.org", day=1))
        gw.MIRROR = gw.Mirror(box, clock=lambda: NOW)
        loop: dict = {}
        delay = gw._loop_step(gw.MIRROR, loop)
        snap = gw._health_snapshot()
        assert snap["connected"] is True and snap["error"] is None and snap["last_poll"]
        assert snap["backfill"] == {"done": True, "pending": 0, "days": 90}
        assert 1.0 <= delay <= gw.MAIL_POLL_SECONDS and len(records(gw)) == 1
        # A failed login or poll: not connected, and the operator is told why.
        box.fail = gw.MailboxError("IMAP login failed for me@example.org: bad credentials")
        assert gw._loop_step(gw.MIRROR, loop) == gw.MAIL_POLL_SECONDS
        snap = gw._health_snapshot()
        assert snap["connected"] is False and "login failed" in snap["error"]
        gw._loop_step(gw.MIRROR, loop)
        assert gw._health_snapshot()["connected"] is True
        # An internal error is a health state too, never a dead loop.
        gw.MIRROR.poll = lambda **kw: 1 / 0
        gw._loop_step(gw.MIRROR, loop)
        snap = gw._health_snapshot()
        assert snap["connected"] is False and "internal error" in snap["error"]
        json.dumps(snap)
    print("ok: health reports unconfigured, misconfigured, connected and failed")


def test_missing_folders_are_reported():
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp, MAIL_POLICY_WAIT_SECONDS=0)
        rail(gw)
        # No archive folder: the mirror works, and says what that means.
        box = FakeMailbox(gw, folders=("INBOX", "Sent"))
        uid = box.put("INBOX", msg("a@x.org", day=1))
        gw.MIRROR = gw.Mirror(box, clock=lambda: NOW)
        loop: dict = {}
        gw._loop_step(gw.MIRROR, loop)
        snap = gw._health_snapshot()
        assert snap["connected"] is True and "MAIL_ARCHIVE_FOLDER" in snap["error"]
        assert len(records(gw)) == 1
        box.remove("INBOX", uid)
        loop.clear()
        gw._loop_step(gw.MIRROR, loop)
        assert records(gw)[0]["removed_at"]
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp, MAIL_POLICY_WAIT_SECONDS=0)
        rail(gw)
        # No Sent folder: a fault. Nothing may be called gone meanwhile.
        box = FakeMailbox(gw)
        box.put("INBOX", msg("a@x.org", day=1))
        uid = box.put("Sent", msg("s@example.org", sender=ME, to="ann@x.org", day=2))
        gw.MIRROR = gw.Mirror(box, clock=lambda: NOW)
        loop: dict = {}
        gw._loop_step(gw.MIRROR, loop)
        assert len(records(gw)) == 2
        sent = box.boxes.pop("Sent")
        box.put("INBOX", msg("b@x.org", day=3))
        loop.clear()
        gw._loop_step(gw.MIRROR, loop)
        snap = gw._health_snapshot()
        assert snap["connected"] is False and "SENT_FOLDER" in snap["error"], snap
        recs = by_id(gw)
        assert "b@x.org" in recs, "INBOX is still mirrored"
        assert not recs["s@example.org"]["removed_at"]
        box.boxes["Sent"] = sent
        loop.clear()
        gw._loop_step(gw.MIRROR, loop)
        assert gw._health_snapshot()["connected"] is True
        assert not by_id(gw)["s@example.org"]["removed_at"] and uid in sent["mails"]
    print("ok: a missing archive folder is a notice, a missing Sent folder a fault")


def test_the_first_backfill_waits_for_the_policy_file():
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp, MAIL_POLICY_WAIT_SECONDS=300)
        rail(gw)
        box = FakeMailbox(gw)
        box.put("INBOX", msg("a@x.org", day=1))
        gw.MIRROR = gw.Mirror(box, clock=lambda: NOW)
        loop: dict = {}
        delay = gw._loop_step(gw.MIRROR, loop)
        snap = gw._health_snapshot()
        assert snap["connected"] is True and "policy file" in snap["error"]
        assert records(gw) == [] and delay <= 5.0 and box.fetched == []
        # The retinue side writes the file (empty: nothing to flag) and the mirror starts.
        gw.POLICY_PATH.parent.mkdir(parents=True)
        gw.POLICY_PATH.write_text("")
        gw._loop_step(gw.MIRROR, loop)
        assert len(records(gw)) == 1 and gw._health_snapshot()["error"] is None
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp, MAIL_POLICY_WAIT_SECONDS=300)
        rail(gw)
        box = FakeMailbox(gw)
        box.put("INBOX", msg("a@x.org", day=1))
        mirror = gw.Mirror(box, clock=lambda: NOW)
        # It never appears: after the wait the mirror proceeds as with no flags.
        loop = {"started": 0.0}
        gw._loop_step(mirror, loop)
        assert len(records(gw)) == 1
        # Once the backfill is done, a missing file holds nothing up.
        box.put("INBOX", msg("b@x.org", day=2))
        gw._loop_step(mirror, {})
        assert len(records(gw)) == 2
    print("ok: the first backfill waits for the policy file, but not for ever")


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def serve(gw):
    server = ThreadingHTTPServer(("127.0.0.1", 0), gw._Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def call(url, body=None, token=None, method=None):
    """``(status, headers, body bytes)`` of one request."""
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, headers=headers,
                                     method=method or ("POST" if body is not None else "GET"))
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def test_http_api():
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp, token="gateway-token")
        rail(gw)
        mirror, box = started(gw)
        gw.MIRROR = mirror
        box.put("INBOX", msg("a/b+c@x.org", subject="Slash", day=1,
                             files=[("notes.txt", "text/plain", b"attached text")]))
        box.put("Sent", msg("s@example.org", sender=ME, to="ann@x.org", day=2))
        mirror.poll()
        server, base = serve(gw)
        token = "gateway-token"
        try:
            # /health is open; everything else needs the token.
            status, _h, body = call(f"{base}/health")
            assert status == 200 and json.loads(body)["account"] == ACCOUNT
            for path in ("/recent-chats", "/threads/x", "/media/" + "0" * 32, "/pending-sends"):
                assert call(base + path)[0] == 401, path
                assert call(base + path, token="wrong")[0] == 401, path
            for path in ("/send", "/outbound-note", "/chats/delete", "/chats/read"):
                assert call(base + path, body=b"{}")[0] == 401, path

            status, _h, body = call(f"{base}/recent-chats", token=token)
            chats = json.loads(body)["recent_chats"]
            assert status == 200 and [c["chat"] for c in chats] == ["s@example.org", "a/b+c@x.org"]
            # The chat key travels URL-encoded.
            status, _h, body = call(f"{base}/threads/{quote('a/b+c@x.org', safe='')}", token=token)
            assert status == 200 and json.loads(body) == mirror.thread("a/b+c@x.org")
            assert json.loads(body)["name"] == "Slash"
            assert call(f"{base}/threads/nope%40x.org", token=token)[0] == 404
            assert call(f"{base}/nope", token=token)[0] == 404

            # Attachments and the part blobs are served with their stored type.
            record = by_id(gw)["a/b+c@x.org"]
            for ref, content, kind in (
                    (record["attachments"][0], b"attached text", "text/plain"),
                    (record["body_part"], b"The numbers are in.\n", "text/plain; charset=utf-8")):
                media_id = gw._ibstore.media_id_of(ref)
                status, headers, body = call(f"{base}/media/{media_id}", token=token)
                assert status == 200 and body == content, (status, body)
                assert headers["Content-Type"] == kind
                assert headers["X-Content-Type-Options"] == "nosniff"
            assert call(f"{base}/media/{'f' * 32}", token=token)[0] == 404
            assert call(f"{base}/media/..%2F..%2Fetc%2Fpasswd", token=token)[0] == 404

            # Present, and saying why: the mirror is read-only.
            reason = {"error": "the mail mirror is read-only"}
            for path in ("/send", "/chats/delete", "/chats/read", "/chats/archive",
                         "/pending-sends/" + "a" * 32 + "/approve",
                         "/pending-sends/" + "a" * 32 + "/retract"):
                status, _h, body = call(base + path, body=b'{"chat": "a@x.org"}', token=token)
                assert (status, json.loads(body)) == (501, reason), path
            for path in ("/pending-sends", "/pending-sends/" + "a" * 32):
                status, _h, body = call(base + path, token=token)
                assert (status, json.loads(body)) == (501, reason), path
            assert call(f"{base}/nope", body=b"{}", token=token)[0] == 404
            assert len(records(gw)) == 2

            # /outbound-note
            def note(payload):
                status, _h, body = call(f"{base}/outbound-note", token=token,
                                        body=payload if isinstance(payload, bytes)
                                        else json.dumps(payload).encode())
                return status, json.loads(body)
            assert note({"message_id": "<s@example.org>", "author": "agent"}) == (200, {"ok": True})
            assert by_id(gw)["s@example.org"]["author"] == "agent"
            assert note({"message_id": "s@example.org", "author": "device"})[0] == 400
            assert note({"message_id": "s@example.org", "author": "root"})[0] == 400
            assert note({"message_id": "not an id", "author": "agent"})[0] == 400
            assert note({"author": "agent"})[0] == 400
            assert note(b"[1]")[0] == 400 and note(b"not json")[0] == 400
            assert note(b" " * (gw.MAX_BODY_BYTES + 1))[0] == 400
            gw.MIRROR = None
            assert note({"message_id": "x@example.org", "author": "user"})[0] == 503
            assert json.loads(call(f"{base}/recent-chats", token=token)[2]) == {"recent_chats": []}
        finally:
            server.shutdown()
    with tempfile.TemporaryDirectory() as tmp:
        # No token configured: open, like the other gateways on the agents network.
        gw = load(tmp)
        server, base = serve(gw)
        try:
            assert call(f"{base}/recent-chats")[0] == 200
            assert call(f"{base}/send", body=b"{}")[0] == 501
        finally:
            server.shutdown()
    print("ok: the HTTP API is token-gated, serves threads and media, and refuses to write")


def main():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failures = 0
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            import traceback
            print(f"FAIL {test.__name__}: {exc}")
            traceback.print_exc()
    if failures:
        print(f"\n{failures} mail gateway check(s) failed.")
        return 1
    print("\nAll mail gateway checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
