"""Mail threading for the mail gateway: which chat a mail belongs to.

A mail thread is a **chat** (``docs/mail-chats.md``), keyed by the thread
root's ``Message-ID`` without angle brackets. This module decides that key for
every mail the gateway mirrors, and remembers the decision in a small sqlite
index — one file per account, because a chat is ``<channel>:<account>:<key>``
and the same thread reaching two of the user's accounts stays two chats.

Threading follows the headers and nothing else. **There is no subject-only
threading, ever**: two mails that share a subject but no ``References`` /
``In-Reply-To`` link are two chats. A subject is a name, not an identity — "Hi",
"Invoice" and "(no subject)" would otherwise weld strangers together.

The algorithm, for a mail ``M`` whose *cited ids* are its ``References`` in
header order followed by its ``In-Reply-To`` (duplicates dropped, first
position kept, ``M`` itself never cited):

1. Look up ``M`` and every cited id in the index.
2. **None known** — the mail starts a chat keyed by its *declared root*: the
   first cited id when there is one, else ``M``. A reply whose parent was never
   mirrored therefore starts the chat its root names, and the parent — arriving
   later, out of order or by backfill — lands in that chat with no merge.
3. **One chat known** — the mail joins it.
4. **Several chats known** — they are one thread: all merge into the chat whose
   earliest mail is oldest (ties: the smaller key), and every id of the
   absorbed chats is re-pointed to the survivor. This is the client that sent
   only ``In-Reply-To``, or cut ``References`` short, so two chats were started
   before the mail that links them arrived.
5. ``M`` and every cited id are mapped to the surviving key.

The index maps **every id ever seen or cited** to a chat key, so a parent that
was only ever cited is found when it finally arrives. Resolving the same mail
twice changes nothing and reports no merge.

**What one mail may do is bounded**, because every header of it is its
sender's to write and a merge is for good. A mail cites at most
:data:`MAX_CITED_IDS` ids (the declared root and the nearest parents; a
longer ``References`` loses its middle, as RFC 5322 has clients trim it). And
rule 4 joins at most :data:`MAX_MERGE_CHATS` chats: a mail that cites more
merges none of them — it joins the chat of its nearest known parent, and
:attr:`Resolution.unmerged` names the chats it was not allowed to weld.
"Oldest" is by the ``date`` the caller hands in, so a caller that must not let
a sender choose the survivor passes a date the sender cannot write (the mail
gateway: the server's arrival time, not the ``Date`` header).

Schema (``PRAGMA user_version`` 1), kept small on purpose — the ledger is the
record, this is a lookup table that :meth:`ThreadIndex.rebuild` can recreate:

- ``ids(id, chat)`` — every id seen or cited → its chat key.
- ``mails(id, chat, date, subject)`` — the mails actually resolved (not the
  ids only cited), with the facts a merge and the chat's name need: the mail's
  date (epoch seconds, NULL when it had none) and its own subject as given.

Derived values — a chat's first subject, its earliest and latest date, its
members — are read from ``mails`` on demand, so they can never go stale: a
merge or an older mail arriving late re-evaluates them by construction.

Concurrency: the gateway polls on one thread (the only writer) and serves HTTP
on others (readers). One connection guarded by one lock serves them all; every
write is one ``BEGIN IMMEDIATE`` transaction, so a reader never sees half a
merge and a crash never leaves one. Pure stdlib, no network: the slim gateway
image imports it.
"""
from __future__ import annotations

import datetime as _dt
import email.utils
import math
import re
import sqlite3
import threading
from typing import Iterable, NamedTuple

SCHEMA_VERSION = 1

# How many ids one mail may cite. A long thread's References runs to a few
# dozen ids; clients that trim keep the first and the last ones (RFC 5322,
# 3.6.4), and so does this: the first cited id — the declared root, which keys
# the chat — and the nearest parents. Without a cap one mail writes as many
# permanent index rows as its sender cares to type.
MAX_CITED_IDS = 100
# How many chats one mail may join into one. Two is the ordinary case (a
# client cut References short), three or four a long thread cut more than
# once. Beyond that it is not a thread found again but a header written to
# weld strangers' conversations together — and a merge cannot be undone.
MAX_MERGE_CHATS = 4


# --------------------------------------------------------------------------
# Message-ID parsing
# --------------------------------------------------------------------------

# Characters that never occur in a usable Message-ID. RFC 5322's msg-id is
# dot-atom-text or a domain literal on either side of "@"; obsolete forms allow
# quoted local parts, but a mail that relies on one loses at most a thread link
# here, while accepting quotes, commas or parentheses lets prose and address
# lists from broken headers through as "ids".
_FORBIDDEN = set('<>()",') | {chr(c) for c in range(0x20)} | {"\x7f"}

# A header line break followed by the whitespace that continues the line. Seen
# inside an angle-bracketed id it is a fold some MTA put into a long id, and
# removing it restores the id; any other whitespace inside an id is junk.
_FOLD = re.compile(r"\r?\n[ \t]*")


def _valid(token: str) -> bool:
    # (A lone surrogate — what a lenient decoder leaves of a byte it could not
    # read — can be neither stored nor sent: a token holding one is no id.)
    if not token or any(ch in _FORBIDDEN or ch.isspace() or "\ud800" <= ch <= "\udfff"
                        for ch in token):
        return False
    if "@" in token:
        left, _, right = token.rpartition("@")
        if not left or not right:
            return False
    return True


def _scan(raw: str) -> tuple[list[str], bool, str]:
    """Split a header value into (bracketed contents, saw a bracket, bare text).

    A small state machine rather than a regex, because the junk worth surviving
    nests: comments may hold parentheses, quoted strings and angle brackets
    ("(Joe's message of "Mon" <joe@x>)"), and neither a comment nor a quoted
    string may contribute an id. An unclosed ``<`` (a truncated header) yields
    nothing — its content may be a cut-off id.
    """
    ids: list[str] = []
    bare: list[str] = []
    saw_bracket = False
    i, n = 0, len(raw)
    while i < n:
        ch = raw[i]
        if ch == "(":
            depth = 1
            i += 1
            while i < n and depth:
                c = raw[i]
                if c == "\\":
                    i += 1
                elif c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                i += 1
            bare.append(" ")
            continue
        if ch == '"':
            i += 1
            while i < n and raw[i] != '"':
                i += 2 if raw[i] == "\\" else 1
            i += 1
            # A quoted string is prose; it makes the bare text non-id-like.
            bare.append(" \" ")
            continue
        if ch == "<":
            saw_bracket = True
            j = i + 1
            buf: list[str] = []
            closed = False
            while j < n:
                c = raw[j]
                if c == ">":
                    closed = True
                    break
                if c == "<":
                    # "<<a@b>" or "<junk <a@b>": the innermost opening wins.
                    buf = []
                else:
                    buf.append(c)
                j += 1
            if closed:
                ids.append("".join(buf))
                i = j + 1
            else:
                i = n
            continue
        bare.append(ch)
        i += 1
    return ids, saw_bracket, "".join(bare)


def _clean_bracketed(content: str) -> str:
    token = _FOLD.sub("", content).strip()
    return token if _valid(token) else ""


def _parse_ids(raw, *, bare_needs_at: bool) -> list[str]:
    """Every usable id of one header value, in header order.

    Angle-bracketed ids are authoritative: once a header has any, the text
    around them is ignored ("<a@b> (message of Monday)"). A header without any
    bracket is read as bare ids only when *every* token of it looks like one;
    with ``bare_needs_at`` a token must also carry an "@", which is what keeps
    "message from x@y of Mon, 1 Jan" from producing ids.
    """
    if raw is None:
        return []
    raw = str(raw)
    found, saw_bracket, bare = _scan(raw)
    if saw_bracket:
        return [t for t in (_clean_bracketed(c) for c in found) if t]
    tokens = [t for t in re.split(r"[\s,]+", bare) if t]
    if not tokens:
        return []
    if all(_valid(t) and (not bare_needs_at or "@" in t) for t in tokens):
        return tokens
    return []


def normalize_id(raw) -> str:
    """``"<a@b>"`` → ``"a@b"``; ``""`` for anything that is not one usable id.

    Strips the angle brackets, surrounding whitespace and comments, and folds;
    keeps case (the left side of a Message-ID is case-sensitive, and the index
    compares bytes). Idempotent: a normalised id normalises to itself, so
    callers may pass either form. A header holding several bracketed ids yields
    the first — a malformed Message-ID header still names its mail.
    """
    ids = _parse_ids(raw, bare_needs_at=False)
    if not ids:
        return ""
    if len(ids) > 1 and "<" not in str(raw):
        # Several bare tokens are prose, not an id with a stray space.
        return ""
    return ids[0]


def _bounded(ids: list[str]) -> list[str]:
    """At most :data:`MAX_CITED_IDS` of a mail's cited ids: the first (the
    declared root) and the last ones (the nearest parents), order kept."""
    if len(ids) <= MAX_CITED_IDS:
        return ids
    return ids[:1] + ids[len(ids) - (MAX_CITED_IDS - 1):]


def cited_ids(references, in_reply_to, message_id="") -> list[str]:
    """The ids a mail cites: ``References`` in header order, then ``In-Reply-To``.

    Duplicates are dropped keeping the first position (``In-Reply-To`` normally
    repeats the last reference), and the mail's own id — when given — is never
    cited, so a client that lists itself cannot make a mail its own parent.
    At most :data:`MAX_CITED_IDS` come back: the first and the last ones.
    """
    own = normalize_id(message_id) if message_id else ""
    out: list[str] = []
    seen: set[str] = set()
    for header in (references, in_reply_to):
        for token in _parse_ids(header, bare_needs_at=True):
            if token != own and token not in seen:
                seen.add(token)
                out.append(token)
    return _bounded(out)


# --------------------------------------------------------------------------
# Subjects
# --------------------------------------------------------------------------

# Reply and forward prefixes, as data: multilingual by design, extended by
# adding a row, never by special-casing a language in code. Matched
# case-insensitively and only when a colon follows, so a subject that merely
# starts with one of these letters ("Rex", "TRavel", "Answers") is untouched.
# Rows are left out where the bare word is a common subject of its own —
# "PR:" (a pull request), "Ref:" (an order or case number) — since stripping
# one would rename a thread that was never a reply.
REPLY_FORWARD_PREFIXES: tuple[str, ...] = (
    # Latin script
    "re", "fw", "fwd",                 # English and most clients
    "aw", "wg",                        # German: Antwort, Weitergeleitet
    "antw", "doorst",                  # Dutch: Antwoord, Doorsturen
    "sv", "vs", "vb",                  # Nordic: Svar, Videresendt, Vidarebefordrat
    "vl",                              # Finnish: Välitetty (reply is "VS")
    "r", "rif", "i",                   # Italian: Risposta, Riferimento, Inoltro
    "tr", "réf",                       # French: Transféré, Référence
    "rv", "res", "enc",                # Spanish/Portuguese: Reenviado, Resposta, Encaminhado
    "odp", "pd",                       # Polish: Odpowiedź, Przekazanie dalej
    "př",                              # Czech: Přeposlat (reply is "Odp")
    "ynt", "ilt",                      # Turkish: Yanıt, İlet
    "atb", "ats",                      # Latvian: Atbilde; Lithuanian: Atsakymas
    "bls", "trs",                      # Indonesian: Balas, Terusan
    "yml",                             # Welsh: Ymlaen (reply is "ATB")
    # Greek
    "απ", "σχετ", "πρθ",
    # Cyrillic
    "отв", "пересл",
    # Hebrew
    "השב", "העבר",
    # Chinese
    "回复", "回覆", "答复", "转发", "轉寄", "轉發",
)

_PREFIX_RE = re.compile(
    r"^(?:" + "|".join(re.escape(p) for p in
                       sorted(REPLY_FORWARD_PREFIXES, key=len, reverse=True))
    + r")"
    # Counted forms: "Re[2]:", "Re(3):", "Re^2:".
    + r"\s*(?:\[\d+\]|\(\d+\)|\^\d+)?"
    # "Re :" (French spacing) and the full-width colon of CJK clients.
    + r"\s*[:：]",
    re.IGNORECASE,
)
_LIST_TAG_RE = re.compile(r"^\[([^\[\]]*)\]")
# The full-width forms of ASCII ("Ｒｅ：", as an East Asian input method types
# it) and the ideographic space, folded to ASCII for the prefix match only.
# One character for one, so a match's length on the folded text is its length
# on the subject — which a compatibility normalisation would not promise.
_FULLWIDTH_FOLD = {code: code - 0xFEE0 for code in range(0xFF01, 0xFF5F)}
_FULLWIDTH_FOLD[0x3000] = 0x20


def base_subject(subject) -> str:
    """The subject a thread is named by: reply/forward prefixes stripped.

    Prefixes are stripped repeatedly ("RE: AW: Fwd: x" → "x"). A leading list
    tag is part of the name and kept, once, in front ("Re: [dev] Re: x" →
    "[dev] x"). Whitespace collapses; nothing left is ``""`` — the caller
    shows "(no subject)".
    """
    if not subject:
        return ""
    s = re.sub(r"\s+", " ", str(subject)).strip()
    tags: list[str] = []
    seen: set[str] = set()
    while s:
        m = _PREFIX_RE.match(s.translate(_FULLWIDTH_FOLD))
        if m:
            s = s[m.end():].lstrip()
            continue
        m = _LIST_TAG_RE.match(s)
        if m:
            tag = m.group(1).strip()
            if tag and tag.casefold() not in seen:
                seen.add(tag.casefold())
                tags.append(f"[{tag}]")
            s = s[m.end():].lstrip()
            continue
        break
    return " ".join(tags + ([s] if s else []))


# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------

def to_epoch(value) -> float | None:
    """A mail date as epoch seconds: number, datetime, ISO-8601 or RFC 2822.

    ``None`` for no date or an unparsable one. A naive datetime is taken as
    UTC. Never raises: a mail with a broken Date header still threads.
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            f = float(value)
        except OverflowError:   # an integer beyond any float
            return None
        return f if math.isfinite(f) else None
    if isinstance(value, _dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=_dt.timezone.utc)
        try:
            return value.timestamp()
        except (OverflowError, OSError, ValueError):   # a year no clock reaches
            return None
    text = str(value).strip()
    if re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        return to_epoch(float(text))
    # The two parsers raise more than they document on a hostile value (a
    # zone offset of twenty digits is an OverflowError on some versions).
    try:
        return to_epoch(_dt.datetime.fromisoformat(text.replace("Z", "+00:00")))
    except (ValueError, OverflowError, TypeError):
        pass
    try:
        return to_epoch(email.utils.parsedate_to_datetime(text))
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


# --------------------------------------------------------------------------
# The index
# --------------------------------------------------------------------------

class Resolution(NamedTuple):
    """What :meth:`ThreadIndex.resolve` decided for one mail.

    ``chat`` — the key the mail belongs to now. ``merged`` — the keys absorbed
    into ``chat`` by this call (sorted; the gateway re-keys their records and
    tells the web gateway). ``new`` — this call started the chat. ``repeat`` —
    the mail was already recorded (a re-poll, a re-delivery): nothing changed.
    ``unmerged`` — the other chats the mail cited when it cited more than
    :data:`MAX_MERGE_CHATS`: none was merged, and the caller should say so.
    """
    chat: str
    merged: list[str]
    new: bool
    repeat: bool = False
    unmerged: tuple = ()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS ids (
    id   TEXT PRIMARY KEY,
    chat TEXT NOT NULL
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ids_chat ON ids (chat);
CREATE TABLE IF NOT EXISTS mails (
    id      TEXT PRIMARY KEY,
    chat    TEXT NOT NULL,
    date    REAL,
    subject TEXT
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS mails_chat ON mails (chat, date);
"""

# Earliest first, undated mails last, the id as a deterministic tie-break.
_ORDER = "ORDER BY date IS NULL, date, id"


def _record_fields(record) -> tuple[str, object, str | None, object, object]:
    if isinstance(record, dict):
        return (record.get("id") or record.get("message_id") or "",
                record.get("cited") or (), record.get("chat"),
                record.get("date"), record.get("subject"))
    rec = tuple(record)
    if len(rec) not in (4, 5):
        raise ValueError("a record is (id, cited, chat, date[, subject])")
    return rec[0], rec[1], rec[2], rec[3], rec[4] if len(rec) == 5 else None


class ThreadIndex:
    """The per-account map of message ids to chat keys (sqlite3, one file)."""

    def __init__(self, path):
        self._lock = threading.RLock()
        # One connection shared by the writer and the readers, serialised by
        # the lock; autocommit mode so every write names its own transaction.
        self._conn = sqlite3.connect(str(path), check_same_thread=False,
                                     isolation_level=None)
        with self._lock:
            self._conn.executescript(_SCHEMA)
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version != SCHEMA_VERSION:
                raise RuntimeError(
                    f"thread index {path}: schema {version}, expected "
                    f"{SCHEMA_VERSION} — rebuild it from the ledger")

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _write(self, fn, *args):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                result = fn(*args)
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")
            return result

    # -- writes ------------------------------------------------------------

    def resolve(self, message_id, cited, date=None, subject=None) -> Resolution:
        """Thread one mail, record it, and say which chat it is in.

        ``cited`` is :func:`cited_ids`' list (entries are normalised again, and
        the mail's own id dropped, so a raw list is safe). ``date`` is anything
        :func:`to_epoch` reads; ``subject`` the mail's own subject, which names
        the chat when it is the earliest one. A repeat keeps what was recorded
        and only fills in a date or subject the first call lacked.
        """
        mid = normalize_id(message_id)
        if not mid:
            raise ValueError(f"not a usable Message-ID: {message_id!r}")
        return self._write(self._resolve, mid, self._clean_cited(mid, cited),
                           to_epoch(date), subject, None)

    def forget_chat(self, chat) -> None:
        """Drop a chat and every id that pointed to it (after a delete).

        A later mail of the same thread finds nothing and starts the chat
        afresh — normally under the same declared-root key.
        """
        def run():
            self._conn.execute("DELETE FROM ids WHERE chat = ?", (chat,))
            self._conn.execute("DELETE FROM mails WHERE chat = ?", (chat,))
        self._write(run)

    def rebuild(self, records: Iterable) -> dict[str, str]:
        """Recreate the index from ledger records, given in any order.

        A record is ``(id, cited, chat, date[, subject])`` or a dict with those
        keys; ``cited`` a list or a space-separated string. Records are
        replayed oldest first (undated last), which is how a backfill feeds
        them, so the result does not depend on the order they are listed in.

        A record's ``chat`` — the key the ledger holds — is honoured: the
        index must agree with the records it is rebuilt from, and a key chosen
        under a different arrival order is still a correct key for the same
        thread. Records whose ledger keys the headers prove to be one thread
        (a merge a crash interrupted) are merged by the usual rule. A record
        without a ``chat`` is threaded as :meth:`resolve` would.

        Returns ``{ledger key: surviving key}`` for every ledger key that did
        not survive — empty for a consistent ledger — so the caller can
        re-key those records.
        """
        rows = []
        for record in records:
            rid, cited, chat, date, subject = _record_fields(record)
            mid = normalize_id(rid)
            if not mid:
                continue
            if isinstance(cited, str):
                cited = cited_ids(cited, "", mid)
            rows.append((mid, self._clean_cited(mid, cited),
                         (chat or "").strip() or None, to_epoch(date), subject))
        rows.sort(key=lambda r: (r[3] is None, r[3] or 0.0, r[0]))

        def run():
            self._conn.execute("DELETE FROM ids")
            self._conn.execute("DELETE FROM mails")
            # Every mail is put where the ledger has it before any header is
            # replayed. A record's date is its sender's claim, and replayed
            # first into an empty index, a backdated mail would take every
            # id it cites for its own chat — and the mails those ids name
            # would then be merged into it one by one, each merge within the
            # bound. With the mails in place, what a mail cites is counted
            # against the chats as they stand, as it was when it arrived.
            for mid, _cited, chat, date, subject in rows:
                if chat is not None:
                    self._conn.execute(
                        "INSERT OR IGNORE INTO ids (id, chat) VALUES (?, ?)", (mid, chat))
                    self._conn.execute(
                        "INSERT OR IGNORE INTO mails (id, chat, date, subject) "
                        "VALUES (?, ?, ?, ?)", (mid, chat, date, self._storable(subject)))
            for mid, cited, chat, date, subject in rows:
                self._resolve(mid, cited, date, subject, chat)
            moved: dict[str, str] = {}
            for mid, _cited, chat, _date, _subject in rows:
                if chat is None:
                    continue
                now = self._chat_of(mid)
                if now is not None and now != chat:
                    moved[chat] = now
            return moved
        return self._write(run)

    # -- reads -------------------------------------------------------------

    def chat_of(self, message_id) -> str | None:
        """The chat key an id (seen or only cited) belongs to, or None."""
        mid = normalize_id(message_id)
        if not mid:
            return None
        with self._lock:
            return self._chat_of(mid)

    def members(self, chat, include_cited: bool = False) -> list[str]:
        """The message ids of a chat, earliest first (undated last).

        By default only the mails actually recorded — the ones with ledger
        records. ``include_cited`` adds the ids that were only ever cited
        (parents not mirrored), after them.
        """
        with self._lock:
            mails = [r[0] for r in self._conn.execute(
                f"SELECT id FROM mails WHERE chat = ? {_ORDER}", (chat,))]
            if not include_cited:
                return mails
            known = set(mails)
            cited = [r[0] for r in self._conn.execute(
                "SELECT id FROM ids WHERE chat = ? ORDER BY id", (chat,))
                if r[0] not in known]
            return mails + cited

    def dates(self, chat) -> tuple[float | None, float | None]:
        """``(earliest, latest)`` mail date of a chat, epoch seconds."""
        with self._lock:
            row = self._conn.execute(
                "SELECT MIN(date), MAX(date) FROM mails WHERE chat = ?",
                (chat,)).fetchone()
        return row[0], row[1]

    def earliest(self, chat) -> float | None:
        return self.dates(chat)[0]

    def latest(self, chat) -> float | None:
        return self.dates(chat)[1]

    def date_of(self, message_id) -> float | None:
        """The recorded date of one mail, or None."""
        mid = normalize_id(message_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT date FROM mails WHERE id = ?", (mid,)).fetchone()
        return row[0] if row else None

    def subject_of(self, chat) -> str:
        """The chat's name: :func:`base_subject` of its earliest-dated mail.

        Mails whose subject strips to nothing (or that carried none) are
        skipped, so a bare "Re:" never names a thread another mail named.
        ``""`` when no mail has one. Derived on every call, so a merge or an
        older mail arriving late changes it without bookkeeping.
        """
        with self._lock:
            rows = self._conn.execute(
                f"SELECT subject FROM mails WHERE chat = ? AND subject IS NOT NULL "
                f"{_ORDER}", (chat,)).fetchall()
        for (subject,) in rows:
            name = base_subject(subject)
            if name:
                return name
        return ""

    def chats(self) -> list[str]:
        """Every chat key that has at least one recorded mail."""
        with self._lock:
            return [r[0] for r in self._conn.execute(
                "SELECT DISTINCT chat FROM mails ORDER BY chat")]

    # -- internals (caller holds the lock, inside a transaction) -----------

    @staticmethod
    def _clean_cited(mid: str, cited) -> list[str]:
        out: list[str] = []
        seen = {mid}
        for raw in cited or ():
            token = normalize_id(raw)
            if token and token not in seen:
                seen.add(token)
                out.append(token)
        return _bounded(out)

    @staticmethod
    def _storable(subject) -> str | None:
        """A subject as sqlite can hold it. A lone surrogate — a caller's
        decoder may leave one — would fail the whole statement."""
        if subject is None:
            return None
        return str(subject).encode("utf-8", "replace").decode("utf-8")

    def _chat_of(self, mid: str) -> str | None:
        row = self._conn.execute(
            "SELECT chat FROM ids WHERE id = ?", (mid,)).fetchone()
        return row[0] if row else None

    def _chat_exists(self, chat: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM ids WHERE chat = ? LIMIT 1", (chat,)).fetchone() is not None

    def _resolve(self, mid, cited, date, subject, prefer) -> Resolution:
        conn = self._conn
        existing = conn.execute(
            "SELECT chat FROM mails WHERE id = ?", (mid,)).fetchone()
        known: list[str] = []
        for token in [mid, *cited]:
            chat = self._chat_of(token)
            if chat is not None and chat not in known:
                known.append(chat)
        if prefer is not None and prefer not in known:
            known.append(prefer)

        merged: list[str] = []
        unmerged: tuple = ()
        if not known:
            survivor = cited[0] if cited else mid
        elif len(known) == 1:
            survivor = known[0]
        elif len(known) > MAX_MERGE_CHATS:
            # More chats than one mail may join: none is merged. The mail
            # stays where it is on record (a repeat; the ledger's key on a
            # rebuild), else goes where a reply to it already put it, else to
            # its nearest known parent.
            if existing is not None:
                survivor = existing[0]
            elif prefer is not None:
                survivor = prefer
            else:
                survivor = self._chat_of(mid) or next(
                    chat for chat in map(self._chat_of, reversed(cited)) if chat is not None)
            unmerged = tuple(sorted(c for c in known if c != survivor))
        else:
            def age(chat):
                first = conn.execute(
                    "SELECT MIN(date) FROM mails WHERE chat = ?", (chat,)).fetchone()[0]
                if first is None and not conn.execute(
                        "SELECT 1 FROM mails WHERE chat = ? LIMIT 1",
                        (chat,)).fetchone():
                    # A ledger key with no mail replayed yet holds only this one.
                    first = date
                return (math.inf if first is None else first, chat)
            survivor = min(known, key=age)
            merged = sorted(c for c in known if c != survivor)
            for chat in merged:
                conn.execute("UPDATE ids SET chat = ? WHERE chat = ?", (survivor, chat))
                conn.execute("UPDATE mails SET chat = ? WHERE chat = ?", (survivor, chat))

        new = not self._chat_exists(survivor)
        subject = self._storable(subject)
        conn.execute(
            "INSERT INTO ids (id, chat) VALUES (?, ?) "
            "ON CONFLICT (id) DO UPDATE SET chat = excluded.chat", (mid, survivor))
        # The cited ids follow the mail — unless it was refused a merge: then
        # an id that belongs to another chat stays that chat's.
        conn.executemany(
            "INSERT INTO ids (id, chat) VALUES (?, ?) ON CONFLICT (id) DO "
            + ("NOTHING" if unmerged else "UPDATE SET chat = excluded.chat"),
            [(token, survivor) for token in cited])
        if existing is None:
            conn.execute(
                "INSERT INTO mails (id, chat, date, subject) VALUES (?, ?, ?, ?)",
                (mid, survivor, date, subject))
        else:
            conn.execute(
                "UPDATE mails SET chat = ?, date = COALESCE(date, ?), "
                "subject = COALESCE(subject, ?) WHERE id = ?",
                (survivor, date, subject, mid))
        return Resolution(survivor, merged, new, existing is not None, unmerged)
