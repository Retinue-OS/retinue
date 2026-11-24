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
_ENV_NAMES = ("SENT_FOLDER", "DRAFTS_FOLDER", "INBOUND_STORE_DIR", "MESSENGER_BUILTIN_CHANNELS")
TOKEN = "gateway-token"


def load(tmp, *, token=TOKEN, days=90, configured=True, rail=True, **env):
    """Import a fresh gateway module whose state lives under ``tmp``. With a
    token, as every deployment that mirrors has one (``token=""`` for none)."""
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
        self.refused = set()  # folders the server will not open (see _check)

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

    def ensure(self):
        self._check()

    def close(self):
        pass

    def folders(self):
        self._check()
        return {role: name if name in self.boxes else None
                for role, name in self.roles.items()}

    def _check(self, folder=None):
        if self.fail is not None:
            failure, self.fail = self.fail, None
            raise failure
        # A folder the server will not open just now (NO to EXAMINE for a
        # reason other than "no such folder"): the adapter raises on that.
        if folder is not None and folder in self.refused:
            raise self.gw.MailboxError(
                f"the IMAP server would not open {folder!r}: [UNAVAILABLE] try again later")

    def list_uids(self, folder, *, since=None, first_uid=None, dates=False):
        self._check(folder)
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
        self._check(folder)
        self.fetched.append((folder, uid, limit))
        mail = self.boxes.get(folder, {"mails": {}})["mails"].get(uid)
        if mail is None:
            return None
        raw = mail["raw"]
        return self.gw.Fetched(raw[:limit] if limit else raw, len(raw),
                               mail["flags"], mail["date"])

    def find(self, folder, message_id):
        self._check(folder)
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


def test_a_folder_the_server_will_not_open_is_not_an_empty_folder():
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp)
        events = rail(gw)
        mirror, box = started(gw)
        uid = box.put("INBOX", msg("a@x.org", day=1))
        mirror.poll()
        # Archived in a client — and for a moment the server refuses the
        # archive (NO to EXAMINE: unavailable, in use, throttled).
        box.move_mail("INBOX", uid, "Archive")
        box.refused = {"Archive"}
        for _ in range(3):
            mirror.poll()
            assert "would not open 'Archive'" in mirror.health_notice()
        record = by_id(gw)["a@x.org"]
        assert record["removed_at"] is None and record["body_part"], "nothing is called gone"
        assert len(events) == 1 and box.found == []
        # New mail was still mirrored meanwhile: INBOX answered.
        box.put("INBOX", msg("b@x.org", day=2))
        mirror.poll()
        assert "b@x.org" in by_id(gw)
        box.refused = set()
        mirror.poll()
        assert by_id(gw)["a@x.org"]["removed_at"] is None and ("Archive", "a@x.org") in box.found
        assert not any(kind == "removed" for kind, _p in events)
        assert mirror.health_notice() is None
        # INBOX or Sent refused is another matter: without them the pass
        # has nothing to stand on, and fails.
        for folder in ("INBOX", "Sent"):
            box.refused = {folder}
            try:
                mirror.poll()
                raise AssertionError("a refused folder must fail the pass")
            except gw.MailboxError as exc:
                assert "would not open" in str(exc)
    with tempfile.TemporaryDirectory() as tmp:
        # The same during a backfill: a mail the server would not hand over is
        # not "expunged", and the backfill is not done without it.
        gw = load(tmp)
        rail(gw)
        box = FakeMailbox(gw)
        for n in range(1, 5):
            box.put("INBOX", msg(f"old{n}@x.org", day=n))
            box.put("Sent", msg(f"sent{n}@example.org", sender=ME, to="ann@x.org", day=n + 0.5))
        mirror = gw.Mirror(box, clock=lambda: NOW)
        assert mirror.poll(backfill_limit=2) is True
        box.refused = {"INBOX"}
        try:
            mirror.poll(sync=False, backfill_limit=50)
            raise AssertionError("a refused folder must fail the pass")
        except gw.MailboxError:
            pass
        assert mirror.backfill_status()["done"] is False
        box.refused = set()
        while mirror.poll():
            pass
        assert len(records(gw)) == 8 and mirror.backfill_status()["done"] is True
    print("ok: a folder the server refuses to open is not read as empty: nothing is "
          "tombstoned or skipped on it")


def test_an_archive_that_keeps_refusing_holds_back_only_what_needs_it():
    """A server can refuse one folder for days (a broken mailbox answering
    [SERVERBUG], a missing permission). The archive's answer is needed to
    find a mail again and to call one gone — not to read INBOX and Sent:
    the ninety days of old mail arrive, /health says what is waiting and
    why, and nothing is called gone meanwhile."""
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp)
        _write_policy(gw)
        events = rail(gw)
        box = FakeMailbox(gw)
        for n in range(6):
            box.put("INBOX", msg(f"old{n}@x.org", subject=f"Old {n}", day=30 + n))
        box.put("Sent", msg("sent@example.org", sender=ME, to="ann@x.org", day=33.5))
        box.refused = {"Archive"}          # from the first poll on, every time
        gw.MIRROR = mirror = gw.Mirror(box, clock=lambda: NOW)
        loop: dict = {}
        for turn in range(6):
            if turn == 2:
                box.put("INBOX", msg("new@x.org", subject="New", day=39.9), date=NOW)
            loop.pop("synced", None)
            gw._loop_step(mirror, loop)
            snap = gw._health_snapshot()
            # The mirror works; the notice carries the server's own words.
            assert snap["connected"] is True, snap["error"]
            assert "would not open 'Archive'" in snap["error"] and "[UNAVAILABLE]" in snap["error"]
            assert "none is called removed" in snap["error"]
        assert sorted(by_id(gw)) == ["new@x.org"] + [f"old{n}@x.org" for n in range(6)] + [
            "sent@example.org"]
        assert mirror.backfill_status() == {"done": True, "pending": 0, "days": 90}
        assert [e.get("backfill", False) for e in inbound(events)].count(True) == 7
        # A mail that leaves INBOX meanwhile — archived, for all anyone can
        # tell — is not looked for and not called gone …
        box.move_mail("INBOX", box.uid_of("INBOX", "old3@x.org"), "Archive")
        gone = box.remove("INBOX", box.uid_of("INBOX", "old4@x.org"))
        for _ in range(3):
            loop.pop("synced", None)
            gw._loop_step(mirror, loop)
        assert not any(r["removed_at"] for r in records(gw)) and box.found == []
        assert not any(kind == "removed" for kind, _p in events)
        # … until the archive answers: then the one is found there, and the
        # other, which is in no folder, leaves its tombstone.
        box.refused = set()
        loop.pop("synced", None)
        gw._loop_step(mirror, loop)
        loop.pop("synced", None)
        gw._loop_step(mirror, loop)
        recs = by_id(gw)
        assert recs["old3@x.org"]["removed_at"] is None and recs["old4@x.org"]["removed_at"]
        assert [p["message_ids"] for kind, p in events if kind == "removed"] == [["old4@x.org"]]
        assert gw._health_snapshot()["error"] is None and gone["raw"]
    print("ok: an archive folder that keeps refusing holds back neither new mail nor the "
          "backfill; nothing is called gone until it answers")


def test_a_search_that_does_not_settle_it_calls_nothing_gone():
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp)
        events = rail(gw)
        mirror, box = started(gw)
        uid = box.put("INBOX", msg("a@x.org", day=1))
        other = box.put("INBOX", msg("b@x.org", day=2))
        mirror.poll()
        box.move_mail("INBOX", uid, "Archive")
        box.remove("INBOX", other)
        # The archive's search matched more look-alike ids than are compared.
        real = box.find
        box.find = lambda folder, mid: (
            gw.UNSURE if (folder, mid) == ("Archive", "a@x.org") else real(folder, mid))
        mirror.poll()
        mirror.poll()
        recs = by_id(gw)
        assert recs["a@x.org"]["removed_at"] is None, "could not tell is not gone"
        # It holds nothing else up: the mail that IS gone is a tombstone.
        assert recs["b@x.org"]["removed_at"]
        assert [p["message_ids"] for kind, p in events if kind == "removed"] == [["b@x.org"]]
        box.find = real
        mirror.poll()
        assert by_id(gw)["a@x.org"]["removed_at"] is None
        assert mirror.index.one("SELECT role FROM locations WHERE message_id = 'a@x.org'")[
            "role"] == "archive"
        # UNSURE cannot be mistaken for a UID or for None.
        try:
            bool(gw.UNSURE)
            raise AssertionError("UNSURE must not be truth-tested")
        except TypeError:
            pass
    print("ok: a search that does not settle where a mail is leaves it as it is")


def test_a_mail_without_a_searchable_id_is_found_among_many():
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp)
        events = rail(gw)
        box = FakeMailbox(gw)
        # The archive holds many mails of these days that nobody mirrored
        # (filed by rules; on some servers it is every mail there is).
        for n in range(150):
            box.put("Archive", msg(f"filed{n}@x.org", day=38), date=T0 + 38 * DAY)
        mirror = gw.Mirror(box, clock=lambda: NOW)
        mirror.poll()
        uid = box.put("INBOX", msg(None, subject="No message id", day=39))
        gone = box.put("INBOX", msg(None, subject="Another without", day=38.5))
        mirror.poll()
        first, second = sorted(by_id(gw), key=lambda m: by_id(gw)[m]["subject_line"],
                               reverse=True)
        assert by_id(gw)[first]["subject_line"] == "No message id"
        # Archived: its headers are read back newest first, and it is found.
        box.move_mail("INBOX", uid, "Archive")
        mirror.poll()
        assert by_id(gw)[first]["removed_at"] is None
        assert mirror.index.one("SELECT role FROM locations WHERE message_id = ?",
                                (first,))["role"] == "archive"
        # Deleted: it is a tombstone only once every candidate was compared —
        # which takes more than one pass reads, and until then it is left alone.
        gw.SCAN_BUDGET = 100
        box.remove("INBOX", gone)
        box.fetched.clear()
        mirror.poll()
        assert by_id(gw)[second]["removed_at"] is None, "not after the first hundred"
        assert len(box.fetched) == 100
        assert all(limit == gw.HEADER_PEEK_BYTES for _f, _u, limit in box.fetched)
        for _ in range(3):
            mirror.poll()
        assert by_id(gw)[second]["removed_at"]
        assert [p["message_ids"] for kind, p in events if kind == "removed"] == [[second]]
        # No header is read twice for it.
        peeks = [(folder, u) for folder, u, _l in box.fetched]
        assert len(peeks) == len(set(peeks)) == 150, "each candidate is compared once"
    with tempfile.TemporaryDirectory() as tmp:
        # One pass reads a bounded number of headers, however many mails ask —
        # and however many ask, each header is read once for all of them.
        gw = load(tmp)
        rail(gw)
        box = FakeMailbox(gw)
        for n in range(120):
            box.put("Archive", msg(f"filed{n}@x.org", day=38), date=T0 + 38 * DAY)
        mirror = gw.Mirror(box, clock=lambda: NOW)
        mirror.poll()
        gw.SCAN_BUDGET = 50
        uids = [box.put("INBOX", msg(None, subject=f"No id {n}", day=38)) for n in range(4)]
        mirror.poll()
        for u in uids:
            box.remove("INBOX", u)
        box.fetched.clear()
        mirror.poll()
        assert len(box.fetched) == 50
        assert not any(r["removed_at"] for r in records(gw))
        for _ in range(3):
            mirror.poll()
        assert all(r["removed_at"] for r in records(gw))
        assert len(box.fetched) == 120, "four mails looked for, each header read once"
        # What is remembered of the headers read is bounded too. A search
        # that needs more than may be kept stays undecided — the mail is left
        # as it is — and reads nothing over and over meanwhile.
        for n in range(80):
            box.put("Archive", msg(f"more{n}@x.org", day=38), date=T0 + 38 * DAY)
        gw.SCAN_CACHE_MAX = 150
        uid = box.put("INBOX", msg(None, subject="One more", day=38))
        mirror.poll()
        box.remove("INBOX", uid)
        box.fetched.clear()
        for _ in range(5):
            mirror.poll()
        assert len(box.fetched) == 30 and sum(map(len, mirror._peeked.values())) == 150
        assert [r["subject_line"] for r in records(gw) if not r["removed_at"]] == ["One more"]
        gw.SCAN_CACHE_MAX = 100_000
        mirror.poll()
        assert all(r["removed_at"] for r in records(gw)) and len(box.fetched) == 80
    with tempfile.TemporaryDirectory() as tmp:
        # A mail's time on record is its sender's Date where that is earlier
        # than its arrival, and it is looked for by when it ARRIVED: a mail
        # with no Message-ID and a Date of years ago, archived, is found.
        gw = load(tmp)
        events = rail(gw)
        box = FakeMailbox(gw)
        for n in range(30):
            box.put("Archive", msg(f"filed{n}@x.org", day=38), date=T0 + 38 * DAY)
        mirror = gw.Mirror(box, clock=lambda: NOW)
        mirror.poll()
        uid = box.put("INBOX", msg(None, subject="Backdated", day=-9000), date=NOW)
        # A server that stamps a copy with the time it was made: this one
        # arrived days ago and is in the archive as of today.
        old = box.put("INBOX", msg(None, subject="Stamped anew", day=20), date=T0 + 20 * DAY)
        mirror.poll()
        ids = {r["subject_line"]: m for m, r in by_id(gw).items()}
        assert by_id(gw)[ids["Backdated"]]["received_at"].startswith("2001-")
        box.move_mail("INBOX", uid, "Archive")
        box.put("Archive", box.remove("INBOX", old)["raw"], date=NOW)
        for _ in range(3):
            mirror.poll()
        assert not any(r["removed_at"] for r in records(gw)) and not any(
            kind == "removed" for kind, _p in events)
        assert {row["role"] for row in mirror.index.rows("SELECT role FROM locations")} == {
            "archive"}
        # The index lost: when a mail arrived is in its record, so it is
        # looked for in the same window as before — not among everything the
        # folder has held since the year its Date claims.
        mirror.index.close()
        mirror.threads.close()
        shutil.rmtree(gw.MAIL_STATE_DIR)
        mirror = gw.Mirror(box, clock=lambda: NOW)
        box.listed.clear()
        for _ in range(3):
            mirror.poll()
        assert not any(r["removed_at"] for r in records(gw))
        assert mirror.index.one("SELECT COUNT(*) AS n FROM locations WHERE message_id "
                                "IS NOT NULL")["n"] == 2
        assert min(since for folder, since, _u, _d in box.listed
                   if folder == "Archive" and since is not None) > T0
    with tempfile.TemporaryDirectory() as tmp:
        # An id some relay folded in the middle is restored for threading —
        # and the server, which searches the header as it stands, knows no
        # mail by the restored id. It is not asked; the headers are read.
        gw = load(tmp)
        events = rail(gw)
        mirror, box = started(gw)
        raw = msg("placeholder@x.org", subject="Folded id", day=39.9).replace(
            b"<placeholder@x.org>", b"<a-very-long-left-part-of-an-id\r\n .continued@x.org>")
        uid = box.put("INBOX", raw, date=NOW)
        mirror.poll()
        mid = "a-very-long-left-part-of-an-id.continued@x.org"
        assert list(by_id(gw)) == [mid] and box.find("INBOX", mid) is None
        box.move_mail("INBOX", uid, "Archive")
        box.found.clear()
        mirror.poll()
        mirror.poll()
        assert by_id(gw)[mid]["removed_at"] is None and box.found == []
        assert mirror.index.one("SELECT role FROM locations")["role"] == "archive"
        assert not any(kind == "removed" for kind, _p in events)
        # An ordinary id is still asked for, not read for.
        uid = box.put("INBOX", msg("plain@x.org", day=39.9), date=NOW)
        mirror.poll()
        box.move_mail("INBOX", uid, "Archive")
        fetched = len(box.fetched)
        mirror.poll()
        assert ("Archive", "plain@x.org") in box.found and len(box.fetched) == fetched
    print("ok: a mail the server cannot be searched for is looked for among all the "
          "candidates, across passes, before it is called gone")


def test_an_error_mid_settle_keeps_the_removals_already_made():
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp)
        events = rail(gw)
        mirror, box = started(gw)
        uids = [box.put("INBOX", msg(f"m{n}@x.org", subject=f"s{n}", day=1 + n))
                for n in range(4)]
        mirror.poll()
        for uid in uids:
            box.remove("INBOX", uid)
        real = box.find
        calls = {"n": 0}

        def flaky(folder, message_id):
            calls["n"] += 1
            if calls["n"] == 8:     # the search for the third mail times out
                raise gw.MailboxError("the IMAP connection failed: timed out")
            return real(folder, message_id)
        box.find = flaky
        try:
            mirror.poll()
            raise AssertionError("the poll must report the failure")
        except gw.MailboxError:
            pass
        # What was found gone before the error is tombstoned AND its event
        # queued, in the same breath.
        queued = [json.loads(row["payload"])["message_ids"] for row in mirror.index.rows(
            "SELECT payload FROM outbox WHERE kind = 'removed' ORDER BY seq")]
        assert queued == [["m0@x.org"], ["m1@x.org"]], queued
        assert sorted(m for m, r in by_id(gw).items() if r["removed_at"]) == [
            "m0@x.org", "m1@x.org"]
        box.find = real
        mirror.poll()
        removed = sorted(p["message_ids"][0] for kind, p in events if kind == "removed")
        assert removed == ["m0@x.org", "m1@x.org", "m2@x.org", "m3@x.org"]
        assert all(r["removed_at"] for r in records(gw))
    print("ok: an IMAP error in the middle of a pass loses no removal already made")


def test_a_mail_that_passed_through_inbox_between_polls_is_mirrored():
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp)
        events = rail(gw)
        mirror, box = started(gw)
        box.put("INBOX", msg("seen@x.org", day=30))
        mirror.poll()
        # Mail a rule files straight into the archive is nobody's, as ever …
        box.put("Archive", msg("filed@x.org", day=39.9), date=NOW)
        mirror.poll()
        mirror.poll()
        assert list(by_id(gw)) == ["seen@x.org"] and box.fetched[-1][0] == "INBOX"
        # … but a mail that was in INBOX and is archived before the next poll
        # (read on the phone within the minute) is the user's mail.
        uid = box.put("INBOX", msg("quick@x.org", subject="Quick", day=39.9), date=NOW)
        box.move_mail("INBOX", uid, "Archive")
        # Their own mail too: sent, and filed away at once by the client.
        uid = box.put("Sent", msg("own@example.org", sender=ME, to="ann@x.org",
                                  refs=["quick@x.org"], day=39.95), date=NOW)
        box.move_mail("Sent", uid, "Archive")
        # Old mail moved into the archive in the same minute is not new mail.
        box.put("Archive", msg("ancient@x.org", day=2), date=T0 + 2 * DAY)
        # Nor is a draft saved just then, where the archive is "all mail".
        draft = box.put("Archive", msg("draft@example.org", sender=ME, to="ann@x.org",
                                       day=39.95), date=NOW, flags=("\\Draft",))
        box.fetched.clear()
        mirror.poll()
        recs = by_id(gw)
        assert sorted(recs) == ["own@example.org", "quick@x.org", "seen@x.org"], sorted(recs)
        assert mirror.index.one("SELECT skipped FROM locations WHERE role = 'archive' "
                                "AND uid = ?", (draft,))["skipped"] == "draft"
        quick, own = recs["quick@x.org"], recs["own@example.org"]
        assert quick["type"] == gw._ibstore.T_INBOUND and quick["text"]
        assert own["type"] == gw._ibstore.T_OUTBOUND and own["author"] == "device"
        assert own["chat"] == "quick@x.org"
        places = {row["message_id"]: row["role"] for row in mirror.index.rows(
            "SELECT message_id, role FROM locations WHERE message_id IS NOT NULL")}
        assert places["quick@x.org"] == "archive" and places["own@example.org"] == "archive"
        live = [e for e in inbound(events) if e["message_id"] in ("quick@x.org",
                                                                 "own@example.org")]
        assert [e["direction"] for e in live] == ["in", "out"]
        assert not any("backfill" in e for e in live)
        # The old mail's and the rule-filed mail's headers were at most looked
        # at (the draft was read whole, once: its flag comes with the mail).
        whole = [u for folder, u, limit in box.fetched
                 if folder == "Archive" and limit != gw.HEADER_PEEK_BYTES]
        assert len(whole) == 3
        # Followed from there like any mail; and nothing is read twice.
        fetched = len(box.fetched)
        mirror.poll()
        mirror.poll()
        assert len(box.fetched) == fetched and len(records(gw)) == 3
        box.remove("Archive", box.uid_of("Archive", "quick@x.org"))
        mirror.poll()
        assert by_id(gw)["quick@x.org"]["removed_at"]
        # A known mail archived in the usual way is found, not read again.
        uid = box.put("INBOX", msg("slow@x.org", day=39.9), date=NOW)
        mirror.poll()
        box.move_mail("INBOX", uid, "Archive")
        fetched = len(box.fetched)
        mirror.poll()
        assert len(box.fetched) == fetched and not by_id(gw)["slow@x.org"]["removed_at"]
    with tempfile.TemporaryDirectory() as tmp:
        # The archive refuses just then: the mail is not lost, the next look asks again.
        gw = load(tmp)
        rail(gw)
        mirror, box = started(gw)
        mirror.poll()
        uid = box.put("INBOX", msg("quick@x.org", day=39.9), date=NOW)
        box.move_mail("INBOX", uid, "Archive")
        box.refused = {"Archive"}
        try:
            mirror.poll()
        except gw.MailboxError:
            pass
        assert records(gw) == []
        box.refused = set()
        mirror.poll()
        assert list(by_id(gw)) == ["quick@x.org"]
        # A From is its sender's to write. A mail that came in under the
        # user's own address and was filed away within the minute did not
        # leave Sent: it is not recorded as something the user wrote.
        uid = box.put("INBOX", msg("spoof@evil.org", sender=ME, to="ann@x.org",
                                   body="Please wire the money.\n", day=39.9), date=NOW)
        box.move_mail("INBOX", uid, "Archive")
        mirror.poll()
        spoof = by_id(gw)["spoof@evil.org"]
        assert spoof["type"] == gw._ibstore.T_INBOUND and spoof["author"] is None
        assert mirror.index.get("archive_due") is None and mirror.index.get("archive_own") is None
    print("ok: a mail that came and went between two polls is mirrored from the archive")


def test_a_copy_in_sent_is_the_users_own_whatever_address_it_is_from():
    """MAIL_OWN_ADDRESSES is empty by default and never complete: the user's
    own mail goes out under a plus-address, a send-as alias, a custom-domain
    From. Its copy in Sent says it is theirs; the copy that comes back to
    INBOX (a Cc to oneself, the list's echo) under the same unlisted From
    takes nothing away."""
    alias = "Me <me+shop@example.org>"
    for order in (("Sent", "INBOX"), ("INBOX", "Sent"), ("both",)):
        with tempfile.TemporaryDirectory() as tmp:
            gw = load(tmp)
            assert "me+shop@example.org" not in gw.OWN_ADDRESSES
            events = rail(gw)
            mirror, box = started(gw)
            mail = msg("own1@example.org", sender=alias, to="ann@x.org", cc=ACCOUNT, day=1)
            for folder in order:
                for name in (("Sent", "INBOX") if folder == "both" else (folder,)):
                    box.put(name, mail)
                mirror.poll()
            (record,) = records(gw)
            assert record["type"] == gw._ibstore.T_OUTBOUND, order
            assert record["author"] == "device" and record["sender"] == "me+shop@example.org"
            directions = [e["direction"] for e in inbound(events)]
            # Sent first, the rail hears of it once; INBOX first, it is
            # corrected once the Sent copy shows — never the other way.
            assert directions == (["in", "out"] if order[0] == "INBOX" else ["out"]), directions
            assert mirror.thread("own1@example.org")["peer"]["address"] == "ann@x.org"
            assert mirror.index.one("SELECT COUNT(*) AS n FROM locations")["n"] == 2
            mirror.poll()
            assert len(records(gw)) == 1 and len(inbound(events)) == len(directions)
    with tempfile.TemporaryDirectory() as tmp:
        # A mail someone else wrote, filed under Sent by hand, carries nothing
        # that says it was sent on: Sent's word stands there too, as it did
        # before anything looked at the From.
        gw = load(tmp)
        rail(gw)
        mirror, box = started(gw)
        mail = msg("filed@x.org", day=1)
        box.put("INBOX", mail)
        mirror.poll()
        box.put("Sent", mail)
        mirror.poll()
        assert records(gw)[0]["type"] == gw._ibstore.T_OUTBOUND
    print("ok: a copy in Sent is the user's own mail, from a listed address or not")


def test_a_received_mail_filed_under_sent_is_not_the_users_own():
    for order in (("INBOX", "Sent"), ("Sent", "INBOX")):
        with tempfile.TemporaryDirectory() as tmp:
            gw = load(tmp)
            events = rail(gw)
            mirror, box = started(gw)
            # Ann's mail, and the copy a client keeps under Sent when the
            # user redirects it: the same Message-ID, the same From — and
            # the Resent- fields whoever sends a mail on adds to it.
            mail = msg("a@x.org", day=1)
            sent_on = msg("a@x.org", day=1, headers=[("Resent-From", ME),
                                                     ("Resent-To", "bob@y.org")])
            for folder in order:
                box.put(folder, sent_on if folder == "Sent" else mail)
                mirror.poll()
            recs = records(gw)
            assert len(recs) == 1 and recs[0]["type"] == gw._ibstore.T_INBOUND, order
            assert recs[0]["sender"] == "ann@x.org" and recs[0]["author"] is None
            assert inbound(events)[-1]["direction"] == "in"
            assert mirror.thread("a@x.org")["peer"]["address"] == "ann@x.org"
            # Both copies are places of the one mail.
            assert mirror.index.one("SELECT COUNT(*) AS n FROM locations")["n"] == 2
            mirror.poll()
            assert len(records(gw)) == 1 and len(inbound(events)) <= 2
    with tempfile.TemporaryDirectory() as tmp:
        # Unless someone said who wrote it: an outbound note is the user's word.
        gw = load(tmp)
        rail(gw)
        mirror, box = started(gw)
        mirror.note_author("n@x.org", "agent")
        mail = msg("n@x.org", sender="Shared box <team@x.org>", day=1,
                   headers=[("Resent-From", ME)])
        box.put("Sent", mail)
        mirror.poll()
        box.put("INBOX", mail)
        mirror.poll()
        assert records(gw)[0]["type"] == gw._ibstore.T_OUTBOUND
        assert records(gw)[0]["author"] == "agent"
    with tempfile.TemporaryDirectory() as tmp:
        # Or the mail is From the user: sent on, it is still what they wrote.
        gw = load(tmp)
        rail(gw)
        mirror, box = started(gw)
        mail = msg("mine@example.org", sender=ME, to="ann@x.org", cc=ACCOUNT, day=1,
                   headers=[("Resent-From", ME), ("Resent-To", "bob@y.org")])
        box.put("INBOX", mail)
        mirror.poll()
        box.put("Sent", mail)
        mirror.poll()
        assert records(gw)[0]["type"] == gw._ibstore.T_OUTBOUND
    with tempfile.TemporaryDirectory() as tmp:
        # An id is its sender's to choose, and whoever was sent a mail knows
        # its id: a mail that arrives under the id of one the user sent is
        # not that mail's INBOX copy, and replaces nothing.
        gw = load(tmp)
        events = rail(gw)
        mirror, box = started(gw)
        box.put("Sent", msg("s@example.org", sender=ME, to="mallory@evil.org",
                            body="What I really wrote.\n", day=1))
        mirror.poll()
        box.put("INBOX", msg("s@example.org", sender="Mallory <mallory@evil.org>",
                             body="What I would like you to have written.\n", day=2))
        mirror.poll()
        mirror.poll()
        (record,) = records(gw)
        assert record["type"] == gw._ibstore.T_OUTBOUND and record["sender"] == ACCOUNT
        assert record["text"] == "What I really wrote." and len(inbound(events)) == 1
    print("ok: a mail the user sent on is still its author's, in either order; an id alone "
          "replaces nothing")


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
        assert mirror.backfill_status() == {"done": True, "pending": 0, "days": 0}
        mirror.index.close()
        mirror.threads.close()
        # The window is fixed at the first run. A later setting does not move
        # it — and /health does not claim it did.
        gw = load(tmp, days=365)
        mirror = gw.Mirror(box, clock=lambda: NOW)
        mirror.poll()
        assert list(by_id(gw)) == ["new@x.org"]
        assert mirror.backfill_status() == {"done": True, "pending": 0, "days": 0}
    print("ok: MAIL_BACKFILL_DAYS=0 mirrors only what arrives from then on, and /health "
          "reports the window in force")


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


def test_a_ledger_older_than_the_index_is_followed_again():
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp)
        events = rail(gw)
        mirror, box = started(gw)
        box.put("INBOX", msg("a@x.org", day=1))
        box.put("INBOX", msg("b@x.org", subject="Re: Quarterly numbers", irt="zz@x.org", day=2))
        gone = box.put("INBOX", msg("c@x.org", subject="Other", day=3))
        mirror.poll()
        backup = Path(tmp) / "ledger-backup"
        shutil.copytree(Path(tmp) / "inbound", backup)
        # After the backup: one mail is deleted (a tombstone), two chats merge.
        box.remove("INBOX", gone)
        box.put("INBOX", msg("d@x.org", refs=["a@x.org", "zz@x.org"], irt="b@x.org", day=4))
        mirror.poll()
        recs = by_id(gw)
        assert recs["c@x.org"]["removed_at"] and recs["b@x.org"]["chat"] == "a@x.org"
        mirror.index.close()
        mirror.threads.close()

        # The ledger volume alone comes back from the backup. The index says
        # c is removed and b is in a@x.org; the records say neither.
        shutil.rmtree(Path(tmp) / "inbound")
        shutil.copytree(backup, Path(tmp) / "inbound")
        events.clear()
        mirror = gw.Mirror(box, clock=lambda: NOW)
        while mirror.poll():
            pass
        mirror.poll()
        recs = by_id(gw)
        assert sorted(recs) == ["a@x.org", "b@x.org", "c@x.org", "d@x.org"]
        assert recs["c@x.org"]["removed_at"] and recs["c@x.org"]["body_part"] is None
        assert {m: r["chat"] for m, r in recs.items() if m != "c@x.org"} == {
            "a@x.org": "a@x.org", "b@x.org": "a@x.org", "d@x.org": "a@x.org"}
        kinds = sorted(kind for kind, _p in events)
        assert kinds == ["inbound", "merge", "removed"], kinds   # d again, quietly
        assert [e.get("backfill") for e in inbound(events)] == [True]
        # No blob of the restored, tombstoned-again record is left.
        named = {gw._ibstore.media_id_of(ref) for r in records(gw)
                 for ref in gw._ibstore._media_refs(r)}
        on_disk = {p.name for p in gw._ibstore.media_dir(gw.INBOUND_STORE_DIR).iterdir()
                   if "." not in p.name}
        assert on_disk == named
    print("ok: a ledger restored to an older copy is tombstoned and re-keyed again")


def test_tombstones_and_merges_read_only_their_own_records():
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp)
        rail(gw)
        mirror, box = started(gw)
        uids = [box.put("INBOX", msg(f"m{n}@x.org", subject=f"s{n}", day=1 + n))
                for n in range(6)]
        box.put("INBOX", msg("b@x.org", subject="Re: s0", irt="zz@x.org", day=8))
        mirror.poll()
        # From here on, count every read of a ledger record.
        directory = gw._ibstore.messages_dir(gw.INBOUND_STORE_DIR)
        reads = []
        real_read = Path.read_text

        def read_text(self, *args, **kwargs):
            if self.parent == directory:
                reads.append(self.name)
            return real_read(self, *args, **kwargs)
        scan = gw._ibstore._blob_ids_referenced_elsewhere
        gw._ibstore._blob_ids_referenced_elsewhere = lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("a mail's blobs are its record's own: no scan of the ledger"))
        Path.read_text = read_text
        try:
            for uid in uids[3:]:
                box.remove("INBOX", uid)
            mirror.poll()
            tombstone_reads = list(reads)
            reads.clear()
            box.put("INBOX", msg("c@x.org", refs=["m0@x.org", "zz@x.org"], irt="b@x.org", day=9))
            mirror.poll()
            merge_reads = list(reads)
        finally:
            Path.read_text = real_read
            gw._ibstore._blob_ids_referenced_elsewhere = scan
        recs = by_id(gw)
        assert sorted(m for m, r in recs.items() if r["removed_at"]) == [
            "m3@x.org", "m4@x.org", "m5@x.org"]
        assert recs["b@x.org"]["chat"] == "m0@x.org"
        # Three tombstones read three records; the merge read the one record
        # of the absorbed chat (twice: to move it, and to see that it moved).
        assert len(set(tombstone_reads)) == 3, tombstone_reads
        assert len(set(merge_reads)) == 1, merge_reads
        media = {p.name for p in gw._ibstore.media_dir(gw.INBOUND_STORE_DIR).iterdir()
                 if "." not in p.name}
        named = {gw._ibstore.media_id_of(ref) for r in records(gw)
                 for ref in gw._ibstore._media_refs(r)}
        assert media == named, "the tombstones' blobs are gone, nobody else's"
    print("ok: a tombstone and a merge cost their own records, not a read of the ledger")


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


class _Kill(BaseException):
    """The process dies here: no handler runs, nothing is cleaned up."""


def _killed_at(n, setup, act, check) -> bool:
    """Run ``act`` with the process killed right after its n-th write (to
    either index or to the ledger), restart, let the mirror settle, and
    ``check(n, gw, events, mirror)``. False when ``act`` has fewer writes."""
    tmp = tempfile.mkdtemp()
    try:
        gw = load(tmp)
        events = rail(gw)
        mirror, box = started(gw)
        setup(mirror, box)
        count = {"n": 0}

        def arm(owner, name):
            real = getattr(owner, name)

            def armed(*args, **kwargs):
                out = real(*args, **kwargs)
                count["n"] += 1
                if count["n"] == n:
                    raise _Kill()
                return out
            setattr(owner, name, armed)
            return real
        act(box)
        ledger = ("write_message", "write_outbound", "store_media", "tombstone_message",
                  "rekey_chat", "update_message")
        saved = {name: arm(gw._ibstore, name) for name in ledger}
        for name in ("run", "atomic"):
            arm(mirror.index, name)
        arm(mirror.threads, "resolve")
        try:
            mirror.poll()
            killed = False
        except _Kill:
            killed = True
        finally:
            for name, real in saved.items():
                setattr(gw._ibstore, name, real)
        if not killed:
            return False
        mirror.index.close()
        mirror.threads.close()
        mirror = gw.Mirror(box, clock=lambda: NOW)
        for _ in range(4):
            mirror.poll()
        # Whatever happened: one record per mail, and an index that says what
        # the ledger says.
        recs = records(gw)
        ids = [r["message_id"] for r in recs]
        assert len(ids) == len(set(ids)), (n, ids)
        index = {row["message_id"]: (row["chat"], bool(row["removed"])) for row in
                 mirror.index.rows("SELECT * FROM mails")}
        assert index == {r["message_id"]: (r["chat"], bool(r["removed_at"])) for r in recs}, n
        assert mirror.index.one("SELECT COUNT(*) AS n FROM claims")["n"] == 0, n
        check(n, gw, events, mirror)
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _every_kill_point(setup, act, check) -> int:
    n = 1
    while _killed_at(n, setup, act, check):
        n += 1
    assert n > 5, "the scenario must have writes to be killed after"
    return n - 1


def test_wherever_it_is_killed_one_record_and_the_rail_is_told():
    told = lambda events, kind: [p for k, p in events if k == kind]   # noqa: E731

    # A new mail, with an attachment.
    def check_ingest(n, gw, events, mirror):
        recs = by_id(gw)
        assert list(recs) == ["a@x.org"] and recs["a@x.org"]["text"], n
        mine = [e for e in inbound(events) if e["message_id"] == "a@x.org"]
        assert mine and "backfill" not in mine[0], (n, "a live mail is announced as one")
        assert len(mine[0]["attachments"]) == 1 and mine[0]["chat_name"] == "Quarterly numbers"
    points = _every_kill_point(
        lambda mirror, box: None,
        lambda box: box.put("INBOX", msg("a@x.org", day=1,
                                         files=[("f.pdf", "application/pdf", b"%PDF-1")])),
        check_ingest)

    # Old mail, read by the backfill: announced as old, with its \Seen.
    def check_backfill(n, gw, events, mirror):
        mine = [e for e in inbound(events) if e["message_id"] == "old@x.org"]
        assert mine and mine[0].get("backfill") is True and mine[0].get("seen") is True, n

    def old_mail(mirror, box):
        box.put("INBOX", msg("old@x.org", day=1), flags=["\\Seen"])
        mirror.index.run("UPDATE folders SET ceiling = 9, hwm = 9 WHERE role = 'inbox'")
        mirror.index.set("backfill_done", "0")
    points += _every_kill_point(old_mail, lambda box: None, check_backfill)

    # A mail that links two chats: the merge reaches the ledger AND the rail.
    def two_chats(mirror, box):
        box.put("INBOX", msg("a@x.org", day=1))
        box.put("INBOX", msg("b@x.org", subject="Re: Quarterly numbers", irt="zz@x.org", day=2))
        mirror.poll()

    def check_merge(n, gw, events, mirror):
        assert {r["message_id"]: r["chat"] for r in records(gw)} == {
            "a@x.org": "a@x.org", "b@x.org": "a@x.org", "c@x.org": "a@x.org"}, n
        merges = told(events, "merge")
        assert merges and all(m["from"] == ["zz@x.org"] and m["into"] == "a@x.org"
                              for m in merges), (n, merges)
        assert any(e["message_id"] == "c@x.org" for e in inbound(events)), n
    points += _every_kill_point(
        two_chats,
        lambda box: box.put("INBOX", msg("c@x.org", refs=["a@x.org", "zz@x.org"],
                                         irt="b@x.org", day=3)),
        check_merge)

    # Two mails deleted in a client: both tombstoned, both announced.
    def two_mails(mirror, box):
        box.uids = [box.put("INBOX", msg("a@x.org", day=1)),
                    box.put("INBOX", msg("b@x.org", subject="Other", day=2))]
        mirror.poll()

    def check_removed(n, gw, events, mirror):
        assert all(r["removed_at"] and not r["text"] for r in records(gw)), n
        gone = {mid for p in told(events, "removed") for mid in p["message_ids"]}
        assert gone == {"a@x.org", "b@x.org"}, (n, gone)
        assert all(p["live"] == 0 for p in told(events, "removed")), n
    points += _every_kill_point(
        two_mails, lambda box: [box.remove("INBOX", uid) for uid in box.uids], check_removed)

    # The user's own mail, recorded from its INBOX copy, when the Sent copy shows.
    def inbox_copy(mirror, box):
        box.put("INBOX", msg("s@example.org", sender=ME, to=ACCOUNT, day=1))
        mirror.poll()

    def check_own(n, gw, events, mirror):
        recs = records(gw)
        assert len(recs) == 1 and recs[0]["type"] == gw._ibstore.T_OUTBOUND, n
        assert recs[0]["removed_at"] is None and recs[0]["text"], n
        assert inbound(events)[-1]["direction"] == "out", n
    points += _every_kill_point(
        inbox_copy,
        lambda box: box.put("Sent", msg("s@example.org", sender=ME, to=ACCOUNT, day=1)),
        check_own)
    print(f"ok: killed after any of {points} writes, a mail is recorded once and the "
          f"rail is told of it, of its merge and of its removal")


def test_an_index_write_that_fails_after_the_record_stands():
    import sqlite3
    for failing in ("UPDATE mails SET record", "INSERT INTO outbox",
                    "INSERT OR REPLACE INTO locations"):
        with tempfile.TemporaryDirectory() as tmp:
            gw = load(tmp)
            events = rail(gw)
            mirror, box = started(gw)
            uid = box.put("INBOX", msg("a@x.org", day=1))
            real_run, real_atomic = mirror.index.run, mirror.index.atomic
            state = {"failed": False}

            def full(sql):
                if sql.startswith(failing) and not state["failed"]:
                    state["failed"] = True
                    raise sqlite3.OperationalError("database or disk is full")

            def run(sql, args=()):
                full(sql)
                return real_run(sql, args)

            def atomic(statements):
                for statement in statements:
                    if statement is not None:
                        full(statement[0])
                return real_atomic(statements)
            mirror.index.run, mirror.index.atomic = run, atomic
            try:
                mirror.poll()
                raise AssertionError("the state volume failing must fail the pass")
            except sqlite3.OperationalError:
                pass
            assert state["failed"], failing
            # The same process goes on (no restart): the record is matched to
            # its claim, never written a second time, and the rail hears of it.
            for _ in range(3):
                mirror.poll()
            assert [r["message_id"] for r in records(gw)] == ["a@x.org"], failing
            assert [e["message_id"] for e in inbound(events)] == ["a@x.org"], failing
            box.remove("INBOX", uid)
            mirror.poll()
            assert [bool(r["removed_at"]) for r in records(gw)] == [True], failing
    print("ok: a failed index write after the ledger write leaves one record, found "
          "again without a restart")


def test_a_merge_decided_by_a_mail_whose_ingest_fails_is_still_applied():
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp)
        events = rail(gw)
        mirror, box = started(gw)
        box.put("INBOX", msg("a@x.org", day=1))
        box.put("INBOX", msg("b@x.org", subject="Re: Quarterly numbers", irt="zz@x.org", day=2))
        mirror.poll()
        box.put("INBOX", msg("c@x.org", refs=["a@x.org", "zz@x.org"], irt="b@x.org", day=3))
        real = gw._ibstore.write_message

        def full(*args, **kwargs):
            raise OSError(28, "No space left on device")
        gw._ibstore.write_message = full
        try:
            mirror.poll()
            raise AssertionError("a full disk must fail the pass")
        except OSError:
            pass
        finally:
            gw._ibstore.write_message = real
        # The thread index has merged; the retry is a repeat and reports no
        # merge — it must have been remembered, or b stays under its old key.
        assert mirror.threads.chat_of("b@x.org") == "a@x.org"
        mirror.poll()
        assert {r["message_id"]: r["chat"] for r in records(gw)} == {
            "a@x.org": "a@x.org", "b@x.org": "a@x.org", "c@x.org": "a@x.org"}
        assert [p for kind, p in events if kind == "merge"] == [
            {"channel": "email", "account": ACCOUNT, "from": ["zz@x.org"], "into": "a@x.org"}]
        assert mirror._merges_due == set() and mirror.thread("zz@x.org") is None
    with tempfile.TemporaryDirectory() as tmp:
        # A merge that cannot be made (a fault in the re-keying itself, here)
        # stays due and stops nothing: the mail is mirrored, the next pass too.
        gw = load(tmp)
        events = rail(gw)
        mirror, box = started(gw)
        box.put("INBOX", msg("a@x.org", day=1))
        box.put("INBOX", msg("b@x.org", subject="Re: Quarterly numbers", irt="zz@x.org", day=2))
        mirror.poll()
        box.put("INBOX", msg("c@x.org", refs=["a@x.org", "zz@x.org"], irt="b@x.org", day=3))
        real = gw._ibstore.rekey_chat
        gw._ibstore.rekey_chat = lambda *a, **kw: 1 / 0
        try:
            mirror.poll()
            box.put("INBOX", msg("d@x.org", day=4))
            mirror.poll()
        finally:
            gw._ibstore.rekey_chat = real
        recs = by_id(gw)
        assert sorted(recs) == ["a@x.org", "b@x.org", "c@x.org", "d@x.org"]
        assert recs["b@x.org"]["chat"] == "zz@x.org" and mirror._merges_due == {"zz@x.org"}
        assert not any(kind == "merge" for kind, _p in events)
        mirror.poll()
        assert by_id(gw)["b@x.org"]["chat"] == "a@x.org" and mirror._merges_due == set()
        assert [p["from"] for kind, p in events if kind == "merge"] == [["zz@x.org"]]
    print("ok: a merge survives the failed ingest of the mail that caused it")


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


def test_the_users_other_account_is_not_a_correspondent():
    with tempfile.TemporaryDirectory() as tmp:
        # MAIL_OWN_ADDRESSES is every address of the user's that can appear
        # on this mailbox's mail: aliases, and their other mirrored accounts.
        gw = load(tmp, MAIL_OWN_ADDRESSES="alias@example.org, Me@Other.example")
        assert gw.OWN_ADDRESSES == {ACCOUNT, "alias@example.org", "me@other.example"}
        events = rail(gw)
        mirror, box = started(gw)
        # Ann writes to this account, copying the other one …
        box.put("INBOX", msg("a@x.org", cc="Me Elsewhere <me@other.example>", day=1))
        mirror.poll()
        thread = mirror.thread("a@x.org")
        assert thread["participants"] == [{"address": "ann@x.org", "name": "Ann Example"}]
        assert thread["group"] is False
        # … and the user answers from there; the answer lands in this INBOX.
        box.put("INBOX", msg("r@other.example", sender="Me Elsewhere <me@other.example>",
                             to="ann@x.org", cc=ACCOUNT, subject="Re: Quarterly numbers",
                             refs=["a@x.org"], irt="a@x.org", day=2))
        mirror.poll()
        thread = mirror.thread("a@x.org")
        assert thread["messages"] == 2 and thread["group"] is False
        assert thread["participants"] == [{"address": "ann@x.org", "name": "Ann Example"}]
        assert thread["peer"] == {"address": "ann@x.org", "name": "Ann Example"}
        last = inbound(events)[-1]
        assert last["group"] is False and last["peer"]["address"] == "ann@x.org"
    print("ok: a thread that reaches two of the user's accounts has the user in it once")


def test_one_mail_cannot_weld_many_chats_or_choose_the_survivor():
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp)
        events = rail(gw)
        mirror, box = started(gw)
        count = 12
        for n in range(count):
            box.put("INBOX", msg(f"t{n}@x.org", sender=f"P{n} <p{n}@x.org>",
                                 subject=f"Thread {n}", day=10 + n))
        mirror.poll()
        assert len(mirror.threads.chats()) == count
        # A sender who knows the ids cites them all — from a mail dated
        # before every one of them, with a subject of their choosing.
        box.put("INBOX", msg("evil0@evil.org", sender="Mallory <m@evil.org>",
                             subject="URGENT: wire transfer", day=-9000), date=NOW)
        box.put("INBOX", msg("evil1@evil.org", sender="Mallory <m@evil.org>",
                             refs=["evil0@evil.org"] + [f"t{n}@x.org" for n in range(count)],
                             irt=f"t{count - 1}@x.org", subject="x", day=-9000), date=NOW)
        mirror.poll()
        # Nothing was merged: every chat is still itself, under its own name.
        assert len(mirror.threads.chats()) == count + 1
        assert not any(kind == "merge" for kind, _p in events)
        for n in range(count - 1):
            thread = mirror.thread(f"t{n}@x.org")
            assert thread["messages"] == 1 and thread["name"] == f"Thread {n}"
        # The mail is in the chat of the mail it answers, and nowhere else.
        recs = by_id(gw)
        assert recs["evil1@evil.org"]["chat"] == f"t{count - 1}@x.org"
        assert recs["evil0@evil.org"]["chat"] == "evil0@evil.org"
        assert {r["chat"] for m, r in recs.items() if m.startswith("t")} == {
            f"t{n}@x.org" for n in range(count)}

        # A merge within the bound goes by when the server received the mails,
        # not by the Date a sender wrote: the backdated mail's chat does not
        # survive, and its subject does not name the thread.
        box.put("INBOX", msg("link@evil.org", sender="Mallory <m@evil.org>", subject="x",
                             refs=["evil0@evil.org", "t0@x.org"], day=-9000), date=NOW)
        mirror.poll()
        merge = [p for kind, p in events if kind == "merge"]
        assert merge == [{"channel": "email", "account": ACCOUNT,
                          "from": ["evil0@evil.org"], "into": "t0@x.org"}], merge
        assert mirror.thread("t0@x.org")["name"] == "Thread 0"
        assert by_id(gw)["evil0@evil.org"]["chat"] == "t0@x.org"
        # The record still shows the date the mail claims; only threading ignores it.
        assert by_id(gw)["link@evil.org"]["received_at"] == gw._ibstore._iso(T0 - 9000 * DAY)
        assert by_id(gw)["link@evil.org"]["arrived_at"] == gw._ibstore._iso(NOW)
    with tempfile.TemporaryDirectory() as tmp:
        # Nor after the state volume was lost: the record keeps when the
        # server received each mail, and the rebuilt index goes by that.
        gw = load(tmp)
        events = rail(gw)
        mirror, box = started(gw)
        box.put("INBOX", msg("t0@x.org", subject="Lunch on Friday", day=10))
        gone = box.put("INBOX", msg("t1@x.org", subject="Dinner", day=11))
        mirror.poll()
        box.put("INBOX", msg("evil0@evil.org", sender="Mallory <m@evil.org>",
                             subject="URGENT: wire transfer", day=-9000), date=NOW)
        mirror.poll()
        box.remove("INBOX", gone)       # a tombstone keeps its arrival too
        mirror.poll()
        assert by_id(gw)["t1@x.org"]["removed_at"]
        mirror.index.close()
        mirror.threads.close()
        shutil.rmtree(Path(tmp) / "data")
        mirror = gw.Mirror(box, clock=lambda: NOW)
        assert mirror.threads.earliest("evil0@evil.org") == NOW
        assert mirror.threads.earliest("t1@x.org") == T0 + 11 * DAY
        assert mirror.index.one("SELECT arrived FROM mails WHERE message_id = 'evil0@evil.org'")[
            "arrived"] == NOW
        mirror.poll()
        box.put("INBOX", msg("link@evil.org", sender="Mallory <m@evil.org>", subject="x",
                             refs=["evil0@evil.org", "t0@x.org", "t1@x.org"], day=-9000),
                date=NOW)
        while mirror.poll():
            pass
        recs = by_id(gw)
        assert {r["chat"] for r in recs.values()} == {"t0@x.org"}, recs
        assert mirror.thread("t0@x.org")["name"] == "Lunch on Friday"
        assert [p["into"] for kind, p in events if kind == "merge"] == ["t0@x.org"]
    with tempfile.TemporaryDirectory() as tmp:
        # And a References header of any length cites a bounded number of ids.
        gw = load(tmp)
        rail(gw)
        mirror, box = started(gw)
        many = [f"r{n}@x.org" for n in range(3000)]
        box.put("INBOX", msg("long@x.org", refs=many, irt="r2999@x.org", day=1))
        mirror.poll()
        record = by_id(gw)["long@x.org"]
        kept = record["references"].split()
        assert len(kept) == gw._threads.MAX_CITED_IDS
        assert kept[0] == "r0@x.org" and kept[-1] == "r2999@x.org" and kept[1] == "r2901@x.org"
        assert record["chat"] == "r0@x.org" and record["in_reply_to"] == "r2999@x.org"
        ids = mirror.threads._conn.execute("SELECT COUNT(*) FROM ids").fetchone()[0]
        assert ids == gw._threads.MAX_CITED_IDS + 1
    print("ok: one mail joins a few chats at most, cites a bounded number of ids, and "
          "cannot date itself into owning a thread")


def test_every_cited_id_is_on_record_for_a_rebuild():
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp)
        rail(gw)
        mirror, box = started(gw)
        # In-Reply-To may name several parents; neither is mirrored yet.
        box.put("INBOX", msg("b@x.org", subject="Re: Both", day=3,
                             headers=[("In-Reply-To", "<p1@x.org> <p2@x.org>")]))
        mirror.poll()
        record = by_id(gw)["b@x.org"]
        assert record["chat"] == "p1@x.org" and record["in_reply_to"] == "p1@x.org"
        assert record["references"] == "p2@x.org"
        assert mirror.threads.chat_of("p2@x.org") == "p1@x.org"
        mirror.index.close()
        mirror.threads.close()
        # The state volume is lost; the index rebuilt from the record knows
        # both parents, so the second one still joins the chat when it arrives.
        shutil.rmtree(Path(tmp) / "data")
        mirror = gw.Mirror(box, clock=lambda: NOW)
        assert mirror.threads.chat_of("p2@x.org") == "p1@x.org"
        box.put("INBOX", msg("p2@x.org", subject="Both", day=2))
        while mirror.poll():
            pass
        assert by_id(gw)["p2@x.org"]["chat"] == "p1@x.org"
        assert len(mirror.threads.chats()) == 1
    print("ok: a thread index rebuilt from the records cites what the live one cited")


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
    with tempfile.TemporaryDirectory() as tmp:
        # Nothing that is not text leaves by the rail, whatever got into an
        # event: the receiver writes what it is sent into files.
        gw = load(tmp)
        events = rail(gw)
        mirror, _box = started(gw)
        mirror.index.atomic([gw.Mirror._event(
            "inbound", {"text": "a\ud800b", "participants": [{"name": "\udfff", "address": "é@x"}]})])
        mirror.poll()
        assert events == [("inbound", {"text": "a?b", "participants": [
            {"name": "?", "address": "é@x"}]})]
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
        # The record's text is bounded too, further out: one literal that
        # every reader of the record parses. The whole text is the body blob.
        gw.MAX_RECORD_TEXT_CHARS = 2000
        body = "\n".join(f"Line {n} of a very long report." for n in range(400)) + "\n"
        box.put("INBOX", msg("long@x.org", body=body, day=2))
        mirror.poll()
        record = by_id(gw)["long@x.org"]
        assert len(record["text"]) <= 2000 + 2 and record["text"].endswith("\n…")
        assert record["text"].startswith("Line 0 of") and record["trim"] == "wanting"
        assert blob(gw, record["body_part"]).decode() == body
    print("ok: an event carries a bounded text, the record a larger bounded one, the blob all")


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
        assert snap["blocked"] is None
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
        # A deployment that pinned its built-in channels without e-mail runs
        # no mirror: the retinue side has no entry for this gateway then.
        gw = load(tmp, MESSENGER_BUILTIN_CHANNELS="signal, whatsapp,telegram")
        snap = gw._health_snapshot()
        assert snap["configured"] is False and snap["account"] is None
        assert "MESSENGER_BUILTIN_CHANNELS" in snap["error"]
        assert gw.CONFIGURED is False and gw.MAIL_CONFIG is None
        assert load(tmp, MESSENGER_BUILTIN_CHANNELS="")._health_snapshot()["configured"] is False
        for pinned in ("signal,email", " email "):
            assert load(tmp, MESSENGER_BUILTIN_CHANNELS=pinned).CONFIGURED is True, pinned
        assert load(tmp).CONFIGURED is True            # unset: every built-in
        # A named account is enrolled through MESSENGER_GATEWAYS, not the pin.
        gw = load(tmp, configured=False, MESSENGER_BUILTIN_CHANNELS="signal",
                  MAIL_ACCOUNT_NAME="work", EMAIL_USER_WORK="w@example.org",
                  EMAIL_PASS_WORK="x", IMAP_HOST_WORK="imap.example.org",
                  SMTP_HOST_WORK="smtp.example.org")
        assert gw.CONFIGURED is True and gw.ACCOUNT == "w@example.org"
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp)
        _write_policy(gw)
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
        assert snap["blocked"] is None
        gw._loop_step(gw.MIRROR, loop)
        assert gw._health_snapshot()["connected"] is True
        # An internal error is a health state too, never a dead loop — and
        # it is the mirror's own, said so: the mailbox answered, and nobody
        # is to be sent to check a password over it.
        gw.MIRROR.poll = lambda **kw: 1 / 0
        assert gw._loop_step(gw.MIRROR, loop) == gw.MAIL_POLL_SECONDS
        snap = gw._health_snapshot()
        assert snap["connected"] is False and "internal error" in snap["error"]
        assert snap["blocked"] == "mirror" and "division by zero" in snap["error"]
        json.dumps(snap)
    print("ok: health reports unconfigured, misconfigured, connected and failed")


def test_a_fault_of_the_mirror_is_tried_again_less_and_less_often():
    """While a fault of the mirror's own lasts, every turn of the loop
    fetches the mail in hand whole again before it gets to where it failed,
    and nothing changes between two turns that fail the same way: a full
    disk and one mail of 18 MB were 180 MB off the server in ten minutes,
    and at the fetch limit 72 GB a day. The wait doubles with every such
    failure in a row, up to an hour; a pass that goes through resets it."""
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp)
        _write_policy(gw)
        rail(gw)
        box = FakeMailbox(gw)
        gw.MIRROR = mirror = gw.Mirror(box, clock=lambda: NOW)
        loop: dict = {}
        assert gw._loop_step(mirror, loop) <= gw.MAIL_POLL_SECONDS == 60.0
        box.put("INBOX", msg("big@x.org", day=39.9))
        real = gw._ibstore.store_media
        gw._ibstore.store_media = lambda *a, **kw: (_ for _ in ()).throw(
            OSError(28, "No space left on device"))
        try:
            loop.pop("synced")
            waits = [gw._loop_step(mirror, loop) for _ in range(9)]
            assert waits == [60.0, 120.0, 240.0, 480.0, 960.0, 1920.0, 3600.0, 3600.0, 3600.0]
            assert gw.MIRROR_FAULT_WAIT_MAX_SECONDS == 3600.0
            # (Each try still fetches the mail once: the saving is the wait.)
            assert len(box.fetched) == 9 and records(gw) == []
            snap = gw._health_snapshot()
            assert snap["connected"] is False and snap["blocked"] == "mirror"
            assert "No space left" in snap["error"]
            # A mailbox that does not answer is another matter: it is asked
            # again after one interval, and is neither such a failure nor a
            # pass that went through.
            box.fail = gw.MailboxError("the IMAP connection failed: timed out")
            assert gw._loop_step(mirror, loop) == gw.MAIL_POLL_SECONDS
            assert gw._health_snapshot()["blocked"] is None
            assert gw._loop_step(mirror, loop) == 3600.0
        finally:
            gw._ibstore.store_media = real
        # The disk has room again: the pass goes through, and the next
        # fault of the mirror starts at one interval.
        assert gw._loop_step(mirror, loop) <= gw.MAIL_POLL_SECONDS
        assert gw._health_snapshot()["connected"] is True and len(records(gw)) == 1
        mirror.poll = lambda **kw: 1 / 0
        assert [gw._loop_step(mirror, loop) for _ in range(2)] == [60.0, 120.0]
    with tempfile.TemporaryDirectory() as tmp:
        # Never less than the poll interval a deployment set, however long.
        gw = load(tmp, MAIL_POLL_SECONDS=7200)
        _write_policy(gw)
        rail(gw)
        gw.MIRROR = mirror = gw.Mirror(FakeMailbox(gw), clock=lambda: NOW)
        mirror.poll = lambda **kw: 1 / 0
        assert [gw._loop_step(mirror, {}) for _ in range(2)] == [7200.0, 7200.0]
        loop = {}
        assert [gw._loop_step(mirror, loop) for _ in range(3)] == [7200.0, 7200.0, 7200.0]
    print("ok: after a fault of its own the mirror waits twice as long each time, up to "
          "an hour, and a pass that goes through resets the wait")


def test_missing_folders_are_reported():
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp)
        _write_policy(gw)
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
        gw = load(tmp)
        _write_policy(gw)
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
        # Nothing set: the wait has no end of its own. A backfill read with no
        # flags makes a chat of every newsletter, and they stay.
        gw = load(tmp)
        assert gw.MAIL_POLICY_WAIT_SECONDS == 0
        rail(gw)
        box = FakeMailbox(gw)
        box.put("INBOX", msg("a@x.org", day=1))
        gw.MIRROR = gw.Mirror(box, clock=lambda: NOW)
        loop: dict = {}
        delay = gw._loop_step(gw.MIRROR, loop)
        snap = gw._health_snapshot()
        assert snap["connected"] is True and "no e-mail policy file" in snap["error"]
        assert snap["blocked"] == "policy"
        assert records(gw) == [] and delay <= 5.0 and box.fetched == [] and box.listed == []
        # Hours later it still waits — and is a fault by now, which the
        # gateway monitor reports: nothing is being mirrored. But it is the
        # deployment's setup that is waited for, not the mailbox: /health
        # names that, and says where to look.
        loop["started"] -= 6 * 3600
        gw._loop_step(gw.MIRROR, loop)
        snap = gw._health_snapshot()
        assert snap["connected"] is False and "no e-mail policy file" in snap["error"]
        assert snap["blocked"] == "policy" and snap["needs_repair"] is False
        assert "the mailbox is reachable" in snap["error"]
        assert "TRIAGE_EMAIL_WHITELIST_PATH" in snap["error"] and "start log" in snap["error"]
        assert records(gw) == [] and box.listed == []
        # What the connection monitor makes of exactly this answer: a gateway
        # held by its setup, in the gateway's own words — no IMAP outage.
        spec = importlib.util.spec_from_file_location("gateway_monitor_for_mail",
                                                      SCRIPTS_DIR / "gateway-monitor.py")
        monitor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(monitor)
        verdict, reason = monitor.classify_health(200, json.loads(json.dumps(snap)))
        assert (verdict, reason) == ("blocked", snap["error"])
        notice = monitor.outage_message("E-mail", reason, channel="email", blocked=True)
        assert snap["error"] in notice and "mail server" not in notice
        assert "password" not in notice and "provider" not in notice
        # A file that cannot be read is no policy either.
        gw.POLICY_PATH.parent.mkdir(parents=True)
        gw.POLICY_PATH.write_bytes(b"\xff\xfe not text")
        gw._loop_step(gw.MIRROR, loop)
        snap = gw._health_snapshot()
        assert records(gw) == [] and "cannot be read" in snap["error"]
        assert snap["connected"] is False and snap["blocked"] == "policy"
        # A mailbox that fails meanwhile is the mailbox's fault, and said so.
        box.fail = gw.MailboxError("IMAP login failed for me@example.org: bad credentials")
        gw._loop_step(gw.MIRROR, loop)
        snap = gw._health_snapshot()
        assert snap["connected"] is False and snap["blocked"] is None
        assert "login failed" in snap["error"]
        assert monitor.classify_health(200, snap)[0] == "down"
        # The retinue side writes the file (empty: nothing to flag) and the mirror starts.
        gw.POLICY_PATH.write_text("")
        gw._loop_step(gw.MIRROR, loop)
        snap = gw._health_snapshot()
        assert len(records(gw)) == 1 and snap["error"] is None and snap["connected"] is True
        assert snap["blocked"] is None and gw.MIRROR.backfill_done()
        # Which mail is news is decided when it is first read, backfill or
        # not: with the file gone again, new mail waits too, and is read —
        # with the flags — once the file is back.
        gw.POLICY_PATH.unlink()
        box.put("INBOX", msg("n@news.org", sender="News <n@news.org>", day=39))
        loop.clear()
        gw._loop_step(gw.MIRROR, loop)
        assert len(records(gw)) == 1 and "no e-mail policy file" in gw._health_snapshot()["error"]
        _write_policy(gw, news={"n@news.org"})
        gw._loop_step(gw.MIRROR, loop)
        assert len(records(gw)) == 1 and gw._health_snapshot()["error"] is None
        assert gw.MIRROR.index.one("SELECT skipped FROM locations WHERE message_id = "
                                   "'n@news.org'")["skipped"] == "news"
    with tempfile.TemporaryDirectory() as tmp:
        # The opt-out, for a deployment that keeps no policy file: a positive
        # MAIL_POLICY_WAIT_SECONDS proceeds, as with no flags, after that long.
        gw = load(tmp, MAIL_POLICY_WAIT_SECONDS=300)
        rail(gw)
        box = FakeMailbox(gw)
        box.put("INBOX", msg("a@x.org", day=1))
        mirror = gw.Mirror(box, clock=lambda: NOW)
        loop = {}
        gw._loop_step(mirror, loop)
        assert records(gw) == []
        loop["started"] -= 301
        gw._loop_step(mirror, loop)
        assert len(records(gw)) == 1
        # And goes on so, for as long as it runs.
        box.put("INBOX", msg("b@x.org", day=2))
        loop.pop("synced")
        gw._loop_step(mirror, loop)
        assert len(records(gw)) == 2
    print("ok: the mirror waits for the policy file — without end, unless told otherwise")


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
    print("ok: the HTTP API is token-gated, serves threads and media, and refuses to write")


def test_without_a_token_nothing_is_mirrored_or_served():
    with tempfile.TemporaryDirectory() as tmp:
        # The mailbox credentials are there (a deployment that used
        # email_client.py has them), the token is not: closed, not open.
        gw = load(tmp, token="")
        server, base = serve(gw)
        try:
            for path in ("/recent-chats", "/threads/x", "/media/" + "0" * 32, "/pending-sends"):
                assert call(base + path)[0] == 401, path
                assert call(base + path, token="anything")[0] == 401, path
            for path in ("/send", "/outbound-note", "/chats/delete"):
                assert call(base + path, body=b"{}", token="anything")[0] == 401, path
            # /health answers, and says what to do. Unconfigured, not
            # disconnected: the token is the opt-in, and an upgrade that set
            # nothing must not have the monitor report an outage.
            status, _h, body = call(f"{base}/health")
            snap = json.loads(body)
            assert status == 200 and snap["configured"] is False and snap["connected"] is False
            assert "MAIL_GATEWAY_TOKEN" in snap["error"] and ".env" in snap["error"]
            assert snap["account"] is None and snap["status"] == "ok"
        finally:
            server.shutdown()
        # And main() builds no mirror: the mailbox is not even logged in to.
        built = []
        gw.Mirror = lambda *a, **kw: built.append(a)
        gw._serve_http = lambda: None
        gw.main()
        assert gw.MIRROR is None and built == []
        assert not (Path(tmp) / "inbound").exists()
    with tempfile.TemporaryDirectory() as tmp:
        # An unconfigured gateway is still just that, token or none.
        gw = load(tmp, token="", configured=False)
        snap = gw._health_snapshot()
        assert snap["configured"] is False and "EMAIL_USER" in snap["error"]
        # A mailbox half set up and no token: the opt-in was not taken, so it
        # is the token that is named, not a fault in a mirror nobody asked for.
        snap = load(tmp, token="", configured=False,
                    EMAIL_USER="me@example.org")._health_snapshot()
        assert snap["configured"] is False and "MAIL_GATEWAY_TOKEN" in snap["error"]
    print("ok: with no MAIL_GATEWAY_TOKEN the gateway neither mirrors nor serves, and says why")


def test_a_token_that_is_not_ascii_is_just_a_wrong_token():
    with tempfile.TemporaryDirectory() as tmp:
        gw = load(tmp)
        server, base = serve(gw)
        try:
            # A byte above 0x7f in the header used to raise in the comparison:
            # no answer, a traceback per request. It is a wrong token.
            assert call(f"{base}/recent-chats", token="s\xe9krit")[0] == 401
            assert call(f"{base}/outbound-note", body=b"{}", token="\xff\xfe")[0] == 401
            assert call(f"{base}/recent-chats", token=TOKEN)[0] == 200
        finally:
            server.shutdown()
    with tempfile.TemporaryDirectory() as tmp:
        # And a deployment whose own token is not ASCII is served by it: the
        # peer sends its UTF-8 bytes, which is what is compared.
        gw = load(tmp, token="sékrit-ł")
        server, base = serve(gw)
        try:
            sent = "sékrit-ł".encode("utf-8").decode("latin-1")
            assert call(f"{base}/recent-chats", token=sent)[0] == 200
            assert call(f"{base}/recent-chats", token="s\xe9krit-l")[0] == 401
        finally:
            server.shutdown()
    print("ok: a bearer token with non-ASCII bytes is refused (or accepted) like any other")


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
