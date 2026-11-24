#!/usr/bin/env python3
"""The mail gateway's write half (scripts/mail-gateway.py): sending, the
pending-send store, and the mailbox following the chat.

Against the fake mailbox of tests/test_mail_gateway.py and a scripted SMTP
server on a socket pair, spoken to by Python's real ``smtplib`` through
``email_client``'s own send helper — so what is pinned is what would be on
the wire: the envelope (who the server is told the mail is for) and the
message, byte for byte.

  * replies, built from the gateway's own records: 1:1, a group answered to
    all and to the sender only, Reply-To, the user's own mail as the latest,
    a tombstone as the latest, ids that cannot stand in a header, ids longer
    than a line; new mail;
  * the send policy — verify, trust, allow — with and without the user's
    press, and that ``author`` grants nothing;
  * the pending store: queue, approve, reject, retract, the listing and its
    chat filter, the approval link, the origin thread;
  * one record per sent mail whichever of the record and the Sent copy the
    mirror meets first, the rail told once, the grace before a sent mail may
    be called gone;
  * a send stopped at every write, and restarted: never sent twice, and what
    the server took has its record;
  * read, archive and delete against the mailbox, with a server that cannot
    move, one that names no UIDs, a folder renumbered underneath, a move that
    fails halfway; the erase capability; read and archive limited to the
    mails the caller counted; a delete that erases nothing while a mail is
    neither in the Trash nor known to be gone; no Trash is ever invented;
  * input that tries to add a header or a recipient — an address is one
    address of a stated grammar or it is refused, and no mail leaves whose
    headers and envelope disagree;
  * the answer: given when the SMTP server has the mail and waiting for
    nothing else; a ``request_key`` that makes a repeat answer the first
    one's outcome; a failed send listed with its reason until dismissed;
    refused recipients said everywhere; ``/reply-preview``;
  * a provider that files the Sent copy under a Message-ID of its own;
  * all of it on the mirror's own mechanisms: every mail under an id looked
    at, a placeholder's own copy told by its header block, header blocks
    read as guarded as mail, a record written again in its own place, a
    record lost from the ledger looked for under every id it had.

    python3 tests/test_mail_gateway_send.py
"""
import base64
import email
import email.policy
import importlib.util
import json
import os
import shutil
import smtplib
import socket
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("mail_gateway_helpers",
                                               HERE / "test_mail_gateway.py")
t = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(t)

ACCOUNT, ME, DAY, NOW = t.ACCOUNT, t.ME, t.DAY, t.NOW
HOST = "mail-gateway:8096"
ERASE = "erase-token"
_REAL_SMTP = smtplib.SMTP
_FOREIGN_ENV = ("SEND_APPROVAL_BASE_URL", "SEND_APPROVAL_SLUG", "CHAT_ERASE_TOKEN",
                "REPLY_TOKEN_KEY")


class MiniSmtp:
    """Just enough of an SMTP submission server: it takes mail, and can be
    told to refuse a recipient, to refuse the message, or to hang up at a
    chosen moment. ``mails`` is what it took — each with the envelope it was
    given, which is what decides where a mail goes."""

    def __init__(self):
        self.mails: list = []       # {"from", "rcpt": [...], "data": bytes}
        self.sessions = 0
        self.logins: list = []
        self.refuse_rcpt: set = set()
        self.refuse_login = False
        self.reject_data = None     # the answer to the message instead of 250
        # Hang up without an answer: "mail" (before anything of the message),
        # "dot" (the whole message arrived; `takes` says whether it is kept).
        self.drop = None
        self.takes = True
        self.fail_quit = False

    def client(self):
        server = self

        class Loopback(_REAL_SMTP):
            def _get_socket(self, host, port, timeout):
                if server.drop == "connect":
                    raise ConnectionRefusedError(111, "Connection refused")
                ours, theirs = socket.socketpair()
                ours.settimeout(timeout)
                threading.Thread(target=server._serve, args=(theirs,), daemon=True).start()
                return ours

            def starttls(self, *args, **kwargs):
                return 220, b"ready"    # the wire's privacy is not what is under test
        return Loopback

    def _serve(self, sock) -> None:
        self.sessions += 1
        reader = sock.makefile("rb")

        def send(line):
            sock.sendall(line.encode() + b"\r\n")
        try:
            send("220 mini ESMTP")
            sender, rcpt = None, []
            while True:
                line = reader.readline()
                if not line:
                    return
                command = line.rstrip(b"\r\n").decode()
                verb = command.split(" ", 1)[0].upper()
                if verb in ("EHLO", "HELO"):
                    sock.sendall(b"250-mini\r\n250 AUTH PLAIN LOGIN\r\n")
                elif verb == "AUTH":
                    self.logins.append(command)
                    send("535 5.7.8 authentication failed" if self.refuse_login
                         else "235 2.7.0 ok")
                elif verb == "MAIL":
                    if self.drop == "mail":
                        sock.close()
                        return
                    sender, rcpt = command.split(":", 1)[1].split()[0].strip("<>"), []
                    send("250 ok")
                elif verb == "RCPT":
                    address = command.split(":", 1)[1].strip().strip("<>")
                    if address in self.refuse_rcpt:
                        send("550 5.1.1 no such user")
                    else:
                        rcpt.append(address)
                        send("250 ok")
                elif verb == "DATA":
                    send("354 go ahead")
                    data = b""
                    while not data.endswith(b"\r\n.\r\n"):
                        chunk = reader.readline()
                        if not chunk:
                            return
                        data += chunk
                    body = data[:-3].replace(b"\r\n..", b"\r\n.")
                    if self.drop == "dot":
                        if self.takes:
                            self.mails.append({"from": sender, "rcpt": rcpt, "data": body})
                        sock.close()
                        return
                    if self.reject_data:
                        send(self.reject_data)
                    else:
                        self.mails.append({"from": sender, "rcpt": rcpt, "data": body})
                        send("250 2.0.0 queued")
                elif verb == "RSET":
                    send("250 ok")
                elif verb == "QUIT":
                    send("421 going down" if self.fail_quit else "221 bye")
                    return
                else:
                    send("500 what")
        except OSError:
            return
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def parsed(self, index=-1):
        return email.message_from_bytes(self.mails[index]["data"], policy=email.policy.default)


class World:
    """One gateway under test: its module, mirror, fake mailbox, the rail's
    events and the SMTP server its sends reach."""

    def __init__(self, tmp, *, policy="allow", save_sent=True, folders=None, **env):
        for name in _FOREIGN_ENV:
            os.environ.pop(name, None)
        if policy is not None:
            env["EMAIL_SEND_POLICY"] = json.dumps([{"address": "ME@example.org",
                                                    "category": policy}])
        if not save_sent:
            env["SMTP_SAVE_SENT"] = "false"
        env.setdefault("EMAIL_FROM_NAME", "Me Myself")
        self.tmp = tmp
        self.gw = gw = t.load(tmp, **env)
        self.events = t.rail(gw)
        # (A mailbox has a Trash; the gateway never makes one.)
        self.mirror, self.box = t.started(gw, t.FakeMailbox(
            gw, folders=folders or ("INBOX", "Sent", "Archive", "Trash")))
        gw.MIRROR = self.mirror
        self.smtp = MiniSmtp()
        smtplib.SMTP = self.smtp.client()
        gw._now = lambda: NOW
        # An approval is carried out in the background; here, inline.
        gw._in_background = lambda send, request_id: send(request_id)

    def send(self, **payload):
        payload.setdefault("message", "Thanks.")
        return self.gw._accept_send(payload, HOST)

    def told(self):
        """Let the mirror loop do what a send left it, and return every
        outbound mail the rail has heard of."""
        self.gw._between_polls(self.mirror)
        return [p for kind, p in self.events if kind == "inbound" and p["direction"] == "out"]

    def sent_copies(self, message_id=None):
        return [uid for uid, mail in sorted(self.box.boxes["Sent"]["mails"].items())
                if message_id is None or
                (email.message_from_bytes(mail["raw"]).get("Message-ID") or "")
                .strip("<>") == message_id]

    def restart(self):
        """The process ends here and starts again: nothing in memory
        survives, and a start does what main() does for sends."""
        gw = self.gw
        self.mirror.index.close()
        self.mirror.threads.close()
        gw.SENDS = gw.Sends(gw.MAIL_PENDING_SENDS_DIR)
        self.mirror = gw.MIRROR = gw.Mirror(self.box, clock=lambda: NOW)
        gw._recover_sends()
        return self.mirror


def world(**kwargs):
    tmp = tempfile.mkdtemp()
    return World(tmp, **kwargs)


def done(w):
    smtplib.SMTP = _REAL_SMTP
    try:
        w.mirror.index.close()
        w.mirror.threads.close()
    except Exception:  # noqa: BLE001
        pass
    shutil.rmtree(w.tmp, ignore_errors=True)


def later(seconds: float) -> float:
    """``day=`` for a mail written this many seconds after the tests' NOW."""
    return (NOW - t.T0 + seconds) / DAY


def header(message, name):
    """A header of a sent mail, unfolded."""
    value = message.get(name)
    return None if value is None else " ".join(str(value).split())


# --------------------------------------------------------------------------
# Replies
# --------------------------------------------------------------------------

def test_a_reply_in_a_one_to_one_thread():
    w = world()
    try:
        gw, box, mirror, smtp = w.gw, w.box, w.mirror, w.smtp
        box.put("INBOX", t.msg("a@x.org", day=1))
        mirror.poll()
        fetched = len(box.fetched)
        status, answer = w.send(recipient="a@x.org", message="Thanks.\r\nAll good.",
                                author="user", user_approved=True)
        assert status == 200, answer
        mid = answer["message_id"]
        assert answer == {"status": "sent", "message_id": mid, "chat": "a@x.org",
                          "ts": NOW, "attachments": []}
        assert mid.endswith("@example.org") and "<" not in mid

        # What the server was told, and what it was given.
        assert len(smtp.mails) == 1 and smtp.sessions == 1
        assert smtp.mails[0]["from"] == "Me@Example.org"
        assert smtp.mails[0]["rcpt"] == ["ann@x.org"]
        sent = smtp.parsed()
        assert header(sent, "From") == "Me Myself <Me@Example.org>"
        assert header(sent, "To") == "ann@x.org" and sent.get("Cc") is None
        assert sent.get("Bcc") is None
        assert header(sent, "Subject") == "Re: Quarterly numbers"
        assert header(sent, "Message-Id") == f"<{mid}>"
        assert header(sent, "In-Reply-To") == "<a@x.org>"
        assert header(sent, "References") == "<a@x.org>"
        assert sent.get("Date") is not None
        assert sent.get_content().replace("\r\n", "\n") == "Thanks.\nAll good.\n"

        # The record, at once: the message itself, by its author.
        record = t.by_id(gw)[mid]
        assert record["type"] == gw._ibstore.T_OUTBOUND and record["author"] == "user"
        assert record["chat"] == "a@x.org" and record["account"] == ACCOUNT
        assert record["text"] == "Thanks.\nAll good." and record["trim"] == "deterministic"
        assert record["sender"] == ACCOUNT and record["sender_name"] == "Me Myself"
        assert record["recipients"] == ["ann@x.org"] and record["cc"] == []
        assert record["subject_line"] == "Re: Quarterly numbers"
        assert record["in_reply_to"] == "a@x.org" and record["references"] == "a@x.org"
        assert record["sent_at"] == gw._ibstore._iso(NOW)
        assert t.blob(gw, record["body_part"]) == b"Thanks.\nAll good."
        head = t.blob(gw, record["header_part"]).decode()
        assert f"Message-Id: <{mid}>" in head and "In-Reply-To: <a@x.org>" in head
        assert "\n\n" not in head.strip() and "Thanks." not in head

        # The rail is told by the mirror loop, once.
        told = w.told()
        assert len(told) == 1 and told[0]["message_id"] == mid, told
        assert told[0]["author"] == "user" and told[0]["chat"] == "a@x.org"
        assert told[0]["text"] == "Thanks.\nAll good." and told[0]["ts"] == NOW
        assert told[0]["peer"] == {"address": "ann@x.org", "name": "Ann Example"}
        assert "backfill" not in told[0] and told[0]["subject"] == "Re: Quarterly numbers"

        # The Sent copy, appended and seen — and the same mail to the mirror:
        # the next polls read nothing and write nothing.
        copies = w.sent_copies(mid)
        assert len(copies) == 1 and len(w.sent_copies()) == 1
        assert "\\Seen" in box.boxes["Sent"]["mails"][copies[0]]["flags"]
        assert ("append", "Sent") in box.writes
        for _ in range(3):
            mirror.poll()
        assert len(t.records(gw)) == 2 and len(w.told()) == 1
        assert len(box.fetched) == fetched, "the gateway's own Sent copy is not read back"
        assert mirror.thread("a@x.org")["messages"] == 2
        # No entry is kept of a direct send, and nothing is owed.
        assert gw.SENDS.entries() == [] and gw.SENDS.due() == []
        assert mirror.index.one("SELECT COUNT(*) AS n FROM settling")["n"] == 0
    finally:
        done(w)
    print("ok: a reply is addressed and threaded from the records, recorded, told and filed once")


def test_a_group_thread_is_answered_to_all_or_to_the_sender():
    w = world()
    try:
        gw, box, mirror, smtp = w.gw, w.box, w.mirror, w.smtp
        box.put("INBOX", t.msg("a@x.org", to=f"{ACCOUNT}, Bob <bob@y.org>",
                               cc="Cat <cat@z.org>, alias@example.org", day=1))
        box.put("INBOX", t.msg("b@y.org", sender="Bob <bob@y.org>",
                               to="ann@x.org, me@example.org", cc="cat@z.org",
                               subject="RE: Quarterly numbers", refs=["a@x.org"],
                               irt="a@x.org", day=2))
        mirror.poll()
        # Reply-all is the default: to whoever wrote last, in copy to
        # everyone else on that mail — never to the user's own addresses.
        status, answer = w.send(recipient="a@x.org")
        assert status == 200, answer
        assert smtp.mails[-1]["rcpt"] == ["bob@y.org", "ann@x.org", "cat@z.org"]
        sent = smtp.parsed()
        assert header(sent, "To") == "bob@y.org"
        assert header(sent, "Cc") == "ann@x.org, cat@z.org"
        assert header(sent, "Subject") == "Re: Quarterly numbers", "no second Re:"
        assert header(sent, "In-Reply-To") == "<b@y.org>"
        assert header(sent, "References") == "<a@x.org> <b@y.org>"
        first = t.by_id(gw)[answer["message_id"]]
        assert first["recipients"] == ["bob@y.org"]
        assert sorted(first["cc"]) == ["ann@x.org", "cat@z.org"]
        assert first["references"] == "a@x.org b@y.org" and first["in_reply_to"] == "b@y.org"

        # Explicitly "all" is the same; "sender" is the To alone.
        box.put("INBOX", t.msg("c@x.org", to="me@example.org, bob@y.org", cc="cat@z.org",
                               refs=["a@x.org", "b@y.org"], irt="b@y.org", day=later(60),
                               subject="Re: Quarterly numbers"))
        gw._now = lambda: NOW + 120
        mirror._clock = lambda: NOW + 120
        mirror.poll()
        status, answer = w.send(recipient="a@x.org", reply="sender")
        assert status == 200 and smtp.mails[-1]["rcpt"] == ["ann@x.org"], smtp.mails[-1]
        assert smtp.parsed().get("Cc") is None
        assert header(smtp.parsed(), "In-Reply-To") == "<c@x.org>"
        assert t.by_id(gw)[answer["message_id"]]["cc"] == []
        assert w.send(recipient="a@x.org", reply="everyone")[0] == 400
        assert w.send(recipient="a@x.org", reply="all")[0] == 200
    finally:
        done(w)
    print("ok: a group thread is answered to all by default, or to the sender only")


def test_reply_to_is_kept_and_honoured():
    w = world()
    try:
        gw, box, mirror, smtp = w.gw, w.box, w.mirror, w.smtp
        box.put("INBOX", t.msg("a@x.org", cc="bob@y.org", day=1,
                               headers=[("Reply-To", "Desk <desk@x.org>, second@x.org")]))
        mirror.poll()
        # The record keeps it from now on.
        assert sorted(t.by_id(gw)["a@x.org"]["reply_to"]) == ["desk@x.org", "second@x.org"]
        assert w.send(recipient="a@x.org")[0] == 200
        assert smtp.mails[-1]["rcpt"] == ["desk@x.org", "second@x.org", "bob@y.org"]
        assert header(smtp.parsed(), "To") == "desk@x.org, second@x.org"
        assert header(smtp.parsed(), "Cc") == "bob@y.org"
        assert "ann@x.org" not in smtp.mails[-1]["rcpt"], "Reply-To stands for the sender"

        # A record written before Reply-To was kept has it in the header
        # block it kept: a reply to that mail reads it there.
        box.put("INBOX", t.msg("old@x.org", subject="Older", day=2,
                               headers=[("Reply-To", "list@x.org")]))
        mirror.poll()
        row = mirror.index.one("SELECT record FROM mails WHERE message_id = 'old@x.org'")
        path = mirror._record_path(row["record"])
        text = path.read_text(encoding="utf-8")
        kept = [line for line in text.splitlines(keepends=True) if "replyTo" not in line]
        assert len(kept) < len(text.splitlines())
        path.write_text("".join(kept), encoding="utf-8")
        assert t.by_id(gw)["old@x.org"]["reply_to"] == []
        assert w.send(recipient="old@x.org")[0] == 200
        assert smtp.mails[-1]["rcpt"] == ["list@x.org"]
        # And a tombstone loses the addresses, like To and Cc.
        assert gw._ibstore.tombstone_message(mirror._record_path(mirror.index.one(
            "SELECT record FROM mails WHERE message_id = 'a@x.org'")["record"]))
        assert t.by_id(gw)["a@x.org"]["reply_to"] == []
    finally:
        done(w)
    print("ok: Reply-To is on the record, is where a reply goes, and is read from an older "
          "record's header block")


def test_a_follow_up_to_the_users_own_mail_goes_to_its_recipients_again():
    w = world()
    try:
        gw, box, mirror, smtp = w.gw, w.box, w.mirror, w.smtp
        # A thread the user started, from some mail client.
        box.put("Sent", t.msg("s@example.org", sender=ME, to="ann@x.org, bob@y.org",
                              cc="cat@z.org, alias@example.org", subject="Plan", day=1))
        mirror.poll()
        assert w.send(recipient="s@example.org")[0] == 200
        assert smtp.mails[-1]["rcpt"] == ["ann@x.org", "bob@y.org", "cat@z.org"]
        sent = smtp.parsed()
        assert header(sent, "To") == "ann@x.org, bob@y.org" and header(sent, "Cc") == "cat@z.org"
        assert header(sent, "Subject") == "Re: Plan"
        # No mail of anyone else's: the user's own is the one answered.
        assert header(sent, "In-Reply-To") == "<s@example.org>"

        # Someone answers; then the user writes twice. The second follows
        # the user's own latest mail — to whom that went — and is threaded
        # under the last mail that was not the user's.
        box.put("INBOX", t.msg("r@x.org", to=ACCOUNT, cc="bob@y.org", subject="Re: Plan",
                               refs=["s@example.org"], irt="s@example.org", day=later(10)))
        gw._now = lambda: NOW + 20
        mirror._clock = lambda: NOW + 20
        mirror.poll()
        status, first = w.send(recipient="s@example.org", reply="sender")
        assert status == 200 and smtp.mails[-1]["rcpt"] == ["ann@x.org"]
        gw._now = lambda: NOW + 30
        status, second = w.send(recipient="s@example.org")
        assert status == 200 and smtp.mails[-1]["rcpt"] == ["ann@x.org"], smtp.mails[-1]
        assert header(smtp.parsed(), "In-Reply-To") == "<r@x.org>"
        assert header(smtp.parsed(), "References") == "<s@example.org> <r@x.org>"
        assert t.by_id(gw)[second["message_id"]]["chat"] == "s@example.org"
        # "sender" on the user's own latest mail: its To, without the copies.
        box.put("Sent", t.msg("s2@example.org", sender=ME, to="dora@w.org", cc="eve@v.org",
                              subject="Re: Plan", refs=["s@example.org"], day=later(40)))
        gw._now = lambda: NOW + 50
        mirror._clock = lambda: NOW + 50
        mirror.poll()
        assert w.send(recipient="s@example.org", reply="sender")[0] == 200
        assert smtp.mails[-1]["rcpt"] == ["dora@w.org"]
        # A mail to oneself alone is answered to oneself.
        box.put("Sent", t.msg("self@example.org", sender=ME, to=ACCOUNT, subject="Note", day=4))
        mirror.poll()
        assert w.send(recipient="self@example.org")[0] == 200
        assert smtp.mails[-1]["rcpt"] == [ACCOUNT]
    finally:
        done(w)
    print("ok: with the user's own mail as the latest, a reply goes to its recipients again")


def test_a_thread_of_mails_from_the_users_own_addresses_is_followed_up():
    """A mail that came in From an address of the user's has no say in a
    reply beside real mails — its To and Cc are its sender's to write — but
    where only such mails are left, the newest is followed up as the user's
    own is: to its To and Cc but the user's own addresses (D: From this very
    account, a "send as" from a phone whose Sent copy is in no followed
    folder; D': from another listed address), to that address only where it
    went to nobody else. Beside a real mail, a forgery From the user To
    mal@ steers nothing, and nor does the user's mail from another listed
    address: Bob, whom it added, is not on the reply (the stated cost)."""
    def drafts(env_own, *mails):
        w = world(save_sent=False, MAIL_OWN_ADDRESSES=env_own)
        try:
            for message in mails:
                w.box.put("INBOX", message)
            for _ in range(3):
                w.mirror.poll()
            (chat,) = {r["chat"] for r in t.records(w.gw)}
            return {mode: (d["to"], d["cc"], d["in_reply_to"])
                    for mode, d in w.mirror.reply_drafts(chat).items()}
        finally:
            done(w)

    own, work = "alias@example.org", "alias@example.org, me@work.org"
    # D: only the user's mail, From the account, To Ann and Bob, Cc itself.
    got = drafts(own, t.msg("ph1@example.org", sender=f"Me <{ACCOUNT}>",
                            to="ann@x.org, bob@x.org", cc=ACCOUNT, subject="Plans", day=2))
    assert got == {m: (["ann@x.org", "bob@x.org"], [], "ph1@example.org")
                   for m in ("all", "sender")}, got
    # D': the same from another listed address.
    got = drafts(work, t.msg("w1@work.org", sender="Me <me@work.org>",
                             to="ann@x.org, bob@x.org", cc=ACCOUNT, subject="Plans", day=2))
    assert got == {m: (["ann@x.org", "bob@x.org"], [], "w1@work.org")
                   for m in ("all", "sender")}, got
    # To nobody else: to that address.
    got = drafts(work, t.msg("w2@work.org", sender="Me <me@work.org>", to=ACCOUNT,
                             subject="Note", day=2))
    assert got["all"] == (["me@work.org"], [], "w2@work.org"), got
    # Beside Ann's mail, a forgery From the user To Mallory steers nothing.
    ann = t.msg("a1@x.org", sender="Ann <ann@x.org>", to=ACCOUNT, cc="dan@x.org",
                subject="Plans", day=1)
    got = drafts(own, ann, t.msg("f1@evil.org", sender=f"Me <{ACCOUNT}>",
                                 to="mal@evil.org", subject="Re: Plans", refs=["a1@x.org"],
                                 irt="a1@x.org", day=2))
    assert got == {"all": (["ann@x.org"], ["dan@x.org"], "a1@x.org"),
                   "sender": (["ann@x.org"], [], "a1@x.org")}, got
    # Nor does the user's answer from work, to Ann and Bob: Bob is not on it.
    got = drafts(work, ann, t.msg("w1@work.org", sender="Me <me@work.org>",
                                  to="ann@x.org, bob@x.org", cc=ACCOUNT, subject="Re: Plans",
                                  refs=["a1@x.org"], irt="a1@x.org", day=2))
    assert got["all"] == (["ann@x.org"], ["dan@x.org"], "a1@x.org"), got
    print("ok: a thread of mails from the user's own addresses alone is followed up to whom "
          "they went; beside a real mail they have no say")


def test_a_tombstone_is_not_answered():
    w = world()
    try:
        gw, box, mirror, smtp = w.gw, w.box, w.mirror, w.smtp
        box.put("INBOX", t.msg("a@x.org", day=1))
        gone = box.put("INBOX", t.msg("b@y.org", sender="Bob <bob@y.org>", to=ACCOUNT,
                                      subject="Re: Quarterly numbers", refs=["a@x.org"],
                                      irt="a@x.org", day=2))
        mirror.poll()
        box.remove("INBOX", gone)
        mirror.poll()
        assert t.by_id(gw)["b@y.org"]["removed_at"]
        # The latest mail is gone from the mailbox: the one before it is
        # what a reply answers — its sender, its id.
        assert w.send(recipient="a@x.org")[0] == 200
        assert smtp.mails[-1]["rcpt"] == ["ann@x.org"]
        assert header(smtp.parsed(), "In-Reply-To") == "<a@x.org>"
        assert header(smtp.parsed(), "References") == "<a@x.org>"

        # A thread with nothing left to answer: 409, and no mail.
        box.put("INBOX", t.msg("lone@x.org", subject="Lone", day=3))
        mirror.poll()
        box.remove("INBOX", box.uid_of("INBOX", "lone@x.org"))
        mirror.poll()
        before = len(smtp.mails)
        status, answer = w.send(recipient="lone@x.org")
        assert status == 409 and "no mail left" in answer["error"], answer
        # A thread this mailbox does not mirror: 400.
        status, answer = w.send(recipient="nope@x.org")
        assert status == 400 and "unknown chat" in answer["error"], answer
        # A mail that names nobody to answer (no readable From).
        box.put("INBOX", t.msg("anon@x.org", sender="undisclosed", to="someone@x.org",
                               subject="Anon", day=5))
        mirror.poll()
        status, answer = w.send(recipient="anon@x.org")
        assert status == 409 and "nobody" in answer["error"], answer
        assert len(smtp.mails) == before and gw.SENDS.entries() == []
    finally:
        done(w)
    print("ok: a reply answers the latest mail still in the mailbox; none left is a 409")


def test_ids_in_the_headers_are_ids():
    w = world()
    try:
        gw, box, mirror, smtp = w.gw, w.box, w.mirror, w.smtp
        long_id = "AM9PR08MB6018" + "F3A2B1C4D5E6" * 6 + "@AM9PR08MB6018.eurprd08.prod.outlook.com"
        chain = [f"r{i}-" + "x" * 50 + "@x.org" for i in range(40)]
        box.put("INBOX", t.msg(long_id, refs=chain, irt=chain[-1], day=1,
                               subject="Re: Long thread"))
        mirror.poll()
        status, answer = w.send(recipient=chain[0])
        assert status == 200, answer
        raw = smtp.mails[-1]["data"].decode("ascii")
        # No id is rewritten as an encoded word, or broken across lines.
        head = raw.split("\r\n\r\n", 1)[0]
        assert "=?" not in head, head
        sent = smtp.parsed()
        assert header(sent, "In-Reply-To") == f"<{long_id}>"
        refs = header(sent, "References").split()
        # The root and the nearest parents, the answered mail last.
        assert len(refs) == gw.MAX_REFERENCES
        assert refs[0] == f"<{chain[0]}>" and refs[-1] == f"<{long_id}>"
        assert refs[1:-1] == [f"<{c}>" for c in chain[-(gw.MAX_REFERENCES - 2):]]
        assert all(len(line) <= 998 for line in head.split("\r\n"))
        # The record cites all of them: the thread index goes by the record.
        record = t.by_id(gw)[answer["message_id"]]
        assert record["references"].split() == chain + [long_id]
        assert record["chat"] == chain[0]

        # A mail the mirror keyed under a stand-in id (it had none of its
        # own) is answered, but no stand-in is written into a header: it
        # would name no mail anywhere else.
        box.put("INBOX", t.msg(None, sender="Bob <bob@y.org>", subject="No id", day=2))
        mirror.poll()
        chat = next(r["chat"] for r in t.records(gw) if r["subject_line"] == "No id")
        assert chat.endswith("@no-message-id.mail.invalid")
        status, answer = w.send(recipient=chat)
        assert status == 200 and smtp.mails[-1]["rcpt"] == ["bob@y.org"]
        sent = smtp.parsed()
        assert sent.get("In-Reply-To") is None and sent.get("References") is None
        # It is still in that chat: the gateway knows what it answered.
        assert t.by_id(gw)[answer["message_id"]]["chat"] == chat
    finally:
        done(w)
    print("ok: In-Reply-To and References carry real ids, whole, however long")


# --------------------------------------------------------------------------
# New mail
# --------------------------------------------------------------------------

def test_a_new_mail_starts_the_chat_its_id_names():
    w = world()
    try:
        gw, box, mirror, smtp = w.gw, w.box, w.mirror, w.smtp
        status, answer = w.send(to=["Ann Example <Ann@X.org>", "bob@y.org", "ann@x.org"],
                                cc=["cat@z.org", "bob@y.org"], subject="  Minutes\tof today ",
                                message="Here they are.", author="agent")
        assert status == 200, answer
        mid = answer["message_id"]
        assert answer["chat"] == mid, "a new mail's chat key is its own id"
        assert smtp.mails[0]["rcpt"] == ["ann@x.org", "bob@y.org", "cat@z.org"]
        sent = smtp.parsed()
        # Bare addresses: a display name the caller gave is not passed on.
        assert header(sent, "To") == "ann@x.org, bob@y.org" and header(sent, "Cc") == "cat@z.org"
        assert header(sent, "Subject") == "Minutes of today"
        assert sent.get("In-Reply-To") is None and sent.get("References") is None
        record = t.by_id(gw)[mid]
        assert record["chat"] == mid and record["author"] == "agent"
        assert record["in_reply_to"] is None and record["references"] is None
        told = w.told()
        assert told[0]["chat"] == mid and told[0]["chat_name"] == "Minutes of today"
        assert told[0]["peer"] == {"address": "ann@x.org", "name": None}
        assert told[0]["group"] is True
        thread = mirror.thread(mid)
        assert thread["name"] == "Minutes of today" and thread["messages"] == 1
        # A reply to it arrives: the same chat.
        box.put("INBOX", t.msg("re@x.org", subject="Re: Minutes of today", refs=[mid],
                               irt=mid, day=1), date=NOW + 5)
        mirror.poll()
        assert t.by_id(gw)["re@x.org"]["chat"] == mid

        # What a new mail needs, and what it may not carry.
        for payload, word in (
                ({}, "no recipients"), ({"to": []}, "no recipients"),
                ({"to": "ann@x.org", "reply": "all"}, "'reply'"),
                ({"to": ["ann@x.org"], "bcc": ["x@y.org"]}, "blind"),
                ({"to": {"a": 1}}, "'to' must be a list"),
                ({"to": ["ann@x.org"], "message": "", "text": ""}, "needs a text"),
                ({"to": ["ann@x.org"], "message": ["x"]}, "must be text"),
                ({"to": ["ann@x.org"], "author": "device"}, "'author'"),
                ({"to": ["ann@x.org"], "images": "x"}, "'images'"),
                ({"to": ["ann@x.org"], "images": [{"data": "!!!"}]}, "base64"),
                ({"to": ["ann@x.org"] * 2 + [f"p{i}@x.org" for i in range(gw.MAX_SEND_RECIPIENTS)]},
                 "too many"),
                ({"recipient": "re@x.org", "to": ["ann@x.org"]}, "thread's own"),
                ({"recipient": "re@x.org", "subject": "Other"}, "thread's own")):
            status, said = gw._accept_send({"message": "x", **payload}, HOST)
            assert status == 400 and word in said["error"], (payload, said)
        # A single address as a string is a list of one; `text` is `message`.
        status, answer = gw._accept_send({"to": "dora@w.org", "text": "Hi."}, HOST)
        assert status == 200 and smtp.mails[-1]["rcpt"] == ["dora@w.org"]
        assert header(smtp.parsed(), "Subject") == ""
        assert len(smtp.mails) == 2
    finally:
        done(w)
    print("ok: a new mail goes to the addresses named, and starts the chat its id names")


def test_attachments_go_out_and_onto_the_record():
    w = world()
    try:
        gw, box, mirror, smtp = w.gw, w.box, w.mirror, w.smtp
        pdf = b"%PDF-1.4 minutes"
        images = [{"filename": "../../etc/Minutes Q3.pdf", "content_type": "application/pdf",
                   "data": base64.b64encode(pdf).decode()},
                  {"filename": "notes.txt", "data": base64.b64encode(b"plain notes").decode()},
                  {"filename": "inner.eml", "content_type": "message/rfc822",
                   "data": base64.b64encode(b"Subject: x\r\n\r\nbody").decode()}]
        status, answer = w.send(to=["ann@x.org"], subject="Minutes", message="", images=images)
        assert status == 200, answer
        sent = smtp.parsed()
        files = [(p.get_filename(), p.get_content_type(), p.get_payload(decode=True))
                 for p in sent.iter_attachments()]
        assert files == [("Minutes Q3.pdf", "application/pdf", pdf),
                         ("notes.txt", "text/plain", b"plain notes"),
                         # A container type is sent as a file, not as parts.
                         ("inner.eml", "application/octet-stream", b"Subject: x\r\n\r\nbody")]
        record = t.by_id(gw)[answer["message_id"]]
        assert record["attachments"] and sorted(record["attachments"]) == sorted(answer["attachments"])
        meta = record["attachment_meta"]
        names = sorted(meta[ref]["file_name"] for ref in record["attachments"])
        assert names == ["Minutes Q3.pdf", "inner.eml", "notes.txt"]
        assert pdf in [t.blob(gw, ref) for ref in record["attachments"]]
        assert record["text"] == "" and t.blob(gw, record["body_part"]) == b""
        assert w.told()[0]["attachments"] == answer["attachments"]
        # The Sent copy carries them too.
        copy = email.message_from_bytes(
            box.boxes["Sent"]["mails"][w.sent_copies()[0]]["raw"], policy=email.policy.default)
        assert [p.get_filename() for p in copy.iter_attachments()] == [
            "Minutes Q3.pdf", "notes.txt", "inner.eml"]
        too_many = [images[1]] * (gw.MAX_SEND_ATTACHMENTS + 1)
        assert w.send(to=["ann@x.org"], images=too_many)[0] == 400
    finally:
        done(w)
    print("ok: attachments are sent under their names and kept as the record's blobs")


# --------------------------------------------------------------------------
# The send policy and the pending store
# --------------------------------------------------------------------------

def test_the_send_policy_decides():
    cases = [  # (EMAIL_SEND_POLICY, user_approved, author, goes at once)
        ("allow", False, "agent", True), ("allow", True, "user", True),
        ("trust", False, "agent", False), ("trust", True, "agent", True),
        # Who wrote the words grants nothing: only the policy and the press.
        ("trust", False, "user", False), ("trust", "true", "user", False),
        ("verify", False, "agent", False), ("verify", True, "user", False),
        (None, True, "user", False)]
    for policy, approved, author, direct in cases:
        w = world(policy=policy)
        try:
            w.box.put("INBOX", t.msg("a@x.org", day=1))
            w.mirror.poll()
            status, answer = w.send(recipient="a@x.org", user_approved=approved, author=author)
            case = (policy, approved, author)
            if direct:
                assert status == 200 and answer["status"] == "sent", (case, answer)
                assert len(w.smtp.mails) == 1 and len(t.records(w.gw)) == 2, case
                continue
            assert status == 202, (case, answer)
            rid = answer["request_id"]
            assert answer == {
                "status": "pending_approval", "request_id": rid,
                "approval_url": f"/sends/mail-gateway/{rid}",
                "message_id": answer["message_id"], "chat": "a@x.org",
                "note": answer["note"]}, (case, answer)
            assert len(rid) == 32 and answer["message_id"].endswith("@example.org")
            # Queued is not sent: no session, no record, nothing told, no copy.
            assert w.smtp.sessions == 0 and len(t.records(w.gw)) == 1, case
            assert w.told() == [] and w.sent_copies() == [] and w.box.writes == [], case
            entry = w.gw.SENDS.get(rid)
            assert entry["status"] == "pending" and entry["category"] == (policy or "verify")
            assert entry["author"] == author and entry["direct"] is False
        finally:
            done(w)
    # The policy is the mailbox's own address's; another address's entry, a
    # wildcard, a policy that does not parse.
    for raw, direct in (
            ('[{"address": "other@example.org", "category": "allow"}]', False),
            ('[{"address": "other@example.org", "category": "verify"},'
             ' {"address": "*", "category": "allow"}]', True),
            ('[{"address": "*", "category": "allow"},'
             ' {"address": "me@example.org", "category": "verify"}]', False),
            ("not json", False), ('{"address": "*"}', False)):
        w = world(policy=None, EMAIL_SEND_POLICY=raw)
        try:
            status, _answer = w.send(to=["ann@x.org"], user_approved=True)
            assert status == (200 if direct else 202), (raw, status)
        finally:
            done(w)
    print("ok: verify queues, trust sends on the user's press, allow sends; author grants nothing")


def test_the_approval_link():
    for env, host, expected in (
            ({}, "mail-gateway:8096", "/sends/mail-gateway/{}"),
            ({}, "mail-gateway-work", "/sends/mail-gateway-work/{}"),
            ({}, None, "/sends/mail-gateway/{}"),
            ({"SEND_APPROVAL_BASE_URL": "https://dash.example/"}, "mail-gateway:8096",
             "https://dash.example/sends/mail-gateway/{}"),
            ({"SEND_APPROVAL_SLUG": "/mail-work/", "SEND_APPROVAL_BASE_URL": "https://d.example"},
             "mail-gateway:8096", "https://d.example/sends/mail-work/{}")):
        w = world(policy="verify", **env)
        try:
            status, answer = w.gw._accept_send({"to": ["ann@x.org"], "message": "x"}, host)
            assert status == 202 and answer["approval_url"] == expected.format(answer["request_id"])
        finally:
            done(w)
    print("ok: a queued send's approval link is /sends/<slug>/<id>, as on the other gateways")


def test_a_queued_send_is_listed_decided_and_sent_once():
    w = world(policy="verify")
    try:
        gw, box, mirror, smtp = w.gw, w.box, w.mirror, w.smtp
        box.put("INBOX", t.msg("a@x.org", cc="Bob <bob@y.org>", day=1))
        box.put("INBOX", t.msg("other@x.org", subject="Other", day=2))
        mirror.poll()
        thread_id = "ab" * 16
        status, queued = w.send(recipient="a@x.org", message="On it.", author="agent",
                                thread=thread_id.upper(), user_approved=True,
                                images=[{"filename": "plan.pdf", "data": base64.b64encode(
                                    b"%PDF").decode()}])
        rid, mid = queued["request_id"], queued["message_id"]
        assert status == 202
        entry = gw._public(gw.SENDS.get(rid))
        assert entry == {
            "id": rid, "status": "pending", "category": "verify", "author": "agent",
            "thread": thread_id, "created": NOW, "direct": False, "from": ACCOUNT,
            "chat": "a@x.org", "message_id": mid, "subject": "Re: Quarterly numbers",
            "to": ["ann@x.org"], "cc": ["bob@y.org"], "recipient": "Ann Example <ann@x.org>, +1",
            "message": "On it.", "reply": "all", "in_reply_to": "a@x.org",
            "references": ["a@x.org"], "attachments": ["plan.pdf"], "files": 1}, entry
        json.dumps(entry)
        # A thread id that is none is dropped, not kept.
        _s, second = w.send(recipient="other@x.org", reply="sender", thread="not-a-thread-id")
        assert gw.SENDS.get(second["request_id"])["thread"] is None
        gw._now = lambda: NOW + 1
        _s, third = w.send(to=["dora@w.org"], subject="New", message="Hello.")
        # Listed oldest first; one chat's with the filter; only what waits.
        listed = lambda chat=None: [e["id"] for e in gw.SENDS.waiting(chat)]   # noqa: E731
        assert listed()[-1] == third["request_id"] and set(listed()) == {
            rid, second["request_id"], third["request_id"]}
        assert listed("a@x.org") == [rid] and listed("nope") == []
        assert listed(third["message_id"]) == [third["request_id"]], "a new mail's chat is its id"

        # Retract (whoever queued it) and reject (the user): nothing is sent,
        # and a decision is final.
        retracted, decided = gw.SENDS.decide(second["request_id"], "retract")
        assert decided and retracted["status"] == "retracted"
        assert gw._decide_send(second["request_id"], "approve")["status"] == "retracted"
        assert gw._decide_send(third["request_id"], "reject")["status"] == "rejected"
        assert gw._decide_send(third["request_id"], "retract")["status"] == "rejected"
        assert gw._decide_send("f" * 32, "approve") is None
        assert gw._decide_send("x" + "f" * 31, "approve") is None
        assert listed() == [rid] and smtp.sessions == 0

        # Approve: the answer says "sending"; the entry then says what became
        # of it, as the approval page and the chat read it.
        assert gw._decide_send(rid, "approve")["status"] == "sending"
        entry = gw.SENDS.get(rid)
        # Sent and on record at once; its Sent copy is the mirror loop's.
        assert entry["status"] == "approved" and entry["recorded"] and "done" not in entry
        assert len(smtp.mails) == 1 and w.sent_copies(mid) == [] and gw.SENDS.due() == [rid]
        assert listed() == [] and gw.SENDS.listed() == [], "a sent mail is a message, not a send"
        w.told()
        entry = gw.SENDS.get(rid)
        assert entry["status"] == "approved" and entry["done"] is True
        assert entry["message_id"] == mid and entry["sent_at"] == NOW + 1
        assert entry["chat"] == "a@x.org" and len(entry["attachments"]) == 1
        assert "images" not in entry and "refs" not in entry, "the bytes are not kept twice"
        assert len(smtp.mails) == 1 and smtp.mails[0]["rcpt"] == ["ann@x.org", "bob@y.org"]
        assert header(smtp.parsed(), "Message-Id") == f"<{mid}>", "the id minted when it was queued"
        record = t.by_id(gw)[mid]
        assert record["author"] == "agent" and record["attachments"] == entry["attachments"]
        assert [e["message_id"] for e in w.told()] == [mid]
        assert len(w.sent_copies(mid)) == 1
        # A second press of Allow sends nothing more.
        assert gw._decide_send(rid, "approve")["status"] == "approved"
        mirror.poll()
        assert len(smtp.mails) == 1 and len(w.told()) == 1 and listed() == []
        assert gw.SENDS.due() == []
    finally:
        done(w)
    print("ok: a queued send is listed, retracted, rejected or approved — and sent once")


def test_the_http_api_sends_and_decides():
    w = world(policy="trust", CHAT_ERASE_TOKEN=ERASE)
    try:
        gw, box, mirror, smtp = w.gw, w.box, w.mirror, w.smtp
        box.put("INBOX", t.msg("a@x.org", day=1))
        mirror.poll()
        server, base = t.serve(gw)
        token = t.TOKEN

        def post(path, payload, **headers):
            body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            request = t.urllib.request.Request(base + path, data=body, method="POST", headers={
                "Content-Type": "application/json", "Authorization": f"Bearer {token}",
                **headers})
            try:
                with t.urllib.request.urlopen(request, timeout=10) as response:
                    return response.status, json.loads(response.read())
            except t.urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read())

        def get(path):
            status, _h, body = t.call(base + path, token=token)
            return status, json.loads(body)
        try:
            assert t.call(f"{base}/send", body=b'{"to": ["ann@x.org"], "message": "x"}')[0] == 401
            # Not approved by the user: queued.
            status, queued = post("/send", {"recipient": "a@x.org", "message": "Soon."})
            assert status == 202 and queued["status"] == "pending_approval", queued
            rid = queued["request_id"]
            # The slug is the service name the caller reached the gateway by.
            assert queued["approval_url"] == f"/sends/127.0.0.1/{rid}"
            status, listing = get("/pending-sends")
            assert status == 200 and [e["id"] for e in listing["pending"]] == [rid]
            assert "images" not in listing["pending"][0]
            assert get("/pending-sends?chat=a%40x.org")[1]["pending"][0]["chat"] == "a@x.org"
            assert get("/pending-sends?chat=nope%40x.org")[1] == {"pending": []}
            status, entry = get(f"/pending-sends/{rid}")
            assert status == 200 and entry["status"] == "pending" and entry["to"] == ["ann@x.org"]
            assert entry["subject"] == "Re: Quarterly numbers" and entry["message"] == "Soon."
            status, entry = post(f"/pending-sends/{rid}/approve", b"")
            assert status == 200 and entry["status"] == "sending" and entry["chat"] == "a@x.org"
            status, entry = get(f"/pending-sends/{rid}")
            assert entry["status"] == "approved" and entry["message_id"] == queued["message_id"]
            assert len(smtp.mails) == 1
            # The user's press: sent at once.
            status, sent = post("/send", {"recipient": "a@x.org", "message": "Now.",
                                          "author": "user", "user_approved": True})
            assert status == 200 and sent["status"] == "sent" and sent["chat"] == "a@x.org"
            assert len(smtp.mails) == 2 and get("/pending-sends")[1] == {"pending": []}
            # Retract over HTTP, as the push CLI does it.
            _s, again = post("/send", {"to": ["dora@w.org"], "message": "Hm."})
            status, entry = post(f"/pending-sends/{again['request_id']}/retract", b"")
            assert status == 200 and entry["status"] == "retracted"
            assert post(f"/pending-sends/{'0' * 32}/retract", b"")[0] == 404
            # Refusals carry one sentence under "error".
            for payload, code in (({"recipient": "nope@x.org", "message": "x"}, 400),
                                  ({"to": ["not an address"], "message": "x"}, 400),
                                  ({"recipient": "a@x.org", "message": "x",
                                    "account": "other@example.org"}, 404),
                                  (b"[1]", 400), (b"not json", 400)):
                status, said = post("/send", payload)
                assert status == code and isinstance(said["error"], str), (payload, said)
            # The mailbox this gateway serves may be named, however it is cased.
            assert post("/send", {"recipient": "a@x.org", "message": "x", "user_approved": True,
                                  "account": "ME@Example.org"})[0] == 200
            assert len(smtp.mails) == 3
        finally:
            server.shutdown()
    finally:
        done(w)
    print("ok: /send, /pending-sends and its three verbs over HTTP")


def test_reply_tokens_and_the_origin_thread():
    w = world()
    try:
        gw, box, mirror, smtp = w.gw, w.box, w.mirror, w.smtp
        box.put("INBOX", t.msg("a@x.org", day=1))
        mirror.poll()
        token = gw.reply_tokens().mint("a@x.org", channel="email")
        # The token names the thread; a recipient beside it is not consulted.
        status, answer = w.send(reply_to=token, recipient="nope@x.org")
        assert status == 200 and answer["chat"] == "a@x.org", answer
        assert smtp.mails[-1]["rcpt"] == ["ann@x.org"]
        status, answer = w.send(reply_to=token[:-2] + "xx")
        assert status == 400 and "reply_to" in answer["error"]
        assert len(smtp.mails) == 1
        # The key file is on the state volume of a gateway that runs.
        assert (gw.MAIL_STATE_DIR / "reply-tokens").is_dir()
    finally:
        done(w)
    print("ok: a reply token names the thread a reply goes into")


def test_the_gateway_writes_no_policy():
    w = world()
    try:
        gw, smtp = w.gw, w.smtp
        calls = []
        real = gw._triage._mutate_email
        gw._triage._mutate_email = lambda **kw: calls.append(kw)
        try:
            assert gw._ec.WHITELIST_ON_SEND is False
            assert w.send(to=["new.person@gmail.com"])[0] == 200 and len(smtp.mails) == 1
        finally:
            gw._triage._mutate_email = real
        assert calls == [] and not gw.POLICY_PATH.exists(), \
            "a mail the gateway sends whitelists nobody"
    finally:
        done(w)
    print("ok: email_client's whitelist-on-send does not run inside the gateway")


def test_a_merge_takes_a_queued_send_along():
    w = world(policy="verify")
    try:
        gw, box, mirror = w.gw, w.box, w.mirror
        box.put("INBOX", t.msg("a@x.org", subject="Offer", day=1))
        box.put("INBOX", t.msg("b@x.org", subject="Re: Offer", irt="lost@x.org", day=2))
        mirror.poll()
        _s, queued = w.send(recipient="lost@x.org")
        assert queued["chat"] == "lost@x.org"
        box.put("INBOX", t.msg("c@x.org", subject="Re: Offer", refs=["a@x.org", "lost@x.org"],
                               day=3))
        mirror.poll()
        assert gw.SENDS.get(queued["request_id"])["chat"] == "a@x.org"
        assert [e["id"] for e in gw.SENDS.waiting("a@x.org")] == [queued["request_id"]]
        # Approved after the merge: it is a mail of the surviving chat.
        gw._decide_send(queued["request_id"], "approve")
        assert t.by_id(gw)[queued["message_id"]]["chat"] == "a@x.org"
    finally:
        done(w)
    print("ok: a send queued in a chat that is merged away waits in the survivor")


# --------------------------------------------------------------------------
# One record per sent mail; the grace
# --------------------------------------------------------------------------

def blobs(gw) -> set:
    """The id of every blob in the media store."""
    folder = gw._ibstore.media_dir(gw.INBOUND_STORE_DIR)
    return {p.name for p in folder.iterdir() if "." not in p.name} if folder.exists() else set()


def referenced(gw) -> set:
    """The id of every blob a record refers to."""
    return {gw._ibstore.media_id_of(ref) for record in t.records(gw)
            for ref in [record["body_part"], record["html_part"], record["header_part"],
                        *record["attachments"]] if ref}


def settling(mirror, message_id):
    row = mirror.index.one("SELECT until FROM settling WHERE message_id = ?", (message_id,))
    return None if row is None else (row["until"],)


def test_the_provider_files_the_sent_copy():
    w = world(save_sent=False)
    try:
        gw, box, mirror, smtp = w.gw, w.box, w.mirror, w.smtp
        box.put("INBOX", t.msg("a@x.org", day=1))
        mirror.poll()
        status, answer = w.send(recipient="a@x.org", author="user", user_approved=True)
        mid = answer["message_id"]
        assert status == 200 and len(smtp.mails) == 1
        # SMTP_SAVE_SENT=false: the gateway appends nothing.
        assert not [x for x in box.writes if x[0] == "append"] and w.sent_copies() == []
        assert gw.SENDS.entries() == [] and gw.SENDS.due() == []
        # It stays a live record with no copy, however long none shows — and
        # is not searched for on every poll meanwhile.
        assert settling(mirror, mid) == (None,)
        mirror._clock = lambda: NOW + 30 * DAY
        for _ in range(3):
            mirror.poll()
        assert not t.by_id(gw)[mid]["removed_at"] and t.by_id(gw)[mid]["text"] == "Thanks."
        assert not [x for x in box.found if x[1] == mid]
        # The provider's copy shows: the same mail, by its Message-ID. Nothing
        # is written twice, and what was recorded at send time stands.
        uid = box.put("Sent", smtp.mails[0]["data"], date=NOW + 5)
        mirror.poll()
        assert len(t.records(gw)) == 2 and [e["message_id"] for e in w.told()] == [mid]
        assert t.by_id(gw)[mid]["author"] == "user"
        assert settling(mirror, mid) is None
        assert mirror.index.one("SELECT COUNT(*) AS n FROM locations WHERE message_id = ?",
                                (mid,))["n"] == 1
        # From here on it is followed like any mail: deleted in a client, it
        # leaves a tombstone.
        box.remove("Sent", uid)
        mirror.poll()
        assert t.by_id(gw)[mid]["removed_at"]
    finally:
        done(w)
    print("ok: where the provider files the Sent copy, the record stands with or without it")


def test_a_busy_mirror_delays_the_record_not_the_mail():
    for save_sent in (True, False):
        w = world(save_sent=save_sent)
        try:
            gw, box, mirror, smtp = w.gw, w.box, w.mirror, w.smtp
            gw.RECORD_LOCK_SECONDS = 0.05
            box.put("INBOX", t.msg("a@x.org", day=1))
            mirror.poll()
            held, release = threading.Event(), threading.Event()

            def hold():
                with mirror._lock:      # a pass over the mailbox, taking its time
                    held.set()
                    release.wait(10)
            holder = threading.Thread(target=hold)
            holder.start()
            held.wait(5)
            try:
                status, answer = w.send(recipient="a@x.org", images=[
                    {"filename": "a.txt", "data": base64.b64encode(b"file").decode()}])
            finally:
                release.set()
                holder.join()
            mid = answer["message_id"]
            # The mail is out, and the answer says so; its record waits.
            assert status == 200 and len(smtp.mails) == 1 and len(answer["attachments"]) == 1
            assert mid not in t.by_id(gw) and gw.SENDS.due() and gw._WAKE.is_set()
            # Its Sent copy is the loop's as well: the answer waited for neither.
            assert w.sent_copies(mid) == [] and box.writes == []
            if not save_sent:
                # And here the provider's copy is mirrored before the record
                # is written: the mirror reads it as a mail the user sent,
                # credited to whoever the gateway said wrote it.
                box.put("Sent", smtp.mails[0]["data"], date=NOW + 5)
                mirror.poll()
                assert t.by_id(gw)[mid]["author"] == "agent"
            told = w.told()
            assert [e["message_id"] for e in told] == [mid] and told[0]["author"] == "agent"
            record = t.by_id(gw)[mid]
            assert record["text"] == "Thanks." and len(record["attachments"]) == 1
            assert gw.SENDS.due() == [] and gw.SENDS.entries() == []
            assert len(w.sent_copies(mid)) == 1
            mirror.poll()
            assert len(t.records(gw)) == 2 and len(w.told()) == 1
            # Whichever came first, no blob is left that no record refers to.
            assert blobs(gw) == referenced(gw)
        finally:
            done(w)
    print("ok: a send does not wait out a pass of the mirror; record and Sent copy are one mail")


def test_a_sent_mail_is_not_called_gone_while_its_copy_is_still_to_come():
    w = world()
    try:
        gw, box, mirror, smtp = w.gw, w.box, w.mirror, w.smtp
        grace = gw.MAIL_SENT_GRACE_SECONDS
        assert grace == 600.0
        box.put("INBOX", t.msg("a@x.org", day=1))
        mirror.poll()
        appends = lambda: len([x for x in box.writes if x[0] == "append"])   # noqa: E731

        # The mailbox will not take the Sent copy (over quota, say).
        box.no_writes.add(("append", "Sent"))
        status, answer = w.send(recipient="a@x.org")
        mid = answer["message_id"]
        assert status == 200 and len(smtp.mails) == 1, "the mail is out all the same"
        assert t.by_id(gw)[mid]["text"] == "Thanks." and w.sent_copies() == []
        (entry,) = gw.SENDS.entries()
        assert entry["status"] == "approved" and entry["recorded"] and not entry.get("filed")
        assert gw.SENDS.due() == [entry["id"]] and settling(mirror, mid) == (NOW + grace,)
        # Polls within the grace look for it, do not find it, and call
        # nothing gone; the copy is tried again each time.
        mirror._clock = lambda: NOW + 300
        tried = appends()
        mirror.poll()
        assert not t.by_id(gw)[mid]["removed_at"] and appends() == tried + 1
        # While the copy is still owed, the grace is renewed: past the first
        # deadline the record is still that of a mail that was sent.
        assert settling(mirror, mid) == (NOW + 300 + grace,)
        mirror._clock = lambda: NOW + grace + 100
        mirror.poll()
        assert not t.by_id(gw)[mid]["removed_at"]
        # The mailbox takes it: filed once, placed, nothing owed.
        box.no_writes.clear()
        mirror.poll()
        assert len(w.sent_copies(mid)) == 1 and settling(mirror, mid) is None
        assert gw.SENDS.due() == [] and gw.SENDS.entries() == []
        mirror.poll()
        assert len(w.sent_copies()) == 1 and len(t.records(gw)) == 2

        # An append that got through without its answer is not made twice:
        # the copy is looked for before it is appended again.
        real_append = box.append

        def answer_lost(folder, raw, *, when=None):
            real_append(folder, raw, when=when)
            raise gw.MailboxError("the IMAP connection failed: timed out")
        box.append = answer_lost
        status, answer = w.send(recipient="a@x.org", message="Second.")
        second = answer["message_id"]
        assert status == 200 and w.sent_copies(second) == [], "no answer waits for the copy"
        w.told()        # the loop files it, and the mailbox's answer is lost
        box.append = real_append
        assert len(w.sent_copies(second)) == 1 and gw.SENDS.due()
        tried = appends()
        mirror.poll(sync=False)
        assert appends() == tried and len(w.sent_copies(second)) == 1
        assert gw.SENDS.due() == [] and settling(mirror, second) is None

        # A server that does not name the new UID: found by Message-ID.
        box.names_uids = False
        status, answer = w.send(recipient="a@x.org", message="Third.")
        third = answer["message_id"]
        w.told()
        assert mirror.index.one("SELECT COUNT(*) AS n FROM locations WHERE message_id = ?",
                                (third,))["n"] == 1 and settling(mirror, third) is None
    finally:
        done(w)
    # And the grace ends: a sent mail with no copy anywhere and nothing owed
    # for it any more (its entry is gone with the state it was in) is, after
    # the grace, a mail that is in no folder.
    w = world()
    try:
        gw, box, mirror = w.gw, w.box, w.mirror
        box.no_writes.add(("append", "Sent"))
        _status, answer = w.send(to=["ann@x.org"])
        mid = answer["message_id"]
        gw.SENDS.remove(gw.SENDS.entries()[0]["id"])
        mirror._clock = lambda: NOW + gw.MAIL_SENT_GRACE_SECONDS - 5
        mirror.poll()
        assert not t.by_id(gw)[mid]["removed_at"]
        mirror._clock = lambda: NOW + gw.MAIL_SENT_GRACE_SECONDS + 5
        mirror.poll()
        assert t.by_id(gw)[mid]["removed_at"] and settling(mirror, mid) is None
    finally:
        done(w)
    print("ok: a sent mail waits out the grace for its Sent copy, which is filed exactly once")


# --------------------------------------------------------------------------
# What can go wrong with the SMTP server
# --------------------------------------------------------------------------

def test_a_send_the_server_does_not_take_is_not_sent():
    def all_refused(smtp):
        smtp.refuse_rcpt = {"ann@x.org"}
    cases = (
        (lambda smtp: setattr(smtp, "drop", "connect"), "Connection refused"),
        (lambda smtp: setattr(smtp, "refuse_login", True), "535"),
        (lambda smtp: setattr(smtp, "drop", "mail"), "SMTP send failed"),
        (all_refused, "550"),
        # Refused once the message was handed over: an answer all the same.
        (lambda smtp: setattr(smtp, "reject_data", "554 5.7.1 content rejected"), "554"))
    for arrange, word in cases:
        w = world(policy="verify")
        try:
            gw, box, mirror, smtp = w.gw, w.box, w.mirror, w.smtp
            box.put("INBOX", t.msg("a@x.org", day=1))
            mirror.poll()
            _s, queued = w.send(recipient="a@x.org", images=[
                {"filename": "a.txt", "data": base64.b64encode(b"file").decode()}])
            arrange(smtp)
            assert gw._decide_send(queued["request_id"], "approve")["status"] == "sending"
            entry = gw.SENDS.get(queued["request_id"])
            assert entry["status"] == "error" and word in entry["error"], (word, entry)
            assert "images" not in entry and entry["attachments"] == ["a.txt"]
            assert smtp.mails == [] and len(t.records(gw)) == 1 and w.told() == []
            assert w.sent_copies() == [] and gw.SENDS.due() == []
            assert blobs(gw) == referenced(gw), "what was stored for the record is undone"
            mirror.poll()
            assert len(t.records(gw)) == 1
            # A failed entry is not sent by a later press, or by a restart.
            assert gw._decide_send(queued["request_id"], "approve")["status"] == "error"
            w.restart()
            w.mirror.poll()
            assert smtp.mails == []
        finally:
            done(w)
        # The same as a direct send: an error for the caller, and no entry.
        w = world()
        try:
            w.box.put("INBOX", t.msg("a@x.org", day=1))
            w.mirror.poll()
            arrange(w.smtp)
            status, answer = w.send(recipient="a@x.org")
            assert status == 502 and word in answer["error"], (word, answer)
            assert answer["error"].startswith("send failed: ") and "unconfirmed" not in answer
            assert w.gw.SENDS.entries() == [] and len(t.records(w.gw)) == 1
        finally:
            done(w)
    print("ok: a send the SMTP server refuses, or never gets, fails as what it is")


def test_what_the_server_took_is_sent():
    w = world()
    try:
        gw, box, mirror, smtp = w.gw, w.box, w.mirror, w.smtp
        box.put("INBOX", t.msg("a@x.org", cc="bob@y.org, cat@z.org", day=1))
        mirror.poll()
        # One recipient refused: the mail went to the others, and says so.
        smtp.refuse_rcpt = {"bob@y.org"}
        status, answer = w.send(recipient="a@x.org")
        assert status == 200 and answer["refused"] == ["bob@y.org"], answer
        assert smtp.mails[-1]["rcpt"] == ["ann@x.org", "cat@z.org"]
        # The record names whom the server took it for, and whom it did not;
        # the mail itself — its headers, its Sent copy — names everyone it
        # was addressed to, as the others read it.
        first = answer["message_id"]
        record = t.by_id(gw)[first]
        assert record["recipients"] == ["ann@x.org"] and record["cc"] == ["cat@z.org"]
        assert record["refused"] == ["bob@y.org"]
        assert header(smtp.parsed(), "Cc") == "bob@y.org, cat@z.org"
        (told,) = w.told()
        assert told["refused"] == ["bob@y.org"]
        # A follow-up goes to whom that mail went — not to the refused one.
        smtp.refuse_rcpt = set()
        status, answer = w.send(recipient="a@x.org", message="Once more.")
        assert status == 200 and smtp.mails[-1]["rcpt"] == ["ann@x.org", "cat@z.org"]
        assert "refused" not in answer and "refused" not in w.told()[-1]
        assert t.by_id(gw)[answer["message_id"]]["refused"] == []
        # And so says an index rebuilt from the ledger.
        (gw.MAIL_STATE_DIR / "mailbox.sqlite").unlink()
        mirror = w.restart()
        row = mirror.index.one("SELECT recipients, cc FROM mails WHERE message_id = ?", (first,))
        assert json.loads(row["recipients"]) == [["ann@x.org", None]]
        assert json.loads(row["cc"]) == [["cat@z.org", None]]
        # The session fails only after the server took the mail: it is sent.
        smtp.refuse_rcpt, smtp.fail_quit = set(), True
        status, answer = w.send(recipient="a@x.org", message="Again.")
        assert status == 200 and len(smtp.mails) == 3 and "refused" not in answer
        assert answer["message_id"] in t.by_id(gw)
    finally:
        done(w)
    # A queued send says it on its entry, for whoever approved it.
    w = world(policy="verify")
    try:
        gw, smtp = w.gw, w.smtp
        _s, queued = w.send(to=["ann@x.org", "typo@y.org"], cc=["cat@z.org"])
        smtp.refuse_rcpt = {"typo@y.org"}
        gw._decide_send(queued["request_id"], "approve")
        entry = gw.SENDS.get(queued["request_id"])
        assert entry["status"] == "approved" and entry["refused"] == ["typo@y.org"], entry
        assert entry["to"] == ["ann@x.org", "typo@y.org"], "the mail as it was written"
        record = t.by_id(gw)[queued["message_id"]]
        assert record["recipients"] == ["ann@x.org"] and record["refused"] == ["typo@y.org"]
    finally:
        done(w)
    print("ok: a partial refusal and a failed goodbye do not unsend a mail; who got none is said")


def test_a_mail_handed_over_with_no_answer_is_never_sent_again():
    # The gateway files the Sent copy itself: nothing in the mailbox can say
    # whether the mail went out, and the send ends saying exactly that.
    w = world(policy="verify")
    try:
        gw, box, mirror, smtp = w.gw, w.box, w.mirror, w.smtp
        box.put("INBOX", t.msg("a@x.org", day=1))
        mirror.poll()
        _s, queued = w.send(recipient="a@x.org")
        smtp.drop = "dot"       # the whole mail arrived; the line died before the answer
        gw._decide_send(queued["request_id"], "approve")
        entry = gw.SENDS.get(queued["request_id"])
        assert entry["status"] == "error" and entry["error"] == gw.UNKNOWN_OUTCOME, entry
        assert "not sent again" in entry["error"] and entry["message"] == "Thanks."
        smtp.drop = None
        for _ in range(2):
            w.restart()
            w.mirror.poll()
            gw._decide_send(queued["request_id"], "approve")
        assert smtp.sessions == 1 and len(smtp.mails) == 1, "handed over once, whatever follows"
        assert len(t.records(gw)) == 1 and w.sent_copies() == [] and gw.SENDS.due() == []
        assert blobs(gw) == referenced(gw)
        # A direct send: the caller is told the same.
        smtp.drop = "dot"
        status, answer = w.send(to=["ann@x.org"], user_approved=True)
        assert status == 202        # (verify: queued — nothing to do with the server)
    finally:
        done(w)
    w = world()
    try:
        w.smtp.drop = "dot"
        status, answer = w.send(to=["ann@x.org"])
        assert status == 502 and "not known whether the mail went out" in answer["error"]
        # Never "failed": the mail may be out, and the answer says that.
        assert answer["unconfirmed"] is True and answer["outcome_unknown"] is True
        assert not answer["error"].startswith("send failed")
        # And it stays where the chat finds it, until someone dismisses it.
        (entry,) = w.gw.SENDS.entries()
        assert entry["status"] == "error" and entry["outcome_unknown"] is True
        assert entry["id"] == answer["request_id"] and entry["message"] == "Thanks."
        assert [e["id"] for e in w.gw.SENDS.listed(answer["chat"])] == [entry["id"]]
        assert w.smtp.sessions == 1 and t.records(w.gw) == []
    finally:
        done(w)

    # The provider files the Sent copy: then the mailbox is asked, and says.
    w = world(save_sent=False)
    try:
        gw, box, mirror, smtp = w.gw, w.box, w.mirror, w.smtp
        box.put("INBOX", t.msg("a@x.org", day=1))
        mirror.poll()
        smtp.drop = "dot"
        status, answer = w.send(recipient="a@x.org", author="user", user_approved=True)
        mid = answer["message_id"]
        # Sent does not hold it yet: not confirmed, and no failure either.
        assert status == 502 and answer["unconfirmed"] is True and answer["chat"] == "a@x.org"
        (entry,) = gw.SENDS.entries()
        assert entry["status"] == "sending" and entry["phase"] == "data"
        assert gw.SENDS.due() == [entry["id"]]
        mirror.poll()
        assert mid not in t.by_id(gw) and gw.SENDS.due() == [entry["id"]]
        # A restart in the middle of the wait waits on, and sends nothing.
        mirror = w.restart()
        mirror.poll()
        assert smtp.sessions == 1 and gw.SENDS.due() == [entry["id"]]
        # The copy shows: the mail went out. Its record is the user's.
        box.put("Sent", smtp.mails[0]["data"], date=NOW + 5)
        mirror.poll()
        record = t.by_id(gw)[mid]
        assert record["author"] == "user" and record["chat"] == "a@x.org"
        assert [e["message_id"] for e in w.told()] == [mid]
        assert gw.SENDS.entries() == [] and gw.SENDS.due() == []
        assert smtp.sessions == 1 and blobs(gw) == referenced(gw)
    finally:
        done(w)

    # …and where no copy shows within the grace, it did not go, as far as
    # anyone can tell: the send ends, unsent again, as "outcome unknown".
    w = world(save_sent=False, policy="verify")
    try:
        gw, box, mirror, smtp = w.gw, w.box, w.mirror, w.smtp
        _s, queued = w.send(to=["ann@x.org"])
        smtp.drop, smtp.takes = "dot", False
        gw._decide_send(queued["request_id"], "approve")
        assert gw.SENDS.get(queued["request_id"])["status"] == "sending"
        mirror.poll()
        assert gw.SENDS.get(queued["request_id"])["status"] == "sending"
        gw._now = lambda: NOW + gw.MAIL_SENT_GRACE_SECONDS + 1
        mirror.poll()
        entry = gw.SENDS.get(queued["request_id"])
        assert entry["status"] == "error" and entry["error"] == gw.UNKNOWN_OUTCOME
        assert smtp.sessions == 1 and t.records(gw) == [] and gw.SENDS.due() == []
    finally:
        done(w)
    print("ok: a mail handed over without an answer is looked for in Sent, never sent again")


# --------------------------------------------------------------------------
# A send stopped anywhere, and started again
# --------------------------------------------------------------------------

class _Kill(BaseException):
    """The process dies here: no handler runs, nothing is cleaned up."""


def _stopped_after(n: int, *, direct: bool, save_sent: bool, keyed: bool = False):
    """One send with the process killed right after its n-th write — to the
    send's own entry, the ledger, either index, or the mailbox — then a
    restart, and the mirror left to settle. None when the send has fewer
    writes; else what was found, checked against what must hold wherever the
    stop came.

    ``keyed``: the request names itself, and is made again after the restart
    — what a caller does that lost the answer. Whatever the stop left, the
    repeat sends the mail only where nothing of it had left the gateway."""
    w = world(policy="allow" if direct else "verify", save_sent=save_sent)
    try:
        gw, box, mirror, smtp = w.gw, w.box, w.mirror, w.smtp
        box.put("INBOX", t.msg("a@x.org", day=1))
        mirror.poll()
        payload = dict(recipient="a@x.org", images=[
            {"filename": "a.txt", "data": base64.b64encode(b"file").decode()}])
        if keyed:
            payload["request_key"] = "stop-1"
        queued = None if direct else w.send(**payload)[1]
        count = {"n": 0}
        armed = []

        def arm(owner, name):
            real = getattr(owner, name)

            def stop_after(*args, **kwargs):
                out = real(*args, **kwargs)
                count["n"] += 1
                if count["n"] == n:
                    raise _Kill()
                return out
            setattr(owner, name, stop_after)
            armed.append((owner, name, real))
        arm(gw, "_atomic_json")
        for name in ("store_media", "write_outbound"):
            arm(gw._ibstore, name)
        for name in ("run", "atomic"):
            arm(mirror.index, name)
        arm(mirror.threads, "resolve")
        arm(box, "append")
        arm(gw._ec, "_smtp_send")
        try:
            if direct:
                w.send(**payload)
            else:
                gw._decide_send(queued["request_id"], "approve")
            killed = False
        except _Kill:
            killed = True
        finally:
            for owner, name, real in armed:
                setattr(owner, name, real)
        if not killed:
            return None
        # (What stood of the request when the stop came — on disk, which is
        # all a start has to go by: status, mark, and whether it was direct.)
        on_disk = gw.SENDS.by_key("stop-1") if keyed else None
        stood = on_disk is not None
        on_disk = on_disk and (on_disk["status"], on_disk.get("phase"),
                               bool(on_disk.get("direct")))

        def provider():
            # Where the provider files the Sent copy, it does so for a mail
            # it took — whenever that was.
            if not save_sent and smtp.mails and not w.sent_copies():
                box.put("Sent", smtp.mails[0]["data"], date=NOW + 5)
        provider()
        mirror = w.restart()
        for _ in range(3):
            provider()
            mirror.poll()
            gw._between_polls(mirror)
        if gw.SENDS.due():
            # Handed over, no answer seen, and no copy in a Sent folder the
            # provider files: that is waited for, as long as the grace.
            assert not save_sent and not smtp.mails, (n, gw.SENDS.entries())
            gw._now = lambda: NOW + gw.MAIL_SENT_GRACE_SECONDS + 1
            mirror.poll()

        taken = len(smtp.mails)
        assert taken <= 1 and smtp.sessions <= 2, (n, "never sent twice")
        mid = (queued or {}).get("message_id") or (
            smtp.parsed()["Message-Id"].strip("<>") if taken else None)
        records = [r for r in t.records(gw) if r["type"] == gw._ibstore.T_OUTBOUND]
        told = [e for e in w.told() if e["message_id"] == mid]
        entry = gw.SENDS.get(queued["request_id"]) if queued else None
        # A mail the server took has its record, the rail was told, and Sent
        # holds one copy; a mail it did not take has none of the three.
        assert len(records) == taken == len(w.sent_copies()), (n, taken, records)
        assert bool(told) == bool(taken), (n, told)
        if taken:
            assert records[0]["message_id"] == mid and records[0]["text"] == "Thanks."
            assert len(records[0]["attachments"]) == 1 and records[0]["author"] == "agent"
        if direct and taken and not keyed:
            assert gw.SENDS.entries() == [], (n, "no entry outlives a direct send that went out")
        elif direct and taken:
            # (One that named its request stays, done, for the repeat's answer.)
            (left,) = gw.SENDS.entries()
            assert left["status"] == "approved" and left["done"] and gw.SENDS.listed() == []
        elif direct:
            # Stopped before anything was handed over: its caller had no
            # answer. Without a key it is dropped; with one it ends as not
            # sent, for certain, and is listed. Stopped after the mark
            # "handed over": nobody can know — kept, saying so, where the
            # chat lists it.
            left = gw.SENDS.entries()
            assert len(left) <= 1 and all(
                e["status"] == "error"
                and (e["outcome_unknown"], e["error"]) in (
                    [(True, gw.UNKNOWN_OUTCOME), (False, gw.STOPPED_UNSENT)] if keyed
                    else [(True, gw.UNKNOWN_OUTCOME)]) for e in left), (n, left)
            assert gw.SENDS.listed() == left
        elif taken:
            assert entry["status"] == "approved" and entry["done"] and "images" not in entry, n
            assert entry["message_id"] == mid and entry["sent_at"]
        else:
            # Stopped after the mark "handed over" and before anything was:
            # nothing left, and nobody can know that — it is said so.
            assert entry["status"] == "error" and entry["error"] == gw.UNKNOWN_OUTCOME, (n, entry)
        assert gw.SENDS.due() == [], n
        assert mirror.index.one("SELECT COUNT(*) AS n FROM claims")["n"] == 0, n
        index = {row["message_id"] for row in mirror.index.rows("SELECT message_id FROM mails")}
        assert index == {r["message_id"] for r in t.records(gw)}, n
        assert referenced(gw) <= blobs(gw), (n, "no record refers to a blob that is gone")
        found = {"taken": taken, "status": (entry or {}).get("status"),
                 "left": on_disk}
        if not keyed:
            return found

        # The same request again, as its caller makes it who got no answer.
        # What the stop left on disk, by status and mark, decides:
        #   no entry (stopped before it stood)        the first send: carried out
        #   error, outcome known (nothing had left)   a new attempt: carried out
        #   error, outcome unknown (handed over)      answered so; nothing is sent
        #   approved (the server took it)             answered "sent"; nothing is sent
        # ("Carried out" is: sent, where the policy sends; queued, where it queues.)
        before = gw.SENDS.by_key("stop-1")
        state = (None if before is None
                 else (before["status"], before.get("phase"), before.get("outcome_unknown")))
        # A send that named its request never just goes: what stood when the
        # stop came stands after it, as what became of it.
        assert (state is not None) == stood, (n, state, stood)
        sessions = smtp.sessions
        status, answer = w.send(**payload)
        for _ in range(2):
            provider()
            mirror.poll()
            gw._between_polls(mirror)
        assert len(smtp.mails) <= 1, (n, state, "never sent twice")
        if state is None or state == ("error", None, False):
            assert not taken and "repeat" not in answer, (n, state, answer)
            assert direct and status == 200 and answer["status"] == "sent", (n, state, answer)
            assert len(smtp.mails) == 1 and smtp.sessions == sessions + 1
            assert gw.SENDS.by_key("stop-1")["message_id"] == answer["message_id"]
            found["repeat"] = "sent"
        elif state == ("error", None, True):
            assert not taken and status == 502 and answer["unconfirmed"] is True, (n, answer)
            assert answer["outcome_unknown"] is True and answer["repeat"] is True
            assert "sent" not in answer and smtp.sessions == sessions and not smtp.mails
            found["repeat"] = "unknown"
        else:
            assert state[0] == "approved" and taken, (n, state)
            assert status == 200 and answer["repeat"] is True and answer["message_id"] == mid
            assert smtp.sessions == sessions
            found["repeat"] = "told"
        assert len(gw.SENDS.listed()) == (1 if found["repeat"] == "unknown" else 0), n
        records = [r for r in t.records(gw) if r["type"] == gw._ibstore.T_OUTBOUND]
        assert len(records) == len(smtp.mails) == len(w.sent_copies()), (n, records)
        assert gw.SENDS.due() == [], n
        return found
    finally:
        done(w)


def test_a_send_stopped_anywhere_is_never_made_twice():
    points = 0
    for direct in (False, True):
        for save_sent in (True, False):
            n, outcomes = 1, []
            while True:
                found = _stopped_after(n, direct=direct, save_sent=save_sent)
                if found is None:
                    break
                outcomes.append(found)
                n += 1
            assert len(outcomes) >= (6 if save_sent else 5), (direct, save_sent, len(outcomes))
            # An approval given is carried out — but for the one stop no mark
            # can tell from "sent": that ends as unknown, never as a resend.
            sent = [o for o in outcomes if o["taken"]]
            assert sent and (direct or len(outcomes) - len(sent) == 1), outcomes
            points += len(outcomes)
    # The same walk for a request that names itself and is made again after
    # the restart (a caller that lost the answer): wherever the stop came,
    # the repeat sends the mail only where nothing of it had left — and every
    # way a repeat can end is met on the way.
    walked: dict = {}
    for direct in (False, True):
        for save_sent in (True, False):
            n, outcomes = 1, []
            while True:
                found = _stopped_after(n, direct=direct, save_sent=save_sent, keyed=True)
                if found is None:
                    break
                outcomes.append(found)
                n += 1
            ended = [o["repeat"] for o in outcomes]
            # The hand-over's one blind spot is met once, and a mail the
            # server took is "sent" to the repeat ever after.
            assert ended.count("unknown") == 1 and "told" in ended, ended
            if direct:
                # A stop before the hand-over, then the blind spot, then a
                # mail the server took: in that order, and no other — the
                # repeat is what sends a direct send that had not left.
                assert ended == sorted(ended, key=("sent", "unknown", "told").index), ended
                assert ended.count("sent") >= 2, ended
            else:
                # An approval given is carried out by the start itself: the
                # repeat never has anything to send.
                assert "sent" not in ended, ended
            for o in outcomes:
                walked.setdefault(o["left"], set()).add(o["repeat"])
            points += len(outcomes)
    # Every state a stop leaves a send in, and what the same request made
    # again after the restart does there. A repeat sends in two of them, and
    # in both nothing of the mail had left the gateway: the mark "data" is on
    # disk before any of the message is on the wire, so an entry without it
    # was never handed over.
    assert walked == {
        # (status, mark, direct) on disk      the repeat
        None:                                 {"sent"},     # no entry stood: the first send
        ("sending", None, True):              {"sent"},     # ended "not sent" by the start;
                                                            # the repeat is a new attempt
        ("sending", None, False):             {"told"},     # allowed: the start sends it, once
        ("sending", "data", True):            {"unknown"},  # handed over, no answer seen:
        ("sending", "data", False):           {"unknown"},  # never sent again
        ("approved", "sent", True):           {"told"},     # the server took it
        ("approved", "sent", False):          {"told"},
    }, walked
    _the_states_no_stop_changes()
    print(f"ok: stopped after any of {points} writes, a send is made at most once — also "
          f"by a repeat of its request — and what the server took is on record")


def _the_states_no_stop_changes():
    """The rest of what a start can find under a request's key — the states
    a send rests in, which a stop leaves as they are — and what a repeat of
    the request does in each, before a restart and after one. It sends in
    one: a send that failed with nothing having left the gateway."""
    def rest(make, *, policy, expect, sends):
        w = world(policy=policy)
        try:
            gw, box, smtp = w.gw, w.box, w.smtp
            box.put("INBOX", t.msg("a@x.org", day=1))
            w.mirror.poll()
            press = dict(recipient="a@x.org", message="Once.", request_key="rest-1")
            first = make(w, press)
            mails, sessions = len(smtp.mails), smtp.sessions
            for again in (False, True):
                if again:
                    w.restart()
                    assert (len(smtp.mails), smtp.sessions) == (mails, sessions), \
                        (expect, "a start sends nothing of a send at rest")
                elif sends:
                    continue    # (asked once, after the restart: it sends)
                status, answer = w.send(**press)
                if sends:
                    break
                assert status == expect[0], (expect, again, status, answer)
                assert answer["repeat"] is True and "sent" not in answer, (expect, answer)
                for name, value in expect[1].items():
                    assert answer[name] == value, (expect, name, answer)
                assert answer["message_id"] == first["message_id"]
                assert (len(smtp.mails), smtp.sessions) == (mails, sessions), expect
            if sends:
                assert status == expect[0] and "repeat" not in answer, (expect, answer)
                assert answer["message_id"] != first["message_id"], "composed afresh"
                assert (len(smtp.mails), smtp.sessions) == (mails + 1, sessions + 1)
                assert gw.SENDS.get(first["id"]) is None
                # …and that new attempt is a send like any other: remembered.
                assert w.send(**press) == (200, {**answer, "repeat": True})
                assert len(smtp.mails) == mails + 1
        finally:
            done(w)

    def queued(w, press):
        _s, answer = w.send(**press)
        return w.gw.SENDS.get(answer["request_id"])

    def decided(verb):
        def make(w, press):
            entry = queued(w, press)
            w.gw._decide_send(entry["id"], verb)
            return entry
        return make

    def sent(w, press):
        w.send(**press)
        return w.gw.SENDS.by_key(press["request_key"])

    def unknown(dismissed):
        def make(w, press):
            w.smtp.drop = "dot"
            w.send(**press)
            w.smtp.drop = None
            entry = w.gw.SENDS.by_key(press["request_key"])
            assert entry["status"] == "error" and entry["outcome_unknown"] is True
            if dismissed:
                w.gw.SENDS.dismiss(entry["id"])
            return entry
        return make

    def failed(w, press):
        w.smtp.reject_data = "554 5.7.1 content rejected"
        w.gw._accept_send(dict(press), HOST, caller_waits=lambda: False)
        w.smtp.reject_data = None
        entry = w.gw.SENDS.by_key(press["request_key"])
        assert entry["status"] == "error" and entry["outcome_unknown"] is False
        return entry

    def unsaid(w, press):
        entry = failed(w, press)
        return w.gw.SENDS.update(entry["id"], drop=("outcome_unknown",))

    not_known = {"unconfirmed": True, "outcome_unknown": True}
    #    the entry at rest                              a repeat is answered     and sends
    rest(queued, policy="verify", expect=(202, {"status": "pending_approval"}), sends=False)
    rest(decided("reject"), policy="verify", expect=(409, {"status": "rejected"}), sends=False)
    rest(decided("retract"), policy="verify", expect=(409, {"status": "retracted"}),
         sends=False)
    rest(decided("approve"), policy="verify", expect=(200, {"status": "sent"}), sends=False)
    rest(sent, policy="allow", expect=(200, {"status": "sent"}), sends=False)
    rest(unknown(False), policy="allow", expect=(502, not_known), sends=False)
    rest(unknown(True), policy="allow", expect=(502, not_known), sends=False)
    rest(unsaid, policy="allow", expect=(502, not_known), sends=False)
    rest(failed, policy="allow", expect=(200, {"status": "sent"}), sends=True)


def test_the_stop_between_the_servers_answer_and_its_mark():
    """The one window no ordering closes: the server took the mail, and the
    process dies before it can write that down."""
    for save_sent in (True, False):
        w = world(policy="verify", save_sent=save_sent)
        try:
            gw, box, mirror, smtp = w.gw, w.box, w.mirror, w.smtp
            box.put("INBOX", t.msg("a@x.org", day=1))
            mirror.poll()
            press = dict(recipient="a@x.org", request_key="blind-1")
            _s, queued = w.send(**press)
            rid, mid = queued["request_id"], queued["message_id"]
            real_update = gw.SENDS.update

            def dies_first(request_id, drop=(), **changes):
                if changes.get("phase") == "sent":
                    raise _Kill()
                return real_update(request_id, drop=drop, **changes)
            gw.SENDS.update = dies_first
            try:
                gw._decide_send(rid, "approve")
                raise AssertionError("the stop did not happen")
            except _Kill:
                pass
            assert len(smtp.mails) == 1 and gw.SENDS.get(rid)["phase"] == "data"
            if not save_sent:
                box.put("Sent", smtp.mails[0]["data"], date=NOW + 5)
            mirror = w.restart()
            for _ in range(3):
                mirror.poll()
                gw._between_polls(mirror)
            entry = gw.SENDS.get(rid)
            assert smtp.sessions == 1 and len(smtp.mails) == 1, "not sent again"
            status, said = w.send(**press)
            if save_sent:
                # Nothing can say: reported as what it is, text and all.
                assert entry["status"] == "error" and entry["error"] == gw.UNKNOWN_OUTCOME
                assert entry["message"] == "Thanks." and mid not in t.by_id(gw)
                # …and to the request made again: not known — never "failed",
                # never a new attempt.
                assert status == 502 and said["unconfirmed"] is True, said
                assert said["outcome_unknown"] is True and "sent" not in said
            else:
                # The provider's Sent copy says it went out: finished.
                assert entry["status"] == "approved" and entry["done"]
                assert t.by_id(gw)[mid]["author"] == "agent" and len(w.told()) == 1
                assert status == 200 and said["status"] == "sent" and said["message_id"] == mid
            assert said["repeat"] is True
            assert smtp.sessions == 1 and len(smtp.mails) == 1, "nor by the repeat"
        finally:
            done(w)
    print("ok: a stop between the server's answer and its mark never resends; Sent decides "
          "where it can")


# --------------------------------------------------------------------------
# The mailbox follows the chat
# --------------------------------------------------------------------------

def thread_of_three(w) -> None:
    """A thread with a mail in INBOX (with a file), one a client archived
    and the user's reply in Sent — and another chat beside it."""
    box, mirror = w.box, w.mirror
    box.put("INBOX", t.msg("a@x.org", day=1, files=[("f.pdf", "application/pdf", b"%PDF-1")]))
    archived = box.put("INBOX", t.msg("b@x.org", subject="Re: Quarterly numbers",
                                      refs=["a@x.org"], irt="a@x.org", day=2))
    box.put("Sent", t.msg("s@example.org", sender=ME, to="ann@x.org",
                          subject="Re: Quarterly numbers", refs=["a@x.org"], irt="a@x.org",
                          day=3))
    box.put("INBOX", t.msg("other@x.org", subject="Other", day=4))
    mirror.poll()
    box.move_mail("INBOX", archived, "Archive")
    mirror.poll()
    assert {r["chat"] for r in t.records(w.gw)} == {"a@x.org", "other@x.org"}
    assert not any(r["removed_at"] for r in t.records(w.gw))


def flags(box, folder, message_id) -> frozenset:
    return box.boxes[folder]["mails"][box.uid_of(folder, message_id)]["flags"]


def held_in(box, folder) -> list:
    return sorted((email.message_from_bytes(mail["raw"]).get("Message-ID") or "").strip("<>")
                  for mail in box.boxes[folder]["mails"].values())


def test_mark_as_read_sets_seen_in_inbox_and_archive():
    w = world()
    try:
        gw, box, mirror = w.gw, w.box, w.mirror
        thread_of_three(w)
        fetched = len(box.fetched)
        assert mirror.mark_read("a@x.org") == 2
        assert "\\Seen" in flags(box, "INBOX", "a@x.org")
        assert "\\Seen" in flags(box, "Archive", "b@x.org")
        # Not the user's own mail in Sent, and no other thread's.
        assert flags(box, "Sent", "s@example.org") == frozenset()
        assert flags(box, "INBOX", "other@x.org") == frozenset()
        assert sorted(x[:2] for x in box.writes) == [("seen", "Archive"), ("seen", "INBOX")]
        # Again, and for a chat nobody knows: nothing breaks, nothing else is touched.
        assert mirror.mark_read("a@x.org") == 2 and mirror.mark_read("nope@x.org") == 0
        assert len(box.writes) == 4
        # The mirror does not read its own flag back as news.
        mirror.poll()
        assert len(box.fetched) == fetched and len(t.records(gw)) == 4
        # A mail a client took away since the last poll is not there to flag.
        box.remove("INBOX", box.uid_of("INBOX", "a@x.org"))
        assert mirror.mark_read("a@x.org") == 1
        mirror.poll()

        # A folder renumbered underneath: the UIDs the index holds name other
        # mails now. Nothing is flagged, and the caller is told to come again.
        box.put("INBOX", t.msg("c@x.org", subject="Re: Quarterly numbers", refs=["a@x.org"],
                               day=5))
        mirror.poll()
        box.renumber("INBOX", offset=1)     # every UID now names its neighbour
        try:
            mirror.mark_read("a@x.org")
            raise AssertionError("flagged by stale UIDs")
        except gw.FolderChanged:
            pass
        assert flags(box, "INBOX", "c@x.org") == frozenset()
        assert flags(box, "INBOX", "other@x.org") == frozenset()
        mirror.poll()
        mirror.poll()
        assert mirror.mark_read("a@x.org") == 2
        assert "\\Seen" in flags(box, "INBOX", "c@x.org")
        assert flags(box, "INBOX", "other@x.org") == frozenset()
    finally:
        done(w)
    print("ok: mark as read sets \\Seen on the thread's mails in INBOX and the archive only")


def test_archive_moves_the_inbox_mails_and_the_mirror_follows():
    w = world()
    try:
        gw, box, mirror = w.gw, w.box, w.mirror
        thread_of_three(w)
        events, fetched = len(w.events), len(box.fetched)
        assert mirror.archive("a@x.org") == {"moved": 1, "flagged": 0}
        assert held_in(box, "INBOX") == ["other@x.org"]
        assert held_in(box, "Archive") == ["a@x.org", "b@x.org"]
        assert held_in(box, "Sent") == ["s@example.org"], "only the INBOX mails move"
        # The index knew at once: the polls that follow read nothing, call
        # nothing gone, and tell the rail nothing.
        for _ in range(3):
            mirror.poll()
        assert len(w.events) == events and len(box.fetched) == fetched
        assert not any(r["removed_at"] for r in t.records(gw))
        assert mirror.thread("a@x.org")["live"] == 3
        # Nothing left to move: no write at all.
        writes = len(box.writes)
        assert mirror.archive("a@x.org") == {"moved": 0, "flagged": 0}
        assert mirror.archive("nope@x.org") == {"moved": 0, "flagged": 0}
        assert len(box.writes) == writes
        # A new mail brings the thread back to the INBOX; archived again, it
        # alone moves. And the archived mails are still the thread's to mark.
        box.put("INBOX", t.msg("c@x.org", subject="Re: Quarterly numbers", refs=["a@x.org"],
                               day=5))
        mirror.poll()
        assert mirror.archive("a@x.org") == {"moved": 1, "flagged": 0}
        assert mirror.mark_read("a@x.org") == 3
        # A mail a client moved first is not there to move; the settle pass
        # finds where it went, as for any mail that left a folder.
        box.put("INBOX", t.msg("d@x.org", subject="Re: Quarterly numbers", refs=["a@x.org"],
                               day=6))
        mirror.poll()
        box.move_mail("INBOX", box.uid_of("INBOX", "d@x.org"), "Archive")
        assert mirror.archive("a@x.org") == {"moved": 0, "flagged": 0}
        mirror.poll()
        assert not t.by_id(gw)["d@x.org"]["removed_at"]
        # A server that refuses: an error for the caller, nothing changed.
        box.put("INBOX", t.msg("e@x.org", subject="Re: Quarterly numbers", refs=["a@x.org"],
                               day=7))
        mirror.poll()
        box.no_writes.add("INBOX")
        try:
            mirror.archive("a@x.org")
            raise AssertionError("no error")
        except gw.MailboxError:
            pass
        assert "e@x.org" in held_in(box, "INBOX")
    finally:
        done(w)
    print("ok: archive moves the thread's INBOX mails, and the mirror follows by its index")


def test_read_and_archive_touch_only_the_mails_the_chat_counted():
    """The hook that follows a read or an archive is served after the fact —
    behind the pass that has the mirror's lock, and that pass may just have
    mirrored a new mail of the thread. With ``message_ids`` the gateway acts
    on what the chat held when the user acted, not on what the thread holds
    by the time it gets the lock."""
    w = world()
    try:
        gw, box, mirror = w.gw, w.box, w.mirror
        box.put("INBOX", t.msg("a@x.org", day=1))
        mirror.poll()
        calls, results = [], {}

        def rail(kind, payload):
            # The web gateway: the user's own reply, mirrored from Sent, is a
            # read of the chat as it stood — one mail, A. Its hook is fired
            # on a thread and reaches the gateway while this very pass still
            # has the lock.
            if kind == "inbound" and payload.get("direction") == "out":
                def hook():
                    results["seen"] = mirror.mark_read(payload["chat"], {"a@x.org"})
                calls.append(threading.Thread(target=hook))
                calls[-1].start()
                threading.Event().wait(0.2)
            return "ok"
        gw._rail_send = rail
        # Between two polls: the user answers from a mail client, and the
        # correspondent writes again. Sent is read first, both are told in
        # one flush.
        box.put("Sent", t.msg("s@example.org", sender=ME, to="ann@x.org",
                              subject="Re: Quarterly numbers", refs=["a@x.org"],
                              irt="a@x.org", day=2))
        box.put("INBOX", t.msg("m2@x.org", subject="Re: Quarterly numbers",
                               refs=["a@x.org", "s@example.org"], irt="s@example.org", day=3))
        mirror.poll()
        for call in calls:
            call.join(30)
        assert results == {"seen": 1}, results
        assert "\\Seen" in flags(box, "INBOX", "a@x.org")
        assert flags(box, "INBOX", "m2@x.org") == frozenset(), \
            "a mail the chat counts as unread was flagged read in the mailbox"
        # Without the list, the call is what it was: the thread as it stands.
        gw._rail_send = lambda kind, payload: "ok"
        assert mirror.mark_read("a@x.org") == 2
        assert "\\Seen" in flags(box, "INBOX", "m2@x.org")

        # Archive: the chat as it was archived moves; what arrived since —
        # and un-archived it — stays in the INBOX.
        box.put("INBOX", t.msg("m3@x.org", subject="Re: Quarterly numbers",
                               refs=["a@x.org"], day=4))
        mirror.poll()
        assert mirror.archive("a@x.org", {"a@x.org", "m2@x.org"}) == {"moved": 2, "flagged": 0}
        assert held_in(box, "INBOX") == ["m3@x.org"]
        assert sorted(held_in(box, "Archive")) == ["a@x.org", "m2@x.org"]
        # None of them, and ids the thread does not hold: nothing is touched.
        writes = len(box.writes)
        assert mirror.archive("a@x.org", set()) == {"moved": 0, "flagged": 0}
        assert mirror.archive("a@x.org", {"other@x.org", "s@example.org"}) == {
            "moved": 0, "flagged": 0}
        assert mirror.mark_read("a@x.org", set()) == 0 and len(box.writes) == writes
        assert held_in(box, "INBOX") == ["m3@x.org"]
        # The archived ones are still the thread's to mark, by their ids.
        assert mirror.mark_read("a@x.org", {"m2@x.org", "m3@x.org"}) == 2
        assert "\\Seen" in flags(box, "INBOX", "m3@x.org")
    finally:
        done(w)

    # Over HTTP: the list as the caller has the ids, brackets or not.
    w = world(CHAT_ERASE_TOKEN=ERASE)
    try:
        gw, box, mirror = w.gw, w.box, w.mirror
        long_id = "x" * 300 + "@x.org"       # mirrored under a digest of itself
        box.put("INBOX", t.msg("a@x.org", day=1))
        box.put("INBOX", t.msg("b@x.org", subject="Re: Quarterly numbers", refs=["a@x.org"],
                               day=2))
        box.put("INBOX", t.msg(long_id, subject="Re: Quarterly numbers", refs=["a@x.org"],
                               day=3))
        mirror.poll()
        digest = next(r["message_id"] for r in t.records(gw)
                      if r["message_id"].endswith(gw._OVERLONG_DOMAIN))
        server, base = t.serve(gw)

        def post(path, **body):
            status, _h, raw = t.call(base + path, token=t.TOKEN, body=json.dumps(
                {"chat": "a@x.org", "account": ACCOUNT, **body}).encode())
            return status, json.loads(raw)
        try:
            assert post("/chats/read", message_ids=["<b@x.org>"]) == (200, {"ok": True, "seen": 1})
            assert "\\Seen" in flags(box, "INBOX", "b@x.org")
            assert flags(box, "INBOX", "a@x.org") == frozenset()
            assert post("/chats/read", message_ids=[digest, long_id])[1]["seen"] == 1
            assert post("/chats/read", message_ids=[])[1]["seen"] == 0
            for bad in ("a@x.org", [1], [["a@x.org"]], {"a@x.org": 1}, 7):
                for action in ("read", "archive"):
                    status, said = post(f"/chats/{action}", message_ids=bad)
                    assert status == 400 and "message_ids" in said["error"], (bad, said)
            assert flags(box, "INBOX", "a@x.org") == frozenset()
            assert post("/chats/archive", message_ids=["a@x.org", "nope@x.org"])[1]["moved"] == 1
            assert sorted(held_in(box, "INBOX")) == sorted(["b@x.org", long_id])
            # A long thread's list fits: more ids than the old body limit held.
            many = [f"{'m' * 200}{n}@x.org" for n in range(600)] + ["b@x.org"]
            assert post("/chats/archive", message_ids=many)[1]["moved"] == 1
            # Null is "no list"; without one, as before: all of the thread.
            assert post("/chats/read", message_ids=None)[1]["seen"] == 3
            assert post("/chats/archive")[1]["moved"] == 1
        finally:
            server.shutdown()
    finally:
        done(w)
    print("ok: read and archive act on the mails the caller counted, not on later arrivals")


def test_archive_on_servers_that_can_do_less():
    # No archive folder: made, by the configured name.
    w = world(folders=("INBOX", "Sent"), MAIL_ARCHIVE_FOLDER="Filed")
    try:
        gw, box, mirror = w.gw, w.box, w.mirror
        box.roles["archive"] = "Filed"
        box.put("INBOX", t.msg("a@x.org", day=1))
        mirror.poll()
        events = len(w.events)
        assert mirror.archive("a@x.org") == {"moved": 1, "flagged": 0}
        assert ("create", "Filed") in box.writes and held_in(box, "Filed") == ["a@x.org"]
        # The mirror had never seen that folder: it is taken in there and
        # then, and the mail has its place in it at once.
        row = mirror.index.one("SELECT role, uid FROM locations WHERE message_id = 'a@x.org'")
        assert (row["role"], row["uid"]) == ("archive", box.uid_of("Filed", "a@x.org"))
        assert mirror._folder("archive")["name"] == "Filed"
        assert settling(mirror, "a@x.org") is None
        for _ in range(3):
            mirror.poll()
        assert not t.by_id(gw)["a@x.org"]["removed_at"] and len(w.events) == events
        assert settling(mirror, "a@x.org") is None
        assert mirror.mark_read("a@x.org") == 1
    finally:
        done(w)
    # A server that does not say which UID a moved mail got: found by its id.
    w = world()
    try:
        gw, box, mirror = w.gw, w.box, w.mirror
        box.names_uids = False
        box.put("INBOX", t.msg("a@x.org", day=1))
        mirror.poll()
        assert mirror.archive("a@x.org") == {"moved": 1, "flagged": 0}
        row = mirror.index.one("SELECT role, uid FROM locations WHERE message_id = 'a@x.org'")
        assert (row["role"], row["uid"]) == ("archive", box.uid_of("Archive", "a@x.org"))
        assert settling(mirror, "a@x.org") is None
    finally:
        done(w)
    # A server with neither MOVE nor UIDPLUS: the copy is made, the original
    # stays flagged \Deleted (nothing expunges a whole folder), and that is
    # said — and remembered, so the mail is not copied a second time.
    w = world()
    try:
        gw, box, mirror = w.gw, w.box, w.mirror
        box.can_expunge = False
        box.put("INBOX", t.msg("a@x.org", day=1))
        mirror.poll()
        events, fetched = len(w.events), len(box.fetched)
        assert mirror.archive("a@x.org") == {"moved": 1, "flagged": 1}
        assert held_in(box, "INBOX") == ["a@x.org"] and held_in(box, "Archive") == ["a@x.org"]
        assert "\\Deleted" in flags(box, "INBOX", "a@x.org")
        assert mirror.archive("a@x.org") == {"moved": 0, "flagged": 0}
        assert held_in(box, "Archive") == ["a@x.org"], "one copy"
        for _ in range(2):
            mirror.poll()
        assert len(w.events) == events and len(box.fetched) == fetched
        assert not t.by_id(gw)["a@x.org"]["removed_at"]
        # Marked read where it lives now, not on the copy that is on its way out.
        box.writes.clear()
        assert mirror.mark_read("a@x.org") == 1
        assert [x[:2] for x in box.writes] == [("seen", "Archive")]
        # Once a client expunges the flagged original, the mail is simply
        # where it was moved.
        box.remove("INBOX", box.uid_of("INBOX", "a@x.org"))
        mirror.poll()
        assert not t.by_id(gw)["a@x.org"]["removed_at"]
    finally:
        done(w)
    print("ok: archive makes its folder, finds a moved mail by id, and never expunges a folder")


def test_delete_moves_to_the_trash_then_erases():
    w = world(policy="verify", folders=("INBOX", "Sent", "Archive"))
    try:
        gw, box, mirror = w.gw, w.box, w.mirror
        thread_of_three(w)
        _s, queued = w.send(recipient="a@x.org")
        _s, kept = w.send(recipient="other@x.org")
        mirror.mark_read("a@x.org")     # (flags are no part of what is erased)
        events = len(w.events)
        # A mailbox in which no Trash is found: none is invented. The delete
        # fails, says what to set, and moves and erases nothing.
        result = mirror.delete_chat("a@x.org")
        assert result["errors"] == 3 and result["trashed"] == 0 and result["messages"] == 0
        assert "MAIL_TRASH_FOLDER" in result["error"] and "Trash" in result["error"]
        assert not any(x[0] in ("create", "move") for x in box.writes), box.writes
        assert "Trash" not in box.boxes and len(t.records(gw)) == 4
        assert mirror.thread("a@x.org")["live"] == 3 and len(w.events) == events
        box.boxes["Trash"] = {"validity": 500, "next": 1, "mails": {}}
        result = mirror.delete_chat("a@x.org")
        assert result["errors"] == 0 and result["trashed"] == 3 and result["messages"] == 3
        assert "error" not in result
        assert sorted(result["message_ids"]) == ["a@x.org", "b@x.org", "s@example.org"]
        assert len(result["subjects"]) == 3 and result["pending_sends"] == 1
        assert result["recent"] == 0 and result["media"] >= 7 and result["parts"] == 6
        # Every mail of the thread, from every mirrored folder, is in the
        # Trash — and nothing is expunged.
        assert held_in(box, "Trash") == ["a@x.org", "b@x.org", "s@example.org"]
        assert held_in(box, "INBOX") == ["other@x.org"]
        assert held_in(box, "Archive") == [] and held_in(box, "Sent") == []
        # The mirror of it is gone: records, blobs, both indexes, its queue.
        assert [r["message_id"] for r in t.records(gw)] == ["other@x.org"]
        assert blobs(gw) == referenced(gw)
        assert mirror.thread("a@x.org") is None and mirror.threads.chat_of("b@x.org") is None
        assert mirror.index.one("SELECT COUNT(*) AS n FROM locations WHERE message_id IN "
                                "('a@x.org', 'b@x.org', 's@example.org')")["n"] == 0
        assert gw.SENDS.get(queued["request_id"]) is None
        assert gw.SENDS.get(kept["request_id"])["status"] == "pending"
        # The other chat is untouched, and nothing comes back by itself.
        for _ in range(3):
            mirror.poll()
        assert len(t.records(gw)) == 1 and len(w.events) == events
        assert mirror.mark_read("other@x.org") == 1
        # Unknown now: the answer has delete_chat's shape, all zeros.
        again = mirror.delete_chat("a@x.org")
        assert again["messages"] == again["trashed"] == again["errors"] == 0
        # Restored from the Trash, a mail starts the chat anew.
        box.move_mail("Trash", box.uid_of("Trash", "a@x.org"), "INBOX")
        mirror.poll()
        assert t.by_id(gw)["a@x.org"]["chat"] == "a@x.org" and len(t.records(gw)) == 2
    finally:
        done(w)
    print("ok: delete moves every mail of the thread to the Trash, then erases the mirror")


def test_a_delete_that_cannot_move_everything_erases_nothing():
    w = world()
    try:
        gw, box, mirror = w.gw, w.box, w.mirror
        box.boxes["Trash"] = {"validity": 500, "next": 1, "mails": {}}
        thread_of_three(w)
        # The Trash will not take mail: nothing moves, nothing is erased.
        box.no_writes.add(("into", "Trash"))
        result = mirror.delete_chat("a@x.org")
        assert result["errors"] == 3 and result["trashed"] == 0 and result["messages"] == 0
        assert len(t.records(gw)) == 4 and held_in(box, "Trash") == []
        assert mirror.thread("a@x.org")["live"] == 3
        box.no_writes.clear()
        # One folder will not let go of its mail: the others' mails are in
        # the Trash already, and still nothing is erased.
        box.no_writes.add("Archive")
        result = mirror.delete_chat("a@x.org")
        assert result["errors"] == 1 and result["trashed"] == 2 and result["messages"] == 0
        assert result["message_ids"] == [] and result["subjects"] == []
        assert len(t.records(gw)) == 4 and held_in(box, "Archive") == ["b@x.org"]
        assert held_in(box, "Trash") == ["a@x.org", "s@example.org"]
        # (What went to the Trash is gone from the mirrored folders, and the
        # mirror says so, as for any mail: tombstones, not an erased chat.)
        mirror.poll()
        assert len(t.records(gw)) == 4
        assert sorted(r["message_id"] for r in t.records(gw) if r["removed_at"]) == [
            "a@x.org", "s@example.org"]
        # Made again once the server relents, it finishes.
        box.no_writes.clear()
        result = mirror.delete_chat("a@x.org")
        assert result["errors"] == 0 and result["trashed"] == 1 and result["messages"] == 3
        assert [r["message_id"] for r in t.records(gw)] == ["other@x.org"]
        assert held_in(box, "Trash") == ["a@x.org", "b@x.org", "s@example.org"]

        # A ledger that will not let a record go: reported, and what could
        # not be erased is still indexed, for the call that finishes it.
        box.put("INBOX", t.msg("x@x.org", subject="X", day=8))
        box.put("INBOX", t.msg("y@x.org", subject="Re: X", refs=["x@x.org"], day=9))
        mirror.poll()
        real = gw._ibstore.delete_chat
        gw._ibstore.delete_chat = lambda *a, **kw: {**real(*a, **kw), "errors": 1}
        try:
            assert mirror.delete_chat("x@x.org")["errors"] == 1
        finally:
            gw._ibstore.delete_chat = real
    finally:
        done(w)
    # A server that cannot remove what it copied: the mails are in the Trash,
    # the originals stay flagged — and are not mirrored again as new mail,
    # now or after a restart.
    w = world()
    try:
        gw, box, mirror = w.gw, w.box, w.mirror
        box.boxes["Trash"] = {"validity": 500, "next": 1, "mails": {}}
        box.can_expunge = False
        thread_of_three(w)
        result = mirror.delete_chat("a@x.org")
        assert result["errors"] == 0 and result["trashed"] == 3 and result["messages"] == 3
        assert held_in(box, "Trash") == ["a@x.org", "b@x.org", "s@example.org"]
        assert "\\Deleted" in flags(box, "INBOX", "a@x.org")
        mirror = w.restart()
        for _ in range(3):
            mirror.poll()
        assert [r["message_id"] for r in t.records(gw)] == ["other@x.org"]
    finally:
        done(w)
    print("ok: while a mail of the thread cannot be moved to the Trash, nothing is erased")


def test_a_delete_erases_nothing_while_a_mail_is_neither_trashed_nor_known_gone():
    """Nothing is erased while a live mail of the thread is not positively
    in the Trash or positively absent from every mirrored folder."""
    # Archived into a folder the archive had to make, and deleted before the
    # next poll — the page's own order: Delete is offered on the archived chat.
    w = world(folders=("INBOX", "Sent", "Trash"), MAIL_ARCHIVE_FOLDER="Filed")
    try:
        gw, box, mirror = w.gw, w.box, w.mirror
        box.roles["archive"] = "Filed"
        box.put("INBOX", t.msg("a@x.org", day=1))
        box.put("INBOX", t.msg("b@x.org", subject="Re: Quarterly numbers", refs=["a@x.org"],
                               day=2))
        mirror.poll()
        assert mirror.archive("a@x.org") == {"moved": 2, "flagged": 0}
        result = mirror.delete_chat("a@x.org")
        assert result["errors"] == 0 and result["trashed"] == 2 and result["messages"] == 2
        assert held_in(box, "Trash") == ["a@x.org", "b@x.org"] and held_in(box, "Filed") == []
        for _ in range(2):
            mirror.poll()
        assert t.records(gw) == []
    finally:
        done(w)

    # The same where the mirror, for whatever reason, has not taken the
    # folder in: its mails are where no index says, and that is no "gone".
    w = world(folders=("INBOX", "Sent", "Trash"), MAIL_ARCHIVE_FOLDER="Filed")
    try:
        gw, box, mirror = w.gw, w.box, w.mirror
        box.roles["archive"] = "Filed"
        box.put("INBOX", t.msg("a@x.org", day=1))
        box.put("INBOX", t.msg("b@x.org", subject="Re: Quarterly numbers", refs=["a@x.org"],
                               day=2))
        mirror.poll()
        mirror._first_sight = lambda role, name: False      # (as before it was taken in at once)
        assert mirror.archive("a@x.org") == {"moved": 2, "flagged": 0}
        assert mirror._folder("archive") is None
        result = mirror.delete_chat("a@x.org")
        assert result["errors"] == 2 and result["trashed"] == 0 and result["messages"] == 0
        assert "nothing was deleted" in result["error"]
        assert held_in(box, "Filed") == ["a@x.org", "b@x.org"] and held_in(box, "Trash") == []
        assert len(t.records(gw)) == 2 and mirror.thread("a@x.org")["live"] == 2
        del mirror._first_sight
        mirror.poll()       # the folder is read; the mails are found in it
        result = mirror.delete_chat("a@x.org")
        assert result["errors"] == 0 and result["trashed"] == 2 and result["messages"] == 2
        assert held_in(box, "Trash") == ["a@x.org", "b@x.org"] and t.records(gw) == []
    finally:
        done(w)

    # A mail a client moved since the last poll is not where the index says:
    # not there to be trashed, and not known to be gone. It is looked for at
    # once, in every mirrored folder — and while one of them cannot say, the
    # chat stays; the call made again finds the mail, and finishes.
    w = world()
    try:
        gw, box, mirror = w.gw, w.box, w.mirror
        box.put("INBOX", t.msg("a@x.org", day=1))
        box.put("INBOX", t.msg("b@x.org", subject="Re: Quarterly numbers", refs=["a@x.org"],
                               day=2))
        box.put("INBOX", t.msg("other@x.org", subject="Other", day=3))
        mirror.poll()
        events = len(w.events)
        box.move_mail("INBOX", box.uid_of("INBOX", "b@x.org"), "Archive")
        box.refused.add("Archive")
        result = mirror.delete_chat("a@x.org")
        assert result["errors"] == 1 and result["trashed"] == 1 and result["messages"] == 0
        assert "not deleted" in result["error"] and "cannot say" in result["error"]
        assert held_in(box, "Archive") == ["b@x.org"] and held_in(box, "Trash") == ["a@x.org"]
        assert len(t.records(gw)) == 3 and mirror.thread("a@x.org")["live"] == 2
        assert len(w.events) == events, "nothing is told: the chat is as it was"
        box.refused.clear()
        result = mirror.delete_chat("a@x.org")
        assert result["errors"] == 0 and result["trashed"] == 1 and result["messages"] == 2
        assert held_in(box, "Trash") == ["a@x.org", "b@x.org"] and held_in(box, "Archive") == []
        assert [r["message_id"] for r in t.records(gw)] == ["other@x.org"]
    finally:
        done(w)
    # Where every folder answers, one call does it: the mail is found where
    # the client put it, and goes to the Trash from there.
    w = world()
    try:
        gw, box, mirror = w.gw, w.box, w.mirror
        box.put("INBOX", t.msg("a@x.org", day=1))
        box.put("INBOX", t.msg("b@x.org", subject="Re: Quarterly numbers", refs=["a@x.org"],
                               day=2))
        box.put("INBOX", t.msg("other@x.org", subject="Other", day=3))
        mirror.poll()
        box.move_mail("INBOX", box.uid_of("INBOX", "b@x.org"), "Archive")
        result = mirror.delete_chat("a@x.org")
        assert result["errors"] == 0 and result["trashed"] == 2 and result["messages"] == 2
        assert "error" not in result
        assert held_in(box, "Trash") == ["a@x.org", "b@x.org"] and held_in(box, "Archive") == []
        assert [r["message_id"] for r in t.records(gw)] == ["other@x.org"]
        for _ in range(2):
            mirror.poll()
        assert [r["message_id"] for r in t.records(gw)] == ["other@x.org"]
    finally:
        done(w)
    # …but one that is moved on again while the call looks is still open.
    w = world()
    try:
        gw, box, mirror = w.gw, w.box, w.mirror
        box.put("INBOX", t.msg("a@x.org", day=1))
        box.put("INBOX", t.msg("b@x.org", subject="Re: Quarterly numbers", refs=["a@x.org"],
                               day=2))
        mirror.poll()
        box.move_mail("INBOX", box.uid_of("INBOX", "b@x.org"), "Archive")
        real_move = box.move

        def not_there(folder, uids, to_folder, **kwargs):
            if folder == "Archive":     # a client was quicker, once more
                box.move_mail("Archive", box.uid_of("Archive", "b@x.org"), "INBOX")
            return real_move(folder, uids, to_folder, **kwargs)
        box.move = not_there
        result = mirror.delete_chat("a@x.org")
        box.move = real_move
        assert result["errors"] == 1 and result["trashed"] == 1 and result["messages"] == 0
        assert held_in(box, "INBOX") == ["b@x.org"] and len(t.records(gw)) == 2
        result = mirror.delete_chat("a@x.org")
        assert result["errors"] == 0 and result["trashed"] == 1 and result["messages"] == 2
        assert held_in(box, "Trash") == ["a@x.org", "b@x.org"] and t.records(gw) == []
    finally:
        done(w)

    # One a client deleted outright: gone is said only once every folder has
    # answered that it does not hold it.
    w = world()
    try:
        gw, box, mirror = w.gw, w.box, w.mirror
        box.put("INBOX", t.msg("a@x.org", day=1))
        box.put("INBOX", t.msg("b@x.org", subject="Re: Quarterly numbers", refs=["a@x.org"],
                               day=2))
        mirror.poll()
        box.remove("INBOX", box.uid_of("INBOX", "b@x.org"))
        # While one folder will not say, "gone" is not said either.
        box.refused.add("Archive")
        result = mirror.delete_chat("a@x.org")
        assert result["errors"] == 1 and result["trashed"] == 1 and len(t.records(gw)) == 2
        result = mirror.delete_chat("a@x.org")
        assert result["errors"] >= 1 and result["messages"] == 0 and len(t.records(gw)) == 2
        box.refused.clear()
        result = mirror.delete_chat("a@x.org")
        assert result["errors"] == 0 and result["trashed"] == 0 and result["messages"] == 2
        assert t.records(gw) == [] and held_in(box, "Trash") == ["a@x.org"]
    finally:
        done(w)
    w = world()
    try:
        gw, box, mirror = w.gw, w.box, w.mirror
        box.put("INBOX", t.msg("a@x.org", day=1))
        box.put("INBOX", t.msg("b@x.org", subject="Re: Quarterly numbers", refs=["a@x.org"],
                               day=2))
        mirror.poll()
        box.remove("INBOX", box.uid_of("INBOX", "b@x.org"))
        found = len(box.found)
        result = mirror.delete_chat("a@x.org")
        assert result["errors"] == 0 and result["trashed"] == 1 and result["messages"] == 2
        # (Every mirrored folder was asked for it before it was called gone.)
        assert sorted(box.found[found:]) == [
            ("Archive", "b@x.org"), ("INBOX", "b@x.org"), ("Sent", "b@x.org")]
        assert t.records(gw) == [] and held_in(box, "Trash") == ["a@x.org"]
    finally:
        done(w)

    # A mirrored folder that is gone altogether: the same rule — the server
    # says it has no such folder, so nothing is in it.
    w = world()
    try:
        gw, box, mirror = w.gw, w.box, w.mirror
        thread_of_three(w)
        del box.boxes["Archive"]
        result = mirror.delete_chat("a@x.org")
        assert result["errors"] == 0 and result["trashed"] == 2 and result["messages"] == 3
        assert [r["message_id"] for r in t.records(gw)] == ["other@x.org"]
    finally:
        done(w)
    print("ok: delete erases only once every mail is in the Trash or known to be in no folder")


def label_mailbox(box, labels=("INBOX", "Archive")) -> None:
    """Make the fake a mailbox in which a folder is a label: one message
    shows in every folder whose label it has, and in the Trash it has none.
    So a move to the Trash from one of them takes the message out of the
    others, and a move to a folder it is in already adds no second copy —
    the server names the place it has there."""
    real = box.move

    def held(folder) -> dict:
        return {uid: (email.message_from_bytes(mail["raw"]).get("Message-ID") or "")
                .strip("<>") for uid, mail in box.boxes[folder]["mails"].items()}

    def move(folder, uids, to_folder, *, validity=None):
        if folder not in labels or folder not in box.boxes or to_folder not in box.boxes:
            return real(folder, uids, to_folder, validity=validity)
        before = held(folder)
        there = {mid: uid for uid, mid in held(to_folder).items()}
        result = real(folder, uids, to_folder, validity=validity)
        moved = dict(result.moved)
        for uid, new in result.moved.items():
            if to_folder in labels and before[uid] in there:
                added = max(u for u, mid in held(to_folder).items() if mid == before[uid])
                del box.boxes[to_folder]["mails"][added]
                moved[uid] = there[before[uid]] if box.names_uids else None
            elif to_folder not in labels:
                for other in labels:
                    for u, mid in held(other).items():
                        if other != folder and mid == before[uid]:
                            del box.boxes[other]["mails"][u]
        return box.gw.Moved(moved, result.left, result.absent)
    box.move = move


def test_one_message_in_two_folders_is_read_archived_and_deleted_as_one():
    """A mailbox that shows one message in two mirrored folders — a label
    mailbox, where the archive holds every mail of the INBOX as well. The
    mirror gives such a mail both places itself. Moving it to the Trash from
    the one takes it out of the other, and that is no mail a client moved
    away: the delete completes, in one call."""
    w = world()
    try:
        gw, box, mirror = w.gw, w.box, w.mirror
        label_mailbox(box)

        def arrive(raw):
            box.put("INBOX", raw)
            box.put("Archive", raw)
        arrive(t.msg("old@x.org", day=1))
        mirror.poll()
        mirror.poll()
        # Between two polls: two mails arrive and stay, and a third is
        # archived on the phone at once — which has the mirror look for the
        # archive's copies of what it holds.
        arrive(t.msg("y@x.org", sender="Yves <y@x.org>", subject="Lunch?", day=later(10)))
        arrive(t.msg("x@x.org", sender="Xena <x@x.org>", subject="Dinner?", day=later(15)))
        arrive(t.msg("z@x.org", sender="Zoe <z@x.org>", subject="Invoice", day=later(20)))
        box.remove("INBOX", box.uid_of("INBOX", "z@x.org"))
        for _ in range(3):
            mirror.poll()
        chat, other = t.by_id(gw)["y@x.org"]["chat"], t.by_id(gw)["x@x.org"]["chat"]
        for mail, of in (("y@x.org", chat), ("x@x.org", other)):
            assert mirror._places(of, ("inbox", "sent", "archive")) == {
                "archive": {box.uid_of("Archive", mail): mail},
                "inbox": {box.uid_of("INBOX", mail): mail}}, "one mail, two places"
        events = len(w.events)

        # Read: flagged where it is — in both — and nothing else is touched.
        assert mirror.mark_read(chat) == 2
        assert "\\Seen" in flags(box, "INBOX", "y@x.org")
        assert "\\Seen" in flags(box, "Archive", "y@x.org")
        assert flags(box, "INBOX", "x@x.org") == frozenset()

        # Delete: to the Trash from the first place; the second is then
        # empty by this call's own doing. No error, no second call.
        result = mirror.delete_chat(chat)
        assert result["errors"] == 0 and "error" not in result, result
        assert result["trashed"] == 1 and result["messages"] == 1
        assert held_in(box, "Trash") == ["y@x.org"]
        assert "y@x.org" not in held_in(box, "INBOX") + held_in(box, "Archive")
        assert "y@x.org" not in t.by_id(gw) and mirror.thread(chat) is None
        assert mirror.index.one("SELECT COUNT(*) AS n FROM locations WHERE message_id = "
                                "'y@x.org'")["n"] == 0
        for _ in range(3):
            mirror.poll()
        assert "y@x.org" not in t.by_id(gw), "nothing comes back"

        # Archive: out of the INBOX, and the place it has in the archive is
        # the one the index keeps — no second copy, nothing called gone.
        events = len(w.events)
        kept = box.uid_of("Archive", "x@x.org")
        assert mirror.archive(other) == {"moved": 1, "flagged": 0}
        assert "x@x.org" not in held_in(box, "INBOX")
        assert held_in(box, "Archive").count("x@x.org") == 1
        assert mirror._places(other, ("inbox", "sent", "archive")) == {
            "archive": {kept: "x@x.org"}}
        assert settling(mirror, "x@x.org") is None
        for _ in range(3):
            mirror.poll()
        assert len(w.events) == events and not t.by_id(gw)["x@x.org"]["removed_at"]
        assert mirror.mark_read(other) == 1
        # …the same where the server does not say which place that is.
        arrive(t.msg("v@x.org", sender="Vera <v@x.org>", subject="Tea?", day=later(30)))
        arrive(t.msg("u@x.org", sender="Udo <u@x.org>", subject="Bill", day=later(35)))
        box.remove("INBOX", box.uid_of("INBOX", "u@x.org"))
        for _ in range(3):
            mirror.poll()
        third = t.by_id(gw)["v@x.org"]["chat"]
        assert set(mirror._places(third, ("inbox", "archive"))) == {"archive", "inbox"}
        box.names_uids = False
        assert mirror.archive(third) == {"moved": 1, "flagged": 0}
        assert mirror._places(third, ("inbox", "sent", "archive")) == {
            "archive": {box.uid_of("Archive", "v@x.org"): "v@x.org"}}
        box.names_uids = True
        # And the archived chat is deleted like any other.
        result = mirror.delete_chat(other)
        assert result["errors"] == 0 and result["trashed"] == 1 and result["messages"] == 1
        assert held_in(box, "Trash") == ["x@x.org", "y@x.org"]
    finally:
        done(w)

    # Two real copies, where folders are folders: each is moved, and one
    # that a client took away meanwhile is looked for — not taken for the
    # other copy's move.
    w = world()
    try:
        gw, box, mirror = w.gw, w.box, w.mirror
        raw = t.msg("a@x.org", day=1)
        box.put("INBOX", raw)
        box.put("Archive", raw)
        mirror.poll()
        mirror.poll()
        if set(mirror._places("a@x.org", ("inbox", "archive"))) != {"archive", "inbox"}:
            mirror._locate("archive", box.uid_of("Archive", "a@x.org"), "a@x.org")
        # The INBOX copy is moved by a client into the archive: a second
        # copy there, under a UID no index knows.
        box.move_mail("INBOX", box.uid_of("INBOX", "a@x.org"), "Archive")
        result = mirror.delete_chat("a@x.org")
        assert result["errors"] == 0 and result["trashed"] == 2 and result["messages"] == 1
        assert held_in(box, "Trash") == ["a@x.org", "a@x.org"]
        assert held_in(box, "Archive") == [] and held_in(box, "INBOX") == []
        for _ in range(3):
            mirror.poll()
        assert t.records(gw) == [], "no copy was left behind to bring the chat back"
    finally:
        done(w)

    # A server with neither MOVE nor UIDPLUS: a move leaves its original in
    # the folder, flagged \Deleted, and its place marked as moved on. The
    # search for a mail missing from its other place finds that copy — and
    # it is not moved to the Trash again: one mail, one copy in the Trash.
    w = world()
    try:
        gw, box, mirror = w.gw, w.box, w.mirror
        box.can_expunge = False
        raw = t.msg("y@x.org", day=1)
        box.put("INBOX", raw)
        mirror.poll()
        # A second real copy in the archive, which the mirror knows of; a
        # client removes it again before the next poll.
        uid = box.put("Archive", raw)
        mirror._locate("archive", uid, "y@x.org")
        box.remove("Archive", uid)
        result = mirror.delete_chat("y@x.org")
        assert result["errors"] == 0 and result["trashed"] == 1, result
        assert held_in(box, "Trash") == ["y@x.org"]
        assert "\\Deleted" in flags(box, "INBOX", "y@x.org")
        assert t.records(gw) == [] and mirror.thread("y@x.org") is None
        moves = [write for write in box.writes if write[0] == "move"]
        assert moves.count(("move", "INBOX", (1,), "Trash")) == 1, moves
    finally:
        done(w)
    # …and the copy an earlier Archive left in the INBOX, once the archive's
    # copy has gone (a client took it away): the mail is in no mirrored
    # folder, and the leftover is not "found" there and trashed.
    w = world()
    try:
        gw, box, mirror = w.gw, w.box, w.mirror
        box.can_expunge = False
        box.put("INBOX", t.msg("z@x.org", day=1))
        mirror.poll()
        assert mirror.archive("z@x.org")["moved"] == 1
        box.remove("Archive", box.uid_of("Archive", "z@x.org"))
        box.writes.clear()
        result = mirror.delete_chat("z@x.org")
        assert result["errors"] == 0 and result["trashed"] == 0, result
        assert held_in(box, "Trash") == []
        assert [write for write in box.writes if write[0] == "move"] == [
            ("move", "Archive", (1,), "Trash")]
        assert t.records(gw) == [] and mirror.thread("z@x.org") is None
    finally:
        done(w)
    # …but a leftover a client undeleted since (the same UID, no \Deleted
    # any more) is the mail's place again: found there and moved to the
    # Trash, not called absent and erased while it sits live in the INBOX.
    w = world()
    try:
        gw, box, mirror = w.gw, w.box, w.mirror
        box.can_expunge = False
        box.put("INBOX", t.msg("u@x.org", day=1))
        mirror.poll()
        assert mirror.archive("u@x.org")["moved"] == 1
        leftover = box.boxes["INBOX"]["mails"][box.uid_of("INBOX", "u@x.org")]
        leftover["flags"] = leftover["flags"] - {"\\Deleted"}
        box.remove("Archive", box.uid_of("Archive", "u@x.org"))
        box.writes.clear()
        result = mirror.delete_chat("u@x.org")
        assert result["errors"] == 0 and result["trashed"] == 1, result
        assert held_in(box, "Trash") == ["u@x.org"]
        assert "\\Deleted" in flags(box, "INBOX", "u@x.org")
        assert [write for write in box.writes if write[0] == "move"] == [
            ("move", "Archive", (1,), "Trash"), ("move", "INBOX", (1,), "Trash")]
        assert t.records(gw) == [] and mirror.thread("u@x.org") is None
    finally:
        done(w)
    print("ok: one message in two folders is flagged, archived and deleted as the one mail "
          "it is")


def test_the_mailbox_endpoints_and_the_erase_capability():
    for erase in (ERASE, None):
        w = world(**({"CHAT_ERASE_TOKEN": erase} if erase else {}))
        try:
            gw, box, mirror = w.gw, w.box, w.mirror
            gw.EXCLUSIVE_SECONDS = 0.05
            thread_of_three(w)
            server, base = t.serve(gw)

            def post(path, payload, **headers):
                request = t.urllib.request.Request(
                    base + path, data=json.dumps(payload).encode(), method="POST",
                    headers={"Content-Type": "application/json",
                             "Authorization": f"Bearer {t.TOKEN}", **headers})
                try:
                    with t.urllib.request.urlopen(request, timeout=10) as response:
                        return response.status, json.loads(response.read())
                except t.urllib.error.HTTPError as exc:
                    return exc.code, json.loads(exc.read())
            ours = {"chat": "a@x.org", "account": ACCOUNT}
            try:
                if erase is None:
                    # No capability configured: deleting is refused outright.
                    status, said = post("/chats/delete", ours,
                                        **{"X-Chat-Erase-Token": "anything"})
                    assert status == 503 and "CHAT_ERASE_TOKEN" in said["error"]
                    assert len(t.records(gw)) == 4 and box.writes == []
                    continue
                # Read and archive need the gateway's token and no more.
                assert post("/chats/read", ours) == (200, {"ok": True, "seen": 2})
                assert post("/chats/archive", ours) == (200, {"ok": True, "moved": 1,
                                                              "flagged": 0})
                # Not this gateway's mailbox: 404, for all three — a thread of
                # another account is not here, whatever its key.
                for account in ("other@example.org", "", None):
                    body = {"chat": "a@x.org", "account": account}
                    for path in ("/chats/read", "/chats/archive"):
                        assert post(path, body)[0] == 404, (path, account)
                    assert post("/chats/delete", body,
                                **{"X-Chat-Erase-Token": ERASE})[0] == 404
                assert post("/chats/read", {"account": ACCOUNT})[0] == 400
                # An unknown chat is no error.
                unknown = {"chat": "nope@x.org", "account": "ME@example.org"}
                assert post("/chats/read", unknown) == (200, {"ok": True, "seen": 0})
                assert post("/chats/archive", unknown)[1] == {"ok": True, "moved": 0,
                                                             "flagged": 0}
                # Delete is behind the erase capability, checked first.
                assert post("/chats/delete", ours)[0] == 403
                for wrong in ("wrong", t.TOKEN, "erase-toke\xf1"):
                    assert post("/chats/delete", ours,
                                **{"X-Chat-Erase-Token": wrong})[0] == 403, wrong
                assert post("/chats/delete", {}, **{"X-Chat-Erase-Token": "wrong"})[0] == 403
                assert len(t.records(gw)) == 4
                # A mailbox that fails is a 502 with its words; a mirror in
                # the middle of a pass is a 503, to be asked again.
                box.fail = gw.MailboxError("the IMAP connection failed: timed out")
                status, said = post("/chats/read", ours)
                assert status == 502 and "timed out" in said["error"]
                held, release = threading.Event(), threading.Event()

                def hold():
                    with mirror._lock:
                        held.set()
                        release.wait(10)
                holder = threading.Thread(target=hold)
                holder.start()
                held.wait(5)
                try:
                    for path in ("/chats/read", "/chats/archive"):
                        assert post(path, ours)[0] == 503, path
                    assert post("/chats/delete", ours, **{"X-Chat-Erase-Token": ERASE})[0] == 503
                finally:
                    release.set()
                    holder.join()
                assert len(t.records(gw)) == 4
                status, result = post("/chats/delete", ours, **{"X-Chat-Erase-Token": ERASE})
                assert status == 200 and result["status"] == "deleted", result
                assert result["errors"] == 0 and result["trashed"] == 3
                assert result["messages"] == 3 and result["pending_sends"] == 0
                assert isinstance(result["subjects"], list) and len(result["message_ids"]) == 3
                unknown = post("/chats/delete", ours, **{"X-Chat-Erase-Token": ERASE})
                assert unknown[0] == 200 and unknown[1]["messages"] == 0
                assert unknown[1]["errors"] == 0 and unknown[1]["trashed"] == 0
            finally:
                server.shutdown()
        finally:
            done(w)
    print("ok: read, archive and delete over HTTP; delete needs the erase capability")


# --------------------------------------------------------------------------
# What a caller, or a mail, cannot do
# --------------------------------------------------------------------------

_OUR_HEADERS = {"from", "to", "cc", "subject", "message-id", "in-reply-to", "references",
                "date", "content-type", "content-transfer-encoding", "mime-version",
                "x-retinue-message-id"}


def wire_headers(smtp, index=-1) -> list:
    """The header lines of a sent mail as they stood on the wire: names."""
    head = smtp.mails[index]["data"].split(b"\r\n\r\n", 1)[0].decode("utf-8")
    return [line.split(":", 1)[0].lower() for line in head.split("\r\n")
            if line[:1] not in (" ", "\t")]


def test_a_caller_cannot_add_a_header_or_a_recipient():
    w = world()
    try:
        gw, smtp = w.gw, w.smtp
        hostile = [
            "ann@x.org\r\nBcc: victim@x.org", "ann@x.org\nBcc: victim@x.org",
            "ann@x.org, victim@x.org", "ann@x.org; victim@x.org", "ann@x.org victim@x.org",
            "Ann <ann@x.org>, <victim@x.org>", "Ann <ann@x.org> <victim@x.org>",
            "<ann@x.org", "ann@x.org>", "ann@x.org (victim@x.org)", "ann@x.org\x00",
            "ann@x.org\u202e", "\u200bann@x.org", "ann@", "@x.org", "ann", "", "  ",
            "ann@x.org@victim.org", "\"ann@x.org\"@victim.org", "ünï@x.org", "ann@x.örg",
            "a" * 250 + "@x.org", 7, None, ["ann@x.org"], {"address": "ann@x.org"},
            # An address before the brackets is a second address, not a name:
            # the entry is refused — never sent to the bracketed one alone.
            "ann@x.org, Bob <bob@y.org>", "ann@x.org; Bob <bob@y.org>",
            "ann@x.org Bob <bob@y.org>", "victim@x.org <bob@y.org>",
            "Ann, Bob <bob@y.org>", "Ann (victim@x.org) <ann@x.org>",
            "Ann \"unclosed <ann@x.org>", "list: <ann@x.org>", "Ann <ann@x.org> Bob",
            # An encoded word is decoded by whoever writes or reads the header.
            "=?utf-8?q?ceo=40company.com=2C?=y@evil.org", "=?utf-8?q?Ann?= <ann@x.org>",
            "ann=?x@x.org", "\"=?utf-8?q?ceo=40x.org=2C?=\" <ann@x.org>",
            # The plainest form only: no quoted or dotted-odd local part, no
            # literal, no label a host name cannot have.
            "\"ann b\"@x.org", "ann.@x.org", ".ann@x.org", "an..n@x.org", "ann@[10.0.0.1]",
            "ann@x..org", "ann@-x.org", "ann@x-.org", "ann@x_y.org", "ann@.org", "ann@x.org.",
            "a" * 65 + "@x.org", "ann@" + "x" * 64 + ".org"]
        for bad in hostile:
            for field in ("to", "cc"):
                payload = {"to": ["good@x.org"], "message": "x", field: ["good@x.org", bad]}
                status, said = gw._accept_send(payload, HOST)
                assert status == 400 and "address" in said["error"], (bad, said)
        assert smtp.sessions == 0 and gw.SENDS.entries() == []
        # A display name is read past — a quoted one whatever it holds: the
        # mail goes to the address, and only the address stands in the header.
        status, _answer = w.send(to=['"victim@x.org, x" <ann@x.org>', "Dr. O'Neil  <bob@y.org>",
                                     "  Cat <CAT@Z.org>  "], cc=["<dora@w.org>"])
        assert status == 200 and smtp.mails[-1]["rcpt"] == [
            "ann@x.org", "bob@y.org", "cat@z.org", "dora@w.org"]
        # What the grammar takes, the server is given as it stands.
        plain = ["prvs=12ab=ann@x.org", "a.b+c@sub.x-y.example", "o'neil@x.org",
                 "x!y#z$%&*+/?^_`{|}~-@x.org", "ann@localhost", "a" * 64 + "@" + "x" * 63 + ".org"]
        status, _answer = w.send(to=plain)
        assert status == 200 and smtp.mails[-1]["rcpt"] == plain
        assert header(smtp.parsed(), "To").replace("\t", " ") == ", ".join(plain)
        status, _answer = w.send(to=['"victim@x.org, x" <ann@x.org>', "Dr. O'Neil  <bob@y.org>",
                                     "  Cat <CAT@Z.org>  "], cc=["<dora@w.org>"])
        sent = smtp.parsed()
        assert header(sent, "To") == "ann@x.org, bob@y.org, cat@z.org"
        assert header(sent, "Cc") == "dora@w.org" and "victim" not in smtp.mails[-1]["data"].decode()

        # A subject is one line, whatever was typed into it.
        status, answer = w.send(
            to=["ann@x.org"], message="Body.",
            subject="Hi\r\nBcc: victim@x.org\r\n\r\nInjected body\u2028X\x00\x1b[31m")
        assert status == 200 and smtp.mails[-1]["rcpt"] == ["ann@x.org"]
        assert set(wire_headers(smtp)) <= _OUR_HEADERS and "bcc" not in wire_headers(smtp)
        assert wire_headers(smtp).count("subject") == 1
        subject = header(smtp.parsed(), "Subject")
        assert subject == "Hi Bcc: victim@x.org Injected body X [31m", subject
        assert smtp.parsed().get_content().replace("\r\n", "\n") == "Body.\n"
        assert t.by_id(gw)[answer["message_id"]]["subject_line"] == subject

        # The text is text: a lone dot, a line of dots, "From " and what looks
        # like a header arrive as written, and end no mail early.
        text = "Dear Ann,\n.\n..\n.hidden\nFrom here on:\nBcc: victim@x.org\n\nSubject: no\n" \
               "café 😀 line"
        before = len(smtp.mails)
        status, answer = w.send(to=["ann@x.org"], message=text + "\r\n\x00\x07")
        assert status == 200 and len(smtp.mails) == before + 1
        assert smtp.mails[-1]["rcpt"] == ["ann@x.org"]
        assert set(wire_headers(smtp)) <= _OUR_HEADERS
        assert smtp.parsed().get_content().replace("\r\n", "\n").rstrip("\n") == text
        assert t.by_id(gw)[answer["message_id"]]["text"] == text

        # A file name is a name.
        status, answer = w.send(to=["ann@x.org"], images=[{
            "filename": "x.txt\r\nBcc: victim@x.org", "content_type": "text/plain\r\nX-Evil: 1",
            "data": base64.b64encode(b"data").decode()}])
        assert status == 200 and smtp.mails[-1]["rcpt"] == ["ann@x.org"]
        raw = smtp.mails[-1]["data"].decode("utf-8", "replace")
        assert "\r\nBcc:" not in raw and "\r\nX-Evil:" not in raw
        (part,) = smtp.parsed().iter_attachments()
        assert "\n" not in part.get_filename() and "\r" not in part.get_filename()
    finally:
        done(w)
    print("ok: an address is one address, a subject one line, a text only a text")


def hostile_mail(*lines, body="Hello.\r\n") -> bytes:
    return ("\r\n".join(lines) + "\r\n\r\n" + body).encode("utf-8")


def test_a_mail_cannot_add_a_header_or_a_recipient_to_its_reply():
    w = world()
    try:
        gw, box, mirror, smtp = w.gw, w.box, w.mirror, w.smtp
        date = "Date: " + t.email.utils.formatdate(t.T0 + DAY)
        # Everything a sender controls, bent: an encoded line break in the
        # subject and in a display name, a Reply-To whose name reads as two
        # addresses and whose list holds junk, ids that are no ids.
        box.put("INBOX", hostile_mail(
            "Message-ID: <h1@evil.org>",
            "From: =?utf-8?q?Eve=0D=0ABcc=3A_victim=40x=2Eorg?= <eve@evil.org>",
            "To: me@example.org, =?utf-8?q?V=0D=0ABcc=3A_victim=40x=2Eorg?= <bob@y.org>",
            "Cc: \"victim@x.org, victim2@x.org\" <cat@z.org>",
            "Reply-To: \"a@victim.org, b@victim.org\" <real@evil.org>, "
            "=?utf-8?q?x=0D=0A?= <second@evil.org>, <not an address>, @, <pöst@evil.org>",
            "Subject: =?utf-8?q?Hi=0D=0ABcc=3A_victim=40x=2Eorg=0D=0A=0D=0AInjected?=",
            "References: <root@x.org> <evil\"quote@x.org> <ünï@x.org> "
            "<spa ce@x.org> <ok@x.org>",
            "In-Reply-To: <ok@x.org>", date))
        mirror.poll()
        status, answer = w.send(recipient="root@x.org")
        assert status == 200, answer
        # To the Reply-To addresses that are addresses; in copy to the other
        # addresses of the mail — and to nobody a name spelled out.
        assert smtp.mails[-1]["rcpt"] == ["real@evil.org", "second@evil.org",
                                          "bob@y.org", "cat@z.org"], smtp.mails[-1]["rcpt"]
        # The one that is no address a mail can go to is left out aloud.
        assert answer["skipped"] == ["pöst@evil.org"], answer
        names = wire_headers(smtp)
        assert set(names) <= _OUR_HEADERS and len(names) == len(set(names)), names
        raw = smtp.mails[-1]["data"].decode("utf-8")
        assert "victim" not in raw.replace("Re: Hi Bcc: victim@x.org Injected", "")
        sent = smtp.parsed()
        assert header(sent, "Subject") == "Re: Hi Bcc: victim@x.org Injected"
        assert header(sent, "To") == "real@evil.org, second@evil.org"
        assert header(sent, "Cc") == "bob@y.org, cat@z.org"
        assert header(sent, "In-Reply-To") == "<h1@evil.org>"
        # Only what can stand between angle brackets, in ASCII.
        assert header(sent, "References") == "<root@x.org> <ok@x.org> <h1@evil.org>"
        assert sent.get_content().replace("\r\n", "\n") == "Thanks.\n"

        # A sender whose address cannot be written to is not written to —
        # and is not swapped for another.
        box.put("INBOX", hostile_mail(
            "Message-ID: <h2@evil.org>", "From: P <pöst@evil.org>",
            "To: me@example.org", "Subject: Umlaut", date))
        mirror.poll()
        status, answer = w.send(recipient="h2@evil.org")
        assert status == 409 and "nobody" in answer["error"], answer
        # Queued, the entry shows what the gateway built — as data.
        before = len(smtp.mails)
        os.environ["EMAIL_SEND_POLICY"] = '[{"address": "*", "category": "verify"}]'
        status, queued = w.send(recipient="root@x.org", reply="sender")
        entry = gw._public(gw.SENDS.get(queued["request_id"]))
        assert status == 202 and entry["to"] == ["real@evil.org", "second@evil.org"]
        assert entry["cc"] == [] and "\n" not in entry["subject"] and "\r" not in entry["subject"]
        assert "\n" not in entry["recipient"] and len(smtp.mails) == before
        # (A follow-up to the user's own reply: to whom that went, and
        # nobody is left out of it.)
        assert "skipped" not in queued and "skipped" not in entry
    finally:
        done(w)
    print("ok: nothing a mail says becomes a header line or a recipient of its reply")


def test_an_encoded_word_in_an_address_names_nobody():
    """An RFC 2047 encoded word in a Reply-To address is one valid-looking
    address to the envelope and, decoded by the header writer, other people
    in the To header: it is no address a reply goes to."""
    w = world()
    try:
        gw, box, mirror, smtp = w.gw, w.box, w.mirror, w.smtp
        date = "Date: " + t.email.utils.formatdate(t.T0 + DAY)
        word = "=?utf-8?q?ceo=40company.com=2C?=y@evil.org"
        box.put("INBOX", hostile_mail(
            "Message-ID: <e1@evil.org>", "From: Mallory <mallory@evil.org>",
            "To: me@example.org, bob@y.org, =?utf-8?q?cfo=40company.com=2C?=z@evil.org",
            f"Reply-To: {word}", "Subject: Invoice",
            "References: <=?utf-8?q?zz?=@evil.org> <ok@x.org>", date))
        mirror.poll()
        assert t.by_id(gw)["e1@evil.org"]["reply_to"] == [word.lower()], "read as the mail has it"
        assert not gw._sendable(word) and not gw._sendable(word.lower())
        assert not gw._citable("=?utf-8?q?zz?=@evil.org")
        chat = t.by_id(gw)["e1@evil.org"]["chat"]
        status, answer = w.send(recipient=chat)
        assert status == 200, answer
        # To the sender, whose address is one; the two encoded ones are named
        # as left out, and nothing of them is on the wire, decoded or not.
        assert smtp.mails[-1]["rcpt"] == ["mallory@evil.org", "bob@y.org"]
        assert sorted(answer["skipped"]) == sorted(
            [word.lower(), "=?utf-8?q?cfo=40company.com=2c?=z@evil.org"]), answer
        sent, raw = smtp.parsed(), smtp.mails[-1]["data"].decode("utf-8")
        assert header(sent, "To") == "mallory@evil.org" and header(sent, "Cc") == "bob@y.org"
        assert "company.com" not in raw and "=?" not in raw and "evil.org>" in raw
        assert header(sent, "References") == "<ok@x.org> <e1@evil.org>"

        # The same holds one layer down: a mail whose headers would not name
        # the envelope's recipients is not written at all, whatever let the
        # address through.
        entry = {"to": [word.lower(), "bob@y.org"], "cc": [], "subject": "x",
                 "message": "x", "message_id": "m@example.org", "references": []}
        try:
            gw._build_outgoing(entry, NOW, [])
            raise AssertionError("a header that names other people than the envelope was built")
        except ValueError as exc:
            assert "To header" in str(exc), exc
        for bad in ({**entry, "to": ["a@x.org"], "cc": ["Bob <bob@y.org>"]},
                    {**entry, "to": ["a@x.org"], "references": ["two ids@x.org"]},
                    {**entry, "to": ["a@x.org"], "in_reply_to": "a@x.org> <b@x.org"},
                    {**entry, "to": ["a@x.org, b@y.org"]}):
            try:
                gw._build_outgoing(bad, NOW, [])
                raise AssertionError(f"built: {bad}")
            except ValueError:
                pass
        assert gw._build_outgoing({**entry, "to": ["a@x.org"]}, NOW, [])[1]

        # One that decodes to a line break does not make the thread
        # unanswerable either: it is left out like the other.
        box.put("INBOX", hostile_mail(
            "Message-ID: <e2@evil.org>", "From: Mallory <mallory@evil.org>",
            "To: me@example.org", "Reply-To: =?utf-8?q?x=0D=0A?=y@evil.org",
            "Subject: Second", date))
        mirror.poll()
        status, answer = w.send(recipient="e2@evil.org")
        assert status == 200 and smtp.mails[-1]["rcpt"] == ["mallory@evil.org"], answer
        assert answer["skipped"] == ["=?utf-8?q?x=0d=0a?=y@evil.org"]
    finally:
        done(w)
    print("ok: an encoded word in an address is no address; headers and envelope agree or no mail")


def test_a_subject_that_reads_as_an_encoded_word_arrives_as_written():
    """A thread's subject is its first sender's to choose. One that — once
    decoded — is itself an encoded word must not be decoded a second time on
    the way out: the reply's subject is the thread's, character for
    character, and the reply can always be written."""
    w = world()
    try:
        gw, box, mirror, smtp = w.gw, w.box, w.mirror, w.smtp
        date = "Date: " + t.email.utils.formatdate(t.T0 + DAY)
        literal = "=?utf-8?q?Hi=0D=0ABcc=3A_victim=40x.org?= and more"
        wrapped = base64.b64encode(literal.encode()).decode()
        box.put("INBOX", hostile_mail(
            "Message-ID: <s1@evil.org>", "From: Eve <eve@evil.org>", "To: me@example.org",
            f"Subject: =?utf-8?b?{wrapped}?=", date))
        mirror.poll()
        assert t.by_id(gw)["s1@evil.org"]["subject_line"] == literal
        status, answer = w.send(recipient="s1@evil.org")
        assert status == 200, answer
        names = wire_headers(smtp)
        assert set(names) <= _OUR_HEADERS and names.count("subject") == 1
        raw = smtp.mails[-1]["data"].decode("utf-8")
        assert "victim" not in raw and "\r\nBcc" not in raw
        assert header(smtp.parsed(), "Subject") == "Re: " + literal
        assert t.by_id(gw)[answer["message_id"]]["subject_line"] == "Re: " + literal
        # And typed by the user into a new mail, long enough to fold.
        typed = "=?utf-8?q?not_an_encoded_word?= " + "really not, " * 12 + "ünïcode =?x?="
        status, answer = w.send(to=["ann@x.org"], subject=typed)
        assert status == 200, answer
        assert header(smtp.parsed(), "Subject") == typed
        for line in smtp.mails[-1]["data"].split(b"\r\n\r\n", 1)[0].split(b"\r\n"):
            assert len(line) <= 78, line
    finally:
        done(w)
    print("ok: a subject that reads as an encoded word is sent as the characters it is")


def test_without_an_smtp_server_nothing_is_sent_and_the_mirror_runs():
    w = world(configured=False, EMAIL_USER="Me@Example.org", EMAIL_PASS="app-password",
              IMAP_HOST="imap.example.org")
    try:
        gw, box, mirror, smtp = w.gw, w.box, w.mirror, w.smtp
        box.put("INBOX", t.msg("a@x.org", day=1))
        mirror.poll()
        assert len(t.records(gw)) == 1, "the mailbox is mirrored all the same"
        snap = gw._health_snapshot()
        assert snap["can_send"] is False and "SMTP_HOST" in snap["cannot_send_reason"]
        status, answer = w.send(recipient="a@x.org")
        assert status == 503 and "SMTP_HOST" in answer["error"], answer
        # (A 5xx a caller could read as "outcome open" says that it is not.)
        assert answer["sent"] is False
        assert smtp.sessions == 0 and gw.SENDS.entries() == []
        # The mailbox still follows the chat: that is IMAP.
        assert mirror.mark_read("a@x.org") == 1
        assert mirror.archive("a@x.org") == {"moved": 1, "flagged": 0}
    finally:
        done(w)

    w = world()
    try:
        gw, smtp = w.gw, w.smtp
        # Nor is anything sent where the send could not even be written down.
        real_create = gw.SENDS.create

        def full(*args, **kwargs):
            raise OSError(28, "No space left on device")
        gw.SENDS.create = full
        status, answer = w.send(to=["ann@x.org"], request_key="disk-1")
        gw.SENDS.create = real_create
        assert status == 503 and answer["sent"] is False and "retry" in answer["error"]
        assert smtp.sessions == 0 and gw.SENDS.entries() == []
        # But what became of an earlier send is said first — by a gateway
        # that can send no more as well: "nothing was sent" is never said
        # over a mail that went out under the key.
        status, sent = w.send(to=["ann@x.org"], request_key="out-1")
        assert status == 200 and smtp.sessions == 1
        w.restart()
        gw.SMTP_PROBLEM = "SMTP_HOST is not set"
        assert w.send(to=["ann@x.org"], request_key="out-1") == (200, {**sent, "repeat": True})
        status, answer = w.send(to=["ann@x.org"], request_key="out-2")
        assert status == 503 and answer == {"error": "SMTP_HOST is not set", "sent": False}
        assert smtp.sessions == 1
    finally:
        done(w)
    print("ok: with no SMTP server the mailbox is mirrored and followed, and a send says why not")


# The functions a /send is answered by (_Handler._send), and the one whose
# refusals they hand on.
_SEND_ANSWERERS = ("_accept_send", "_repeat_send", "_send_answer", "_send_unplanned")


def _says_which(answer) -> bool:
    return answer.get("sent") is False or answer.get("unconfirmed") is True


def test_every_5xx_of_a_send_says_whether_the_mail_may_be_out():
    """A 5xx to /send is read by its caller as "the outcome is open" unless
    it says otherwise — so every one says which it is: ``"sent": false``
    (nothing left), or ``"unconfirmed": true`` (it may have). Walked twice:
    in the source, every answer site of the four functions a /send is
    answered by, so that a new one cannot come without it; and by driving
    each of them."""
    import ast
    source = (Path(__file__).resolve().parent.parent / "scripts" / "mail-gateway.py").read_text()
    functions = {node.name: node for node in ast.walk(ast.parse(source))
                 if isinstance(node, ast.FunctionDef)}
    sites = 0
    for name in _SEND_ANSWERERS:
        # Names bound to another answerer's answer: ``status, answer =
        # _send_answer(…)``.
        relayed = {tuple(map(ast.unparse, target.elts)) for node in ast.walk(functions[name])
                   if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                   and isinstance(node.value.func, ast.Name)
                   and node.value.func.id in _SEND_ANSWERERS
                   for target in node.targets if isinstance(target, ast.Tuple)}
        for node in ast.walk(functions[name]):
            if not isinstance(node, ast.Return) or node.value is None:
                continue
            value = node.value
            if isinstance(value, ast.Call):
                # Handed on to another answerer — nothing else.
                assert isinstance(value.func, ast.Name) and \
                    value.func.id in _SEND_ANSWERERS, (name, node.lineno)
                continue
            assert isinstance(value, ast.Tuple) and len(value.elts) == 2, (name, node.lineno)
            status, body = value.elts
            spreads = [ast.unparse(spread) for key, spread in zip(body.keys, body.values)
                       if key is None] if isinstance(body, ast.Dict) else [ast.unparse(body)]
            if any((ast.unparse(status), spread) in relayed for spread in spreads):
                continue    # that answer (a field added, perhaps): walked where it is made
            if not isinstance(status, ast.Constant):
                # Only a refusal's own status: SendRefused, never a 5xx
                # (checked below, where each is raised).
                assert ast.unparse(status) == "exc.status", (name, node.lineno)
                continue
            if status.value < 500:
                continue
            sites += 1
            assert isinstance(body, ast.Dict), (name, node.lineno)
            said = {key.value: val for key, val in zip(body.keys, body.values)
                    if isinstance(key, ast.Constant)}
            assert any(isinstance(said.get(field), ast.Constant) and said[field].value is want
                       for field, want in (("sent", False), ("unconfirmed", True))), \
                f"{name}, line {node.lineno}: a {status.value} that does not say whether " \
                f"the mail may be out"
    assert sites >= 8, sites
    # A refusal is a 4xx wherever one is raised.
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and \
                node.func.id in ("SendRefused", "refuse"):
            statuses = [arg for arg in node.args if isinstance(arg, ast.Constant)
                        and isinstance(arg.value, int)]
            assert all(400 <= arg.value < 500 for arg in statuses), node.lineno

    # Driven: each 5xx a /send can be answered with.
    w = world()
    try:
        gw = w.gw
        seen = []

        def check(status, answer):
            assert status < 500 or _says_which(answer), (status, answer)
            seen.append(status)
            return status, answer
        mirror = gw.MIRROR
        gw.MIRROR = None
        assert check(*w.send(to=["ann@x.org"]))[0] == 503
        gw.MIRROR = mirror
        real_build = gw._build_outgoing

        def broken(*args, **kwargs):
            raise RuntimeError("the mail could not be built")
        gw._build_outgoing = broken
        status, answer = check(*w.send(to=["ann@x.org"], request_key="built-1"))
        gw._build_outgoing = real_build
        assert status == 502 and answer["sent"] is False
        ids = {"id": "f" * 32, "message_id": "m@example.org", "chat": "m@example.org"}
        for entry, field in (({"status": "sending", "direct": True}, "unconfirmed"),
                             ({"status": "error", "outcome_unknown": True}, "unconfirmed"),
                             ({"status": "error"}, "unconfirmed"),
                             ({"status": "error", "outcome_unknown": False}, "sent")):
            status, answer = check(*gw._send_answer({**ids, **entry}, HOST))
            assert status == 502 and field in answer, (entry, answer)
        fault = RuntimeError("boom")
        assert check(*gw._send_unplanned({"request_key": "never-1"}, HOST, fault))[1][
            "sent"] is False
        assert check(*gw._send_unplanned({}, HOST, fault))[1]["unconfirmed"] is True
        status, answer = w.send(to=["ann@x.org"], request_key="out-9")
        assert status == 200
        assert check(*gw._send_unplanned({"request_key": "out-9"}, HOST, fault))[0] == 200
        assert seen.count(502) >= 6 and 503 in seen
    finally:
        done(w)
    print("ok: every 5xx of a send says whether the mail may be out")


def test_the_loop_is_woken_for_the_rail_and_leaves_the_mailbox_alone():
    w = world()
    try:
        gw, box, mirror = w.gw, w.box, w.mirror
        steps = []

        def step(_mirror, _loop):
            steps.append(1)
            return 60.0         # a whole poll interval until the next turn
        gw._loop_step = step
        threading.Thread(target=gw._mirror_loop, args=(mirror,), daemon=True).start()
        deadline = t.threading.Event()
        for _ in range(100):
            if steps:
                break
            deadline.wait(0.02)
        # A mailbox in trouble (the next call to it would fail)…
        box.no_writes.add(("append", "Sent"))
        status, answer = w.send(to=["ann@x.org"])
        assert status == 200
        box.fail = gw.MailboxError("IMAP login failed")
        # …and still the rail hears of the mail within moments, not a poll
        # interval later — with no turn of the loop, and no word to the mailbox.
        for _ in range(200):
            if any(kind == "inbound" for kind, _p in w.events):
                break
            deadline.wait(0.02)
        told = [p for kind, p in w.events if kind == "inbound"]
        assert [e["message_id"] for e in told] == [answer["message_id"]]
        assert steps == [1] and box.fail is not None
        # What only the mailbox can finish stays owed, for the next poll.
        assert gw.SENDS.due() != []
    finally:
        done(w)
    print("ok: a send wakes the mirror loop for the rail, without a turn at the mailbox")


def test_a_queued_sends_attachments_are_kept_beside_its_entry():
    w = world(policy="verify")
    try:
        gw, smtp = w.gw, w.smtp
        pdf = base64.b64encode(b"%PDF-1.4 " + b"x" * 5000).decode()
        files = lambda: sorted(p.name for p in gw.MAIL_PENDING_SENDS_DIR.glob("*.files"))  # noqa: E731

        def queue():
            _s, queued = w.send(to=["ann@x.org"], images=[{"filename": "big.pdf", "data": pdf}])
            return queued["request_id"]
        first, second, third, fourth = queue(), queue(), queue(), queue()
        # The entry is what every listing reads: it names the file, and does
        # not carry it.
        entry_file = gw.MAIL_PENDING_SENDS_DIR / f"{first}.json"
        assert entry_file.stat().st_size < 2000 and "JVBERi0" not in entry_file.read_text()
        assert gw.SENDS.get(first)["attachments"] == ["big.pdf"]
        assert files() == sorted(f"{rid}.files" for rid in (first, second, third, fourth))
        assert len(gw.SENDS.images(first)) == 1 and gw.SENDS.images("../" + first) == []
        # Decided, the bytes go: with a rejection, a retraction, and once sent.
        gw._decide_send(second, "reject")
        gw._decide_send(third, "retract")
        gw._decide_send(first, "approve")
        w.told()        # (kept until the Sent copy, which is built from them, is filed)
        assert files() == [f"{fourth}.files"]
        (part,) = smtp.parsed().iter_attachments()
        assert part.get_filename() == "big.pdf" and len(part.get_payload(decode=True)) == 5009
        assert gw.SENDS.get(second)["attachments"] == ["big.pdf"], "the name stays for the page"
        # A mail is never sent with fewer files than its approver was shown.
        (gw.MAIL_PENDING_SENDS_DIR / f"{fourth}.files").unlink()
        gw._decide_send(fourth, "approve")
        entry = gw.SENDS.get(fourth)
        assert entry["status"] == "error" and "attachments" in entry["error"], entry
        assert len(smtp.mails) == 1
        # Attachments left by a stop between the two files of one create
        # belong to no send, and are cleared at the next start.
        (gw.MAIL_PENDING_SENDS_DIR / ("e" * 32 + ".files")).write_text("[]")
        w.restart()
        assert files() == []
    finally:
        done(w)
    print("ok: a queued send's attachments are stored beside its entry, and go when it is decided")


# --------------------------------------------------------------------------
# The answer, the repeat, and what a failed send leaves
# --------------------------------------------------------------------------

def test_the_answer_waits_for_the_smtp_server_and_for_nothing_else():
    """Once the SMTP server has the mail nothing can unsend it, so nothing
    after that may hold the answer: not a pass that has the mirror's lock,
    not the IMAP server. (A caller that gives up first reports "not sent",
    and the retry is a second mail.)"""
    w = world()
    try:
        gw, box, mirror, smtp = w.gw, w.box, w.mirror, w.smtp
        gw.RECORD_LOCK_SECONDS = 0.05
        box.put("INBOX", t.msg("a@x.org", day=1))
        mirror.poll()
        stalled, release = threading.Event(), threading.Event()
        real_append = box.append

        def stuck(folder, raw, *, when=None):
            stalled.set()
            release.wait(30)        # an IMAP server that has stopped answering
            return real_append(folder, raw, when=when)
        box.append = stuck
        answers = []
        sender = threading.Thread(target=lambda: answers.append(w.send(recipient="a@x.org")))
        with mirror._lock:          # and a pass over the mailbox, under way
            sender.start()
            sender.join(10)
            assert not sender.is_alive(), "the answer waited for the mirror's lock"
        (status, answer), = answers
        assert status == 200 and answer["status"] == "sent" and len(smtp.mails) == 1
        assert not stalled.is_set() and box.writes == [], "nothing was asked of the mailbox"
        (entry,) = gw.SENDS.entries()
        assert entry["status"] == "approved" and gw.SENDS.due() == [entry["id"]]
        # The loop does the rest, in its own time: the record at once, the
        # copy once the mailbox takes it.
        loop = threading.Thread(target=gw._between_polls, args=(mirror,))
        loop.start()
        assert stalled.wait(10)
        assert [p["message_id"] for kind, p in w.events
                if kind == "inbound" and p["direction"] == "out"] \
            == [answer["message_id"]], "the rail hears of it before the copy is filed"
        release.set()
        loop.join(10)
        assert len(w.sent_copies(answer["message_id"])) == 1
        assert gw.SENDS.entries() == [] and gw.SENDS.due() == []
    finally:
        done(w)
    print("ok: /send answers when the SMTP server has the mail; record and copy follow")


def test_the_loop_files_the_copy_while_the_mailbox_answers_and_not_after_it_failed():
    w = world()
    try:
        gw, box, mirror = w.gw, w.box, w.mirror
        turns = []

        def step(_mirror, loop):
            turns.append(loop)
            loop["up"] = True       # the turn went through: the mailbox answers
            return 60.0
        gw._loop_step = step
        threading.Thread(target=gw._mirror_loop, args=(mirror,), daemon=True).start()
        pause = threading.Event()

        def soon(check):
            for _ in range(250):
                if check():
                    return True
                pause.wait(0.02)
            return False
        assert soon(lambda: turns)
        appends = lambda: len([x for x in box.writes if x[0] == "append"])   # noqa: E731
        _status, first = w.send(to=["ann@x.org"])
        assert soon(lambda: w.sent_copies(first["message_id"])), "filed within moments"
        assert soon(lambda: gw.SENDS.due() == []) and len(turns) == 1
        # The mailbox stops taking it: tried once, at that send — and not at
        # the next one. The poll that goes through again tries again.
        box.no_writes.add(("append", "Sent"))
        _status, second = w.send(to=["ann@x.org"], message="Second.")
        assert soon(lambda: appends() == 2 and turns[0]["up"] is False)
        _status, third = w.send(to=["ann@x.org"], message="Third.")
        assert soon(lambda: len([p for kind, p in w.events if kind == "inbound"]) == 3)
        pause.wait(0.2)
        assert appends() == 2 and len(turns) == 1, \
            "a failing mailbox is left alone between polls"
        assert len(gw.SENDS.due()) == 2
        box.no_writes.clear()
        mirror.poll()
        assert len(w.sent_copies()) == 3 and gw.SENDS.due() == []
    finally:
        done(w)
    print("ok: the loop files a Sent copy at once while the mailbox answers, and backs off")


def test_one_press_is_one_mail():
    """``request_key``: a repeat of a request is answered with what became
    of the first — the same Message-ID — and sends nothing. And says that it
    is a repeat: the mail went out before, not by this request."""
    w = world()
    try:
        gw, box, mirror, smtp = w.gw, w.box, w.mirror, w.smtp
        box.put("INBOX", t.msg("a@x.org", day=1))
        mirror.poll()
        press = dict(recipient="a@x.org", message="Once.", request_key="press-1")
        status, first = w.send(**press)
        assert status == 200 and first["status"] == "sent"
        assert "repeat" not in first, "the request that sent the mail is no repeat"
        again = {**first, "repeat": True}
        for _ in range(3):
            assert w.send(**press) == (200, again)
        assert len(smtp.mails) == 1 and smtp.sessions == 1
        (told,) = w.told()
        assert told["request_key"] == "press-1" and told["message_id"] == first["message_id"]
        assert w.send(**press) == (200, again) and len(w.sent_copies()) == 1
        # The entry is what remembers the key: kept, done, and not listed.
        (entry,) = gw.SENDS.entries()
        assert entry["done"] is True and entry["request_key"] == "press-1"
        assert gw.SENDS.listed() == [] and "press" not in gw._public(entry)
        # Also after a restart.
        w.restart()
        assert w.send(**press) == (200, again) and len(smtp.mails) == 1
        # A key names one press: used for another mail it is a mistake, said
        # as one — never answered with the first mail's "sent".
        status, said = w.send(recipient="a@x.org", message="Something else.",
                              request_key="press-1")
        assert status == 409 and "request_key" in said["error"] and len(smtp.mails) == 1
        assert "repeat" not in said, "no answer of the first send's"
        # What a key is.
        for bad in ("", "x" * 65, "two words", "tab\there", "ünï", 7, ["k"], {"k": 1}):
            status, said = w.send(recipient="a@x.org", request_key=bad)
            assert status == 400 and "request_key" in said["error"], (bad, said)
        assert len(smtp.mails) == 1
        status, other = w.send(recipient="a@x.org", message="Once.", request_key="x" * 64)
        assert status == 200 and other["message_id"] != first["message_id"]
        # A request that was refused made no mail, and its key is free.
        status, _said = w.send(recipient="nope@x.org", request_key="press-2")
        assert status == 400 and gw.SENDS.by_key("press-2") is None
        status, _sent = w.send(recipient="a@x.org", request_key="press-2")
        assert status == 200 and len(smtp.mails) == 3
        # Remembered for a day, then cleared away — and the key is new.
        w.mirror.poll()
        assert all(e["done"] for e in gw.SENDS.entries()) and len(gw.SENDS.entries()) == 3
        gw._now = lambda: NOW + gw.REQUEST_KEY_SECONDS + 5
        gw.SENDS.sweep(force=True)
        assert gw.SENDS.entries() == []
        status, anew = w.send(**press)
        assert status == 200 and anew["message_id"] != first["message_id"]
    finally:
        done(w)

    # While the first request is still with the SMTP server, a repeat waits
    # for it — and if that takes too long says "not confirmed", never "sent
    # again" and never "failed".
    w = world()
    try:
        gw, smtp = w.gw, w.smtp
        gw.REPEAT_WAIT_SECONDS = 0.2
        entered, release = threading.Event(), threading.Event()
        real_send = gw._ec._smtp_send

        def slow(*args, **kwargs):
            entered.set()
            release.wait(30)
            return real_send(*args, **kwargs)
        gw._ec._smtp_send = slow
        press = dict(to=["ann@x.org"], message="Slow.", request_key="slow-1")
        answers = []
        firstly = threading.Thread(target=lambda: answers.append(w.send(**press)))
        firstly.start()
        try:
            assert entered.wait(10)
            status, said = w.send(**press)
            assert status == 502 and said["unconfirmed"] is True, said
            assert said["repeat"] is True and "sent" not in said
            assert said["message_id"] == gw.SENDS.by_key("slow-1")["message_id"]
            assert smtp.mails == []
            gw.REPEAT_WAIT_SECONDS = 10
            later = []
            repeat = threading.Thread(target=lambda: later.append(w.send(**press)))
            repeat.start()
        finally:
            release.set()
        firstly.join(10)
        repeat.join(10)
        gw._ec._smtp_send = real_send
        # The request that sent it says "sent"; the one that waited for it
        # says the same, as a repeat.
        assert answers[0][0] == 200 and "repeat" not in answers[0][1], answers
        assert later == [(200, {**answers[0][1], "repeat": True})], (answers, later)
        assert len(smtp.mails) == 1 and answers[0][1]["message_id"] == said["message_id"]
    finally:
        done(w)

    # Queued: the same pending send, then what it became.
    w = world(policy="verify")
    try:
        gw, smtp = w.gw, w.smtp
        press = dict(to=["ann@x.org"], subject="New", message="Hello.", request_key="q-1")
        status, queued = w.send(**press)
        assert status == 202 and "repeat" not in queued
        assert w.send(**press) == (202, {**queued, "repeat": True})
        assert len(gw.SENDS.entries()) == 1
        gw._decide_send(queued["request_id"], "approve")
        status, sent = w.send(**press)
        assert status == 200 and sent["message_id"] == queued["message_id"]
        assert sent["repeat"] is True and len(smtp.mails) == 1
        status, again = w.send(to=["ann@x.org"], message="No.", request_key="q-2")
        gw._decide_send(again["request_id"], "reject")
        status, said = w.send(to=["ann@x.org"], message="No.", request_key="q-2")
        assert status == 409 and said["status"] == "rejected" and len(smtp.mails) == 1
        assert said["repeat"] is True
    finally:
        done(w)
    print("ok: a request_key makes a repeat of /send answer the first one's outcome, unsent")


def test_a_send_that_failed_for_certain_ends_its_press():
    """Only an unknown outcome keeps its key. A send that failed with
    nothing having left the gateway is over: a request under its key is a
    new attempt — the mail is composed and sent afresh — where one whose
    mail may have gone out is answered from its entry for as long as the key
    is remembered, and never sent again."""
    w = world()
    try:
        gw, smtp = w.gw, w.smtp
        # The server refuses the mail: failed, said as certain, and no repeat.
        smtp.reject_data = "554 5.7.1 content rejected"
        press = dict(to=["ann@x.org"], message="Hm.", request_key="f-1")
        status, said = w.send(**press)
        assert status == 502 and said["error"].startswith("send failed: ")
        assert "554" in said["error"] and "unconfirmed" not in said
        assert said["sent"] is False and "repeat" not in said
        # Its caller had the answer: nothing of it is kept, in the chat or
        # for the key.
        assert gw.SENDS.entries() == [] and gw.SENDS.by_key("f-1") is None
        # The same press again, once the server relents: sent — now, for the
        # first time, and not answered with the old failure.
        smtp.reject_data = None
        status, sent = w.send(**press)
        assert status == 200 and sent["status"] == "sent" and "repeat" not in sent
        assert len(smtp.mails) == 1 and smtp.sessions == 2
        # …and that one is remembered: a repeat of it sends nothing.
        assert w.send(**press) == (200, {**sent, "repeat": True}) and len(smtp.mails) == 1

        # The caller had gone before the failure was known: the entry is kept
        # as a failed send, where the chat finds it — and gives way to the
        # new attempt made under its key.
        smtp.drop = "connect"
        press = dict(to=["ann@x.org"], message="Later.", request_key="f-2")
        status, said = gw._accept_send(dict(press), HOST, caller_waits=lambda: False)
        assert status == 502 and said["sent"] is False
        (failed,) = gw.SENDS.listed()
        assert failed["status"] == "error" and failed["outcome_unknown"] is False
        assert failed["request_key"] == "f-2" and "dismissed" not in failed
        smtp.drop = None
        sessions = smtp.sessions
        status, sent = w.send(**press)
        assert status == 200 and "repeat" not in sent and len(smtp.mails) == 2
        assert smtp.sessions == sessions + 1
        assert sent["message_id"] != failed["message_id"], "composed afresh"
        assert gw.SENDS.get(failed["id"]) is None and gw.SENDS.listed() == []
        assert gw.SENDS.by_key("f-2")["message_id"] == sent["message_id"]
        # A key under which nothing was sent names no mail yet: the new
        # attempt may be another text (the user corrected what was refused).
        smtp.reject_data = "552 5.3.4 message too big"
        status, said = gw._accept_send(
            dict(to=["ann@x.org"], message="Much too long.", request_key="f-3"), HOST,
            caller_waits=lambda: False)
        assert status == 502 and len(gw.SENDS.listed()) == 1
        smtp.reject_data = None
        status, sent = w.send(to=["ann@x.org"], message="Shorter.", request_key="f-3")
        assert status == 200 and len(smtp.mails) == 3 and gw.SENDS.listed() == []
        assert smtp.parsed().get_content().strip() == "Shorter."
        # A new attempt that is refused as it stands leaves the failed send
        # where it was: nothing replaced it.
        smtp.reject_data = "552 5.3.4 message too big"
        gw._accept_send(dict(to=["ann@x.org"], message="Big.", request_key="f-4"), HOST,
                        caller_waits=lambda: False)
        smtp.reject_data = None
        (kept,) = gw.SENDS.listed()
        status, said = w.send(to=["not an address"], message="Big.", request_key="f-4")
        assert status == 400 and [e["id"] for e in gw.SENDS.listed()] == [kept["id"]]
        assert len(smtp.mails) == 3

        # Two requests of one press at once, after a failure: one new mail.
        entered, release = threading.Event(), threading.Event()
        real_send = gw._ec._smtp_send

        def slow(*args, **kwargs):
            entered.set()
            release.wait(30)
            return real_send(*args, **kwargs)
        gw._ec._smtp_send = slow
        press = dict(to=["ann@x.org"], message="Big.", request_key="f-4")
        answers = []
        firstly = threading.Thread(target=lambda: answers.append(w.send(**press)))
        firstly.start()
        try:
            assert entered.wait(10)
            assert gw.SENDS.get(kept["id"]) is None, "the failed entry gave way"
            other = []
            second = threading.Thread(target=lambda: other.append(w.send(**press)))
            second.start()
        finally:
            release.set()
        firstly.join(10)
        second.join(10)
        gw._ec._smtp_send = real_send
        assert answers[0][0] == 200 and "repeat" not in answers[0][1]
        assert other == [(200, {**answers[0][1], "repeat": True})], other
        assert len(smtp.mails) == 4
    finally:
        done(w)

    # A repeat that waited for a send still with the SMTP server, which the
    # server then refused: the request that made the attempt has the failure
    # as its answer and clears its entry at once — and the one that waited is
    # told the same, for certain, from how the flight ended. Never
    # "unconfirmed" for a mail nothing of which left: that would keep the
    # press on a key for good.
    w = world()
    try:
        gw, smtp = w.gw, w.smtp
        gw.REPEAT_WAIT_SECONDS = 10
        entered, release, waiting = threading.Event(), threading.Event(), threading.Event()
        real_send, real_wait = gw._ec._smtp_send, gw._Flight.wait

        def slow(*args, **kwargs):
            entered.set()
            release.wait(30)
            return real_send(*args, **kwargs)

        def wait(self, timeout=None):
            waiting.set()
            got = real_wait(self, timeout)
            firstly.join(10)    # by now the first request has cleared its entry
            return got
        gw._ec._smtp_send, gw._Flight.wait = slow, wait
        smtp.reject_data = "554 5.7.1 content rejected"
        press = dict(to=["ann@x.org"], message="No.", request_key="w-1")
        answers, later = [], []
        firstly = threading.Thread(target=lambda: answers.append(w.send(**press)))
        repeat = threading.Thread(target=lambda: later.append(w.send(**press)))
        firstly.start()
        try:
            assert entered.wait(10)
            repeat.start()
            assert waiting.wait(10)
        finally:
            release.set()
        firstly.join(10)
        repeat.join(10)
        gw._ec._smtp_send, gw._Flight.wait = real_send, real_wait
        status, said = answers[0]
        assert status == 502 and said["sent"] is False and "repeat" not in said, answers
        assert later == [(502, {**said, "repeat": True})], later
        assert "unconfirmed" not in later[0][1] and "554" in later[0][1]["error"]
        assert gw.SENDS.entries() == [] and gw._IN_FLIGHT == {} and smtp.sessions == 1
        # …and the press after that is the new attempt.
        smtp.reject_data = None
        status, sent = w.send(**press)
        assert status == 200 and "repeat" not in sent and len(smtp.mails) == 1
    finally:
        done(w)

    # A mail that may have gone out keeps its key: never a new attempt —
    # not after a restart, not once it was dismissed, whatever the server
    # would say now.
    w = world()
    try:
        gw, smtp = w.gw, w.smtp
        smtp.drop = "dot"       # the whole mail arrived; the line died before the answer
        press = dict(to=["ann@x.org"], message="Maybe.", request_key="u-1")
        status, said = w.send(**press)
        assert status == 502 and said["unconfirmed"] is True and said["outcome_unknown"] is True
        assert "sent" not in said and "repeat" not in said
        smtp.drop = None
        unknown = {**said, "repeat": True}
        assert w.send(**press) == (502, unknown)
        w.restart()
        assert w.send(**press) == (502, unknown)
        (entry,) = gw.SENDS.listed()
        dismissed, did = gw.SENDS.dismiss(entry["id"])
        assert did and dismissed["dismissed"] is True and gw.SENDS.listed() == []
        # (Dismissed, it is over for the chat — and kept for this answer.)
        kept = gw.SENDS.get(entry["id"])
        assert kept["status"] == "error" and kept["dismissed"] is True
        assert kept["outcome_unknown"] is True
        assert w.send(**press) == (502, unknown)
        assert smtp.sessions == 1 and len(smtp.mails) == 1, "handed over once"
        # An entry that does not say whether its outcome is known is not
        # taken for unsent either.
        smtp.reject_data = "554 5.7.1 content rejected"
        gw._accept_send(dict(to=["ann@x.org"], message="Old.", request_key="u-2"), HOST,
                        caller_waits=lambda: False)
        smtp.reject_data = None
        old = gw.SENDS.by_key("u-2")
        gw.SENDS.update(old["id"], drop=("outcome_unknown",))
        status, said = w.send(to=["ann@x.org"], message="Old.", request_key="u-2")
        assert status == 502 and said["unconfirmed"] is True and said["repeat"] is True
        assert len(smtp.mails) == 1
    finally:
        done(w)

    # A queued send that failed after it was allowed: the new attempt under
    # its key is queued afresh — the policy decides again — and the failed
    # entry is gone from the chat.
    w = world(policy="verify")
    try:
        gw, smtp = w.gw, w.smtp
        press = dict(to=["ann@x.org"], subject="New", message="Hello.", request_key="q-9")
        _s, queued = w.send(**press)
        smtp.refuse_login = True
        gw._decide_send(queued["request_id"], "approve")
        (failed,) = gw.SENDS.listed()
        assert failed["id"] == queued["request_id"] and failed["outcome_unknown"] is False
        smtp.refuse_login = False
        status, anew = w.send(**press)
        assert status == 202 and anew["status"] == "pending_approval" and "repeat" not in anew
        assert anew["request_id"] != queued["request_id"]
        assert anew["message_id"] != queued["message_id"] and smtp.mails == []
        assert [(e["id"], e["status"]) for e in gw.SENDS.listed()] == [
            (anew["request_id"], "pending")]
        gw._decide_send(anew["request_id"], "approve")
        assert len(smtp.mails) == 1
        assert w.send(**press)[1]["repeat"] is True and len(smtp.mails) == 1
    finally:
        done(w)

    # A direct send a stop caught before anything was handed over: its
    # caller was given no answer (and may have told its user "not confirmed
    # yet"), so it is not dropped — it ends as failed for certain, listed
    # with its words. A repeat under its key then sends the mail: once.
    w = world(policy="trust")
    try:
        gw, box, mirror, smtp = w.gw, w.box, w.mirror, w.smtp
        box.put("INBOX", t.msg("a@x.org", day=1))
        mirror.poll()

        def stopped(*args, **kwargs):
            raise _Kill()       # while the SMTP session is being opened
        real_send = gw._ec._smtp_send
        gw._ec._smtp_send = stopped
        press = dict(recipient="a@x.org", message="My considered answer.", author="user",
                     user_approved=True, request_key="s-1",
                     images=[{"filename": "a.txt", "data": base64.b64encode(b"f").decode()}])
        try:
            w.send(**press)
            raise AssertionError("not stopped")
        except _Kill:
            pass
        finally:
            gw._ec._smtp_send = real_send
        (before,) = gw.SENDS.listed("a@x.org")
        assert before["status"] == "sending" and "phase" not in before
        w.restart()
        (after,) = gw.SENDS.listed("a@x.org")
        assert after["id"] == before["id"] and after["status"] == "error", after
        assert after["error"] == gw.STOPPED_UNSENT and after["outcome_unknown"] is False
        assert after["message"] == "My considered answer." and "dismissed" not in after
        assert gw.SENDS.images(after["id"]) == [] and after["attachments"] == ["a.txt"]
        assert smtp.mails == [] and gw.SENDS.due() == []
        # Asked after by id, it is what the chat shows: failed, with its reason.
        assert gw._public(gw.SENDS.get(after["id"]))["error"] == gw.STOPPED_UNSENT
        # Again and again, a start does nothing more to it.
        w.restart()
        assert [e["id"] for e in gw.SENDS.listed("a@x.org")] == [after["id"]]
        status, sent = w.send(**press)
        assert status == 200 and "repeat" not in sent and len(smtp.mails) == 1
        assert gw.SENDS.get(after["id"]) is None and gw.SENDS.listed() == []
        assert w.send(**press) == (200, {**sent, "repeat": True}) and len(smtp.mails) == 1
        # One that named no request has nobody to ask after it: dropped.
        gw._ec._smtp_send = stopped
        try:
            w.send(recipient="a@x.org", message="Nameless.", user_approved=True)
            raise AssertionError("not stopped")
        except _Kill:
            pass
        finally:
            gw._ec._smtp_send = real_send
        assert [e["status"] for e in gw.SENDS.listed("a@x.org")] == ["sending"]
        w.restart()
        assert gw.SENDS.listed() == [] and len(smtp.mails) == 1
    finally:
        done(w)
    print("ok: a send that failed for certain ends its press; only an unknown outcome "
          "keeps its key")


def test_a_fault_in_a_send_is_answered_from_its_entry():
    """Something raising where nothing was planned to: the answer says what
    the entry says, never "failed" for a mail that was handed over."""
    w = world(save_sent=False)
    try:
        gw, smtp = w.gw, w.smtp
        # After the hand-over, with the server's answer lost: asking Sent
        # breaks in a way nobody planned for.
        smtp.drop = "dot"

        def broken(message_id):
            raise RuntimeError("the index is locked")
        real = w.mirror.sent_copy
        w.mirror.sent_copy = broken
        status, said = w.send(to=["ann@x.org"], request_key="x-1")
        w.mirror.sent_copy = real
        assert status == 502 and said["unconfirmed"] is True and "sent" not in said, said
        (entry,) = gw.SENDS.entries()
        assert entry["status"] == "sending" and entry["phase"] == "data"
        assert gw.SENDS.due() == [entry["id"]], "left to the mirror loop"
        smtp.drop = None
        status, again = w.send(to=["ann@x.org"], request_key="x-1")
        assert status == 502 and again["unconfirmed"] is True and again["repeat"] is True
        assert smtp.sessions == 1 and len(smtp.mails) == 1

        # Before anything was handed over: failed, for certain — and the key
        # is free for the attempt that follows.
        real_build = gw._store_sent_parts

        def breaks(*args, **kwargs):
            raise RuntimeError("no room")
        real_fail = gw._fail_send
        calls = []

        def fail_once(entry, said_, **kwargs):
            calls.append(said_)
            if len(calls) == 1:
                raise RuntimeError("could not even say so")
            return real_fail(entry, said_, **kwargs)
        gw._store_sent_parts, gw._fail_send = breaks, fail_once
        status, said = w.send(to=["ann@x.org"], message="Two.", request_key="x-2")
        gw._store_sent_parts, gw._fail_send = real_build, real_fail
        assert status == 502 and said["sent"] is False and "unconfirmed" not in said, said
        assert gw.SENDS.by_key("x-2") is None and len(smtp.mails) == 1
        status, sent = w.send(to=["ann@x.org"], message="Two.", request_key="x-2")
        assert status == 200 and len(smtp.mails) == 2

        # A fault while the mail is composed — before any entry stands: not
        # sent, and said so.
        real_compose = gw._compose

        def no_compose(*args, **kwargs):
            raise RuntimeError("the index is gone")
        gw._compose = no_compose
        for press in ({"request_key": "x-3"}, {}):
            status, said = w.send(to=["ann@x.org"], message="Three.", **press)
            assert status == 502 and said["sent"] is False and "unconfirmed" not in said
            assert said["error"] == "send failed: the index is gone", said
        gw._compose = real_compose
        assert gw.SENDS.by_key("x-3") is None and len(smtp.mails) == 2
    finally:
        done(w)

    # And where the request itself raises, anywhere: the HTTP answer is made
    # from what stands under its key — never "failed" off the fault alone.
    w = world()
    try:
        gw, smtp = w.gw, w.smtp
        server, base = t.serve(gw)

        def post(payload):
            request = t.urllib.request.Request(
                base + "/send", data=json.dumps(payload).encode(), method="POST", headers={
                    "Content-Type": "application/json", "Authorization": f"Bearer {t.TOKEN}"})
            try:
                with t.urllib.request.urlopen(request, timeout=10) as response:
                    return response.status, json.loads(response.read())
            except t.urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read())
        real_answer, real_category = gw._send_answer, gw._send_category
        faults = []

        def breaks_once(entry, host_header):
            if not faults:
                faults.append(entry["id"])
                raise RuntimeError("no answer could be made")
            return real_answer(entry, host_header)

        def breaks(entry, host_header):
            raise RuntimeError("no answer could be made")

        def no_policy():
            raise RuntimeError("the policy file vanished")
        try:
            # After the server took the mail: it is "sent" — the entry says so.
            gw._send_answer = breaks_once
            status, said = post({"to": ["ann@x.org"], "message": "One.",
                                 "request_key": "y-1"})
            assert faults and status == 200 and said["status"] == "sent", said
            assert len(smtp.mails) == 1
            assert said["message_id"] == gw.SENDS.by_key("y-1")["message_id"]
            # The same without a key, and no way left to say what the entry
            # says: nobody knows from here — "unconfirmed", never "failed",
            # and never "nothing was sent".
            gw._send_answer = breaks
            for press in ({}, {"request_key": "y-2"}):
                status, said = post({"to": ["ann@x.org"], "message": "Two.", **press})
                assert status == 502 and said["unconfirmed"] is True, said
                assert "sent" not in said and not said["error"].startswith("send failed")
            assert len(smtp.mails) == 3
            gw._send_answer = real_answer
            # Before anything stood under its key: failed, for certain.
            gw._send_category = no_policy
            status, said = post({"to": ["ann@x.org"], "message": "Three.",
                                 "request_key": "y-3"})
            assert status == 502 and said["sent"] is False and "unconfirmed" not in said
            assert said["error"] == "send failed: the policy file vanished"
            assert gw.SENDS.by_key("y-3") is None and len(smtp.mails) == 3
            # (Without a key there is nothing to look up: not known.)
            status, said = post({"to": ["ann@x.org"], "message": "Four."})
            assert status == 502 and said["unconfirmed"] is True and "sent" not in said
            gw._send_category = real_category
            assert len(smtp.mails) == 3
        finally:
            gw._send_answer, gw._send_category = real_answer, real_category
            server.shutdown()
    finally:
        done(w)
    print("ok: a fault in a send is answered from what its entry says")


def test_a_failed_send_stays_in_its_chat_until_it_is_dismissed():
    w = world(policy="verify")
    try:
        gw, box, mirror, smtp = w.gw, w.box, w.mirror, w.smtp
        box.put("INBOX", t.msg("a@x.org", day=1))
        mirror.poll()
        listed = lambda chat=None: gw.SENDS.listed(chat)    # noqa: E731
        _s, queued = w.send(recipient="a@x.org", message="My reply.")
        rid = queued["request_id"]
        assert [(e["id"], e["status"]) for e in listed("a@x.org")] == [(rid, "pending")]
        # Allowed — and the server refuses the mail.
        smtp.reject_data = "554 5.7.1 content rejected"
        gw._decide_send(rid, "approve")
        (failed,) = listed("a@x.org")
        assert failed["id"] == rid and failed["status"] == "error", failed
        assert "554" in failed["error"] and failed["outcome_unknown"] is False
        assert failed["message"] == "My reply." and failed["to"] == ["ann@x.org"]
        assert gw.SENDS.waiting("a@x.org") == [], "nothing waits for approval"
        assert listed("other@x.org") == [] and [e["id"] for e in listed()] == [rid]
        # It cannot be released again, by a press or by a restart.
        smtp.reject_data = None
        assert gw._decide_send(rid, "approve")["status"] == "error"
        w.restart()
        w.mirror.poll()
        assert smtp.mails == [] and [e["id"] for e in listed("a@x.org")] == [rid]
        # Only a failed send is dismissed; dismissed, it is gone from the chat.
        _s, waiting = w.send(recipient="a@x.org", message="Another.")
        entry, did = gw.SENDS.dismiss(waiting["request_id"])
        assert not did and entry["status"] == "pending"
        entry, did = gw.SENDS.dismiss(rid)
        assert did and entry["dismissed"] is True
        assert [e["id"] for e in listed("a@x.org")] == [waiting["request_id"]]
        assert gw.SENDS.dismiss(rid) == (None, False) and gw.SENDS.dismiss("nope") == (None, False)
        assert smtp.mails == []

        # A mail that may have gone out says so on its entry, and stays.
        smtp.drop = "dot"
        gw._decide_send(waiting["request_id"], "approve")
        (unknown,) = listed("a@x.org")
        assert unknown["status"] == "error" and unknown["outcome_unknown"] is True
        assert unknown["error"] == gw.UNKNOWN_OUTCOME
    finally:
        done(w)

    # A direct send that failed for certain: over, if its caller has the
    # answer — and kept as a failed send if the caller had gone by then (it
    # may have told its user "not confirmed yet").
    w = world()
    try:
        gw, smtp = w.gw, w.smtp
        smtp.reject_data = "552 5.3.4 message too big"
        status, said = gw._accept_send({"to": ["ann@x.org"], "message": "Big."}, HOST,
                                       caller_waits=lambda: True)
        assert status == 502 and gw.SENDS.entries() == []
        # (Also one that named its request: nothing is kept for a key under
        # which nothing was sent.)
        status, said = gw._accept_send({"to": ["ann@x.org"], "message": "Big.",
                                        "request_key": "big-1"}, HOST,
                                       caller_waits=lambda: True)
        assert status == 502 and said["sent"] is False and gw.SENDS.entries() == []
        status, said = gw._accept_send({"to": ["ann@x.org"], "message": "Big."}, HOST,
                                       caller_waits=lambda: False)
        assert status == 502 and said["error"].startswith("send failed: ")
        (kept,) = gw.SENDS.listed()
        assert kept["status"] == "error" and "552" in kept["error"] and kept["direct"] is True
        assert kept["message"] == "Big." and kept["outcome_unknown"] is False
        # Dismissed, a send that failed for certain is gone, key or no key:
        # there is nothing left for a reader to take for a failed send.
        status, said = gw._accept_send({"to": ["ann@x.org"], "message": "Big.",
                                        "request_key": "big-2"}, HOST,
                                       caller_waits=lambda: False)
        keyed = gw.SENDS.by_key("big-2")
        for entry in (kept, keyed):
            dismissed, did = gw.SENDS.dismiss(entry["id"])
            assert did and dismissed["dismissed"] is True
            assert gw.SENDS.get(entry["id"]) is None
        assert gw.SENDS.entries() == []
    finally:
        done(w)
    print("ok: a send that failed is listed with its reason until dismissed, and never "
          "released again")


def test_failed_sends_over_http_and_a_caller_that_hung_up():
    w = world(policy="trust")
    try:
        gw, box, mirror, smtp = w.gw, w.box, w.mirror, w.smtp
        box.put("INBOX", t.msg("a@x.org", day=1))
        mirror.poll()
        server, base = t.serve(gw)
        token = t.TOKEN

        def post(path, payload):
            body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            request = t.urllib.request.Request(base + path, data=body, method="POST", headers={
                "Content-Type": "application/json", "Authorization": f"Bearer {token}"})
            try:
                with t.urllib.request.urlopen(request, timeout=10) as response:
                    return response.status, json.loads(response.read())
            except t.urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read())

        def pending(query=""):
            status, _h, body = t.call(base + "/pending-sends" + query, token=token)
            assert status == 200
            return json.loads(body)["pending"]
        try:
            # Queued, allowed, refused by the server: listed as failed.
            _s, queued = post("/send", {"recipient": "a@x.org", "message": "Soon."})
            rid = queued["request_id"]
            assert [e["status"] for e in pending()] == ["pending"]
            smtp.reject_data = "554 5.7.1 content rejected"
            post(f"/pending-sends/{rid}/approve", b"")
            (failed,) = pending("?chat=a%40x.org")
            assert failed["id"] == rid and failed["status"] == "error" and "554" in failed["error"]
            assert failed["outcome_unknown"] is False and "press" not in failed
            # Dismiss: only a failed one, once, and it is gone.
            _s, other = post("/send", {"recipient": "a@x.org", "message": "Waits."})
            status, said = post(f"/pending-sends/{other['request_id']}/dismiss", b"")
            assert status == 409 and "failed" in said["error"] and said["status"] == "pending"
            status, entry = post(f"/pending-sends/{rid}/dismiss", b"")
            assert status == 200 and entry["dismissed"] is True and entry["id"] == rid
            assert [e["id"] for e in pending()] == [other["request_id"]]
            assert post(f"/pending-sends/{rid}/dismiss", b"")[0] == 404
            assert post(f"/pending-sends/{'0' * 32}/dismiss", b"")[0] == 404
            assert t.call(f"{base}/pending-sends/{rid}/dismiss", body=b"")[0] == 401
            post(f"/pending-sends/{other['request_id']}/retract", b"")
            assert pending() == [] and smtp.mails == []

            # A direct send whose caller waits has the failure as its answer…
            status, said = post("/send", {"recipient": "a@x.org", "message": "Direct.",
                                          "user_approved": True, "request_key": "h-1"})
            assert status == 502 and said["error"].startswith("send failed: "), said
            assert said["sent"] is False and pending() == []
            assert gw.SENDS.by_key("h-1") is None
            # …and one whose caller hung up before the server answered keeps
            # it where the chat finds it.
            entered, release = threading.Event(), threading.Event()
            real_send = gw._ec._smtp_send

            def slow(*args, **kwargs):
                entered.set()
                release.wait(30)
                return real_send(*args, **kwargs)
            gw._ec._smtp_send = slow
            body = json.dumps({"recipient": "a@x.org", "message": "Gone.",
                               "user_approved": True, "request_key": "h-2"}).encode()
            host, port = base.rsplit("/", 1)[1].split(":")
            caller = socket.create_connection((host, int(port)), timeout=5)
            caller.sendall((f"POST /send HTTP/1.1\r\nHost: {host}\r\n"
                            f"Authorization: Bearer {token}\r\n"
                            f"Content-Type: application/json\r\n"
                            f"Content-Length: {len(body)}\r\n\r\n").encode() + body)
            assert entered.wait(10)
            (sending,) = pending("?chat=a%40x.org")
            assert sending["status"] == "sending" and sending["request_key"] == "h-2"
            caller.close()          # its deadline passed
            release.set()
            gone = threading.Event()
            for _ in range(250):
                if [e["status"] for e in pending()] == ["error"]:
                    break
                gone.wait(0.02)
            gw._ec._smtp_send = real_send
            (kept,) = pending()
            assert kept["status"] == "error" and kept["request_key"] == "h-2", kept
            assert kept["message"] == "Gone." and "554" in kept["error"]
            assert kept["outcome_unknown"] is False
            # Asked after by id it is the same failed send, not yet dismissed.
            status, _h, body = t.call(f"{base}/pending-sends/{kept['id']}", token=token)
            assert status == 200 and "dismissed" not in json.loads(body)
            # Nothing left the gateway under that key: the press again is a
            # new attempt. The mail goes out, and the failed send gives way.
            smtp.reject_data = None
            status, said = post("/send", {"recipient": "a@x.org", "message": "Gone.",
                                          "user_approved": True, "request_key": "h-2"})
            assert status == 200 and said["status"] == "sent" and "repeat" not in said
            assert len(smtp.mails) == 1 and pending() == []
            assert t.call(f"{base}/pending-sends/{kept['id']}", token=token)[0] == 404
            assert post(f"/pending-sends/{kept['id']}/dismiss", b"")[0] == 404
            status, again = post("/send", {"recipient": "a@x.org", "message": "Gone.",
                                           "user_approved": True, "request_key": "h-2"})
            assert (status, again) == (200, {**said, "repeat": True}) and len(smtp.mails) == 1

            # A mail that may have gone out: dismissed, its entry stays for
            # as long as its key is remembered — answered by id as over
            # (``dismissed``), never listed, and never sent by a repeat.
            smtp.drop = "dot"
            status, said = post("/send", {"recipient": "a@x.org", "message": "Maybe.",
                                          "user_approved": True, "request_key": "h-3"})
            smtp.drop = None
            assert status == 502 and said["unconfirmed"] is True and "sent" not in said
            (unknown,) = pending()
            assert unknown["outcome_unknown"] is True and unknown["id"] == said["request_id"]
            status, entry = post(f"/pending-sends/{unknown['id']}/dismiss", b"")
            assert status == 200 and entry["dismissed"] is True and pending() == []
            status, _h, body = t.call(f"{base}/pending-sends/{unknown['id']}", token=token)
            over = json.loads(body)
            assert status == 200 and over["status"] == "error" and over["dismissed"] is True
            assert over["outcome_unknown"] is True
            assert post(f"/pending-sends/{unknown['id']}/dismiss", b"")[0] == 200
            status, again = post("/send", {"recipient": "a@x.org", "message": "Maybe.",
                                           "user_approved": True, "request_key": "h-3"})
            assert status == 502 and again["unconfirmed"] is True and again["repeat"] is True
            assert len(smtp.mails) == 2, "the one that was handed over, and no other"
        finally:
            server.shutdown()
    finally:
        done(w)
    print("ok: failed sends are listed and dismissed over HTTP; a caller that left is not lost")


def test_the_reply_preview_is_what_send_would_address():
    w = world()
    try:
        gw, box, mirror, smtp = w.gw, w.box, w.mirror, w.smtp
        box.put("INBOX", t.msg("a@x.org", to=f"{ACCOUNT}, Bob <bob@y.org>",
                               cc="Cat <cat@z.org>, alias@example.org", day=1))
        box.put("INBOX", t.msg("solo@x.org", sender="Dora <dora@w.org>", subject="Lunch",
                               day=1))
        box.put("INBOX", hostile_mail(
            "Message-ID: <rt@x.org>", "From: News <noreply@list.org>",
            f"To: {ACCOUNT}, bob@y.org", "Reply-To: Desk <desk@list.org>, <pöst@list.org>",
            "Subject: Offer", "Date: " + t.email.utils.formatdate(t.T0 + DAY)))
        mirror.poll()

        def preview(chat, account=ACCOUNT):
            return gw._reply_preview({"chat": chat, "account": account})

        def addresses(addressing):
            return ([p["address"] for p in addressing["to"]],
                    [p["address"] for p in addressing["cc"]])

        def envelope(chat, mode):
            status, answer = w.send(recipient=chat, reply=mode)
            assert status == 200, answer
            sent = smtp.parsed()
            return ([a.addr_spec for a in sent["To"].addresses],
                    [a.addr_spec for a in sent["Cc"].addresses] if sent["Cc"] else [])

        status, shown = preview("a@x.org")
        assert status == 200, shown
        assert shown == {
            "all": {"to": [{"address": "ann@x.org", "name": "Ann Example"}],
                    "cc": [{"address": "bob@y.org", "name": "Bob"},
                           {"address": "cat@z.org", "name": "Cat"}]},
            "sender": {"to": [{"address": "ann@x.org", "name": "Ann Example"}], "cc": []},
            "choice": True, "subject": "Re: Quarterly numbers", "answers": "a@x.org"}, shown
        json.dumps(shown)
        assert smtp.sessions == 0 and gw.SENDS.entries() == [], "a preview sends nothing"
        # A thread with one correspondent has no choice to offer.
        status, solo = preview("solo@x.org")
        assert solo["choice"] is False and solo["all"] == solo["sender"]
        assert addresses(solo["all"]) == (["dora@w.org"], [])
        # Reply-To: the address a reply goes to instead of the From — with
        # no name the thread does not know — and the one that is no address
        # named as left out.
        status, desk = preview("rt@x.org")
        assert desk["all"] == {"to": [{"address": "desk@list.org", "name": None}],
                               "cc": [{"address": "bob@y.org", "name": None}]}, desk
        assert desk["skipped"] == ["pöst@list.org"] and desk["answers"] == "rt@x.org"
        # What is shown is what is sent, mode by mode — and again once the
        # user's own mail is the latest: to whom that went.
        for chat in ("a@x.org", "solo@x.org", "rt@x.org"):
            for mode in ("all", "sender"):
                shown = preview(chat)[1]
                assert addresses(shown[mode]) == envelope(chat, mode), (chat, mode)
        status, follow = preview("a@x.org")
        assert addresses(follow["all"]) == (["ann@x.org"], []), "the last reply went to Ann alone"
        assert follow["choice"] is False and follow["answers"] == "a@x.org"

        # The refusals are /send's, with its sentences.
        for chat in ("nope@x.org", ""):
            status, said = preview(chat)
            assert status == 400 and isinstance(said["error"], str)
        assert preview("a@x.org", account="other@example.org") == (404, gw._NOT_OURS)
        assert preview("a@x.org", account="")[0] == 404
        assert preview("a@x.org", account="ME@EXAMPLE.ORG")[0] == 200
        # A thread whose mails are all gone from the mailbox…
        w.told()
        for folder in ("INBOX", "Sent"):
            for uid in list(box.boxes[folder]["mails"]):
                raw = box.boxes[folder]["mails"][uid]["raw"]
                if b"Lunch" in raw:
                    box.remove(folder, uid)
        mirror._clock = lambda: NOW + gw.MAIL_SENT_GRACE_SECONDS + 5
        mirror.poll()
        status, said = preview("solo@x.org")
        assert (status, said) == w.send(recipient="solo@x.org") and status == 409, said
    finally:
        done(w)
    # …and a chat that so far holds only a queued new mail: nothing to reply
    # to, said as that and not as an unknown chat.
    w = world(policy="verify")
    try:
        gw = w.gw
        _s, queued = w.send(to=["ann@x.org"], subject="New", message="Hello.")
        status, said = gw._reply_preview({"chat": queued["chat"], "account": ACCOUNT})
        assert status == 409 and "not been sent yet" in said["error"], said
        assert w.send(recipient=queued["chat"])[0] == 409
        server, base = t.serve(gw)
        try:
            body = json.dumps({"chat": queued["chat"], "account": ACCOUNT}).encode()
            assert t.call(f"{base}/reply-preview", body=body)[0] == 401
            status, _h, raw = t.call(f"{base}/reply-preview", body=body, token=t.TOKEN)
            assert status == 409 and json.loads(raw) == said
            assert t.call(f"{base}/reply-preview", token=t.TOKEN)[0] == 404, "POST only"
        finally:
            server.shutdown()
    finally:
        done(w)
    print("ok: /reply-preview answers whom /send would write to, in both modes, and sends nothing")


# --------------------------------------------------------------------------
# A provider that files sent mail under a Message-ID of its own
# --------------------------------------------------------------------------

def as_the_provider_files_it(raw: bytes, new_id: str) -> bytes:
    """A submitted mail as a rewriting submission service keeps it: the
    Message-ID replaced by one of its own, every other header as it came."""
    head, sep, body = raw.partition(b"\r\n\r\n")
    lines = [b"Message-ID: <" + new_id.encode() + b">"
             if line.lower().startswith(b"message-id:") else line
             for line in head.split(b"\r\n")]
    assert lines != head.split(b"\r\n")
    return b"\r\n".join(lines) + sep + body


def outbound(gw) -> list:
    return [r for r in t.records(gw) if r["type"] == gw._ibstore.T_OUTBOUND]


def test_a_sent_copy_under_the_providers_own_message_id_is_the_same_mail():
    P = "0100018f-aaaa-bbbb@mail.provider.example"
    # The provider files the copy (SMTP_SAVE_SENT=false): a new mail.
    w = world(save_sent=False)
    try:
        gw, box, mirror, smtp = w.gw, w.box, w.mirror, w.smtp
        status, answer = w.send(to=["ann@x.org"], cc=["bob@y.org"], subject="Plans",
                                message="Hello.", author="user")
        M, chat = answer["message_id"], answer["chat"]
        assert status == 200 and chat == M and len(w.told()) == 1
        assert header(smtp.parsed(), "X-Retinue-Message-ID") == f"<{M}>"
        box.put("Sent", as_the_provider_files_it(smtp.mails[0]["data"], P), date=NOW + 5)
        fetched = len(box.fetched)
        mirror.poll()
        # One record, one chat, one bubble — the copy is a place of the
        # gateway's own record, which now says what the mail is known as.
        (record,) = outbound(gw)
        assert record["message_id"] == M and record["sent_as"] == [P], record
        assert record["author"] == "user" and len(t.records(gw)) == 1
        assert len(w.told()) == 1 and [c["chat"] for c in mirror.recent_chats()] == [M]
        row = mirror.index.one("SELECT role, uid, message_id FROM locations")
        assert (row["role"], row["message_id"]) == ("sent", M)
        assert settling(mirror, M) is None and mirror._known_as(M) == [M, P]
        assert gw._health_snapshot()["rewritten_message_ids"] == 1
        assert mirror.health_notice() is None
        # Read once: the polls that follow leave it alone.
        fetched = len(box.fetched)
        for _ in range(2):
            mirror.poll()
        assert len(box.fetched) == fetched and len(t.records(gw)) == 1

        # A follow-up to our own mail answers it under the id the others
        # have, and cites both.
        status, follow = w.send(recipient=M, message="One more thing.")
        sent = smtp.parsed()
        assert status == 200 and smtp.mails[-1]["rcpt"] == ["ann@x.org", "bob@y.org"]
        assert header(sent, "In-Reply-To") == f"<{P}>"
        assert header(sent, "References") == f"<{M}> <{P}>"
        box.put("Sent", as_the_provider_files_it(smtp.mails[1]["data"], "f@provider.example"),
                date=NOW + 7)
        # The recipient's answer cites the provider's id — the only one they
        # ever saw — and lands in this chat.
        box.put("INBOX", t.msg("r1@x.org", subject="Re: Plans", refs=[P], irt=P,
                               to=f"{ACCOUNT}, bob@y.org", day=41))
        mirror.poll()
        assert t.by_id(gw)["r1@x.org"]["chat"] == M
        assert [c["chat"] for c in mirror.recent_chats()] == [M]
        # A reply of ours to their mail, and the copy of that one: one record.
        status, reply = w.send(recipient=M, message="Fine.")
        assert status == 200 and len(w.told()) == 3
        box.put("Sent", as_the_provider_files_it(smtp.mails[2]["data"], "second@provider.example"),
                date=NOW + 9)
        mirror.poll()
        assert len(outbound(gw)) == 3 and len(t.records(gw)) == 4 and len(w.told()) == 3
        assert t.by_id(gw)[reply["message_id"]]["sent_as"] == ["second@provider.example"]
        assert t.by_id(gw)[follow["message_id"]]["sent_as"] == ["f@provider.example"]

        # Everything above is on the records: with both indexes lost, the
        # copies are still places of their mails and nothing is told anew…
        records, events = len(t.records(gw)), len(w.events)
        for name in ("mailbox.sqlite", "threads.sqlite"):
            (gw.MAIL_STATE_DIR / name).unlink()
        mirror = w.restart()
        for _ in range(3):
            mirror.poll()
        assert len(t.records(gw)) == records and mirror._known_as(M) == [M, P]
        assert not any(r["removed_at"] for r in t.records(gw))
        assert w.events[events:] == [], "nothing is told anew"
        assert mirror.threads.chat_of(P) == M
        assert mirror.threads.chat_of("second@provider.example") == M
        # …and when Sent is renumbered, each is found again under the id the
        # mailbox has it by.
        box.renumber("Sent")
        for _ in range(3):
            mirror.poll()
        assert not any(r["removed_at"] for r in t.records(gw)) and len(t.records(gw)) == records
        assert box.uid_of("Sent", P) in [row["uid"] for row in mirror.index.rows(
            "SELECT l.uid FROM locations l JOIN folders f ON f.role = l.role AND f.gen = l.gen "
            "WHERE l.message_id = ?", (M,))]
    finally:
        done(w)

    # The answer comes before the copy is seen: it starts a chat of its own,
    # which the copy merges into the mail's.
    w = world(save_sent=False)
    try:
        gw, box, mirror, smtp = w.gw, w.box, w.mirror, w.smtp
        _status, answer = w.send(to=["ann@x.org"], subject="Plans")
        M = answer["message_id"]
        w.told()
        mirror._clock = lambda: NOW + 2 * DAY      # (the answer comes a day later)
        box.put("INBOX", t.msg("r1@x.org", subject="Re: Plans", refs=[P], irt=P, day=41))
        mirror.poll()
        assert t.by_id(gw)["r1@x.org"]["chat"] == P
        box.put("Sent", as_the_provider_files_it(smtp.mails[0]["data"], P), date=NOW + 5)
        mirror.poll()
        mirror.poll()
        assert t.by_id(gw)["r1@x.org"]["chat"] == M and len(t.records(gw)) == 2
        assert ("merge", {"channel": "email", "account": ACCOUNT, "from": [P], "into": M}) \
            in w.events
    finally:
        done(w)

    # The gateway files a copy itself and the provider files its own: two
    # copies in Sent, one record — and /health says what to set.
    w = world()
    try:
        gw, box, mirror, smtp = w.gw, w.box, w.mirror, w.smtp
        _status, answer = w.send(to=["ann@x.org"], subject="Plans")
        M = answer["message_id"]
        w.told()
        assert len(w.sent_copies(M)) == 1
        box.put("Sent", as_the_provider_files_it(smtp.mails[0]["data"], P), date=NOW + 5)
        mirror.poll()
        assert len(t.records(gw)) == 1 and t.by_id(gw)[M]["sent_as"] == [P]
        assert mirror.index.one("SELECT COUNT(*) AS n FROM locations WHERE message_id = ?",
                                (M,))["n"] == 2
        assert "SMTP_SAVE_SENT=false" in mirror.health_notice()
    finally:
        done(w)
    print("ok: the provider's copy of a sent mail, under its own Message-ID, is that mail")


def test_a_providers_copy_that_names_nothing_is_known_by_what_it_is():
    """A provider that replaces the Message-ID and drops the gateway's own
    header as well: the copy it files is still the gateway's send — the one
    mail it built with this very Date line, to these people, saying this."""
    def stripped(raw: bytes, new_id: str) -> bytes:
        head, sep, body = as_the_provider_files_it(raw, new_id).partition(b"\r\n\r\n")
        return b"\r\n".join(line for line in head.split(b"\r\n")
                            if not line.lower().startswith(b"x-retinue-")) + sep + body

    w = world(save_sent=False)
    try:
        gw, box, mirror, smtp = w.gw, w.box, w.mirror, w.smtp
        box.put("INBOX", t.msg("a@x.org", cc="bob@y.org", day=1))
        mirror.poll()
        _status, first = w.send(recipient="a@x.org", message="First answer.", author="user")
        gw._now = lambda: NOW + 60
        _status, second = w.send(recipient="a@x.org", message="Second answer.")
        w.told()
        raw_first, raw_second = smtp.mails[0]["data"], smtp.mails[1]["data"]
        assert b"X-Retinue-Message-ID" in raw_first
        box.put("Sent", stripped(raw_second, "p2@provider.example"), date=NOW + 65)
        box.put("Sent", stripped(raw_first, "p1@provider.example"), date=NOW + 5)
        mirror.poll()
        records = t.by_id(gw)
        assert len(records) == 3, sorted(records)
        assert records[first["message_id"]]["sent_as"] == ["p1@provider.example"]
        assert records[second["message_id"]]["sent_as"] == ["p2@provider.example"]
        assert records[first["message_id"]]["author"] == "user" and len(w.told()) == 2
        assert gw._health_snapshot()["rewritten_message_ids"] == 2

        # Not the same mail, and so a mail of its own: another text, other
        # recipients, another second — or a mail some client wrote, which no
        # send of the gateway's is like at all.
        _status, third = w.send(recipient="a@x.org", message="Third answer.")
        w.told()
        raw_third = smtp.mails[2]["data"]
        box.put("Sent", stripped(raw_third.replace(b"Third answer.", b"Something else."),
                         "t1@provider.example"), date=NOW + 65)
        box.put("Sent", stripped(raw_third.replace(b"Cc: bob@y.org", b"Cc: eve@evil.org"),
                         "t2@provider.example"), date=NOW + 65)
        later = raw_third.replace(b"\r\nDate: ", b"\r\nDate: Thu, 01 Jan 2026 00:00:00 +0000"
                                                 b"\r\nX-Was: ")
        box.put("Sent", stripped(later, "t3@provider.example"), date=NOW + 65)
        box.put("Sent", t.msg("phone@example.org", sender=ME, to="ann@x.org",
                              subject="Re: Quarterly numbers", body="Third answer.\n", day=40))
        mirror.poll()
        records = t.by_id(gw)
        assert records[third["message_id"]]["sent_as"] == []
        for other in ("t1@provider.example", "t2@provider.example", "t3@provider.example",
                      "phone@example.org"):
            assert records[other]["author"] == "device", other
        # Two sends nobody can tell apart — the same second, people, subject
        # and words: neither is guessed at.
        gw._now = lambda: NOW + 120
        _status, one = w.send(to=["ann@x.org"], subject="Ping", message="Ping.")
        _status, two = w.send(to=["ann@x.org"], subject="Ping", message="Ping.")
        w.told()
        box.put("Sent", stripped(smtp.mails[-1]["data"], "twin@provider.example"), date=NOW + 125)
        mirror.poll()
        records = t.by_id(gw)
        assert records["twin@provider.example"]["author"] == "device"
        assert records[one["message_id"]]["sent_as"] == records[two["message_id"]]["sent_as"] == []
    finally:
        done(w)
    print("ok: a provider's copy that kept no header is matched by its Date, people and words")


def test_the_providers_copy_settles_a_send_nobody_saw_answered():
    P = "0100018f-cccc-dddd@mail.provider.example"
    # Handed over, the answer lost, Sent cannot say yet — and then the copy
    # shows, under an id of the provider's: the mail went out.
    w = world(save_sent=False)
    try:
        gw, box, mirror, smtp = w.gw, w.box, w.mirror, w.smtp
        smtp.drop = "dot"
        status, answer = w.send(to=["ann@x.org"], subject="Plans", author="user")
        M = answer["message_id"]
        assert status == 502 and answer["unconfirmed"] is True and "outcome_unknown" not in answer
        mirror.poll()
        assert t.records(gw) == [] and gw.SENDS.listed()[0]["status"] == "sending"
        box.put("Sent", as_the_provider_files_it(smtp.mails[0]["data"], P), date=NOW + 5)
        mirror.poll()
        (record,) = t.records(gw)
        assert record["message_id"] == M and record["sent_as"] == [P]
        assert record["author"] == "user" and record["text"] == "Thanks."
        assert [e["message_id"] for e in w.told()] == [M]
        assert gw.SENDS.entries() == [] and gw.SENDS.due() == [] and smtp.sessions == 1
    finally:
        done(w)

    # Given up on as "outcome unknown" after the grace, and the copy shows
    # later still — under the provider's id, or under the gateway's own: the
    # mail is a message of the user's then, and no failed send stands beside it.
    for late_id in (P, None):
        w = world(save_sent=False)
        try:
            gw, box, mirror, smtp = w.gw, w.box, w.mirror, w.smtp
            smtp.drop = "dot"
            status, answer = w.send(to=["ann@x.org"], subject="Plans", author="user",
                                    request_key="late-1")
            M = answer["message_id"]
            gw._now = lambda: NOW + gw.MAIL_SENT_GRACE_SECONDS + 1
            mirror.poll()
            (failed,) = gw.SENDS.listed()
            assert failed["status"] == "error" and failed["outcome_unknown"] is True
            raw = smtp.mails[0]["data"]
            box.put("Sent", as_the_provider_files_it(raw, late_id) if late_id else raw,
                    date=NOW + 900)
            mirror.poll()
            (record,) = t.records(gw)
            assert record["message_id"] == (late_id or M) and record["author"] == "user"
            assert gw.SENDS.listed() == [], "the outcome is known: it is a message"
            # The repeat of that press is told it was sent, and sends nothing.
            smtp.drop = None
            status, said = w.send(to=["ann@x.org"], subject="Plans", author="user",
                                  request_key="late-1")
            assert status == 200 and said["status"] == "sent" and smtp.sessions == 1
        finally:
            done(w)
    print("ok: a copy in Sent settles a send whose answer was lost, whatever id it is under")


def test_the_own_id_header_alone_proves_nothing():
    w = world(save_sent=False)
    try:
        gw, box, mirror, smtp = w.gw, w.box, w.mirror, w.smtp
        _status, answer = w.send(to=["ann@x.org"], subject="Plans")
        M = answer["message_id"]
        w.told()
        claim = [("X-Retinue-Message-ID", f"<{M}>")]
        # A mail that comes in carrying the header — every recipient of the
        # mail has it — is a mail that came in.
        box.put("INBOX", t.msg("in@evil.org", sender="Eve <eve@evil.org>", subject="Re: Plans",
                               headers=claim, day=41))
        # In Sent, but not what the gateway sent: to other people, or from
        # another address, or long after — each is a mail of its own, as any
        # mail a client sent.
        box.put("Sent", t.msg("others@example.org", sender=ME, to="mallory@evil.org",
                              subject="Plans", headers=claim, day=40))
        box.put("Sent", t.msg("from@example.org", sender="Someone <else@example.org>",
                              to="ann@x.org", subject="Plans", headers=claim, day=40))
        box.put("Sent", t.msg("late@example.org", sender=ME, to="ann@x.org",
                              subject="Plans", headers=claim, day=80))
        # And one that names an id the gateway never sent under.
        box.put("Sent", t.msg("never@example.org", sender=ME, to="ann@x.org", subject="Plans",
                              headers=[("X-Retinue-Message-ID", "<nobody@example.org>")],
                              day=40))
        mirror.poll()
        records = t.by_id(gw)
        assert set(records) == {M, "in@evil.org", "others@example.org", "from@example.org",
                                "late@example.org", "never@example.org"}
        assert records[M]["sent_as"] == [] and mirror._known_as(M) == [M]
        assert records["in@evil.org"]["type"] == gw._ibstore.T_INBOUND
        for mid in ("others@example.org", "from@example.org", "late@example.org",
                    "never@example.org"):
            assert records[mid]["author"] == "device", (mid, records[mid]["author"])
        assert gw._health_snapshot()["rewritten_message_ids"] == 0
    finally:
        done(w)
    print("ok: the gateway's own header names its send only with the rest of the evidence")


# --------------------------------------------------------------------------
# The write half on the mirror's mechanisms (phase 1's later rounds): every
# mail under an id, a placed UID handed out by no search, header blocks read
# as guarded as mail, a record replaced in its own place, the `lost` list
# --------------------------------------------------------------------------

def _unreadable(gw):
    """Every mail with an ``X-Unreadable`` header is one the reader cannot
    read (a placeholder under its own id: its header block reads). Returns
    the undo."""
    real = gw.read_mail

    def read_mail(raw, date, *args, **kwargs):
        if b"X-Unreadable: yes" in raw:
            raise gw.UnreadableMail("too much work", "work")
        return real(raw, date, *args, **kwargs)
    gw.read_mail = read_mail
    return lambda: setattr(gw, "read_mail", real)


def _places_of(mirror, role, message_id) -> dict:
    return {row["uid"]: row["skipped"] for row in mirror.index.rows(
        "SELECT l.uid, l.skipped FROM locations l JOIN folders f ON f.role = l.role "
        "AND f.gen = l.gen WHERE l.role = ? AND l.message_id = ?", (role, message_id))}


def _stalled(gw):
    """What the writing half asks of a header block does not come back
    (read_send_facts). Returns the undo."""
    real = gw._send_facts

    def stalls(head):
        raise gw.TookTooLong("a reading that does not come back")
    gw._send_facts = stalls
    return lambda: setattr(gw, "_send_facts", real)


def test_a_move_whose_new_uid_is_not_said_finds_the_placeholders_own_copy():
    """_follow_move looks for a moved mail as the mirror looks (_adopt): of
    every mail the archive holds under a placeholder's id, only the copy its
    record keeps the header block of is its place — not the newest one, which
    may be anybody's."""
    w = world()
    try:
        gw, box, mirror = w.gw, w.box, w.mirror
        undo = _unreadable(gw)
        box.names_uids = False
        box.put("INBOX", t.msg("p@x.org", headers=[("X-Unreadable", "yes")], day=1))
        mirror.poll()
        assert t.by_id(gw)["p@x.org"]["text"] == gw.UNREADABLE_TEXT
        real_move = box.move

        def move(folder, uids, to_folder, *, validity=None):
            moved = real_move(folder, uids, to_folder, validity=validity)
            # Another mail under the same id reaches the archive meanwhile.
            box.put("Archive", t.msg("p@x.org", sender="Mallory <m@evil.org>",
                                     body="Not the mail.\n", day=2))
            return moved
        box.move = move
        assert mirror.archive("p@x.org")["moved"] == 1
        other, own = box.uids_of("Archive", "p@x.org")
        assert _places_of(mirror, "archive", "p@x.org") == {own: None, other: "other"}
        assert settling(mirror, "p@x.org") is None
        undo()
    finally:
        done(w)
    print("ok: a move whose new UID is not said finds the placeholder's own copy, "
          "not the newest mail under its id")


def test_a_deleted_placeholder_leaves_the_other_mail_under_its_id_to_be_read():
    """A delete moves a placeholder's own copy, never another mail under its
    id (_places, _whereabouts); erased with its chat (_forget_chat), what was
    set apart as that other mail's is mirrored as the mail it is — as when a
    placeholder is buried (_bury)."""
    w = world()
    try:
        gw, box, mirror = w.gw, w.box, w.mirror
        undo = _unreadable(gw)
        box.put("INBOX", t.msg("p@x.org", headers=[("X-Unreadable", "yes")], day=1))
        mirror.poll()
        other = box.put("INBOX", t.msg("p@x.org", sender="Mallory <m@evil.org>",
                                       body="Not the mail.\n", day=2))
        mirror.poll()
        assert _places_of(mirror, "inbox", "p@x.org")[other] == "other"
        result = mirror.delete_chat("p@x.org")
        assert result["errors"] == 0 and result["trashed"] == 1, result
        assert held_in(box, "Trash") == ["p@x.org"]
        assert box.uids_of("INBOX", "p@x.org") == (other,)
        assert t.records(gw) == []
        undo()
        mirror.poll()
        (record,) = t.records(gw)
        assert record["sender"] == "m@evil.org" and record["text"] == "Not the mail.", record
        assert _places_of(mirror, "inbox", "p@x.org") == {other: None}
    finally:
        done(w)
    print("ok: a deleted placeholder takes only its own copy to the Trash, and the other "
          "mail under its id is mirrored as what it is")


def test_a_reply_is_not_addressed_off_a_header_block_that_did_not_come_back():
    """reply_draft reads Reply-To off the header block an older record keeps
    as guarded as any header block is read (read_send_facts), and not under
    the mirror's lock: a reading that does not come back is no answer —
    /reply-preview says "try again", /send says nothing was sent, and
    nothing is."""
    w = world()
    try:
        gw, box, mirror, smtp = w.gw, w.box, w.mirror, w.smtp
        box.put("INBOX", t.msg("a@x.org", headers=[("Reply-To", "desk@x.org")], day=1))
        mirror.poll()
        # (An older record: the header block alone says whom to answer.)
        path = mirror._record_path(mirror.index.one(
            "SELECT record FROM mails WHERE message_id = 'a@x.org'")["record"])
        text = path.read_text(encoding="utf-8")
        path.write_text("".join(line for line in text.splitlines(keepends=True)
                                if "replyTo" not in line), encoding="utf-8")
        assert t.by_id(gw)["a@x.org"]["reply_to"] == []
        status, preview = gw._reply_preview({"chat": "a@x.org", "account": ACCOUNT})
        assert status == 200 and preview["all"]["to"][0]["address"] == "desk@x.org", preview
        undo = _stalled(gw)
        try:
            status, preview = gw._reply_preview({"chat": "a@x.org", "account": ACCOUNT})
            assert status == 503 and "try again" in preview["error"], (status, preview)
            status, answer = w.send(recipient="a@x.org")
            assert status == 502 and answer["sent"] is False and smtp.mails == [], answer
        finally:
            undo()
        status, answer = w.send(recipient="a@x.org")
        assert status == 200 and smtp.mails[0]["rcpt"] == ["desk@x.org"], answer
    finally:
        done(w)
    print("ok: a reply is not addressed off a header block whose reading did not come back")


def test_a_sent_copy_whose_send_cannot_be_told_is_not_recorded_twice():
    """_sent_like reads the header blocks it compares as guarded as any
    (read_send_facts): where a reading does not come back, the copy in Sent
    is not mirrored as a mail of its own beside the gateway's send — the
    pass ends on it (I1), and the next one matches it."""
    w = world(save_sent=False)
    try:
        gw, box, mirror, smtp = w.gw, w.box, w.mirror, w.smtp
        _status, answer = w.send(to=["ann@x.org"], subject="Plans", message="Hello.")
        M, P = answer["message_id"], "p@provider.example"
        w.told()
        # A provider that rewrites the id and keeps no header of ours.
        raw = as_the_provider_files_it(smtp.mails[0]["data"], P)
        raw = b"\r\n".join(line for line in raw.split(b"\r\n")
                           if not line.lower().startswith(b"x-retinue-message-id"))
        box.put("Sent", raw, date=NOW + 5)
        undo = _stalled(gw)
        try:
            try:
                mirror.poll()
                raise AssertionError("the pass went on")
            except gw.MirrorFault as exc:
                assert "could not be read" in str(exc), exc
            assert [r["message_id"] for r in t.records(gw)] == [M]
        finally:
            undo()
        mirror.poll()
        (record,) = t.records(gw)
        assert record["message_id"] == M and record["sent_as"] == [P], record
    finally:
        done(w)
    print("ok: a Sent copy whose send cannot be told yet is not recorded beside it")


def test_a_record_written_again_keeps_its_author_and_its_providers_ids():
    """A tombstone whose mail is back is written again in its own place
    (Mirror._write, replace=): a record of the gateway's own send keeps the
    author it was written with — its note may be pruned by then — and the
    ids its provider gave it (kb:sentAs), which its answers cite."""
    w = world()
    try:
        gw, box, mirror = w.gw, w.box, w.mirror
        _status, answer = w.send(to=["ann@x.org"], subject="Plans", author="user")
        M = answer["message_id"]
        w.told()
        (uid,) = w.sent_copies(M)
        path = mirror._record_path(mirror.index.one(
            "SELECT record FROM mails WHERE message_id = ?", (M,))["record"])
        gw._ibstore.update_message(path, sent_as="p@provider.example")
        copy = box.remove("Sent", uid)
        mirror.poll()
        assert t.by_id(gw)[M]["removed_at"], "the copy left: a tombstone"
        mirror.index.run("DELETE FROM notes")
        box.put("Sent", copy["raw"], flags=copy["flags"], date=copy["date"])
        mirror.poll()
        record = t.by_id(gw)[M]
        assert not record["removed_at"] and len(t.records(gw)) == 1
        assert record["author"] == "user" and record["sent_as"] == ["p@provider.example"], record
        assert path.is_file(), "written again in its own place"
    finally:
        done(w)
    print("ok: a record written again keeps its author and the ids its provider gave it")


def test_a_lost_record_of_a_rewritten_send_is_found_under_the_providers_id():
    """A record of the gateway's own send whose provider filed it under an
    id of its own, lost from the ledger: its mail is looked for (`lost`)
    under that id too — the aliases of a lost id are kept while it is — and
    read again from its Sent copy."""
    w = world(save_sent=False)
    try:
        gw, box, mirror, smtp = w.gw, w.box, w.mirror, w.smtp
        _status, answer = w.send(to=["ann@x.org"], subject="Plans", message="Hello.")
        M, P = answer["message_id"], "p@provider.example"
        w.told()
        box.put("Sent", as_the_provider_files_it(smtp.mails[0]["data"], P), date=NOW + 5)
        mirror.poll()
        assert mirror._known_as(M) == [M, P]
        path = mirror._record_path(mirror.index.one(
            "SELECT record FROM mails WHERE message_id = ?", (M,))["record"])
        path.unlink()
        mirror = w.restart()
        assert mirror.lost() == 1 and mirror._known_as(M) == [M, P]
        for _ in range(3):
            mirror.poll()
        assert mirror.lost() == 0
        (record,) = t.records(gw)
        assert record["type"] == gw._ibstore.T_OUTBOUND and record["text"] == "Hello.", record
    finally:
        done(w)
    print("ok: a lost record of a rewritten send is found again under its provider's id")


def test_a_delete_looks_for_a_placeholder_whose_only_place_is_another_mails():
    """A place marked as another mail's is no place of a placeholder (I2):
    a delete looks for the placeholder's own copy as for any mail the index
    has no place for, finds it where a client moved it, and takes it to the
    Trash — it does not erase the chat while that copy is in the archive."""
    w = world()
    try:
        gw, box, mirror = w.gw, w.box, w.mirror
        undo = _unreadable(gw)
        own = box.put("INBOX", t.msg("p@x.org", headers=[("X-Unreadable", "yes")], day=1))
        mirror.poll()
        other = box.put("INBOX", t.msg("p@x.org", sender="Mallory <m@evil.org>",
                                       body="Not the mail.\n", day=2))
        # A client moves the placeholder's own copy to the archive, while the
        # archive will not answer: the poll that sees it leave cannot look.
        box.move_mail("INBOX", own, "Archive")
        box.refused = {"Archive"}
        mirror.poll()
        box.refused = set()
        assert _places_of(mirror, "inbox", "p@x.org") == {other: "other"}
        assert _places_of(mirror, "archive", "p@x.org") == {}
        result = mirror.delete_chat("p@x.org")
        assert result["errors"] == 0 and result["trashed"] == 1, result
        (trashed,) = box.boxes["Trash"]["mails"].values()
        assert b"X-Unreadable: yes" in trashed["raw"]
        assert box.uids_of("INBOX", "p@x.org") == (other,) and box.uids_of("Archive", "p@x.org") == ()
        undo()
    finally:
        done(w)
    print("ok: a delete looks for a placeholder whose only place is another mail's, and "
          "takes its own copy to the Trash")


def _rewritten_send(w, *, keep_own_header=False):
    """A send by the user that its provider files in Sent under an id of its
    own — and, unless ``keep_own_header``, without the gateway's header —
    recognised as that send (kb:sentAs). ``(minted id, provider id, the copy's
    bytes, its UID in Sent)``."""
    _status, answer = w.send(to=["ann@x.org"], subject="Plans", message="Hello.", author="user")
    M, P = answer["message_id"], "p@provider.example"
    w.told()
    raw = as_the_provider_files_it(w.smtp.mails[0]["data"], P)
    if not keep_own_header:
        raw = b"\r\n".join(line for line in raw.split(b"\r\n")
                           if not line.lower().startswith(b"x-retinue-message-id"))
    uid = w.box.put("Sent", raw, date=NOW + 5)
    w.mirror.poll()
    assert w.mirror._known_as(M) == [M, P], w.mirror._known_as(M)
    return M, P, raw, uid


def _the_one_send(w, M, P, author="user"):
    (record,) = t.records(w.gw)
    assert record["message_id"] == M and not record["removed_at"], record
    assert record["type"] == w.gw._ibstore.T_OUTBOUND and record["author"] == author, record
    assert record["sent_as"] == [P] and record["text"] == "Hello.", record
    return record


def test_a_known_provider_id_is_the_send_in_sent_too():
    """A copy under an id recorded in `aliases` that is shown to be the
    send's — in Sent, From the account, to its people, dated when it went —
    IS the send's mail in every folder, Sent included, with no note to go by
    (it is long pruned): Sent renumbered, the copy moved out and back, a
    second copy. And a stranger's INBOX mail under that id — every recipient
    has it — is a mail of its own, under its own id, and no place of the
    send's."""
    for how in ("renumbered", "moved out and back", "a second copy", "a stranger's mail"):
        w = world(save_sent=False)
        try:
            gw, box, mirror = w.gw, w.box, w.mirror
            M, P, raw, uid = _rewritten_send(w)
            mirror.index.run("DELETE FROM notes")   # pruned, by MAIL_NOTES_MAX later sends
            if how == "renumbered":
                box.renumber("Sent")
            elif how == "moved out and back":
                uid = box.move_mail("Sent", uid, "Archive")
                mirror.poll()
                mirror.poll()
                box.move_mail("Archive", uid, "Sent")
            elif how == "a second copy":
                box.put("Sent", raw, date=NOW + 5)
            else:
                box.put("INBOX", t.msg(P, sender="Mallory <m@evil.org>", subject="Re: Plans",
                                       body="Not the user's.\n", day=later(60)))
            for _ in range(3):
                mirror.poll()
            places = {M}
            if how == "a stranger's mail":
                records = t.by_id(gw)
                assert sorted(records) == sorted([M, P]), sorted(records)
                stranger, send = records[P], records[M]
                assert stranger["type"] == gw._ibstore.T_INBOUND, stranger
                assert stranger["sender"] == "m@evil.org" and stranger["sent_as"] == []
                assert send["type"] == gw._ibstore.T_OUTBOUND and send["author"] == "user"
                assert send["sent_as"] == [P] and send["text"] == "Hello.", send
                places.add(P)
            else:
                _the_one_send(w, M, P)
            assert {row["message_id"] for row in mirror.index.rows(
                "SELECT message_id FROM locations WHERE message_id IS NOT NULL")} == places, how
        finally:
            done(w)
    print("ok: a copy under a provider id on record is that send's mail in every folder, "
          "Sent included, where it is shown to be; a stranger's mail under it is its own")


def test_a_tombstoned_send_is_revived_under_its_minted_id():
    """A rewritten send's Sent copy goes to the Trash (a tombstone) and comes
    back: the record is written again in its own place, under the id the
    gateway minted, with its author and kb:sentAs — no second record under
    the provider's id."""
    w = world(save_sent=False)
    try:
        gw, box, mirror = w.gw, w.box, w.mirror
        M, P, _raw, uid = _rewritten_send(w, keep_own_header=True)
        trashed = box.move_mail("Sent", uid, "Trash")
        mirror.poll()
        mirror.poll()
        assert t.by_id(gw)[M]["removed_at"], "the copy left: a tombstone"
        box.move_mail("Trash", trashed, "Sent")
        mirror.poll()
        mirror.poll()
        _the_one_send(w, M, P)
    finally:
        done(w)
    print("ok: a tombstoned rewritten send whose copy is back is revived under its minted id")


def test_a_strangers_mail_under_the_providers_id_revives_no_tombstone():
    """Every recipient has the id the provider gave a send, and every answer
    cites it: a stranger's mail under it, once the send's Sent copy went to
    the Trash, is a mail of its own under that id — the send stays the
    user's tombstone, its kb:sentAs and alias on record across a restart —
    and the Sent copy back from the Trash revives the send, by the user,
    with the provider's id: with or without the gateway's header, with or
    without a restart in between."""
    for keep_own_header in (True, False):
        for restart in (False, True):
            w = world(save_sent=False)
            try:
                gw, box, mirror = w.gw, w.box, w.mirror
                M, P, _raw, uid = _rewritten_send(w, keep_own_header=keep_own_header)
                trashed = box.move_mail("Sent", uid, "Trash")
                mirror.poll()
                mirror.poll()
                box.put("INBOX", t.msg(P, sender="Mallory <mal@evil.org>", subject="Re: Plans",
                                       body="Pay Mallory 5000.\n", day=later(120)),
                        date=NOW + 120)
                mirror.poll()
                mirror.poll()
                records = t.by_id(gw)
                assert records[M]["removed_at"] and records[M]["author"] == "user", records[M]
                assert records[M]["type"] == gw._ibstore.T_OUTBOUND, records[M]
                assert records[M]["sent_as"] == [P], records[M]
                assert records[P]["type"] == gw._ibstore.T_INBOUND, records[P]
                assert records[P]["sender"] == "mal@evil.org", records[P]
                if restart:
                    mirror = w.restart()
                    mirror.poll()
                assert mirror._known_as(M) == [M, P], mirror._known_as(M)
                box.move_mail("Trash", trashed, "Sent")
                mirror.poll()
                mirror.poll()
                records = t.by_id(gw)
                assert sorted(records) == sorted([M, P]), sorted(records)
                send = records[M]
                assert not send["removed_at"] and send["author"] == "user", send
                assert send["type"] == gw._ibstore.T_OUTBOUND and send["text"] == "Hello.", send
                assert send["sent_as"] == [P], send
                assert records[P]["sender"] == "mal@evil.org" and not records[P]["removed_at"]
                assert _places_of(mirror, "sent", M) and not _places_of(mirror, "sent", P)
            finally:
                done(w)
    print("ok: a stranger's mail under a send's provider id is its own; the send's tombstone "
          "stays the user's, and its Sent copy revives it")


def _forged(delivered: bytes, *, to=None) -> bytes:
    """What a recipient of the user's send can write: the header block as
    it was delivered — the provider's id, the gateway's header naming the
    send, From the account, To, Cc, Date — with a text of their own (and,
    with ``to``, other people on its To)."""
    head, sep, body = delivered.partition(b"\r\n\r\n")
    assert b"X-Retinue-Message-ID" in head and b"Hello." in body
    if to is not None:
        head = b"\r\n".join(b"To: " + to.encode() if line.lower().startswith(b"to:") else line
                            for line in head.split(b"\r\n"))
    return head + sep + body.replace(b"Hello.", b"Pay Mallory 5000, as agreed.")


def _the_forgery_is_its_own(w, mirror, M, P, *, send_live: bool, to=None):
    """The send stays what it was, by the user; the forgery is a mail that
    came in, under its own id, in the send's chat; and a reply goes to
    nobody the user did not write to — beside the live send. With the send
    a tombstone, the forgery is all the thread has left: the reply goes to
    its To, as it would for a mail From its writer."""
    gw = w.gw
    records = t.by_id(gw)
    assert sorted(records) == sorted([M, P]), sorted(records)
    send, forgery = records[M], records[P]
    assert send["type"] == gw._ibstore.T_OUTBOUND and send["author"] == "user", send
    assert send["sent_as"] == [P] and bool(send["removed_at"]) != send_live, send
    assert "Mallory" not in (send["text"] or ""), send
    assert (send["text"] == "Hello.") == send_live, send
    assert forgery["type"] == gw._ibstore.T_INBOUND and forgery["sender"] == ACCOUNT, forgery
    assert not forgery.get("author") and not forgery["removed_at"], forgery
    assert "Pay Mallory" in forgery["text"], forgery
    assert not _places_of(mirror, "inbox", M), _places_of(mirror, "inbox", M)
    assert _places_of(mirror, "inbox", P), "the forgery is in INBOX, as its own mail"
    for mode in ("all", "sender"):
        draft = mirror.reply_draft(send["chat"], mode)
        addressed = set(draft["to"] + draft["cc"])
        if send_live:
            assert addressed <= {"ann@x.org", ACCOUNT}, (mode, draft)
        else:
            assert addressed == {to or "ann@x.org"}, (mode, draft)
        assert draft["in_reply_to"] == P, draft


def test_a_recipients_forgery_of_the_send_revives_no_tombstone():
    """Every recipient of the user's send holds its header block as it was
    delivered — the provider's id, the gateway's header naming the send,
    From the account, To, Cc, Date — and can write all of it on a mail of
    their own. Dropped into INBOX once the send's Sent copy went to the
    Trash, such a forgery (with the recipient's text; with other people on
    its To) revives nothing: the send stays the user's tombstone, the
    forgery is a mail that came in under its own id, the rail is told so,
    and a reply goes to nobody the user did not write to — across a
    restart too. Only the Sent copy back from the Trash revives the send."""
    for to in (None, "mal@evil.org"):
        w = world(save_sent=False)
        try:
            gw, box, mirror = w.gw, w.box, w.mirror
            M, P, raw, uid = _rewritten_send(w, keep_own_header=True)
            trashed = box.move_mail("Sent", uid, "Trash")
            mirror.poll()
            mirror.poll()
            assert t.by_id(gw)[M]["removed_at"]
            told = len(w.events)
            box.put("INBOX", _forged(raw, to=to), date=NOW + 600)
            mirror.poll()
            mirror.poll()
            _the_forgery_is_its_own(w, mirror, M, P, send_live=False, to=to)
            news = [(kind, p.get("direction"), p.get("message_id")) for kind, p in
                    w.events[told:] if kind == "inbound"]
            assert news == [("inbound", "in", P)], news
            mirror = w.restart()
            mirror.poll()
            assert mirror._known_as(M) == [M, P], mirror._known_as(M)
            _the_forgery_is_its_own(w, mirror, M, P, send_live=False, to=to)
            box.move_mail("Trash", trashed, "Sent")
            mirror.poll()
            mirror.poll()
            _the_forgery_is_its_own(w, mirror, M, P, send_live=True, to=to)
            assert _places_of(mirror, "sent", M)
        finally:
            done(w)
    print("ok: a recipient's forgery of the send's header block is a mail that came in, "
          "and revives no tombstone of the user's")


def test_a_recipients_forgery_beside_the_live_send_is_no_place_of_it():
    """The same forgery beside the live send is no place of it: trashing the
    user's real Sent copy tombstones the send (the forgery does not keep it
    live), and the forgery moved out of INBOX and back changes nothing about
    the send."""
    for to in (None, "mal@evil.org"):
        w = world(save_sent=False)
        try:
            gw, box, mirror = w.gw, w.box, w.mirror
            M, P, raw, uid = _rewritten_send(w, keep_own_header=True)
            forged = box.put("INBOX", _forged(raw, to=to), date=NOW + 600)
            mirror.poll()
            mirror.poll()
            _the_forgery_is_its_own(w, mirror, M, P, send_live=True, to=to)
            box.move_mail("Sent", uid, "Trash")
            mirror.poll()
            mirror.poll()
            _the_forgery_is_its_own(w, mirror, M, P, send_live=False, to=to)
            told = len(w.events)
            forged = box.move_mail("INBOX", forged, "Archive")
            mirror.poll()
            mirror.poll()
            box.move_mail("Archive", forged, "INBOX")
            mirror.poll()
            mirror.poll()
            _the_forgery_is_its_own(w, mirror, M, P, send_live=False, to=to)
            assert not [p for kind, p in w.events[told:]
                        if kind == "inbound" and p.get("direction") == "out"], w.events[told:]
        finally:
            done(w)
    print("ok: a recipient's forgery beside the live send is no place of it, in or out "
          "of INBOX")


def test_a_record_written_again_inbound_keeps_the_providers_id():
    """No rewrite of a send's record drops the ids its provider gave it: a
    mail under the minted id itself (the gateway's header tells every
    recipient that one too) takes the tombstone's place as a mail that came
    in — and still carries kb:sentAs, so a restart knows the alias, and the
    Sent copy back from the Trash makes it the user's send again, under the
    provider's id as well."""
    w = world(save_sent=False)
    try:
        gw, box, mirror = w.gw, w.box, w.mirror
        M, P, _raw, uid = _rewritten_send(w, keep_own_header=True)
        trashed = box.move_mail("Sent", uid, "Trash")
        mirror.poll()
        mirror.poll()
        box.put("INBOX", t.msg(M, sender="Mallory <mal@evil.org>", subject="Re: Plans",
                               body="Pay Mallory 5000.\n", day=later(120)), date=NOW + 120)
        mirror.poll()
        record = t.by_id(gw)[M]
        assert record["type"] == gw._ibstore.T_INBOUND and record["sent_as"] == [P], record
        mirror = w.restart()
        mirror.poll()
        assert mirror._known_as(M) == [M, P], mirror._known_as(M)
        box.move_mail("Trash", trashed, "Sent")
        mirror.poll()
        mirror.poll()
        _the_one_send(w, M, P)
        assert mirror.reply_draft(M, "all")["in_reply_to"] == P
    finally:
        done(w)
    print("ok: a send's record written again as a mail that came in keeps its provider id")


def test_a_send_to_oneself_under_the_providers_id_is_the_send_either_way_back():
    """The user's send Cc's their own address; the provider files its Sent
    copy and delivers its INBOX copy, both under its own id. Outside Sent
    nothing a recipient holds — the user is one here — says a mail is the
    user's send: the INBOX copy, whose header block is not the one the
    record keeps, is a mail of its own under the provider's id, From the
    user's address and come in, beside the send (as any mail client shows
    it: once sent, once received). Both go to the Trash and come back, the
    INBOX copy first or the Sent copy first, with or without a restart in
    between: the INBOX copy back never revives the send's tombstone; the
    Sent copy back does, by the user, under the minted id; once the record
    keeps that copy's very header block, a copy with it outside Sent is one
    more place of the live record. The provider id is kept across a
    restart, a follow-up still cites it, and goes to whom the user wrote."""
    for first in ("INBOX", "Sent"):
        for restart in (False, True):
            w = world(save_sent=False)
            try:
                gw, box, mirror, smtp = w.gw, w.box, w.mirror, w.smtp
                _status, answer = w.send(to=["ann@x.org"], cc=[t.ME], subject="Plans",
                                         message="Hello.", author="user")
                M, P = answer["message_id"], "p@provider.example"
                w.told()
                raw = as_the_provider_files_it(smtp.mails[0]["data"], P)
                uids = {"Sent": box.put("Sent", raw, date=NOW + 5),
                        "INBOX": box.put("INBOX", raw, date=NOW + 6)}
                mirror.poll()
                mirror.poll()

                def the_send_and_its_inbox_copy(inbox_copy_live: bool):
                    records = t.by_id(gw)
                    assert sorted(records) == sorted([M, P]), (first, restart, sorted(records))
                    send, copy = records[M], records[P]
                    assert send["type"] == gw._ibstore.T_OUTBOUND, send
                    assert send["author"] == "user" and send["sent_as"] == [P], send
                    assert copy["type"] == gw._ibstore.T_INBOUND and copy["sender"] == ACCOUNT
                    assert copy["chat"] == send["chat"], (copy["chat"], send["chat"])
                    assert bool(copy["removed_at"]) != inbox_copy_live, (first, restart, copy)
                    return send

                assert the_send_and_its_inbox_copy(True)["text"] == "Hello."
                trashed = {folder: box.move_mail(folder, uid, "Trash")
                           for folder, uid in uids.items()}
                mirror.poll()
                mirror.poll()
                assert t.by_id(gw)[M]["removed_at"] and t.by_id(gw)[P]["removed_at"]
                second = "Sent" if first == "INBOX" else "INBOX"
                box.move_mail("Trash", trashed[first], first)
                mirror.poll()
                mirror.poll()
                send = the_send_and_its_inbox_copy(first == "INBOX")
                # The INBOX copy back is its own mail again; the send stays
                # the user's tombstone. The Sent copy back revives it.
                assert bool(send["removed_at"]) == (first == "INBOX"), (first, send)
                if restart:
                    mirror = w.restart()
                    mirror.poll()
                box.move_mail("Trash", trashed[second], second)
                mirror.poll()
                mirror.poll()
                # The Sent copy back first: the record now keeps its header
                # block, and the INBOX copy, byte for byte that copy, is one
                # more place of the send — its own record stays a tombstone.
                send = the_send_and_its_inbox_copy(first == "INBOX")
                assert not send["removed_at"] and send["text"] == "Hello.", send
                mirror = w.restart()
                mirror.poll()
                assert mirror._known_as(M) == [M, P], (first, restart, mirror._known_as(M))
                the_send_and_its_inbox_copy(first == "INBOX")
                draft = mirror.reply_draft(M, "all")
                assert draft["in_reply_to"] == P, draft
                assert draft["to"] == ["ann@x.org"] and draft["cc"] == [], draft
            finally:
                done(w)
    print("ok: a send to oneself under its provider id is the send and, in INBOX, a mail "
          "of its own; the Sent copy back revives the send, in either order")


def test_a_mail_under_the_providers_id_read_before_the_sent_copy_stays_its_own():
    """A mail under the id the provider gave a send, met before that send's
    Sent copy (a provider that files late, INBOX read first): a mail of its
    own. The Sent copy then is still the send's — by the gateway's header,
    the account, the people and the date — one more place of its record,
    with the provider's id; never the other mail's record rewritten as the
    user's."""
    w = world(save_sent=False)
    try:
        gw, box, mirror, smtp = w.gw, w.box, w.mirror, w.smtp
        _status, answer = w.send(to=["ann@x.org"], subject="Plans", message="Hello.",
                                 author="user")
        M, P = answer["message_id"], "p@provider.example"
        w.told()
        box.put("INBOX", t.msg(P, sender="Mallory <mal@evil.org>", subject="Re: Plans",
                               body="Pay Mallory 5000.\n", day=later(30)), date=NOW + 30)
        mirror.poll()
        uid = box.put("Sent", as_the_provider_files_it(smtp.mails[0]["data"], P),
                      date=NOW + 5)
        mirror.poll()
        mirror.poll()
        records = t.by_id(gw)
        assert sorted(records) == sorted([M, P]), sorted(records)
        assert records[P]["type"] == gw._ibstore.T_INBOUND, records[P]
        assert records[P]["sender"] == "mal@evil.org", records[P]
        send = records[M]
        assert send["type"] == gw._ibstore.T_OUTBOUND and send["author"] == "user", send
        assert send["sent_as"] == [P], send
        assert _places_of(mirror, "sent", M) == {uid: None}
        assert not _places_of(mirror, "sent", P)
    finally:
        done(w)
    print("ok: a mail under a send's provider id met before its Sent copy stays its own, "
          "and the copy is the send's")


def test_a_lost_rewritten_send_comes_back_under_its_minted_id():
    """A rewritten send whose record the ledger lost: its Sent copy, under
    the provider's id, is the record again — under the minted id, with the
    provider's as kb:sentAs and the author noted under the minted one, in
    its chat, where an answer citing the provider's id lands."""
    w = world(save_sent=False)
    try:
        gw, box, mirror = w.gw, w.box, w.mirror
        M, P, _raw, _uid = _rewritten_send(w)
        path = mirror._record_path(mirror.index.one(
            "SELECT record FROM mails WHERE message_id = ?", (M,))["record"])
        path.unlink()
        mirror = w.restart()
        for _ in range(3):
            mirror.poll()
        assert mirror.lost() == 0
        assert _the_one_send(w, M, P)["chat"] == M
        box.put("INBOX", t.msg("r1@x.org", sender="Ann <ann@x.org>", subject="Re: Plans",
                               refs=[P], irt=P, day=later(60)))
        mirror.poll()
        assert t.by_id(gw)["r1@x.org"]["chat"] == M
        assert [chat["chat"] for chat in mirror.recent_chats()] == [M]
    finally:
        done(w)
    print("ok: a lost rewritten send comes back under its minted id, author and provider id")


def test_an_unreadable_sent_copy_of_a_send_is_that_send():
    """The provider's copy of the gateway's own send that the reader cannot
    read is that send's by what its placeholder still carries — the
    gateway's header naming the minted id — and no placeholder record of its
    own, in a chat of its own."""
    w = world(save_sent=False)
    try:
        gw, box, mirror = w.gw, w.box, w.mirror
        real = gw.read_mail

        def read_mail(raw, date=None, *args, **kwargs):
            if b"p@provider.example" in raw:
                raise gw.UnreadableMail("too much work", "work")
            return real(raw, date, *args, **kwargs)
        gw.read_mail = read_mail
        try:
            M, P, _raw, uid = _rewritten_send(w, keep_own_header=True)
            for _ in range(2):
                mirror.poll()
        finally:
            gw.read_mail = real
        _the_one_send(w, M, P)
        assert _places_of(mirror, "sent", M) == {uid: None}
        assert not mirror._placeholders, mirror._placeholders
    finally:
        done(w)
    print("ok: an unreadable Sent copy of the gateway's own send is that send, not a "
          "placeholder of its own")


def _the_send_as_a_placeholder(w):
    """A rewritten send whose Sent copy went to the Trash (a tombstone) and
    came back while the reader could not read it: the send is a placeholder
    under its minted id, made from that copy's header block, with no
    recipients read. ``(minted id, provider id, the copy's bytes, its UID in
    Sent, the undo)``."""
    gw, box, mirror = w.gw, w.box, w.mirror
    M, P, raw, uid = _rewritten_send(w, keep_own_header=True)
    trashed = box.move_mail("Sent", uid, "Trash")
    mirror.poll()
    mirror.poll()
    assert t.by_id(gw)[M]["removed_at"], "the copy left: a tombstone"
    real = gw.read_mail

    def read_mail(raw, date=None, *args, **kwargs):
        if b"p@provider.example" in raw:
            raise gw.UnreadableMail("too much work", "work")
        return real(raw, date, *args, **kwargs)
    gw.read_mail = read_mail
    back = box.move_mail("Trash", trashed, "Sent")
    mirror.poll()
    mirror.poll()
    (record,) = t.records(gw)
    assert record["message_id"] == M and M in mirror._placeholders, record
    assert record["recipients"] == [] and record["cc"] == [], record
    return M, P, raw, back, lambda: setattr(gw, "read_mail", real)


def _read_again(mirror, rounds=3):
    for _ in range(rounds):
        mirror._retry_at = 0
        mirror._retried.clear()
        mirror.poll()


def test_a_placeholder_of_a_send_is_replaced_by_its_own_copy_outside_sent():
    """A placeholder of the user's send whose copy (the one it was made
    from, under the provider's id) a client moved out of Sent reads again
    from there: the copy, proven its own by the very header block the
    placeholder keeps, takes the placeholder's place, as what the record
    already is — the user's send, under the minted id. A recipient's
    forgery under that id beside it is still a mail of its own."""
    for where in ("Archive", "INBOX"):
        w = world(save_sent=False)
        try:
            gw, box, mirror = w.gw, w.box, w.mirror
            M, P, raw, back, undo = _the_send_as_a_placeholder(w)
            moved = box.move_mail("Sent", back, where)
            mirror.poll()
            mirror.poll()
            assert M in mirror._placeholders, "it still does not read: it stays as it is"
            undo()
            _read_again(mirror)
            assert not mirror._placeholders, mirror._placeholders
            _the_one_send(w, M, P)
            assert _places_of(mirror, where.lower(), M) == {moved: None}, \
                _places_of(mirror, where.lower(), M)
            # (Beside it, a forgery under the provider's id with other
            # people on its To: no place of the send's, the send unchanged.)
            box.put("INBOX", _forged(raw, to="mal@evil.org"), date=NOW + 600)
            mirror.poll()
            mirror.poll()
            records = t.by_id(gw)
            assert sorted(records) == sorted([M, P]), sorted(records)
            assert records[P]["type"] == gw._ibstore.T_INBOUND and not records[P].get("author")
            assert records[M]["text"] == "Hello." and records[M]["author"] == "user"
        finally:
            done(w)
    print("ok: a placeholder of the user's send is replaced by its own copy outside Sent, "
          "as what it already is")


def test_a_placeholder_of_a_send_is_replaced_by_its_own_copy_in_sent():
    """The same placeholder whose copy stays in Sent: a placeholder keeps no
    recipients, so its copy is told by the rest (as a tombstone's is) — the
    copy reads under the minted id, by the user, never as a second record
    under the provider's id by the device."""
    w = world(save_sent=False)
    try:
        gw, box, mirror = w.gw, w.box, w.mirror
        M, P, _raw, back, undo = _the_send_as_a_placeholder(w)
        told = len(w.events)
        undo()
        _read_again(mirror)
        assert not mirror._placeholders, mirror._placeholders
        _the_one_send(w, M, P)
        assert _places_of(mirror, "sent", M) == {back: None}
        assert not _places_of(mirror, "sent", P)
        assert all(p.get("message_id") == M and p.get("author") == "user"
                   for kind, p in w.events[told:] if kind == "inbound"), w.events[told:]
    finally:
        done(w)
    print("ok: a placeholder of the user's send is replaced by its own copy in Sent, "
          "under the minted id")


def test_a_reply_asks_the_reader_only_for_a_header_block_with_a_reply_to():
    """reply_drafts asks the reader whom a recorded mail named only where its
    stored header block has a Reply-To line: a reply to a mail that named
    none — the ordinary one — waits for nothing the reader is doing (the
    mirror's reading of a heavy mail), and one request serves both modes."""
    w = world()
    try:
        gw, box, mirror = w.gw, w.box, w.mirror

        class Reader:
            """Busy with the mirror's reading of a heavy mail: whoever asks
            would wait for it."""
            asked = []

            def send_facts(self, head, seconds):
                self.asked.append(head)
                return gw._send_facts(head)
        box.put("INBOX", t.msg("a@x.org", day=1))
        box.put("INBOX", t.msg("b@y.org", sender="Bob <bob@y.org>", subject="Other",
                               headers=[("Reply-To", "desk@y.org")], day=2))
        mirror.poll()
        # (Older records: the header block alone says whom to answer.)
        for mid in ("a@x.org", "b@y.org"):
            path = mirror._record_path(mirror.index.one(
                "SELECT record FROM mails WHERE message_id = ?", (mid,))["record"])
            text = path.read_text(encoding="utf-8")
            path.write_text("".join(line for line in text.splitlines(keepends=True)
                                    if "replyTo" not in line), encoding="utf-8")
        gw.READER = reader = Reader()
        try:
            status, preview = gw._reply_preview({"chat": "a@x.org", "account": ACCOUNT})
            assert status == 200 and preview["all"]["to"][0]["address"] == "ann@x.org", preview
            assert reader.asked == [], "a reply to a mail with no Reply-To asks the reader"
            status, preview = gw._reply_preview({"chat": "b@y.org", "account": ACCOUNT})
            assert status == 200 and preview["sender"]["to"][0]["address"] == "desk@y.org"
            assert len(reader.asked) == 1, len(reader.asked)
        finally:
            gw.READER = None
    finally:
        done(w)
    print("ok: a reply asks the reader only about a header block with a Reply-To, once")


def test_a_delete_waits_for_a_mail_of_the_chat_whose_record_was_lost():
    """A mail of the chat whose record the ledger lost (`lost`) is looked for
    by a delete like a mail without a place: while its folder cannot say,
    nothing is erased; found, it goes to the Trash with the rest — and the
    deleted chat does not come back."""
    w = world()
    try:
        gw, box, mirror = w.gw, w.box, w.mirror
        box.put("INBOX", t.msg("a1@x.org", day=1))
        box.put("INBOX", t.msg("a2@x.org", irt="a1@x.org", refs=["a1@x.org"], day=2))
        mirror.poll()
        path = mirror._record_path(mirror.index.one(
            "SELECT record FROM mails WHERE message_id = 'a2@x.org'")["record"])
        path.unlink()
        mirror = w.restart()
        assert mirror.lost() == 1
        box.refused = {"INBOX"}
        result = mirror.delete_chat("a1@x.org")
        assert result["errors"] == 1 and result["trashed"] == 0, result
        assert [r["message_id"] for r in t.records(gw)] == ["a1@x.org"] and mirror.lost() == 1
        box.refused = set()
        result = mirror.delete_chat("a1@x.org")
        assert result["errors"] == 0 and result["trashed"] == 2, result
        assert box.uids_of("INBOX", "a1@x.org") == box.uids_of("INBOX", "a2@x.org") == ()
        assert sorted(held_in(box, "Trash")) == ["a1@x.org", "a2@x.org"]
        assert mirror.lost() == 0
        for _ in range(3):
            mirror.poll()
        assert t.records(gw) == [] and mirror.recent_chats() == []
    finally:
        done(w)
    print("ok: a delete looks for a mail of the chat whose record was lost, and waits for it")


def test_a_lost_mail_follows_its_chat_into_a_merge():
    """A mail whose record the ledger lost is still looked for as its chat's
    (`lost`, its hint naming the chat) — and when that chat is merged into
    another while the mail is looked for, its hint follows: the delete of
    the chat it is in now takes it to the Trash too, and the deleted chat
    does not come back. Whether the lost mail's chat had mails on record
    besides it or none."""
    for how in ("merged into an older chat", "all its chat had"):
        w = world()
        try:
            gw, box, mirror = w.gw, w.box, w.mirror
            if how == "merged into an older chat":
                box.put("INBOX", t.msg("c1@x.org", subject="Other thing", day=1))
                box.put("INBOX", t.msg("a1@x.org", day=2))
                box.put("INBOX", t.msg("a2@x.org", irt="a1@x.org", refs=["a1@x.org"], day=3))
                lost, cites = "a2@x.org", ["c1@x.org", "a1@x.org"]
            else:
                box.put("INBOX", t.msg("c1@x.org", day=1))
                box.put("INBOX", t.msg("a2@x.org", sender="Bob <bob@y.org>", subject="Other",
                                       day=2))
                lost, cites = "a2@x.org", ["c1@x.org", "a2@x.org"]
            mirror.poll()
            _record_of(mirror, lost).unlink()
            box.refused = {"Archive"}   # (the lost mail is looked for, and not found yet)
            mirror = w.restart()
            mirror.poll()
            assert mirror.lost() == 1
            box.put("INBOX", t.msg("d1@x.org", irt=cites[-1], refs=cites, subject="Re: Both",
                                   day=4))
            mirror.poll()
            mirror.poll()
            (chat,) = {r["chat"] for r in t.records(gw)}
            assert mirror.index.one("SELECT chat FROM lost")["chat"] == chat, how
            result = mirror.delete_chat(chat)
            assert result["errors"] == 0, result
            assert held_in(box, "INBOX") == [], (how, held_in(box, "INBOX"))
            assert lost in held_in(box, "Trash")
            box.refused = set()
            for _ in range(3):
                mirror.poll()
            assert t.records(gw) == [] and mirror.recent_chats() == [], how
        finally:
            done(w)
    print("ok: a lost mail's chat merged away takes it along, and a delete finds it there")


def test_a_lost_mails_kept_original_after_a_delete_is_no_new_mail():
    """On a server without MOVE or UIDPLUS a delete leaves each original
    flagged \\Deleted; a lost mail's, which had no place, is marked "moved"
    like the rest — kept by the erase, carried across a renumber — and is
    never read back as a new mail that starts the deleted chat again."""
    w = world()
    try:
        gw, box, mirror = w.gw, w.box, w.mirror
        box.can_expunge = False
        box.put("INBOX", t.msg("a1@x.org", day=1))
        uid = box.put("INBOX", t.msg("a2@x.org", irt="a1@x.org", refs=["a1@x.org"], day=2))
        mirror.poll()
        _record_of(mirror, "a2@x.org").unlink()
        box.refused = {"Archive"}
        mirror = w.restart()
        mirror.poll()
        assert mirror.lost() == 1
        result = mirror.delete_chat("a1@x.org")
        assert result["errors"] == 0 and result["trashed"] == 2, result
        assert _places_of(mirror, "inbox", "a2@x.org") == {uid: "moved"}
        box.refused = set()
        for again in range(2):
            for _ in range(3):
                mirror.poll()
            assert t.records(gw) == [] and mirror.recent_chats() == [], again
            mirror = w.restart() if not again else mirror
        box.renumber("INBOX")
        for _ in range(3):
            mirror.poll()
        assert t.records(gw) == [] and mirror.recent_chats() == []
    finally:
        done(w)
    print("ok: a lost mail's \\Deleted original left by a delete is kept, never read anew")


def _record_of(mirror, message_id):
    return mirror._record_path(mirror.index.one(
        "SELECT record FROM mails WHERE message_id = ?", (message_id,))["record"])


def test_a_move_whose_kept_copy_is_all_that_is_known_is_looked_for_again():
    """An archive on a server without MOVE, UIDPLUS or COPYUID, whose look
    in the archive for the new copy cannot tell: the INBOX copy is marked
    "moved", and the mail settles — the next passes find its archive copy,
    which is its place from then on (mark_read reaches it)."""
    w = world()
    try:
        gw, box, mirror = w.gw, w.box, w.mirror
        box.put("INBOX", t.msg("a@x.org", day=1))
        mirror.poll()
        box.can_expunge = box.names_uids = False
        real_adopt = mirror._adopt
        mirror._adopt = lambda *args, **kwargs: gw.UNSURE
        try:
            assert mirror.archive("a@x.org") == {"moved": 1, "flagged": 1}
        finally:
            mirror._adopt = real_adopt
        (kept,) = box.uids_of("INBOX", "a@x.org")
        assert _places_of(mirror, "inbox", "a@x.org") == {kept: "moved"}
        assert settling(mirror, "a@x.org") is not None
        mirror.poll()
        (copy,) = box.uids_of("Archive", "a@x.org")
        assert _places_of(mirror, "archive", "a@x.org") == {copy: None}
        assert settling(mirror, "a@x.org") is None
        assert mirror.mark_read("a@x.org") == 1
        assert "\\Seen" in box.boxes["Archive"]["mails"][copy]["flags"]
    finally:
        done(w)
    print("ok: a moved mail whose new place was not told is looked for again, and found")


def test_a_move_stopped_before_the_index_followed_is_put_right():
    """A stop between the server's move and the index following it (archive
    on a server without MOVE: the INBOX copy is flagged \\Deleted, the
    archive holds the mail): the next start puts it right — the leftover is
    "moved", the archive copy is placed — and a delete then takes that copy
    to the Trash, not the leftover. At each stop from the server's move through the index's
    following of it."""
    for after in range(3):
        w = world()
        try:
            gw, box, mirror = w.gw, w.box, w.mirror
            box.put("INBOX", t.msg("a@x.org", day=1))
            mirror.poll()
            box.can_expunge = box.names_uids = False
            index, real_move = mirror.index, box.move
            real = {name: getattr(index, name) for name in ("run", "atomic")}
            writes = []

            def stops(name):
                def write(*args, **kwargs):
                    if len(writes) >= after:
                        raise t._Kill()
                    writes.append(name)
                    return real[name](*args, **kwargs)
                return write

            def move(*args, **kwargs):
                moved = real_move(*args, **kwargs)
                for name in real:
                    setattr(index, name, stops(name))
                if after == 0:
                    raise t._Kill()
                return moved
            box.move = move
            try:
                mirror.archive("a@x.org")
                raise AssertionError("not stopped")
            except t._Kill:
                pass
            finally:
                box.move = real_move
                for name, write in real.items():
                    setattr(index, name, write)
            mirror = w.restart()
            mirror.poll()
            (kept,) = box.uids_of("INBOX", "a@x.org")
            (copy,) = box.uids_of("Archive", "a@x.org")
            assert _places_of(mirror, "inbox", "a@x.org") == {kept: "moved"}, after
            assert _places_of(mirror, "archive", "a@x.org") == {copy: None}, after
            result = mirror.delete_chat("a@x.org")
            assert result["errors"] == 0 and result["trashed"] == 1, (after, result)
            # (Moved on and kept, flagged \\Deleted: the server has no MOVE.)
            assert all("\\Deleted" in box.boxes["Archive"]["mails"][uid]["flags"]
                       for uid in box.uids_of("Archive", "a@x.org")), after
            (trashed,) = box.boxes["Trash"]["mails"].values()
            assert "\\Deleted" not in trashed["flags"], after
            assert t.records(gw) == []
        finally:
            done(w)
    print("ok: a move stopped before the index followed it is put right at the next start")


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
        print(f"\n{failures} mail gateway send check(s) failed.")
        return 1
    print("\nAll mail gateway send checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
