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
  * a provider that files the Sent copy under a Message-ID of its own.

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


def _stopped_after(n: int, *, direct: bool, save_sent: bool):
    """One send with the process killed right after its n-th write — to the
    send's own entry, the ledger, either index, or the mailbox — then a
    restart, and the mirror left to settle. None when the send has fewer
    writes; else what was found, checked against what must hold wherever the
    stop came."""
    w = world(policy="allow" if direct else "verify", save_sent=save_sent)
    try:
        gw, box, mirror, smtp = w.gw, w.box, w.mirror, w.smtp
        box.put("INBOX", t.msg("a@x.org", day=1))
        mirror.poll()
        payload = dict(recipient="a@x.org", images=[
            {"filename": "a.txt", "data": base64.b64encode(b"file").decode()}])
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
        if direct and taken:
            assert gw.SENDS.entries() == [], (n, "no entry outlives a direct send that went out")
        elif direct:
            # Stopped before anything was handed over: dropped, its caller
            # had no answer. Stopped after the mark "handed over": nobody
            # can know — kept, saying so, where the chat lists it.
            left = gw.SENDS.entries()
            assert len(left) <= 1 and all(
                e["status"] == "error" and e["outcome_unknown"] is True
                and e["error"] == gw.UNKNOWN_OUTCOME for e in left), (n, left)
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
        return {"taken": taken, "status": (entry or {}).get("status")}
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
    print(f"ok: stopped after any of {points} writes, a send is made at most once, and "
          f"what the server took is on record")


def test_the_stop_between_the_servers_answer_and_its_mark():
    """The one window no ordering closes: the server took the mail, and the
    process dies before it can write that down."""
    for save_sent in (True, False):
        w = world(policy="verify", save_sent=save_sent)
        try:
            gw, box, mirror, smtp = w.gw, w.box, w.mirror, w.smtp
            box.put("INBOX", t.msg("a@x.org", day=1))
            mirror.poll()
            _s, queued = w.send(recipient="a@x.org")
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
            if save_sent:
                # Nothing can say: reported as what it is, text and all.
                assert entry["status"] == "error" and entry["error"] == gw.UNKNOWN_OUTCOME
                assert entry["message"] == "Thanks." and mid not in t.by_id(gw)
            else:
                # The provider's Sent copy says it went out: finished.
                assert entry["status"] == "approved" and entry["done"]
                assert t.by_id(gw)[mid]["author"] == "agent" and len(w.told()) == 1
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
    # not there to be trashed, and not known to be gone. The chat stays; the
    # call made again looks for it, finds it, and finishes.
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
        result = mirror.delete_chat("a@x.org")
        assert result["errors"] == 1 and result["trashed"] == 1 and result["messages"] == 0
        assert "not deleted" in result["error"]
        assert held_in(box, "Archive") == ["b@x.org"] and held_in(box, "Trash") == ["a@x.org"]
        assert len(t.records(gw)) == 3 and mirror.thread("a@x.org")["live"] == 2
        assert len(w.events) == events, "nothing is told: the chat is as it was"
        result = mirror.delete_chat("a@x.org")
        assert result["errors"] == 0 and result["trashed"] == 1 and result["messages"] == 2
        assert held_in(box, "Trash") == ["a@x.org", "b@x.org"] and held_in(box, "Archive") == []
        assert [r["message_id"] for r in t.records(gw)] == ["other@x.org"]
    finally:
        done(w)

    # One a client deleted outright: the first call cannot know it is gone;
    # the second has every folder's answer that it is, and erases.
    w = world()
    try:
        gw, box, mirror = w.gw, w.box, w.mirror
        box.put("INBOX", t.msg("a@x.org", day=1))
        box.put("INBOX", t.msg("b@x.org", subject="Re: Quarterly numbers", refs=["a@x.org"],
                               day=2))
        mirror.poll()
        box.remove("INBOX", box.uid_of("INBOX", "b@x.org"))
        result = mirror.delete_chat("a@x.org")
        assert result["errors"] == 1 and result["trashed"] == 1 and len(t.records(gw)) == 2
        # While one folder will not say, "gone" is not said either.
        box.refused.add("Archive")
        result = mirror.delete_chat("a@x.org")
        assert result["errors"] >= 1 and result["messages"] == 0 and len(t.records(gw)) == 2
        box.refused.clear()
        result = mirror.delete_chat("a@x.org")
        assert result["errors"] == 0 and result["trashed"] == 0 and result["messages"] == 2
        assert t.records(gw) == [] and held_in(box, "Trash") == ["a@x.org"]
    finally:
        done(w)

    # A mirrored folder that is gone altogether: the same rule.
    w = world()
    try:
        gw, box, mirror = w.gw, w.box, w.mirror
        thread_of_three(w)
        del box.boxes["Archive"]
        result = mirror.delete_chat("a@x.org")
        assert result["errors"] == 1 and result["trashed"] == 2 and result["messages"] == 0
        assert len(t.records(gw)) == 4
        result = mirror.delete_chat("a@x.org")
        assert result["errors"] == 0 and result["messages"] == 3
        assert [r["message_id"] for r in t.records(gw)] == ["other@x.org"]
    finally:
        done(w)
    print("ok: delete erases only once every mail is in the Trash or known to be in no folder")


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
        assert smtp.sessions == 0 and gw.SENDS.entries() == []
        # The mailbox still follows the chat: that is IMAP.
        assert mirror.mark_read("a@x.org") == 1
        assert mirror.archive("a@x.org") == {"moved": 1, "flagged": 0}
    finally:
        done(w)
    print("ok: with no SMTP server the mailbox is mirrored and followed, and a send says why not")


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
    of the first — the same Message-ID — and sends nothing."""
    w = world()
    try:
        gw, box, mirror, smtp = w.gw, w.box, w.mirror, w.smtp
        box.put("INBOX", t.msg("a@x.org", day=1))
        mirror.poll()
        press = dict(recipient="a@x.org", message="Once.", request_key="press-1")
        status, first = w.send(**press)
        assert status == 200 and first["status"] == "sent"
        for _ in range(3):
            assert w.send(**press) == (200, first)
        assert len(smtp.mails) == 1 and smtp.sessions == 1
        (told,) = w.told()
        assert told["request_key"] == "press-1" and told["message_id"] == first["message_id"]
        assert w.send(**press) == (200, first) and len(w.sent_copies()) == 1
        # The entry is what remembers the key: kept, done, and not listed.
        (entry,) = gw.SENDS.entries()
        assert entry["done"] is True and entry["request_key"] == "press-1"
        assert gw.SENDS.listed() == [] and "press" not in gw._public(entry)
        # Also after a restart.
        w.restart()
        assert w.send(**press) == (200, first) and len(smtp.mails) == 1
        # A key names one press: used for another mail it is a mistake, said
        # as one — never answered with the first mail's "sent".
        status, said = w.send(recipient="a@x.org", message="Something else.",
                              request_key="press-1")
        assert status == 409 and "request_key" in said["error"] and len(smtp.mails) == 1
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
        assert answers[0][0] == 200 and later == answers, (answers, later)
        assert len(smtp.mails) == 1 and answers[0][1]["message_id"] == said["message_id"]
    finally:
        done(w)

    # Queued: the same pending send, then what it became.
    w = world(policy="verify")
    try:
        gw, smtp = w.gw, w.smtp
        press = dict(to=["ann@x.org"], subject="New", message="Hello.", request_key="q-1")
        status, queued = w.send(**press)
        assert status == 202 and w.send(**press) == (202, queued)
        assert len(gw.SENDS.entries()) == 1
        gw._decide_send(queued["request_id"], "approve")
        status, sent = w.send(**press)
        assert status == 200 and sent["message_id"] == queued["message_id"]
        assert len(smtp.mails) == 1
        status, again = w.send(to=["ann@x.org"], message="No.", request_key="q-2")
        gw._decide_send(again["request_id"], "reject")
        status, said = w.send(to=["ann@x.org"], message="No.", request_key="q-2")
        assert status == 409 and said["status"] == "rejected" and len(smtp.mails) == 1
    finally:
        done(w)

    # A send that failed for certain is answered as failed, again — and is
    # not carried out by the repeat, whatever the server would say now.
    w = world()
    try:
        gw, smtp = w.gw, w.smtp
        smtp.reject_data = "554 5.7.1 content rejected"
        press = dict(to=["ann@x.org"], message="Hm.", request_key="f-1")
        status, said = w.send(**press)
        assert status == 502 and said["error"].startswith("send failed: ")
        assert "554" in said["error"] and "unconfirmed" not in said
        smtp.reject_data = None
        assert w.send(**press) == (status, said) and smtp.mails == [] and smtp.sessions == 1
        # Its caller had the answer: nothing of it shows in the chat.
        assert gw.SENDS.listed() == [] and gw.SENDS.entries()[0]["dismissed"] is True
        # A direct send stopped before anything left is dropped at the next
        # start, and its key with it: the caller had no answer, and asks again.
        real_json = gw._atomic_json
        writes = []

        def stop_after_the_entry(path, data):
            real_json(path, data)
            writes.append(path)
            raise _Kill()
        gw._atomic_json = stop_after_the_entry
        try:
            w.send(to=["ann@x.org"], message="Stopped.", request_key="s-1")
            raise AssertionError("not stopped")
        except _Kill:
            pass
        finally:
            gw._atomic_json = real_json
        assert gw.SENDS.by_key("s-1")["status"] == "sending"
        w.restart()
        assert gw.SENDS.by_key("s-1") is None
        status, sent = w.send(to=["ann@x.org"], message="Stopped.", request_key="s-1")
        assert status == 200 and len(smtp.mails) == 1
    finally:
        done(w)
    print("ok: a request_key makes a repeat of /send answer the first one's outcome, unsent")


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
        status, said = gw._accept_send({"to": ["ann@x.org"], "message": "Big."}, HOST,
                                       caller_waits=lambda: False)
        assert status == 502 and said["error"].startswith("send failed: ")
        (kept,) = gw.SENDS.listed()
        assert kept["status"] == "error" and "552" in kept["error"] and kept["direct"] is True
        assert kept["message"] == "Big." and kept["outcome_unknown"] is False
    finally:
        done(w)
    print("ok: a send that failed is listed with its reason until dismissed, and never resent")


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
            assert pending() == []
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
            # The repeat of that press is told it failed; nothing is sent.
            smtp.reject_data = None
            status, said = post("/send", {"recipient": "a@x.org", "message": "Gone.",
                                          "user_approved": True, "request_key": "h-2"})
            assert status == 502 and said["error"].startswith("send failed: ")
            assert smtp.mails == []
            status, entry = post(f"/pending-sends/{kept['id']}/dismiss", b"")
            assert status == 200 and pending() == []
            # (Kept, marked, while its key is remembered: still "failed".)
            assert post("/send", {"recipient": "a@x.org", "message": "Gone.",
                                  "user_approved": True, "request_key": "h-2"})[0] == 502
            assert post(f"/pending-sends/{kept['id']}/dismiss", b"")[0] == 200
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
