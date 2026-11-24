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
    fails halfway; the erase capability;
  * input that tries to add a header or a recipient.

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
        self.mirror, self.box = t.started(
            gw, t.FakeMailbox(gw, folders=folders) if folders else None)
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
            assert w.send(to=["new.person@gmail.com"])[0] == 200 and len(smtp.mails) == 1
        finally:
            gw._triage._mutate_email = real
        assert calls == [] and not gw.POLICY_PATH.exists(), \
            "a mail the gateway sends writes no policy"
    finally:
        done(w)
    print("ok: a send through the gateway writes no policy")


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
            # The Sent copy needed the mailbox, not the mirror's lock.
            assert len(w.sent_copies(mid)) == (1 if save_sent else 0)
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
        box.append = real_append
        second = answer["message_id"]
        assert status == 200 and len(w.sent_copies(second)) == 1 and gw.SENDS.due()
        tried = appends()
        mirror.poll(sync=False)
        assert appends() == tried and len(w.sent_copies(second)) == 1
        assert gw.SENDS.due() == [] and settling(mirror, second) is None

        # A server that does not name the new UID: found by Message-ID.
        box.names_uids = False
        status, answer = w.send(recipient="a@x.org", message="Third.")
        third = answer["message_id"]
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
        assert sorted(t.by_id(gw)[answer["message_id"]]["cc"]) == ["bob@y.org", "cat@z.org"]
        # The session fails only after the server took the mail: it is sent.
        smtp.refuse_rcpt, smtp.fail_quit = set(), True
        status, answer = w.send(recipient="a@x.org", message="Again.")
        assert status == 200 and len(smtp.mails) == 2 and "refused" not in answer
        assert answer["message_id"] in t.by_id(gw)
    finally:
        done(w)
    print("ok: a partial refusal and a failed goodbye do not unsend a mail")


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
        assert w.smtp.sessions == 1 and w.gw.SENDS.entries() == [] and t.records(w.gw) == []
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
        if direct:
            assert gw.SENDS.entries() == [], (n, "no entry outlives a direct send")
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
        # The mirror had never seen that folder: the mail has no place for a
        # moment — and is not called gone meanwhile.
        assert settling(mirror, "a@x.org") is not None
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
        result = mirror.delete_chat("a@x.org")
        assert result["errors"] == 0 and result["trashed"] == 3 and result["messages"] == 3
        assert sorted(result["message_ids"]) == ["a@x.org", "b@x.org", "s@example.org"]
        assert len(result["subjects"]) == 3 and result["pending_sends"] == 1
        assert result["recent"] == 0 and result["media"] >= 7 and result["parts"] == 6
        # Every mail of the thread, from every mirrored folder, is in the
        # Trash (made, since the mailbox had none) — and nothing is expunged.
        assert ("create", "Trash") in box.writes
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
                "date", "content-type", "content-transfer-encoding", "mime-version"}


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
            "a" * 250 + "@x.org", 7, None, ["ann@x.org"], {"address": "ann@x.org"}]
        for bad in hostile:
            for field in ("to", "cc"):
                payload = {"to": ["good@x.org"], "message": "x", field: ["good@x.org", bad]}
                status, said = gw._accept_send(payload, HOST)
                assert status == 400 and "address" in said["error"], (bad, said)
        assert smtp.sessions == 0 and gw.SENDS.entries() == []
        # A display name is read past, whatever it claims: the mail goes to
        # the address, and only the address stands in the header.
        status, _answer = w.send(to=['"victim@x.org" <ann@x.org>', "victim@x.org <bob@y.org>",
                                     "  Cat <CAT@Z.org>  "], cc=["<dora@w.org>"])
        assert status == 200 and smtp.mails[-1]["rcpt"] == [
            "ann@x.org", "bob@y.org", "cat@z.org", "dora@w.org"]
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
    finally:
        done(w)
    print("ok: nothing a mail says becomes a header line or a recipient of its reply")


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
