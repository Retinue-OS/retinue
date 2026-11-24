#!/usr/bin/env python3
"""The mail gateway's mailbox adapter (scripts/mail-gateway.py), over IMAP.

The adapter is the only code that speaks IMAP, and the first long-running
IMAP client in the framework. It is exercised here through Python's real
``imaplib`` against a small scripted IMAP server on a socket pair — so what is
pinned is the adapter's reading of real protocol answers: folder roles from
SPECIAL-USE and from configured names, UIDVALIDITY and UIDNEXT, UID searches,
whole and partial fetches, finding a mail by Message-ID, reconnecting after a
dropped connection, a timeout that does not hang the poll, a failed login in
words an operator can act on — and that nothing it sends can change the
mailbox.

    python3 tests/test_mail_gateway_imap.py
"""
import calendar
import email
import imaplib
import importlib.util
import re
import socket
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("mail_gateway_helpers",
                                               HERE / "test_mail_gateway.py")
t = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(t)

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_TOKEN_RE = re.compile(r'"((?:[^"\\]|\\.)*)"|(\S+)')
WRITES = ("STORE", "COPY", "MOVE", "APPEND", "EXPUNGE", "SELECT", "DELETE", "CREATE",
          "RENAME", "CLOSE", "SUBSCRIBE")


def _internal(epoch: float) -> str:
    when = time.gmtime(epoch)
    return (f"{when.tm_mday:02d}-{_MONTHS[when.tm_mon - 1]}-{when.tm_year} "
            f"{when.tm_hour:02d}:{when.tm_min:02d}:{when.tm_sec:02d} +0000")


class MiniImap:
    """Just enough of an IMAP4rev1 server for a read-only client.

    Folders are keyed by their name on the wire (modified UTF-7). Every command
    received is kept in ``commands``; anything that would change a mailbox is
    answered BAD, and the tests assert none was ever sent."""

    def __init__(self):
        self.folders: dict = {}
        self.commands: list = []
        self.connections = 0
        self.password = "app-password"
        self.drop_next = 0            # close the connection instead of answering
        self.stall = False            # stop answering, keep the connection open
        self.flags_after_body = False
        self.unsolicited = False      # volunteer another mail's flags around a FETCH
        self.literal_names = False    # LIST the folder names as literals
        self.refuse_examine = {}      # folder → what follows "NO" when it is examined
        self.refuse_header_fetch = False   # answer NO to a fetch of a header field
        self._held = []

    def folder(self, name, attrs="\\HasNoChildren", validity=7):
        self.folders[name] = {"attrs": attrs, "validity": validity, "next": 1, "mails": {}}
        return self.folders[name]

    def put(self, name, raw, *, flags=(), date=None) -> int:
        box = self.folders[name]
        uid, box["next"] = box["next"], box["next"] + 1
        box["mails"][uid] = {"raw": raw, "flags": tuple(flags),
                             "date": t.T0 + uid * t.DAY if date is None else date}
        return uid

    # -- the wire --------------------------------------------------------------

    def accept(self, sock) -> None:
        self.connections += 1
        threading.Thread(target=self._serve, args=(sock,), daemon=True).start()

    def _serve(self, sock) -> None:
        reader = sock.makefile("rb")
        selected = None

        def send(*lines):
            sock.sendall(b"".join(line if isinstance(line, bytes) else line.encode() + b"\r\n"
                                  for line in lines))
        try:
            send("* OK IMAP4rev1 server ready")
            while True:
                line = reader.readline()
                if not line:
                    return
                tag, _space, command = line.rstrip(b"\r\n").decode().partition(" ")
                self.commands.append(command)
                verb, _space, args = command.partition(" ")
                verb = verb.upper()
                # (Both only once a session stands, so the tests choose
                # whether a reconnect gets through.)
                if self.stall and verb not in ("CAPABILITY", "LOGIN"):
                    self._held.append(sock)   # a dead peer: no answer, no close
                    return
                if self.drop_next and verb not in ("CAPABILITY", "LOGIN"):
                    self.drop_next -= 1
                    sock.close()
                    return
                if verb == "CAPABILITY":
                    send("* CAPABILITY IMAP4rev1 SPECIAL-USE", f"{tag} OK done")
                elif verb == "LOGIN":
                    ok = args.endswith(f'"{self.password}"')
                    send(f"{tag} OK logged in" if ok
                         else f"{tag} NO [AUTHENTICATIONFAILED] Invalid credentials")
                elif verb == "NOOP":
                    send(f"{tag} OK done")
                elif verb == "LOGOUT":
                    send("* BYE bye", f"{tag} OK done")
                    return
                elif verb == "LIST":
                    for name, box in self.folders.items():
                        if self.literal_names:
                            send(f"* LIST ({box['attrs']}) \"/\" {{{len(name)}}}",
                                 name.encode(), b"\r\n")
                        else:
                            send(f"* LIST ({box['attrs']}) \"/\" \"{name}\"")
                    send(f"{tag} OK done")
                elif verb == "EXAMINE":
                    name = _TOKEN_RE.match(args).group(1)
                    box = self.folders.get(name)
                    if name in self.refuse_examine:
                        # The folder is there; the server just will not open it.
                        selected = None
                        send(f"{tag} NO {self.refuse_examine[name]}".rstrip())
                        continue
                    if box is None or "\\Noselect" in box["attrs"]:
                        selected = None
                        send(f"{tag} NO [NONEXISTENT] no such mailbox")
                        continue
                    selected = box
                    send(f"* {len(box['mails'])} EXISTS",
                         f"* OK [UIDVALIDITY {box['validity']}] UIDs valid",
                         f"* OK [UIDNEXT {box['next']}] predicted next UID",
                         f"{tag} OK [READ-ONLY] EXAMINE completed")
                elif verb == "UID" and selected is not None:
                    sub, _space, rest = args.partition(" ")
                    if sub.upper() == "SEARCH":
                        found = " ".join(str(u) for u in self._search(selected, rest))
                        send(f"* SEARCH {found}".rstrip(), f"{tag} OK done")
                    elif sub.upper() == "FETCH":
                        if self.refuse_header_fetch and "HEADER.FIELDS" in rest:
                            send(f"{tag} NO [SERVERBUG] internal error")
                            continue
                        send(*self._fetch(selected, rest), f"{tag} OK done")
                    else:
                        send(f"{tag} BAD read-only server")
                else:
                    send(f"{tag} BAD not here")
        except OSError:
            return

    def _search(self, box, criteria: str) -> list:
        tokens = [a if b == "" else b for a, b in _TOKEN_RE.findall(criteria)]
        uids = set(box["mails"])
        i = 0
        while i < len(tokens):
            word = tokens[i].upper()
            if word == "ALL":
                i += 1
            elif word == "SINCE":
                day, month, year = tokens[i + 1].split("-")
                since = calendar.timegm((int(year), _MONTHS.index(month) + 1, int(day), 0, 0, 0))
                uids = {u for u in uids if box["mails"][u]["date"] >= since}
                i += 2
            elif word == "UID":
                low, _colon, high = tokens[i + 1].partition(":")
                top = max(box["mails"], default=0)
                low, high = int(low), (top if high == "*" else int(high or low))
                if low > high:           # "n:*" includes the newest mail even below n
                    low, high = high, low
                uids = {u for u in uids if low <= u <= high}
                i += 2
            elif word == "HEADER":
                name, value = tokens[i + 1], tokens[i + 2].lower()
                uids = {u for u in uids if value in
                        (email.message_from_bytes(box["mails"][u]["raw"]).get(name) or "").lower()}
                i += 3
            else:
                raise AssertionError(f"unexpected search key {word}")
        return sorted(uids)

    def _fetch(self, box, args: str) -> list:
        uid_set, _space, items = args.partition(" ")
        wanted = {int(u) for u in uid_set.split(",")}
        order = sorted(box["mails"])
        out = []
        partial = re.search(r"BODY\.PEEK\[\]<0\.(\d+)>", items)
        for uid in sorted(wanted & set(order)):
            mail, seq = box["mails"][uid], order.index(uid) + 1
            flags = "FLAGS (" + " ".join(mail["flags"]) + ")"
            date = f'INTERNALDATE "{_internal(mail["date"])}"'
            if self.unsolicited:
                out.append("* 99 FETCH (UID 4242 FLAGS (\\Deleted \\Answered))")
            if "HEADER.FIELDS (MESSAGE-ID)" in items:
                value = email.message_from_bytes(mail["raw"]).get("Message-ID")
                header = (f"Message-ID: {value}\r\n" if value else "").encode() + b"\r\n"
                out += [f"* {seq} FETCH (UID {uid} BODY[HEADER.FIELDS (MESSAGE-ID)] "
                        f"{{{len(header)}}}", header, b")\r\n"]
            elif "BODY.PEEK[]" in items:
                body = mail["raw"][:int(partial.group(1))] if partial else mail["raw"]
                origin = "<0>" if partial else ""
                size = f"RFC822.SIZE {len(mail['raw'])}"
                if self.flags_after_body:
                    out += [f"* {seq} FETCH (UID {uid} {date} {size} BODY[]{origin} "
                            f"{{{len(body)}}}", body, f" {flags})".encode() + b"\r\n"]
                else:
                    out += [f"* {seq} FETCH (UID {uid} {flags} {date} {size} BODY[]{origin} "
                            f"{{{len(body)}}}", body, b")\r\n"]
            elif "INTERNALDATE" in items:
                out.append(f"* {seq} FETCH (UID {uid} {date})")
            else:
                raise AssertionError(f"unexpected fetch items {items}")
            if self.unsolicited:
                out.append("* 98 FETCH (FLAGS (\\Flagged))")
        return out


class Wired:
    """Point the gateway's ``imaplib.IMAP4_SSL`` at a :class:`MiniImap` for the
    length of a ``with`` block. The client is imaplib's own, unchanged but for
    the socket it opens."""

    def __init__(self, gw, server: MiniImap):
        self.gw, self.server = gw, server
        self.opened = []     # (host, port, timeout, has ssl context) of every connection
        self.refuse = None   # raised instead of connecting

    def __enter__(self):
        wired = self

        class Loopback(imaplib.IMAP4):
            def __init__(self, host, port, *, ssl_context=None, timeout=None):
                wired.opened.append((host, port, timeout, ssl_context is not None))
                if wired.refuse is not None:
                    raise wired.refuse
                super().__init__(host, port, timeout)

            def _create_socket(self, timeout):
                ours, theirs = socket.socketpair()
                ours.settimeout(timeout)
                wired.server.accept(theirs)
                return ours

        self._real = self.gw.imaplib.IMAP4_SSL
        self.gw.imaplib.IMAP4_SSL = Loopback
        return self

    def __exit__(self, *exc):
        self.gw.imaplib.IMAP4_SSL = self._real


def _server() -> MiniImap:
    server = MiniImap()
    server.folder("INBOX")
    server.folder("Sent", "\\HasNoChildren \\Sent")
    server.folder("Archive", "\\HasNoChildren \\Archive")
    return server


# --------------------------------------------------------------------------
# Folder roles
# --------------------------------------------------------------------------

def test_folder_roles():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp, SENT_FOLDER="Sent Items", MAIL_ARCHIVE_FOLDER="Ablage",
                    MAIL_TRASH_FOLDER="Bin")
        # The server says which folder is which: its word beats the configured names.
        server = MiniImap()
        server.folder("INBOX")
        server.folder("Sent Items")                              # a client's own, unmarked
        server.folder("Gesendete Objekte", "\\HasNoChildren \\Sent")
        server.folder("Ablage")
        server.folder("Archiv &AOQ-lter", "\\Archive")           # "Archiv älter"
        server.folder("Gel&APY-scht", "\\Trash \\HasNoChildren")  # "Gelöscht"
        server.folder("Alle", "\\All")
        server.folder("Ordner", "\\Noselect \\HasChildren")
        with Wired(gw, server):
            box = gw.Mailbox(gw.MAIL_CONFIG)
            assert box.folders() == {"inbox": "INBOX", "sent": "Gesendete Objekte",
                                     "archive": "Archiv älter", "trash": "Gelöscht"}
            # The decoded name goes back on the wire in modified UTF-7.
            listing = box.list_uids("Archiv älter")
            assert listing is not None and listing.uidvalidity == 7
            assert 'EXAMINE "Archiv &AOQ-lter"' in server.commands
            # Names sent as literals are read the same.
            server.literal_names = True
            assert box.folders()["trash"] == "Gelöscht"

        # No SPECIAL-USE: the configured names, matched exactly, else without case.
        server = MiniImap()
        for name in ("Inbox", "Sent Items", "ablage", "Drafts"):
            server.folder(name)
        with Wired(gw, server):
            assert gw.Mailbox(gw.MAIL_CONFIG).folders() == {
                "inbox": "Inbox", "sent": "Sent Items", "archive": "ablage", "trash": None}

    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        resolve = gw.resolve_folders
        # Gmail: an archived mail lives in "All Mail"; \Archive, where given, comes first.
        gmail = [({"\\hasnochildren"}, "INBOX"), ({"\\sent"}, "[Gmail]/Sent Mail"),
                 ({"\\all"}, "[Gmail]/All Mail"), ({"\\trash"}, "[Gmail]/Trash"),
                 ({"\\noselect"}, "[Gmail]")]
        assert resolve(gmail, "Sent", "Archive", "Trash") == {
            "inbox": "INBOX", "sent": "[Gmail]/Sent Mail", "archive": "[Gmail]/All Mail",
            "trash": "[Gmail]/Trash"}
        both = gmail + [({"\\archive"}, "Archive")]
        assert resolve(both, "Sent", "Archive", "Trash")["archive"] == "Archive"
        # A folder that cannot be opened plays no role, whatever it is marked.
        assert resolve([({"\\sent", "\\noselect"}, "Sent")], "Sent", "A", "T")["sent"] is None
        assert resolve([], "Sent", "Archive", "Trash") == {
            "inbox": "INBOX", "sent": None, "archive": None, "trash": None}
        entry = gw._parse_list_entry
        assert entry(b'(\\HasNoChildren \\Sent) "/" "Sent \\"old\\""') == (
            {"\\hasnochildren", "\\sent"}, 'Sent "old"')
        assert entry(b"(\\Noselect) NIL Plain") == ({"\\noselect"}, "Plain")
        assert entry((b'(\\Trash) "." {5}', b"M&APw-ll")) == ({"\\trash"}, "Müll")
        assert entry(b"garbage") is None and entry(None) is None
    print("ok: folder roles come from SPECIAL-USE first, then from the configured names")


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

def test_listing_fetching_and_finding():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        server = _server()
        for n in range(1, 6):
            server.put("INBOX", t.msg(f"m{n}@x.org", day=n), flags=["\\Seen"] if n % 2 else [],
                       date=t.T0 + n * t.DAY)
        del server.folders["INBOX"]["mails"][2]       # expunged long ago: UIDs have gaps
        with Wired(gw, server) as wired:
            box = gw.Mailbox(gw.MAIL_CONFIG, timeout=5)
            listing = box.list_uids("INBOX")
            assert listing == gw.Listing(7, 6, {1: None, 3: None, 4: None, 5: None})
            # Implicit TLS, and a socket timeout: this client never exits.
            assert wired.opened == [("imap.example.org", 993, 5, True)]
            assert box.list_uids("INBOX", first_uid=4).uids == {4: None, 5: None}
            # "n:*" answers with the newest mail even below n; the adapter does not.
            assert box.list_uids("INBOX", first_uid=9).uids == {}
            assert "UID SEARCH UID 9:*" in server.commands
            # SINCE is asked a day early: the server compares dates in its own zone.
            since = box.list_uids("INBOX", since=t.T0 + 4 * t.DAY + 3600, dates=True)
            assert since.uids == {3: t.T0 + 3 * t.DAY, 4: t.T0 + 4 * t.DAY, 5: t.T0 + 5 * t.DAY}
            assert any(c.startswith("UID SEARCH SINCE 12-Oct-2025") for c in server.commands)
            assert box.list_uids("Sent").uids == {} and box.list_uids("Sent").uidnext == 1
            # No such folder is an answer, not an error.
            assert box.list_uids("Nope") is None and box.fetch("Nope", 1) is None
            assert box.find("Nope", "m1@x.org") is None

            raw = server.folders["INBOX"]["mails"][3]["raw"]
            got = box.fetch("INBOX", 3)
            assert got == gw.Fetched(raw, len(raw), frozenset({"\\Seen"}), t.T0 + 3 * t.DAY)
            assert box.fetch("INBOX", 4).flags == frozenset()
            assert box.fetch("INBOX", 2) is None and box.fetch("INBOX", 77) is None
            # A capped fetch: the head of the mail, and the whole mail's size.
            head = box.fetch("INBOX", 3, limit=100)
            assert head.raw == raw[:100] and head.size == len(raw)
            assert any("BODY.PEEK[]<0.100>" in c for c in server.commands)
            # Servers differ on where FLAGS stands, and volunteer other mails' flags.
            server.flags_after_body = True
            assert box.fetch("INBOX", 3) == got
            server.unsolicited = True
            assert box.fetch("INBOX", 3) == got
            server.flags_after_body = False
            assert box.fetch("INBOX", 3) == got
            assert box.list_uids("INBOX", dates=True).uids[5] == t.T0 + 5 * t.DAY
            server.unsolicited = False

            # By Message-ID: the header search matches substrings, the adapter does not.
            server.put("INBOX", t.msg("xm1@x.org", day=6))
            server.put("INBOX", t.msg("m1@x.org.example", day=6))
            assert box.find("INBOX", "m1@x.org") == 1
            assert box.find("INBOX", "xm1@x.org") == 6
            assert box.find("INBOX", "m9@x.org") is None and box.find("Sent", "m1@x.org") is None
            copy = server.put("INBOX", t.msg("m1@x.org", day=7))     # the same mail again
            assert box.find("INBOX", "m1@x.org") == copy
            server.put("INBOX", t.msg(None, subject="no id", day=8))
            assert box.find("INBOX", "") is None

            # One connection served all of it; folders are opened read-only and
            # bodies only ever peeked at.
            assert server.connections == 1
            assert not any(c.startswith("UID FETCH") and "BODY[" in c for c in server.commands)
            assert not any(c.split()[0] in WRITES or c.startswith("UID STORE")
                           for c in server.commands), server.commands
    print("ok: listings, whole and capped fetches, and finding a mail by its Message-ID")


def test_a_refusal_is_not_an_answer():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        server = _server()
        for n in range(1, 4):
            server.put("INBOX", t.msg(f"m{n}@x.org", day=n), date=t.T0 + n * t.DAY)
        server.put("Archive", t.msg("arch@x.org", day=1), date=t.T0 + t.DAY)
        with Wired(gw, server):
            box = gw.Mailbox(gw.MAIL_CONFIG, timeout=5)
            assert box.find("Archive", "arch@x.org") == 1
            # NO to EXAMINE says nothing about the folder unless the server
            # says NONEXISTENT: it is not "no such folder", "no such mail" or
            # "expunged" — each of which would have the mirror call mail gone.
            for said in ("[UNAVAILABLE] Temporary problem, try again later",
                         "[INUSE] Mailbox is locked", "[SERVERBUG] oops",
                         "Mailbox is busy"):       # no response code at all
                server.refuse_examine["Archive"] = said
                for ask in (lambda: box.list_uids("Archive"),
                            lambda: box.fetch("Archive", 1),
                            lambda: box.find("Archive", "arch@x.org")):
                    try:
                        answer = ask()
                        raise AssertionError(f"a refused EXAMINE was read as {answer!r}")
                    except gw.MailboxError as exc:
                        assert "would not open 'Archive'" in str(exc), exc
                        assert said.split("]")[0] in str(exc)
            # A folder that answered a moment ago is not trusted to be open either.
            assert box.list_uids("INBOX").uids == {1: None, 2: None, 3: None}
            del server.refuse_examine["Archive"]
            assert box.find("Archive", "arch@x.org") == 1
            # The server's own word for "no such folder" is still an answer.
            assert box.list_uids("Nope") is None and box.find("Nope", "m1@x.org") is None
            assert server.connections == 1, "a refusal is not a broken connection"

            # A header the server will not hand over is not "another mail".
            server.refuse_header_fetch = True
            try:
                box.find("INBOX", "m1@x.org")
                raise AssertionError("a refused FETCH must not read as 'not found'")
            except gw.MailboxError as exc:
                assert "refused to read a header" in str(exc)
            server.refuse_header_fetch = False
            # More mails whose ids CONTAIN the id than are compared (anyone can
            # send those): could not tell — never "not there".
            gw.FIND_CANDIDATES_MAX = 3
            for n in range(4):
                server.put("INBOX", t.msg(f"x{n}-m1@x.org", day=5), date=t.T0 + 5 * t.DAY)
            assert box.find("INBOX", "m1@x.org") is gw.UNSURE
            assert box.find("INBOX", "x3-m1@x.org") == 7       # among the newest: found
            assert box.find("INBOX", "m2@x.org") == 2
            assert box.find("INBOX", "m9@x.org") is None       # the server's answer: none
    print("ok: a folder, a search or a fetch the server refuses is never read as absence")


def test_a_refused_archive_costs_no_archived_mail():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        t._write_policy(gw)
        events = t.rail(gw)
        server = _server()
        with Wired(gw, server):
            mirror = gw.Mirror(gw.Mailbox(gw.MAIL_CONFIG), clock=lambda: t.NOW)
            loop: dict = {}
            gw._loop_step(mirror, loop)
            uid = server.put("INBOX", t.msg("a@x.org", day=39), date=t.NOW - 5)
            loop.clear()
            gw._loop_step(mirror, loop)
            assert "a@x.org" in t.by_id(gw)
            # The user archives it; at the next poll the server refuses the archive.
            mail = server.folders["INBOX"]["mails"].pop(uid)
            server.put("Archive", mail["raw"], date=mail["date"])
            server.refuse_examine["Archive"] = "[UNAVAILABLE] Temporary problem"
            for _ in range(2):
                loop.clear()
                gw._loop_step(mirror, loop)
                snap = gw._health_snapshot()
                assert snap["connected"] is False and "would not open 'Archive'" in snap["error"]
            record = t.by_id(gw)["a@x.org"]
            assert record["removed_at"] is None and record["body_part"]
            del server.refuse_examine["Archive"]
            loop.clear()
            gw._loop_step(mirror, loop)
            assert gw._health_snapshot()["connected"] is True
            assert t.by_id(gw)["a@x.org"]["removed_at"] is None
            assert not any(kind == "removed" for kind, _p in events)
            # Deleted for real, it is a tombstone as before.
            server.folders["Archive"]["mails"].clear()
            loop.clear()
            gw._loop_step(mirror, loop)
            assert t.by_id(gw)["a@x.org"]["removed_at"]
    print("ok: an archive the server refuses for a moment tombstones nothing")


def test_the_adapter_cannot_write():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        server = _server()
        uid = server.put("INBOX", t.msg("a@x.org"))
        with Wired(gw, server):
            box = gw.Mailbox(gw.MAIL_CONFIG)
            box.ensure()
            sent = len(server.commands)
            # The next phase's seams exist, and refuse without a word to the server.
            for attempt in (lambda: box.set_seen("INBOX", [uid]),
                            lambda: box.move("INBOX", uid, "Archive")):
                try:
                    attempt()
                    raise AssertionError("the read-only adapter must refuse to write")
                except gw.MailboxError as exc:
                    assert "read-only" in str(exc)
            assert len(server.commands) == sent
            assert server.folders["INBOX"]["mails"][uid]["flags"] == ()
    print("ok: the adapter's write seams refuse; nothing reaches the server")


# --------------------------------------------------------------------------
# The connection's life
# --------------------------------------------------------------------------

def test_a_dropped_connection_is_reopened_once():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        server = _server()
        server.put("INBOX", t.msg("a@x.org"))
        with Wired(gw, server):
            box = gw.Mailbox(gw.MAIL_CONFIG, timeout=5)
            box.ensure()
            assert server.connections == 1 and server.commands[-1] == "NOOP"
            # The server closed the idle session (they do, after half an hour):
            # the command is simply run again on a new connection.
            server.drop_next = 1
            assert box.list_uids("INBOX").uids == {1: None}
            assert server.connections == 2
            # The folder is opened again there before anything is read from it.
            server.drop_next = 1
            assert box.fetch("INBOX", 1).raw == server.folders["INBOX"]["mails"][1]["raw"]
            assert server.connections == 3
            assert [c for c in server.commands if c.startswith("EXAMINE")][-1] == 'EXAMINE "INBOX"'
            # Twice in a row is an outage, reported as one …
            server.drop_next = 2
            try:
                box.list_uids("INBOX")
                raise AssertionError("two dropped connections must fail the command")
            except gw.MailboxError as exc:
                assert "connection failed" in str(exc)
            # … and over when the server is back.
            assert box.list_uids("INBOX").uids == {1: None}
            box.close()
            box.close()
            assert box.find("INBOX", "a@x.org") == 1
    print("ok: a dropped connection is reopened and the command retried, once")


def test_a_dead_peer_times_out():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        server = _server()
        server.put("INBOX", t.msg("a@x.org"))
        with Wired(gw, server):
            box = gw.Mailbox(gw.MAIL_CONFIG, timeout=0.3)
            box.ensure()
            server.stall = True
            began = time.monotonic()
            try:
                box.list_uids("INBOX")
                raise AssertionError("a server that stops answering must fail the command")
            except gw.MailboxError as exc:
                assert "connection failed" in str(exc), exc
            assert time.monotonic() - began < 5, "the poll thread must not hang"
            server.stall = False
            assert box.list_uids("INBOX").uids == {1: None}
    print("ok: a server that stops answering costs a timeout, not the poll thread")


def test_login_and_reachability_are_reported_for_the_operator():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        server = _server()
        server.password = "another"
        with Wired(gw, server) as wired:
            box = gw.Mailbox(gw.MAIL_CONFIG)
            try:
                box.ensure()
                raise AssertionError("a refused login must fail")
            except gw.MailboxError as exc:
                text = str(exc)
                assert "login failed" in text and "Me@Example.org" in text
                assert "EMAIL_PASS" in text and "app-password" not in text
            # A refused login is not retried within the command (lock-outs).
            assert server.connections == 1
            wired.refuse = OSError(113, "No route to host")
            try:
                box.ensure()
                raise AssertionError("an unreachable server must fail")
            except gw.MailboxError as exc:
                assert "cannot reach" in str(exc) and "imap.example.org:993" in str(exc)
            wired.refuse = None
            server.password = "app-password"
            box.ensure()

            # Through the loop, both are health states.
            t._write_policy(gw)
            server.password = "another"
            box.close()
            mirror = gw.Mirror(box, clock=lambda: t.NOW)
            gw._loop_step(mirror, {"started": 0.0})
            snap = gw._health_snapshot()
            assert snap["connected"] is False and "login failed" in snap["error"]
            server.password = "app-password"
            gw._loop_step(mirror, {"started": 0.0})
            assert gw._health_snapshot()["connected"] is True
    print("ok: a refused login and an unreachable server are reported in an operator's words")


# --------------------------------------------------------------------------
# The mirror over real IMAP answers
# --------------------------------------------------------------------------

def test_the_mirror_over_the_adapter():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        events = t.rail(gw)
        server = _server()
        now = t.NOW
        server.put("INBOX", t.msg("a@x.org", day=1), flags=["\\Seen"], date=t.T0 + t.DAY)
        server.put("Sent", t.msg("b@example.org", sender=t.ME, to="ann@x.org",
                                 refs=["a@x.org"], day=2), flags=["\\Seen"],
                   date=t.T0 + 2 * t.DAY)
        server.put("INBOX", t.msg("old@x.org", day=-200), date=t.T0 - 200 * t.DAY)
        with Wired(gw, server):
            mirror = gw.Mirror(gw.Mailbox(gw.MAIL_CONFIG), clock=lambda: now)
            assert mirror.poll() is False
            recs = t.by_id(gw)
            assert set(recs) == {"a@x.org", "b@example.org"}
            assert {r["chat"] for r in recs.values()} == {"a@x.org"}
            assert [(e["message_id"], e["backfill"], e["seen"]) for e in t.inbound(events)] == [
                ("a@x.org", True, True), ("b@example.org", True, True)]
            # New mail, a move to the archive, a deletion.
            server.put("INBOX", t.msg("c@x.org", refs=["a@x.org", "b@example.org"], day=3),
                       date=t.T0 + 3 * t.DAY)
            mirror.poll()
            assert "c@x.org" in t.by_id(gw) and "backfill" not in events[-1][1]
            moved = server.folders["INBOX"]["mails"].pop(1)
            server.put("Archive", moved["raw"], date=moved["date"])
            mirror.poll()
            assert not t.by_id(gw)["a@x.org"]["removed_at"]
            del server.folders["Sent"]["mails"][1]
            mirror.poll()
            assert t.by_id(gw)["b@example.org"]["removed_at"]
            assert events[-1] == ("removed", {"channel": "email", "account": t.ACCOUNT,
                                              "chat": "a@x.org",
                                              "message_ids": ["b@example.org"], "live": 2})
            # The server renumbers INBOX: nothing is read or recorded twice.
            box = server.folders["INBOX"]
            box["validity"] += 1
            box["mails"] = {uid + 40: mail for uid, mail in box["mails"].items()}
            box["next"] += 40
            before = len(t.records(gw))
            fetches = sum(1 for c in server.commands if "BODY.PEEK[]" in c)
            mirror.poll()
            mirror.poll()
            assert len(t.records(gw)) == before
            assert sum(1 for c in server.commands if "BODY.PEEK[]" in c) == fetches
            assert not t.by_id(gw)["c@x.org"]["removed_at"]
            # A whole session, and not one command that could change the mailbox.
            assert not any(c.split()[0] in WRITES or c.startswith(("UID STORE", "UID COPY",
                                                                   "UID MOVE", "UID EXPUNGE"))
                           for c in server.commands), server.commands
            assert all(mail["flags"] in ((), ("\\Seen",))
                       for box in server.folders.values() for mail in box["mails"].values())
    print("ok: the mirror runs over the real adapter and never writes to the mailbox")


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
        print(f"\n{failures} mail gateway IMAP check(s) failed.")
        return 1
    print("\nAll mail gateway IMAP checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
