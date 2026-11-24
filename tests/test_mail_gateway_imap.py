#!/usr/bin/env python3
"""The mail gateway's mailbox adapter (scripts/mail-gateway.py), over IMAP.

The adapter is the only code that speaks IMAP, and the first long-running
IMAP client in the framework. It is exercised here through Python's real
``imaplib`` against a small scripted IMAP server on a socket pair — so what is
pinned is the adapter's reading of real protocol answers: folder roles from
SPECIAL-USE and from configured names, UIDVALIDITY and UIDNEXT, UID searches,
whole and partial fetches, finding a mail by Message-ID, reconnecting after a
dropped connection, a timeout that does not hang the poll, a failed login in
words an operator can act on — and that nothing a read sends can change the
mailbox.

And its write half, command by command: a folder opened read-write for the one
command that writes and read-only again after it, ``\\Seen``, a move by MOVE,
by COPY and UID EXPUNGE, and by COPY alone on a server that can do no more,
the Sent copy appended, a folder made — with no whole-folder EXPUNGE and no
CLOSE ever sent, UIDs refused once their folder was renumbered, and no write
made twice over a dropped connection.

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
    """Just enough of an IMAP4rev1 server for the gateway's adapter.

    Folders are keyed by their name on the wire (modified UTF-7). Every command
    received is kept in ``commands``. As it stands it serves a read-only
    client: anything that would change a mailbox is answered BAD, and the
    tests of the mirror assert none was ever sent. ``writable`` lets the
    writes through — SELECT, UID STORE, UID COPY, UID MOVE and UID EXPUNGE
    (the last two where ``capabilities`` names MOVE and UIDPLUS), APPEND,
    CREATE and SUBSCRIBE (``subscribed`` holds the names); a whole-folder
    EXPUNGE and CLOSE stay BAD in every mode."""

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
        # The write half.
        self.capabilities = "IMAP4rev1 SPECIAL-USE"
        self.writable = False
        self.names_uids = True        # answer COPYUID / APPENDUID (where UIDPLUS is named)
        self.refuse = {}              # a write's verb ("STORE", "MOVE", …) → what follows "NO"
        self.drop_after = None        # carry this write out, then hang up without an answer
        self.opened = []              # ("SELECT" | "EXAMINE", folder), in order
        self.subscribed = []          # folder names, as SUBSCRIBE named them

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
        selected, writing = None, False

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
                    send(f"* CAPABILITY {self.capabilities}", f"{tag} OK done")
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
                elif verb == "EXAMINE" or (verb == "SELECT" and self.writable):
                    name = _TOKEN_RE.match(args).group(1)
                    box = self.folders.get(name)
                    self.opened.append((verb, name))
                    if name in self.refuse_examine:
                        # The folder is there; the server just will not open it.
                        selected = None
                        send(f"{tag} NO {self.refuse_examine[name]}".rstrip())
                        continue
                    if box is None or "\\Noselect" in box["attrs"]:
                        selected = None
                        send(f"{tag} NO [NONEXISTENT] no such mailbox")
                        continue
                    selected, writing = box, verb == "SELECT"
                    send(f"* {len(box['mails'])} EXISTS",
                         f"* OK [UIDVALIDITY {box['validity']}] UIDs valid",
                         f"* OK [UIDNEXT {box['next']}] predicted next UID",
                         f"{tag} OK [READ-WRITE] SELECT completed" if writing
                         else f"{tag} OK [READ-ONLY] EXAMINE completed")
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
                    elif self.writable and writing:
                        answer = self._write(selected, sub.upper(), rest, tag)
                        if self.drop_after == sub.upper():
                            self.drop_after = None
                            sock.close()        # done, and the answer never leaves
                            return
                        send(*answer)
                    else:
                        send(f"{tag} BAD read-only server")
                elif verb == "APPEND" and self.writable:
                    name, flags, size = re.match(
                        r'"((?:[^"\\]|\\.)*)" \(([^)]*)\) "[^"]*" \{(\d+)\}$', args).groups()
                    box = self.folders.get(name)
                    if box is None or "APPEND" in self.refuse:
                        send(f"{tag} NO {self.refuse.get('APPEND', '[TRYCREATE] no such mailbox')}")
                        continue
                    send("+ go ahead")
                    raw = reader.read(int(size))
                    reader.readline()
                    uid = self.put(name, raw, flags=flags.split(), date=time.time())
                    if self.drop_after == "APPEND":
                        self.drop_after = None
                        sock.close()
                        return
                    code = (f"[APPENDUID {box['validity']} {uid}] " if self._names_uids() else "")
                    send(f"{tag} OK {code}APPEND completed")
                elif verb == "CREATE" and self.writable:
                    name = _TOKEN_RE.match(args).group(1)
                    if "CREATE" in self.refuse:
                        send(f"{tag} NO {self.refuse['CREATE']}")
                    elif name in self.folders:
                        send(f"{tag} NO [ALREADYEXISTS] mailbox exists")
                    else:
                        self.folder(name, validity=70 + len(self.folders))
                        send(f"{tag} OK created")
                elif verb == "SUBSCRIBE" and self.writable:
                    name = _TOKEN_RE.match(args).group(1)
                    if "SUBSCRIBE" in self.refuse:
                        send(f"{tag} NO {self.refuse['SUBSCRIBE']}")
                    else:
                        self.subscribed.append(name)
                        send(f"{tag} OK subscribed")
                else:
                    send(f"{tag} BAD not here")
        except OSError:
            return

    def _names_uids(self) -> bool:
        return self.names_uids and "UIDPLUS" in self.capabilities.split()

    @staticmethod
    def _uids(spec: str) -> list:
        out = []
        for part in spec.split(","):
            low, _colon, high = part.partition(":")
            out.extend(range(int(low), int(high or low) + 1))
        return out

    @staticmethod
    def _ranges(uids: list) -> str:
        """A UID set as servers write one: runs as ``low:high``."""
        runs = []
        for uid in uids:
            if runs and uid == runs[-1][1] + 1:
                runs[-1][1] = uid
            else:
                runs.append([uid, uid])
        return ",".join(str(a) if a == b else f"{a}:{b}" for a, b in runs)

    def _write(self, box, sub: str, rest: str, tag: str) -> list:
        """One UID command that changes the selected folder; its answer."""
        capabilities = self.capabilities.split()
        if sub in self.refuse:
            return [f"{tag} NO {self.refuse[sub]}"]
        uid_set, _space, more = rest.partition(" ")
        uids = [uid for uid in self._uids(uid_set) if uid in box["mails"]]
        if sub == "STORE":
            what, flags = re.match(r"([+-]?FLAGS(?:\.SILENT)?) \(([^)]*)\)$", more).groups()
            for uid in uids:
                have = [f for f in box["mails"][uid]["flags"] if f not in flags.split()]
                box["mails"][uid]["flags"] = tuple(
                    have + ([] if what.startswith("-") else flags.split()))
            return [f"{tag} OK STORE completed"]
        if sub == "EXPUNGE" and "UIDPLUS" in capabilities:
            order = sorted(box["mails"])
            gone = [uid for uid in uids if "\\Deleted" in box["mails"][uid]["flags"]]
            lines = [f"* {order.index(uid) + 1} EXPUNGE" for uid in reversed(gone)]
            for uid in gone:
                del box["mails"][uid]
            return [*lines, f"{tag} OK UID EXPUNGE completed"]
        if sub == "COPY" or (sub == "MOVE" and "MOVE" in capabilities):
            name = _TOKEN_RE.match(more).group(1)
            target = self.folders.get(name)
            if target is None:
                return [f"{tag} NO [TRYCREATE] no such mailbox"]
            new = [self.put(name, box["mails"][uid]["raw"], flags=box["mails"][uid]["flags"],
                            date=box["mails"][uid]["date"]) for uid in uids]
            code = (f"[COPYUID {target['validity']} {self._ranges(uids)} {self._ranges(new)}]"
                    if self._names_uids() and uids else "")
            if sub == "COPY":
                return [f"{tag} OK {code} COPY completed".replace("  ", " ")]
            order = sorted(box["mails"])
            lines = [f"* {order.index(uid) + 1} EXPUNGE" for uid in reversed(uids)]
            for uid in uids:
                del box["mails"][uid]
            return [*([f"* OK {code} moved"] if code else []), *lines, f"{tag} OK MOVE completed"]
        return [f"{tag} BAD unknown command"]

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
                top = max(box["mails"], default=0)
                wanted = set()
                for part in tokens[i + 1].split(","):
                    low, _colon, high = part.partition(":")
                    low, high = int(low), (top if high == "*" else int(high or low))
                    if low > high:       # "n:*" includes the newest mail even below n
                        low, high = high, low
                    wanted |= {u for u in uids if low <= u <= high}
                uids = wanted
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
        # Nothing set for the archive and the Trash: the server says which
        # folder is which, and its word beats the default names — as it beats
        # the configured Sent folder.
        gw = t.load(tmp, SENT_FOLDER="Sent Items")
        assert not gw.MAIL_ARCHIVE_FOLDER_SET and not gw.MAIL_TRASH_FOLDER_SET
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
        marked = server
        marked.literal_names = False

    with tempfile.TemporaryDirectory() as tmp:
        # A name the deployment set IS the folder, whatever the server marks:
        # the archive it names, and for a Trash name the server does not
        # have, none — never the marked one in its place.
        gw = t.load(tmp, SENT_FOLDER="Sent Items", MAIL_ARCHIVE_FOLDER="Ablage",
                    MAIL_TRASH_FOLDER="Bin")
        assert gw.MAIL_ARCHIVE_FOLDER_SET and gw.MAIL_TRASH_FOLDER_SET
        with Wired(gw, marked):
            box = gw.Mailbox(gw.MAIL_CONFIG)
            assert box.folders() == {"inbox": "INBOX", "sent": "Gesendete Objekte",
                                     "archive": "Ablage", "trash": None}
            marked.folder("bin")
            assert box.folders()["trash"] == "bin", "matched without case, like every name"
        # No SPECIAL-USE: the configured names, matched exactly, else without case.
        server = MiniImap()
        for name in ("Inbox", "Sent Items", "ablage", "Drafts"):
            server.folder(name)
        with Wired(gw, server):
            assert gw.Mailbox(gw.MAIL_CONFIG).folders() == {
                "inbox": "Inbox", "sent": "Sent Items", "archive": "ablage", "trash": None}

    with tempfile.TemporaryDirectory() as tmp:
        # Set but empty is not set (compose passes the variables through).
        gw = t.load(tmp, MAIL_ARCHIVE_FOLDER="  ", MAIL_TRASH_FOLDER="")
        assert not gw.MAIL_ARCHIVE_FOLDER_SET and not gw.MAIL_TRASH_FOLDER_SET
        assert (gw.MAIL_ARCHIVE_FOLDER, gw.MAIL_TRASH_FOLDER) == ("Archive", "Trash")

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
        # A set name wins over both marks — and while no folder has it, the
        # role has none (the first Archive makes it; a Trash is never made).
        named = both + [(set(), "Done"), (set(), "Bin")]
        assert resolve(named, "Sent", "Done", "Bin") == {
            "inbox": "INBOX", "sent": "[Gmail]/Sent Mail", "archive": "Archive",
            "trash": "[Gmail]/Trash"}
        assert resolve(named, "Sent", "Done", "Bin", archive_explicit=True,
                       trash_explicit=True) == {
            "inbox": "INBOX", "sent": "[Gmail]/Sent Mail", "archive": "Done", "trash": "Bin"}
        assert resolve(both, "Sent", "Done", "Bin", archive_explicit=True,
                       trash_explicit=True) == {
            "inbox": "INBOX", "sent": "[Gmail]/Sent Mail", "archive": None, "trash": None}
        assert resolve(both, "Sent", "Done", "Bin", trash_explicit=True)["archive"] == "Archive"
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
    print("ok: a set archive or Trash name is the folder; else SPECIAL-USE, then the default")


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
            for turn in range(2):
                # INBOX and Sent answer, so the mirror is at work: new mail
                # arrives, and /health says what waits for the archive, in
                # the server's words.
                server.put("INBOX", t.msg(f"n{turn}@x.org", day=39), date=t.NOW - 5)
                loop.clear()
                gw._loop_step(mirror, loop)
                snap = gw._health_snapshot()
                assert snap["connected"] is True and f"n{turn}@x.org" in t.by_id(gw)
                assert "would not open 'Archive'" in snap["error"]
                assert "[UNAVAILABLE] Temporary problem" in snap["error"]
            record = t.by_id(gw)["a@x.org"]
            assert record["removed_at"] is None and record["body_part"]
            del server.refuse_examine["Archive"]
            loop.clear()
            gw._loop_step(mirror, loop)
            snap = gw._health_snapshot()
            assert snap["connected"] is True and snap["error"] is None
            assert t.by_id(gw)["a@x.org"]["removed_at"] is None
            assert not any(kind == "removed" for kind, _p in events)
            # Deleted for real, it is a tombstone as before.
            server.folders["Archive"]["mails"].clear()
            loop.clear()
            gw._loop_step(mirror, loop)
            assert t.by_id(gw)["a@x.org"]["removed_at"]
    with tempfile.TemporaryDirectory() as tmp:
        # An archive that refuses from the first poll on, for good (a broken
        # mailbox on the server): the old mail in INBOX is still read.
        gw = t.load(tmp)
        t._write_policy(gw)
        t.rail(gw)
        server = _server()
        for n in range(3):
            server.put("INBOX", t.msg(f"old{n}@x.org", day=30 + n), date=t.T0 + (30 + n) * t.DAY)
        server.refuse_examine["Archive"] = "[SERVERBUG] Internal error"
        with Wired(gw, server):
            gw.MIRROR = mirror = gw.Mirror(gw.Mailbox(gw.MAIL_CONFIG), clock=lambda: t.NOW)
            loop = {}
            for _ in range(3):
                loop.pop("synced", None)
                gw._loop_step(mirror, loop)
            snap = gw._health_snapshot()
            assert snap["connected"] is True and "[SERVERBUG] Internal error" in snap["error"]
            assert snap["backfill"]["done"] is True
            assert sorted(t.by_id(gw)) == ["old0@x.org", "old1@x.org", "old2@x.org"]
    print("ok: an archive the server refuses tombstones nothing and holds back no mail of "
          "INBOX or Sent")


def _writable(capabilities="IMAP4rev1 SPECIAL-USE MOVE UIDPLUS") -> MiniImap:
    server = _server()
    server.writable = True
    server.capabilities = capabilities
    return server


def _never(commands) -> list:
    """Commands the adapter must not send in any mode: a whole-folder
    EXPUNGE, and CLOSE, which is one on a folder opened read-write."""
    return [c for c in commands if c.split()[0] in ("EXPUNGE", "CLOSE", "DELETE", "RENAME")]


def _ids(server, folder) -> list:
    return sorted((email.message_from_bytes(mail["raw"]).get("Message-ID") or "").strip("<>")
                  for mail in server.folders[folder]["mails"].values())


def test_seen_is_set_with_the_folder_open_for_that_one_command():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        server = _writable()
        a = server.put("INBOX", t.msg("a@x.org"))
        b = server.put("INBOX", t.msg("b@x.org"))
        c = server.put("INBOX", t.msg("c@x.org"))
        with Wired(gw, server):
            box = gw.Mailbox(gw.MAIL_CONFIG)
            validity = box.list_uids("INBOX").uidvalidity
            mark = len(server.commands)
            assert box.set_seen("INBOX", [c, a], validity=validity) == 2
            # The session is checked, the folder opened for writing, the
            # flag stored — and the folder is read-only again before the
            # adapter returns.
            assert server.commands[mark:] == [
                "NOOP", 'SELECT "INBOX"', f"UID SEARCH UID {a},{c}",
                f"UID STORE {a},{c} +FLAGS.SILENT (\\Seen)", 'EXAMINE "INBOX"'], \
                server.commands[mark:]
            mails = server.folders["INBOX"]["mails"]
            assert mails[a]["flags"] == mails[c]["flags"] == ("\\Seen",) and mails[b]["flags"] == ()
            # A mail that is no longer there is not counted, and none at all
            # stores nothing.
            assert box.set_seen("INBOX", [b, 99]) == 1
            mark = len(server.commands)
            assert box.set_seen("INBOX", [98, 99]) == 0
            assert not [x for x in server.commands[mark:] if "STORE" in x]
            mark = len(server.commands)
            assert box.set_seen("INBOX", []) == 0 and server.commands[mark:] == []
            assert box.set_seen("Nope", [1]) is None
            # A folder renumbered since its UIDs were read: nothing is
            # written, and the folder is left read-only.
            mails[b]["flags"] = ()
            mark = len(server.commands)
            try:
                box.set_seen("INBOX", [b], validity=validity + 1)
                raise AssertionError("stored by UIDs of another numbering")
            except gw.FolderChanged as exc:
                assert "renumbered" in str(exc)
            assert mails[b]["flags"] == () and server.opened[-1] == ("EXAMINE", "INBOX")
            assert not [x for x in server.commands[mark:] if "STORE" in x]
            # A refusal is the server's words, and leaves the folder read-only too.
            server.refuse["STORE"] = "[NOPERM] flags are not yours to set"
            try:
                box.set_seen("INBOX", [b])
                raise AssertionError("no error")
            except gw.MailboxError as exc:
                assert "NOPERM" in str(exc) and not isinstance(exc, gw.FolderChanged)
            assert server.opened[-1] == ("EXAMINE", "INBOX")
            # Reading goes on as before.
            assert box.find("INBOX", "b@x.org") == b
            assert not _never(server.commands)
    print("ok: \\Seen is stored with the folder opened read-write for that command alone")


def test_a_move_by_move_by_copy_and_expunge_or_by_copy_alone():
    cases = (("IMAP4rev1 SPECIAL-USE MOVE UIDPLUS", ["UID MOVE"]),
             ("IMAP4rev1 SPECIAL-USE MOVE", ["UID MOVE"]),
             ("IMAP4rev1 SPECIAL-USE UIDPLUS", ["UID COPY", "UID STORE", "UID EXPUNGE"]),
             ("IMAP4rev1 SPECIAL-USE", ["UID COPY", "UID STORE"]))
    for capabilities, expected in cases:
        with tempfile.TemporaryDirectory() as tmp:
            gw = t.load(tmp)
            server = _writable(capabilities)
            server.put("INBOX", t.msg("x@x.org"))
            a = server.put("INBOX", t.msg("a@x.org"))
            b = server.put("INBOX", t.msg("b@x.org"))
            # A mail the user marked deleted in a client that does not expunge.
            theirs = server.put("INBOX", t.msg("theirs@x.org"), flags=("\\Deleted",))
            server.put("Archive", t.msg("old@x.org"))
            with Wired(gw, server):
                box = gw.Mailbox(gw.MAIL_CONFIG)
                box.ensure()
                mark = len(server.commands)
                result = box.move("INBOX", [b, a, 77], "Archive", validity=7)
                writes = [" ".join(x.split()[:2]) for x in server.commands[mark:]
                          if x.startswith("UID ") and not x.startswith("UID SEARCH")]
                assert writes == expected, (capabilities, server.commands[mark:])
                assert result.absent == (77,)
                assert _ids(server, "Archive") == ["a@x.org", "b@x.org", "old@x.org"]
                named = {a: 2, b: 3} if "UIDPLUS" in capabilities else {a: None, b: None}
                if "MOVE" in capabilities or "UIDPLUS" in capabilities:
                    assert result.moved == named and result.left == {}, result
                    assert _ids(server, "INBOX") == ["theirs@x.org", "x@x.org"]
                else:
                    # No way to remove exactly these: copied, flagged, left.
                    assert result.left == named and result.moved == {}, result
                    mails = server.folders["INBOX"]["mails"]
                    assert "\\Deleted" in mails[a]["flags"] and "\\Deleted" in mails[b]["flags"]
                if "UID EXPUNGE" in expected:
                    assert f"UID EXPUNGE {a},{b}" in server.commands[mark:]
                # In every mode the user's own flagged mail is still there:
                # nothing expunged the folder.
                assert theirs in server.folders["INBOX"]["mails"], capabilities
                assert not _never(server.commands), _never(server.commands)
                assert server.opened[-2:] == [("SELECT", "INBOX"), ("EXAMINE", "INBOX")]
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        server = _writable()
        a = server.put("INBOX", t.msg("a@x.org"))
        with Wired(gw, server):
            box = gw.Mailbox(gw.MAIL_CONFIG)
            # A destination that does not exist is a refusal: the caller
            # makes the folder first.
            try:
                box.move("INBOX", [a], "Nope")
                raise AssertionError("no error")
            except gw.MailboxError as exc:
                assert "TRYCREATE" in str(exc)
            assert _ids(server, "INBOX") == ["a@x.org"]
            assert box.move("Nope", [a], "Archive") is None
            mark = len(server.commands)
            assert box.move("INBOX", [], "Archive") == gw.Moved({}, {}, ())
            assert server.commands[mark:] == []
            try:
                box.move("INBOX", [a], "Archive", validity=8)
                raise AssertionError("moved by UIDs of another numbering")
            except gw.FolderChanged:
                pass
            assert _ids(server, "INBOX") == ["a@x.org"] and _ids(server, "Archive") == []
            # A server with UIDPLUS that names no UIDs all the same: moved,
            # and where to is the caller's to find.
            server.names_uids = False
            assert box.move("INBOX", [a], "Archive") == gw.Moved({a: None}, {}, ())
            assert box.find("Archive", "a@x.org") == 1
    print("ok: a move is MOVE, or COPY and UID EXPUNGE of those UIDs, or COPY alone — "
          "never an EXPUNGE of the folder")


def test_append_and_create():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        server = _writable()
        raw = t.msg("s@example.org", sender=t.ME, to="ann@x.org", body="Line one.\nLine two.\n")
        wire = raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        with Wired(gw, server):
            box = gw.Mailbox(gw.MAIL_CONFIG)
            box.ensure()
            opened = len(server.opened)
            assert box.append("Sent", raw, when=t.T0 + 5 * t.DAY) == (7, 1)
            mail = server.folders["Sent"]["mails"][1]
            assert mail["raw"] == wire and mail["flags"] == ("\\Seen",)
            assert len(server.opened) == opened, "an append selects nothing"
            assert box.find("Sent", "s@example.org") == 1
            # No APPENDUID: the caller finds the mail by its id.
            server.names_uids = False
            assert box.append("Sent", raw) == (None, None)
            assert len(server.folders["Sent"]["mails"]) == 2
            for folder, word in (("Nope", "TRYCREATE"),):
                try:
                    box.append(folder, raw)
                    raise AssertionError("no error")
                except gw.MailboxError as exc:
                    assert word in str(exc)
            server.refuse["APPEND"] = "[OVERQUOTA] mailbox is full"
            try:
                box.append("Sent", raw)
                raise AssertionError("no error")
            except gw.MailboxError as exc:
                assert "OVERQUOTA" in str(exc)
            assert len(server.folders["Sent"]["mails"]) == 2

            assert box.folders()["trash"] is None
            box.create("Trash")
            assert box.folders()["trash"] == "Trash"
            # Made and subscribed: unsubscribed, a mail client that lists
            # subscribed folders would not show it, or the mail moved into it.
            assert server.subscribed == ["Trash"]
            assert server.commands[-3:-1] == ['CREATE "Trash"', 'SUBSCRIBE "Trash"']
            box.create("Trash")         # there already: no failure — and subscribed still
            assert server.subscribed == ["Trash", "Trash"]
            box.create("Ablage/Erledigt ü")
            wire = gw._ec.imap_utf7_encode("Ablage/Erledigt ü")
            assert wire in server.folders and server.subscribed[-1] == wire
            # A server that will not subscribe has still made the folder.
            server.refuse["SUBSCRIBE"] = "[NOPERM] no subscriptions here"
            box.create("Unlisted")
            assert "Unlisted" in server.folders and "Unlisted" not in server.subscribed
            del server.refuse["SUBSCRIBE"]
            server.refuse["CREATE"] = "[NOPERM] not here"
            try:
                box.create("Other")
                raise AssertionError("no error")
            except gw.MailboxError as exc:
                assert "NOPERM" in str(exc)
            assert "Other" not in server.subscribed, "nothing is subscribed that was not made"
            assert not _never(server.commands)
    print("ok: a mail is appended seen, with its UID where the server names it; a folder is "
          "made and subscribed")


def test_a_write_is_made_once_whatever_happens_to_the_connection():
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        server = _writable()
        a = server.put("INBOX", t.msg("a@x.org"))
        raw = t.msg("s@example.org", sender=t.ME, to="ann@x.org")
        count = lambda prefix: len([x for x in server.commands if x.startswith(prefix)])  # noqa: E731
        with Wired(gw, server):
            box = gw.Mailbox(gw.MAIL_CONFIG)
            box.ensure()
            # A session the server closed while it sat idle is reopened
            # before the write — where trying again costs nothing.
            server.drop_next = 1
            assert box.set_seen("INBOX", [a]) == 1
            assert server.connections == 2 and count("UID STORE") == 1
            # The line dies after the server carried the move out: an error
            # for the caller, and no second MOVE.
            server.drop_after = "MOVE"
            try:
                box.move("INBOX", [a], "Archive")
                raise AssertionError("no error")
            except gw.MailboxError as exc:
                assert "connection" in str(exc)
            assert count("UID MOVE") == 1 and _ids(server, "Archive") == ["a@x.org"]
            # The adapter goes on, and the caller finds what the write left.
            assert box.find("Archive", "a@x.org") == 1 and box.find("INBOX", "a@x.org") is None
            server.drop_after = "APPEND"
            try:
                box.append("Sent", raw)
                raise AssertionError("no error")
            except gw.MailboxError:
                pass
            assert count("APPEND") == 1 and box.find("Sent", "s@example.org") == 1
    print("ok: a write is not sent a second time over a dropped connection")


def test_the_mailbox_follows_the_chat_over_the_adapter():
    for capabilities in ("IMAP4rev1 SPECIAL-USE MOVE UIDPLUS", "IMAP4rev1 SPECIAL-USE UIDPLUS",
                         "IMAP4rev1 SPECIAL-USE"):
        with tempfile.TemporaryDirectory() as tmp:
            gw = t.load(tmp)
            events = t.rail(gw)
            server = _writable(capabilities)
            server.folder("Deleted Items", "\\HasNoChildren \\Trash")
            with Wired(gw, server):
                mirror = gw.Mirror(gw.Mailbox(gw.MAIL_CONFIG), clock=lambda: t.NOW)
                mirror.poll()
                server.put("INBOX", t.msg("a@x.org", day=1), date=t.NOW)
                server.put("INBOX", t.msg("b@x.org", subject="Re: Quarterly numbers",
                                          refs=["a@x.org"], irt="a@x.org", day=2), date=t.NOW)
                server.put("Sent", t.msg("s@example.org", sender=t.ME, to="ann@x.org",
                                         subject="Re: Quarterly numbers", refs=["a@x.org"],
                                         irt="a@x.org", day=3), date=t.NOW)
                server.put("INBOX", t.msg("other@x.org", subject="Other", day=4), date=t.NOW)
                mirror.poll()
                assert len(t.records(gw)) == 4
                told = len(events)
                assert mirror.mark_read("a@x.org") == 2
                flagged = [m["flags"] for m in server.folders["INBOX"]["mails"].values()]
                assert flagged.count(("\\Seen",)) == 2
                kept = "MOVE" not in capabilities and "UIDPLUS" not in capabilities
                assert mirror.archive("a@x.org") == {"moved": 2, "flagged": 2 if kept else 0}
                assert _ids(server, "Archive") == ["a@x.org", "b@x.org"]
                # The gateway's own Sent copy: appended and placed — once,
                # however often it is tried — and never read back as new mail.
                own = t.msg("mine@example.org", sender=t.ME, to="ann@x.org", day=5)
                mirror.file_sent("mine@example.org", own, t.NOW, again=False)
                mirror.file_sent("mine@example.org", own, t.NOW, again=True)
                assert _ids(server, "Sent") == ["mine@example.org", "s@example.org"]
                fetches = len([x for x in server.commands if "BODY.PEEK[]" in x])
                for _ in range(3):
                    mirror.poll()
                # The mirror followed its own writes: nothing gone, nothing
                # told, nothing read again.
                assert not any(r["removed_at"] for r in t.records(gw))
                assert events[told:] == [] and len(t.records(gw)) == 4
                assert len([x for x in server.commands if "BODY.PEEK[]" in x]) == fetches
                assert mirror.archive("a@x.org") == {"moved": 0, "flagged": 0}
                assert _ids(server, "Archive") == ["a@x.org", "b@x.org"], "copied once"

                result = mirror.delete_chat("a@x.org")
                assert result["errors"] == 0 and result["trashed"] == 3, result
                # The account's own Trash holds the thread — the user's own
                # reply from Sent with it — and no folder was made for it.
                assert _ids(server, "Deleted Items") == ["a@x.org", "b@x.org", "s@example.org"]
                assert not any(c.startswith("CREATE") for c in server.commands)
                for _ in range(3):
                    mirror.poll()
                assert [r["message_id"] for r in t.records(gw)] == ["other@x.org"], capabilities
                # A whole session of writes: no folder-wide EXPUNGE, no CLOSE,
                # and every folder opened read-write was read-only again at once.
                assert not _never(server.commands), _never(server.commands)
                for at, (verb, name) in enumerate(server.opened):
                    if verb == "SELECT":
                        assert server.opened[at + 1] == ("EXAMINE", name), server.opened[at:at + 2]
                assert server.opened[-1][0] == "EXAMINE"
    print("ok: read, archive, the Sent copy and delete run over the real adapter")


def test_the_archive_and_the_trash_as_configured_over_the_adapter():
    # A name the deployment set wins over what the server marks: Archive
    # makes "Done" — subscribed — beside the server's own \Archive, and the
    # mirror follows "Done". A Trash is never made, set or not.
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp, MAIL_ARCHIVE_FOLDER="Done", MAIL_TRASH_FOLDER="Bin")
        t.rail(gw)
        server = _writable()
        server.folder("Deleted Items", "\\HasNoChildren \\Trash")
        with Wired(gw, server):
            mirror = gw.Mirror(gw.Mailbox(gw.MAIL_CONFIG), clock=lambda: t.NOW)
            mirror.poll()
            assert "'Done'" in mirror.health_notice() and "made" in mirror.health_notice()
            server.put("INBOX", t.msg("a@x.org", day=1), date=t.NOW)
            server.put("INBOX", t.msg("b@x.org", subject="Re: Quarterly numbers",
                                      refs=["a@x.org"], irt="a@x.org", day=2), date=t.NOW)
            mirror.poll()
            assert mirror.archive("a@x.org") == {"moved": 2, "flagged": 0}
            assert _ids(server, "Done") == ["a@x.org", "b@x.org"] and _ids(server, "Archive") == []
            assert server.subscribed == ["Done"]
            # Deleted at once, as the page does it (Delete is offered on the
            # archived chat) — before any poll has looked at the new folder.
            # "Bin" is what was set, and the mailbox has none: the server's
            # own Trash is not taken instead, none is made, nothing is erased.
            result = mirror.delete_chat("a@x.org")
            assert result["errors"] == 2 and result["trashed"] == 0, result
            assert "MAIL_TRASH_FOLDER" in result["error"] and "'Bin'" in result["error"]
            assert "Bin" not in server.folders and _ids(server, "Deleted Items") == []
            assert len(t.records(gw)) == 2 and _ids(server, "Done") == ["a@x.org", "b@x.org"]
            server.folder("Bin")
            result = mirror.delete_chat("a@x.org")
            assert result["errors"] == 0 and result["trashed"] == 2, result
            assert _ids(server, "Bin") == ["a@x.org", "b@x.org"] and _ids(server, "Done") == []
            assert t.records(gw) == [] and server.subscribed == ["Done"]
            for _ in range(2):
                mirror.poll()
            assert t.records(gw) == [] and mirror.health_notice() is None
            assert mirror._folder("archive")["name"] == "Done"
            assert not _never(server.commands)

    # Nothing set, and nothing the server marks or names "Trash": the delete
    # fails and says what to set — no CREATE is ever sent for a Trash.
    with tempfile.TemporaryDirectory() as tmp:
        gw = t.load(tmp)
        t.rail(gw)
        server = _writable()
        server.folder("Papierkorb")         # the account's trash, unmarked
        with Wired(gw, server):
            mirror = gw.Mirror(gw.Mailbox(gw.MAIL_CONFIG), clock=lambda: t.NOW)
            mirror.poll()
            server.put("INBOX", t.msg("a@x.org", day=1), date=t.NOW)
            mirror.poll()
            result = mirror.delete_chat("a@x.org")
            assert result["errors"] == 1 and "MAIL_TRASH_FOLDER" in result["error"], result
            assert "'Trash'" in result["error"]
            assert not any(c.split()[0] in ("CREATE", "SUBSCRIBE") for c in server.commands)
            assert _ids(server, "INBOX") == ["a@x.org"] and len(t.records(gw)) == 1
    print("ok: a set folder name is used and the archive made subscribed; a Trash is never made")


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
