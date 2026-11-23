#!/usr/bin/env python3
"""In-container mail gateway — a mailbox mirrored into the message ledger.

E-mail gets the surface the messengers have (docs/mail-chats.md): a mail
thread is a chat, and this process is the channel gateway behind it — a
sibling of sms-gateway.py, one account per container, and the **single writer
of the mail ledger** on the ``messenger-email`` volume. It holds the mailbox
credentials (the variables ``email_client.Config`` reads, ``MAIL_ACCOUNT_NAME``
selecting a named account's suffixed set) and uses ``email_client.py`` as a
library.

    mailbox (IMAP) ──poll──▶ this gateway ──records, blobs──▶ the ledger
                                          └─events──▶ the web gateway (rail)

**This is the read-only mirror.** Folders are selected read-only and no flag
is ever stored; nothing is sent, nothing pushes, no model turn is started for
a mail, and e-mail triage keeps running beside it untouched. Writing (reply,
compose, the mailbox following the chat), the gate and the model trim are
later phases; where one of them will take over, the code says so.

**The mailbox is the source of what exists; the mirror follows it.** Three
folders are mirrored — INBOX, Sent and the archive folder:

  * a new mail in INBOX becomes a ``kb:InboundMessage``, a new mail in Sent a
    ``kb:OutboundMessage`` (the own-device echo, author ``device``, unless an
    outbound note names its author);
  * the archive folder is only *followed*: a mail that leaves INBOX is looked
    for there and is still that mail; nothing new is ever read from it;
  * there is ONE record per (account, Message-ID), however many folder copies
    exist — a mail to oneself, the list's copy of one's own post;
  * a mail that is in none of the three any more leaves a tombstone;
  * a client's ``\\Seen`` changes are ignored. Only the backfill reads the
    flag, once, so a mailbox of ninety days does not arrive as ninety days of
    unread chats.

How the poll stays cheap: a **mailbox index** (sqlite, on the state volume)
maps folder / UID ↔ Message-ID ↔ ledger record. New mail is the UIDs above a
folder's high-water mark; vanished mail is the difference between the indexed
UIDs and a UID SEARCH; a folder whose UIDVALIDITY changed has its mails found
again by Message-ID instead of being ingested a second time. The index is a
cache of the ledger and the mailbox: with the state volume lost it is rebuilt
from the ledger's records, and each mail's place is found again by Message-ID.

Nothing is reduced to text: the plain body, the HTML body and the header block
are kept as blobs of their own, every other part as an attachment under the
type and name its sender declared, and ``kb:text`` is the trimmed reading text
derived from them (``mail_trim``).

What it serves (token-gated like the other gateways, ``/health`` aside):
``GET /health`` for the gateway monitor, ``GET /media/<id>`` for the blobs,
``GET /recent-chats`` and ``GET /threads/<chat key>`` for a thread's name,
participants and counts, and ``POST /outbound-note`` so a mail an agent or the
user sent through ``email_client.py`` is not recorded as "some mail client".
The endpoints of the later phases (``/send``, ``/pending-sends``,
``/chats/delete|read|archive``) answer 501 with the reason.

What it tells the web gateway, over the chats rail: every mirrored mail once
(held, no handover — so nothing rings and no turn starts), a thread merge, and
a removal. Events wait in a queue on the state volume while the web gateway
is away; the ledger is the record either way.

Everything that speaks IMAP is confined to the "mailbox adapter" section; the
rest works on the adapter's small surface and is unit-tested against a fake
mailbox in tests/test_mail_gateway.py (the adapter itself, against a scripted
IMAP server, in tests/test_mail_gateway_imap.py).
"""
import email
import email.header
import email.message
import email.utils
import hashlib
import hmac
import imaplib
import json
import os
import re
import sqlite3
import ssl
import threading
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import NamedTuple
from urllib.parse import unquote

import email_client as _ec
import inbound_store as _ibstore
import mail_threads as _threads
import mail_trim as _trim
import triage_policy as _triage
import chat_ingest as _chats
import build_stamp as _build

CHANNEL = "email"
# Mail is the user's own correspondence; there is no control mode to run.
MAIL_GATEWAY_MODE = "inbox"
_MEDIA_REF = f"urn:retinue:media:{CHANNEL}:"


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        value = int(os.environ.get(name, "").strip() or default)
    except ValueError:
        print(f"[mail-gateway] warning: {name} is not a number; using {default}", flush=True)
        return default
    return max(minimum, value)


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        value = float(os.environ.get(name, "").strip() or default)
    except ValueError:
        print(f"[mail-gateway] warning: {name} is not a number; using {default}", flush=True)
        return default
    return max(minimum, value)


# ── The account ───────────────────────────────────────────────────────────────
# The mailbox variables are email_client's own (EMAIL_USER, EMAIL_PASS,
# IMAP_HOST, …); a named account reads the _<NAME>-suffixed set and never
# falls back to the unsuffixed one. They live ONLY in this container.
MAIL_ACCOUNT_NAME = os.environ.get("MAIL_ACCOUNT_NAME", "").strip()


def _load_config():
    """``(configured, Config | None, problem | None)`` for this container.

    Not configured means the account's address variable is absent: the
    deployment runs no mail mirror, which is a choice, not a fault. An address
    with the rest missing is a configuration error and is reported as one."""
    suffix = f"_{MAIL_ACCOUNT_NAME.upper()}" if MAIL_ACCOUNT_NAME else ""
    if not os.environ.get("EMAIL_USER" + suffix, "").strip():
        return False, None, f"EMAIL_USER{suffix} is not set"
    try:
        return True, _ec.Config(MAIL_ACCOUNT_NAME or None), None
    except (_ec.EmailError, ValueError) as exc:
        return True, None, str(exc)


CONFIGURED, MAIL_CONFIG, CONFIG_PROBLEM = _load_config()
# The account identity: the mailbox address, lowercased — what kb:account, the
# chat id and /health all carry, compared as a string everywhere.
ACCOUNT = MAIL_CONFIG.user.strip().lower() if MAIL_CONFIG else ""
# The user's own addresses on this mailbox: the account plus its aliases. A
# thread's participants are everyone else.
OWN_ADDRESSES = frozenset(
    a for a in [ACCOUNT, *(part.strip().lower() for part in
                           os.environ.get("MAIL_OWN_ADDRESSES", "").split(","))] if a)

MAIL_ARCHIVE_FOLDER = os.environ.get("MAIL_ARCHIVE_FOLDER", "").strip() or "Archive"
MAIL_TRASH_FOLDER = os.environ.get("MAIL_TRASH_FOLDER", "").strip() or "Trash"

MAIL_POLL_SECONDS = _env_float("MAIL_POLL_SECONDS", 60.0, 5.0)
MAIL_BACKFILL_DAYS = _env_int("MAIL_BACKFILL_DAYS", 90)
# Backfill is rate-polite: a batch of mails, a pause, the next batch — and the
# live poll keeps its own rhythm in between.
MAIL_BACKFILL_BATCH = _env_int("MAIL_BACKFILL_BATCH", 50, 1)
MAIL_BACKFILL_PAUSE_SECONDS = _env_float("MAIL_BACKFILL_PAUSE_SECONDS", 2.0, 0.1)
MAIL_IMAP_TIMEOUT = _env_float("MAIL_IMAP_TIMEOUT", 60.0, 5.0)

# Size discipline. An attachment above the part cap is not stored; a mail above
# the mail cap is fetched only up to it and keeps its text parts and no
# attachments. The text parts are always kept.
MAIL_MAX_PART_BYTES = _env_int("MAIL_MAX_PART_BYTES", 25 * 1024 * 1024, 1)
MAIL_MAX_MAIL_BYTES = _env_int("MAIL_MAX_MAIL_BYTES", 50 * 1024 * 1024, 64 * 1024)
MAX_HEADER_BYTES = 256 * 1024
# A chat key is a Message-ID, and a chat id carries it whole; the web gateway
# caps a chat id's length. An id longer than this is replaced by a digest of
# itself — consistently, wherever it is cited — so its thread still holds.
MAX_ID_CHARS = 240
MAX_RECIPIENTS = 500

# Persistent state of this gateway alone (the two indexes, the outbound
# notes), per account so a volume reused for another mailbox starts clean.
MAIL_DATA_DIR = Path(os.environ.get("MAIL_DATA_DIR", "/root/.local/share/mail"))
MAIL_STATE_DIR = MAIL_DATA_DIR / (re.sub(r"[^a-z0-9._@+-]", "_", ACCOUNT) or "unconfigured")
MAIL_RECENT_CHATS_MAX = _env_int("MAIL_RECENT_CHATS_MAX", 200, 1)
MAIL_NOTES_MAX = 2000

HTTP_PORT = _env_int("MAIL_GATEWAY_HTTP_PORT", 8096, 1)
GATEWAY_TOKEN = os.environ.get("MAIL_GATEWAY_TOKEN", "").strip()
MAX_BODY_BYTES = 64 * 1024

INBOUND_STORE_DIR = Path(os.environ.get("INBOUND_STORE_DIR", "/inbound"))
MAIL_POLICY_WAIT_SECONDS = _env_float("MAIL_POLICY_WAIT_SECONDS", 300.0)
MAIL_RAIL_TIMEOUT = _env_float("MAIL_RAIL_TIMEOUT", 10.0, 1.0)
# The rail is a freshness hint, the ledger is the record: an event carries at
# most this much of a mail's text, so one enormous mail cannot be refused by
# the web gateway's body limit.
MAIL_RAIL_TEXT_CHARS = 64 * 1024

# How many mails one poll looks for again (after a UIDVALIDITY change, a
# rebuilt index, or because they left their folder) before it yields.
RELOCATE_BATCH = 200
# A mail whose Message-ID the server cannot be searched for is looked for by
# reading the headers of at most this many candidates.
SCAN_MAX = 100
HEADER_PEEK_BYTES = 64 * 1024


def _policy_path() -> Path:
    """The e-mail policy file (group flags), read raw off the ledger volume.

    The retinue side writes it into the mail ledger's own volume, which this
    container mounts at INBOUND_STORE_DIR. ``triage_policy`` is asked wherever
    the deployment tells it where the file is; with neither variable set it
    would answer the retinue-side path, which does not exist in this container,
    so the gateway's own mount is the default."""
    if os.environ.get("TRIAGE_EMAIL_WHITELIST_PATH") or os.environ.get("TRIAGE_MESSENGER_DIR"):
        return Path(_triage.email_whitelist_path())
    return INBOUND_STORE_DIR / "policy" / "email-policy.nt"


POLICY_PATH = _policy_path()


# ── Link-state tracking ───────────────────────────────────────────────────────
# Written by the mirror loop, read by /health: the probe itself never touches
# the mailbox.
_STATE_LOCK = threading.Lock()
_state: dict = {
    "connected": False,
    "error": None,      # why the last login or poll failed
    "notice": None,     # something an operator should know while it works
    "last_poll": None,  # epoch of the last poll that went through
}
MIRROR = None  # the Mirror, once main() built it


def _set_state(**changes) -> None:
    with _STATE_LOCK:
        _state.update(changes)


def _health_snapshot() -> dict:
    with _STATE_LOCK:
        state = dict(_state)
    usable = CONFIGURED and MAIL_CONFIG is not None
    connected = bool(usable and state["connected"])
    if not usable:
        error = CONFIG_PROBLEM
    elif not connected:
        error = state["error"] or "the mailbox has not been polled yet"
    else:
        error = state["notice"]
    backfill = (MIRROR.backfill_status() if MIRROR is not None
                else {"done": False, "pending": None, "days": MAIL_BACKFILL_DAYS})
    return {
        "status": "ok",
        "configured": CONFIGURED,
        # Routing identity for the chat surface (see telegram-gateway.py).
        "mode": MAIL_GATEWAY_MODE,
        "account": ACCOUNT or None,
        "connected": connected,
        # A mailbox is not paired with a QR code; a failed login is fixed in
        # the environment, and "error" says what to fix.
        "needs_repair": False,
        "error": error,
        "read_only": True,
        "backfill": backfill,
        "last_poll": state["last_poll"],
        "build": _build.build_info(),
    }


# ── Reading a mail ────────────────────────────────────────────────────────────
# A mail is text from outside, every header of it. What is read here ends up
# in records, in an index and in JSON — never in a path, a header line or a
# prompt — and it is still reduced to the shape its field has: an id is an id,
# an address an address, a name one line without control characters.

_SYNTHETIC_DOMAIN = "no-message-id.mail.invalid"
_OVERLONG_DOMAIN = "overlong-message-id.mail.invalid"
# Control characters, line and paragraph separators, and the bidirectional
# embedding / override / isolate controls, which let a name read as something
# other than what it is.
_UNSAFE_LINE_RE = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]")
_ATOM = r"[^\s<>()\[\]\\,;:\"@\x00-\x1f\x7f]"
_ADDRESS_RE = re.compile(_ATOM + r"{1,64}@" + _ATOM + r"{1,255}")
_UNFOLD_RE = re.compile(r"\r?\n(?=[ \t])")


@dataclass
class Part:
    """One part of a mail that is neither its plain nor its HTML body."""
    data: bytes
    content_type: str          # as the sender declared it; cleaned where it is stored
    file_name: str | None      # likewise
    content_id: str | None
    inline_image: bool         # an image the sender did not mark as an attachment


@dataclass
class Mail:
    """What the mirror reads out of one mail (see :func:`parse_mail`)."""
    message_id: str
    subject: str = ""
    sender: str = ""                        # the From address, lowercased; "" when unreadable
    sender_name: str | None = None
    to: list = field(default_factory=list)  # [(address, display name | None)]
    cc: list = field(default_factory=list)
    in_reply_to: str | None = None
    references: list = field(default_factory=list)
    cited: list = field(default_factory=list)
    list_id: str = ""                       # the raw List-Id header
    date: float | None = None
    headers: str = ""
    plain: str | None = None
    html: str | None = None
    parts: list = field(default_factory=list)


def _decode_bytes(data: bytes, charset: str | None) -> str:
    """Bytes to text under a declared charset that may be wrong or unknown."""
    if charset:
        try:
            return data.decode(charset, errors="replace")
        except (LookupError, ValueError):
            pass
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _raw_header(msg, name: str) -> str:
    """A header's value as it stands, folds and encoded words included."""
    value = msg.get(name)
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    # The parser hands back a Header object for a value with raw 8-bit bytes.
    try:
        return "".join(
            _decode_bytes(chunk, None if str(cs or "") in ("", "unknown-8bit") else str(cs))
            if isinstance(chunk, bytes) else chunk
            for chunk, cs in email.header.decode_header(value))
    except Exception:  # noqa: BLE001 - a header that cannot be read is an empty one
        return ""


_ENCODED_WORD_RE = re.compile(r"=\?[^?\s]+\?[bBqQ]\?[^?\s]*\?=")
_BETWEEN_WORDS_RE = re.compile(r"(\?=)[ \t]+(?==\?[^?\s]+\?[bBqQ]\?)")


def _decode_word(match) -> str:
    try:
        ((chunk, charset),) = email.header.decode_header(match.group(0))
    except Exception:  # noqa: BLE001 - not base64 after all: the word stays as written
        return match.group(0)
    return _decode_bytes(chunk, charset) if isinstance(chunk, bytes) else chunk


def _decode_words(value: str) -> str:
    """RFC 2047 encoded words decoded; never raises on a malformed one."""
    try:
        return _ec._decode(value) or ""
    except Exception:  # noqa: BLE001 - an unknown charset, a broken base64 word
        pass
    # Word by word, so one that cannot be read does not cost the others.
    return _ENCODED_WORD_RE.sub(_decode_word, _BETWEEN_WORDS_RE.sub(r"\1", value))


def _clean_line(text, limit: int) -> str:
    """One line of display text: no control or direction-override characters,
    whitespace collapsed, capped."""
    text = _UNSAFE_LINE_RE.sub(" ", str(text or ""))
    return " ".join(text.split())[:limit].strip()


def _header_text(msg, name: str, limit: int = 998) -> str:
    return _clean_line(_decode_words(_UNFOLD_RE.sub("", _raw_header(msg, name))), limit)


def _addresses(msg, name: str) -> list:
    """``[(address, display name | None)]`` of an address header, in order.

    The header is split into addresses before any encoded word is decoded: a
    decoded display name may hold commas and quotes of its own. An entry whose
    address does not read as one is dropped."""
    raw = _UNFOLD_RE.sub("", _raw_header(msg, name))
    if not raw.strip():
        return []
    try:
        pairs = email.utils.getaddresses([raw])
    except Exception:  # noqa: BLE001
        return []
    out, seen = [], set()
    for display, address in pairs:
        address = (address or "").strip().lower()
        if not _ADDRESS_RE.fullmatch(address) or address in seen:
            continue
        seen.add(address)
        display = _clean_line(_decode_words(display or ""), 200)
        out.append((address, display if display and display.lower() != address else None))
        if len(out) >= MAX_RECIPIENTS:
            break
    return out


def _fit_id(message_id: str) -> str:
    """A Message-ID as the mirror keys it: itself, or a digest when overlong."""
    if len(message_id) <= MAX_ID_CHARS:
        return message_id
    digest = hashlib.sha256(message_id.encode("utf-8", "surrogatepass")).hexdigest()[:32]
    return f"{digest}@{_OVERLONG_DOMAIN}"


def _searchable(message_id: str) -> bool:
    """Whether the server can be asked for a mail by this id (HEADER search).

    Not a synthetic or digested id — no header carries those — and not one
    the search command cannot spell."""
    return (message_id.isascii() and "\\" not in message_id
            and not message_id.endswith(("@" + _SYNTHETIC_DOMAIN, "@" + _OVERLONG_DOMAIN)))


def _identify(msg) -> str:
    """The id a mail is mirrored under: its Message-ID without brackets.

    A mail with none (or an unusable one) gets a stable synthetic id derived
    from the headers every copy of it shares, so the copy in INBOX and the
    copy in Sent are still one mail and a re-read never makes a second."""
    message_id = _threads.normalize_id(_raw_header(msg, "Message-ID"))
    if message_id:
        return _fit_id(message_id)
    basis = "\n".join(_raw_header(msg, name) for name in
                      ("Date", "From", "To", "Cc", "Subject", "In-Reply-To", "References"))
    digest = hashlib.sha256(basis.encode("utf-8", "surrogatepass")).hexdigest()[:32]
    return f"{digest}@{_SYNTHETIC_DOMAIN}"


def _file_name(part) -> str | None:
    """The file name a part declares, decoded; None when it declares none.

    Read from the header values as :func:`_raw_header` gives them, because
    clients do write raw UTF-8 names and the parser would hand those back as
    replacement characters. The result is shown as a name, so no control
    character and no direction override may ride in it; store_media reduces it
    to a base name on top of that."""
    probe = email.message.Message()
    for header in ("Content-Disposition", "Content-Type"):
        value = _raw_header(part, header)
        if value:
            probe[header] = value
    try:
        name = probe.get_filename()
    except Exception:  # noqa: BLE001 - malformed parameters
        return None
    return _UNSAFE_LINE_RE.sub("", _decode_words(str(name or ""))) or None


def _part_bytes(part) -> bytes:
    try:
        data = part.get_payload(decode=True)
    except Exception:  # noqa: BLE001 - a broken transfer encoding
        data = None
    if isinstance(data, bytes):
        return data
    payload = part.get_payload()
    return payload.encode("utf-8", "surrogateescape") if isinstance(payload, str) else b""


def _collect(part, mail: Mail, texts: dict, depth: int = 0) -> None:
    """Walk the MIME tree, keeping every leaf.

    Unattached text/plain and text/html leaves are the mail's two bodies (more
    than one of a kind — a list's footer part, Apple Mail's HTML in pieces —
    are joined in order); every other leaf is a part of its own. A nested
    message is one part, kept whole: its own bodies are not this mail's."""
    if depth > 40:
        return
    payload = part.get_payload()
    maintype = part.get_content_maintype()
    if maintype == "multipart" and isinstance(payload, list):
        for sub in payload:
            _collect(sub, mail, texts, depth + 1)
        return
    content_type = part.get_content_type()
    try:
        disposition = (part.get_content_disposition() or "").lower()
    except Exception:  # noqa: BLE001 - malformed parameters
        disposition = ""
    file_name = _file_name(part)
    if maintype == "message" and isinstance(payload, list):
        try:
            data = b"".join(sub.as_bytes() for sub in payload)
        except Exception:  # noqa: BLE001 - a nested mail the generator cannot write back
            data = b""
        mail.parts.append(Part(data, content_type, file_name, None, False))
        return
    data = _part_bytes(part)
    try:
        attached = _ec._is_attachment(part)
    except Exception:  # noqa: BLE001
        attached = disposition == "attachment"
    # A multipart with no parts in it (no boundary found, or cut off) is its
    # raw body: text, as far as anyone can tell.
    kind = "text/plain" if maintype == "multipart" else content_type
    if kind in ("text/plain", "text/html") and not attached:
        text = _decode_bytes(data, part.get_content_charset()).replace("\r\n", "\n")
        texts[kind].append(text)
        return
    content_id = _threads.normalize_id(_raw_header(part, "Content-ID")) or None
    mail.parts.append(Part(
        data, content_type, file_name, content_id,
        inline_image=maintype == "image" and disposition != "attachment"))


def parse_mail(raw: bytes, internal_date: float | None = None) -> Mail:
    """Read one raw mail into what the mirror records.

    Parsed with the lenient legacy policy on purpose: it never raises on a
    malformed header, and every value is decoded here, defensively, instead.
    ``internal_date`` is when the server received the mail; the mail's own
    Date header is the sender's claim, so it is used only where it is not
    later than that — a mail cannot have been written after it arrived."""
    msg = email.message_from_bytes(raw)
    message_id = _identify(msg)
    mail = Mail(message_id=message_id)
    mail.subject = _header_text(msg, "Subject")
    sender = _addresses(msg, "From") or _addresses(msg, "Sender")
    if sender:
        mail.sender, mail.sender_name = sender[0]
    mail.to = _addresses(msg, "To")
    mail.cc = _addresses(msg, "Cc")
    raw_references = _raw_header(msg, "References")
    raw_in_reply_to = _raw_header(msg, "In-Reply-To")

    def ids(references, in_reply_to):
        return list(dict.fromkeys(
            fitted for fitted in map(_fit_id, _threads.cited_ids(references, in_reply_to))
            if fitted != message_id))

    mail.references = ids(raw_references, "")
    mail.in_reply_to = next(iter(ids("", raw_in_reply_to)), None)
    mail.cited = ids(raw_references, raw_in_reply_to)
    mail.list_id = _clean_line(_UNFOLD_RE.sub("", _raw_header(msg, "List-Id")), 998)
    written = _threads.to_epoch(_UNFOLD_RE.sub("", _raw_header(msg, "Date")).strip())
    if written is not None and written < 0:
        written = None      # before the epoch: no mail was
    if written is not None and internal_date is not None:
        written = min(written, internal_date)
    mail.date = written if written is not None else internal_date
    end = re.search(rb"\r?\n\r?\n", raw)
    block = raw[:end.start()] if end else raw
    mail.headers = block[:MAX_HEADER_BYTES].decode("utf-8", errors="replace").replace("\r\n", "\n")
    texts: dict = {"text/plain": [], "text/html": []}
    _collect(msg, mail, texts)
    if texts["text/plain"]:
        mail.plain = "\n\n".join(texts["text/plain"])
    if texts["text/html"]:
        mail.html = "\n".join(texts["text/html"])
    return mail


def _human_size(count: int) -> str:
    size = float(max(0, count))
    if size < 1000:
        return f"{int(size)} bytes"
    for unit in ("kB", "MB"):
        size /= 1000
        if size < 1000:
            return f"{size:.1f} {unit}"
    return f"{size / 1000:.1f} GB"


def _not_mirrored_note(part: Part) -> tuple[bytes, str]:
    """What stands in for an attachment above the part cap: a small text file
    under the attachment's own name that says so. The bubble shows it like any
    file, so the gap is visible without the reader knowing a new shape."""
    name = _ibstore.safe_file_name(part.file_name) or "attachment"
    text = ("This attachment was not mirrored: it is larger than the mirror keeps "
            f"({_human_size(MAIL_MAX_PART_BYTES)}).\n\n"
            f"Name: {name}\n"
            f"Type: {_ibstore.clean_content_type(part.content_type)}\n"
            f"Size: {_human_size(len(part.data))}\n\n"
            "The mail in the mailbox still carries it.\n")
    return text.encode("utf-8"), f"{name}.not-mirrored.txt"


def _oversized_mail_note(size: int, parts: list) -> tuple[bytes, str]:
    """The same for a mail above the mail cap, whose attachments are all left out."""
    names = [_ibstore.safe_file_name(p.file_name) for p in parts]
    listed = "".join(f"  {name}\n" for name in [n for n in names if n][:20])
    text = ("The attachments of this mail were not mirrored: the mail "
            f"({_human_size(size)}) is larger than the mirror keeps "
            f"({_human_size(MAIL_MAX_MAIL_BYTES)}). Its text is complete.\n\n"
            + (f"Attachments seen:\n{listed}\n" if listed else "")
            + "The mail in the mailbox still carries them.\n")
    return text.encode("utf-8"), "attachments.not-mirrored.txt"


# ══════════════════════════════════════════════════════════════════════════════
# Mailbox adapter — the ONLY section that speaks IMAP. Everything outside it
# works on this surface, which the tests replace with a fake mailbox:
#
#   folders()                       the mirrored folders' names, by role
#   list_uids(folder, …)            a folder's UIDVALIDITY, UIDNEXT and UIDs
#   fetch(folder, uid, limit)       one mail's raw bytes, flags and arrival time
#   find(folder, message_id)        the UID of a mail, by Message-ID
#   ensure() / close()              the connection's life
#   set_seen() / move()             the next phase's seams; refused here
# ══════════════════════════════════════════════════════════════════════════════

class MailboxError(Exception):
    """The mailbox could not be reached or refused a command. The message is
    what /health reports, so it is written for the operator."""


class Listing(NamedTuple):
    """One look at a folder. ``uids`` maps each UID to the mail's arrival time
    (INTERNALDATE, epoch seconds) when dates were asked for, else to None."""
    uidvalidity: int
    uidnext: int | None
    uids: dict


class Fetched(NamedTuple):
    """One mail. ``raw`` is cut at the fetch limit when the mail is larger;
    ``size`` is the whole mail's, so the caller can tell."""
    raw: bytes
    size: int
    flags: frozenset
    date: float | None


_LIST_HEAD_RE = re.compile(rb"^\((?P<flags>[^)]*)\)\s+(?:\"(?:[^\"\\]|\\.)*\"|NIL|\S+)\s*(?P<name>.*)$",
                           re.DOTALL)
_UID_RE = re.compile(rb"\bUID (\d+)")
_SIZE_RE = re.compile(rb"\bRFC822\.SIZE (\d+)")
_RESPONSE_START_RE = re.compile(rb"^\d+ \(")


def _parse_list_entry(item) -> tuple[set, str] | None:
    """``(attributes, folder name)`` of one LIST response item, or None."""
    literal = None
    if isinstance(item, tuple):          # the name came as a literal
        item, literal = item[0], item[1]
    if not isinstance(item, (bytes, bytearray)):
        return None
    m = _LIST_HEAD_RE.match(bytes(item).strip())
    if not m:
        return None
    name = literal if literal is not None else m.group("name").strip()
    if literal is None and name.startswith(b'"') and name.endswith(b'"') and len(name) >= 2:
        name = re.sub(rb"\\(.)", rb"\1", name[1:-1])
    text = name.decode("utf-8", errors="replace")
    try:
        text = _ec.imap_utf7_decode(text)
    except Exception:  # noqa: BLE001 - not modified UTF-7 after all
        pass
    flags = {f.lower() for f in m.group("flags").decode("ascii", errors="replace").split()}
    return flags, text


def resolve_folders(entries, sent: str, archive: str, trash: str) -> dict:
    """Which folder plays each role: ``{inbox, sent, archive, trash}``, a name
    or None each.

    The server's own SPECIAL-USE attributes (RFC 6154) are honoured first —
    they survive a localised or renamed folder — and the configured names are
    the fallback. For the archive, ``\\All`` (Gmail's "All Mail", which is where
    an archived mail lives there) stands in when nothing is ``\\Archive``."""
    usable = [(flags, name) for flags, name in entries
              if "\\noselect" not in flags and "\\nonexistent" not in flags]

    def special(*attributes):
        for attribute in attributes:
            for flags, name in usable:
                if attribute in flags:
                    return name
        return None

    def named(wanted):
        names = [name for _flags, name in usable]
        if wanted in names:
            return wanted
        return next((n for n in names if n.casefold() == wanted.casefold()), None)

    return {
        "inbox": named("INBOX") or "INBOX",
        "sent": special("\\sent") or named(sent),
        "archive": special("\\archive", "\\all") or named(archive),
        "trash": special("\\trash") or named(trash),
    }


def _imap_date(epoch: float) -> str:
    """IMAP's DD-Mon-YYYY for SINCE, a day early: the server compares dates in
    its own zone, and a mail too many costs nothing where one too few is lost."""
    day = datetime.fromtimestamp(max(0.0, epoch - 86400), tz=timezone.utc)
    return f"{day.day:02d}-{_ec._MONTHS[day.month - 1]}-{day.year}"


def _internal_date(envelope: bytes) -> float | None:
    try:
        parsed = imaplib.Internaldate2tuple(envelope)
        return time.mktime(parsed) if parsed else None
    except (ValueError, OverflowError):
        return None


class Mailbox:
    """The IMAP side of one account, read-only.

    email_client.py logs in once per command; this is the first long-running
    IMAP client here, so the connection's life is handled in one place: every
    command runs through :meth:`_run`, which connects on demand, and on a
    dropped or timed-out connection reconnects and tries once more — servers
    close an idle session after half an hour, and that is not an outage.
    Folders are opened with EXAMINE, never SELECT, and bodies are read with
    BODY.PEEK: this phase cannot change a flag even by accident."""

    def __init__(self, cfg, timeout: float = MAIL_IMAP_TIMEOUT):
        self._cfg = cfg
        self._timeout = timeout
        self._conn = None
        self._selected = None
        self._lock = threading.RLock()

    # -- the connection --------------------------------------------------------

    def _connect(self):
        cfg = self._cfg
        try:
            # Like email_client.imap_connect, with the one thing a process that
            # never exits needs: a socket timeout, so a dead peer is an error
            # and not a poll thread asleep for good.
            conn = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port,
                                     ssl_context=ssl.create_default_context(),
                                     timeout=self._timeout)
        except (OSError, imaplib.IMAP4.error) as exc:
            raise MailboxError(
                f"cannot reach the IMAP server {cfg.imap_host}:{cfg.imap_port}: {exc}") from exc
        try:
            conn.login(cfg.user, cfg.password)
        except imaplib.IMAP4.error as exc:
            try:
                conn.shutdown()
            except Exception:  # noqa: BLE001
                pass
            raise MailboxError(
                f"IMAP login failed for {cfg.user}: {exc} — check the address and "
                f"EMAIL_PASS (an app password, where the provider issues them)") from exc
        except OSError as exc:
            raise MailboxError(f"the IMAP connection failed during login: {exc}") from exc
        return conn

    def close(self) -> None:
        with self._lock:
            conn, self._conn, self._selected = self._conn, None, None
        if conn is not None:
            try:
                conn.shutdown()
            except Exception:  # noqa: BLE001 - it is being dropped either way
                pass

    def _run(self, command):
        with self._lock:
            for attempt in (1, 2):
                if self._conn is None:
                    self._conn = self._connect()
                    self._selected = None
                try:
                    return command(self._conn)
                except MailboxError:
                    raise
                except (imaplib.IMAP4.abort, OSError) as exc:
                    # The connection is gone or in an unknown state (a timeout
                    # mid-answer leaves half a response behind): drop it.
                    self.close()
                    if attempt == 2:
                        raise MailboxError(f"the IMAP connection failed: {exc}") from exc
                except imaplib.IMAP4.error as exc:
                    # The server answered and said no; the connection is fine.
                    raise MailboxError(f"the IMAP server refused a command: {exc}") from exc

    def ensure(self) -> None:
        """Make sure the mailbox answers (NOOP), reconnecting when it does not."""
        self._run(lambda conn: conn.noop())

    # -- reading ---------------------------------------------------------------

    def _examine(self, conn, folder: str, fresh: bool = False) -> bool:
        """Open ``folder`` read-only; False when the server has no such folder."""
        if self._selected == folder and not fresh:
            return True
        self._selected = None
        typ, _data = conn.select(_ec._quote(folder), readonly=True)
        if typ != "OK":
            return False
        self._selected = folder
        return True

    @staticmethod
    def _number(conn, name: str) -> int | None:
        _typ, data = conn.response(name)
        try:
            return int(data[0]) if data and data[0] is not None else None
        except (ValueError, TypeError):
            return None

    def folders(self) -> dict:
        def command(conn):
            typ, data = conn.list()
            if typ != "OK":
                raise MailboxError(f"the IMAP server refused LIST: {data}")
            entries = [entry for entry in map(_parse_list_entry, data or []) if entry]
            return resolve_folders(entries, self._cfg.sent_folder,
                                   MAIL_ARCHIVE_FOLDER, MAIL_TRASH_FOLDER)
        return self._run(command)

    def list_uids(self, folder: str, *, since: float | None = None,
                  first_uid: int | None = None, dates: bool = False) -> Listing | None:
        """The folder's UIDVALIDITY, UIDNEXT and UIDs — all of them, or those
        that arrived on or after ``since`` (epoch), or those from ``first_uid``
        up. None when the folder does not exist."""
        def command(conn):
            # Opened afresh every time: UIDVALIDITY is only announced then.
            if not self._examine(conn, folder, fresh=True):
                return None
            validity = self._number(conn, "UIDVALIDITY")
            uidnext = self._number(conn, "UIDNEXT")
            if validity is None:
                raise MailboxError(f"the IMAP server gave no UIDVALIDITY for {folder!r}")
            criteria = []
            if since is not None:
                criteria += ["SINCE", _imap_date(since)]
            if first_uid:
                criteria += ["UID", f"{int(first_uid)}:*"]
            typ, data = conn.uid("search", None, *(criteria or ["ALL"]))
            if typ != "OK":
                raise MailboxError(f"the IMAP server refused a search of {folder!r}: {data}")
            uids = sorted({int(token) for chunk in data or [] if chunk
                           for token in chunk.split()})
            # "n:*" always matches the newest mail, even one below n.
            uids = [uid for uid in uids if not first_uid or uid >= first_uid]
            found = dict.fromkeys(uids)
            for start in range(0, len(uids) if dates else 0, 500):
                chunk = ",".join(str(uid) for uid in uids[start:start + 500])
                typ, data = conn.uid("fetch", chunk, "(INTERNALDATE)")
                if typ != "OK":
                    continue  # dates only order the backfill; the UIDs stand
                for item in data or []:
                    line = item[0] if isinstance(item, tuple) else item
                    m = _UID_RE.search(line or b"")
                    if m and int(m.group(1)) in found:
                        found[int(m.group(1))] = _internal_date(line)
            return Listing(validity, uidnext, found)
        return self._run(command)

    def fetch(self, folder: str, uid: int, limit: int | None = None) -> Fetched | None:
        """One mail by UID, at most ``limit`` bytes of it. None when it is not
        there (expunged since it was listed)."""
        def command(conn):
            if not self._examine(conn, folder):
                return None
            body = "BODY.PEEK[]" + (f"<0.{int(limit)}>" if limit else "")
            typ, data = conn.uid("fetch", str(int(uid)),
                                 f"(UID FLAGS INTERNALDATE RFC822.SIZE {body})")
            if typ != "OK":
                raise MailboxError(f"the IMAP server refused to fetch a mail from {folder!r}: {data}")
            raw, envelope, tail_due = None, b"", False
            for item in data or []:
                if isinstance(item, tuple):
                    m = _UID_RE.search(item[0] or b"")
                    if m and int(m.group(1)) != int(uid):
                        tail_due = False
                        continue  # an unsolicited update about another mail
                    envelope, raw, tail_due = item[0] or b"", item[1], True
                elif tail_due and isinstance(item, (bytes, bytearray)):
                    # What followed the literal: ")" or, on some servers, the
                    # FLAGS. Anything that opens a response of its own is an
                    # unsolicited update, and its flags are another mail's.
                    if not _RESPONSE_START_RE.match(item):
                        envelope += b" " + bytes(item)
                    tail_due = False
            if raw is None:
                return None
            size = _SIZE_RE.search(envelope)
            flags = frozenset(flag.decode("ascii", errors="replace")
                              for flag in imaplib.ParseFlags(envelope))
            return Fetched(bytes(raw), int(size.group(1)) if size else len(raw),
                           flags, _internal_date(envelope))
        return self._run(command)

    def find(self, folder: str, message_id: str) -> int | None:
        """The UID of the mail with this Message-ID in ``folder``, or None.

        The server's HEADER search matches substrings, so each candidate's own
        header is read back and compared whole; the newest match wins."""
        def command(conn):
            if not message_id or not self._examine(conn, folder):
                return None
            typ, data = conn.uid("search", None, "HEADER", "Message-ID",
                                 _ec._quote_mid(message_id))
            if typ != "OK":
                raise MailboxError(f"the IMAP server refused a search of {folder!r}: {data}")
            candidates = sorted({int(token) for chunk in data or [] if chunk
                                 for token in chunk.split()}, reverse=True)
            for uid in candidates[:20]:
                typ, data = conn.uid("fetch", str(uid),
                                     "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
                if typ != "OK":
                    continue
                for item in data or []:
                    if not isinstance(item, tuple):
                        continue
                    header = email.message_from_bytes(item[1] or b"")
                    if _threads.normalize_id(_raw_header(header, "Message-ID")) == message_id:
                        return uid
            return None
        return self._run(command)

    # -- the next phase's seams ------------------------------------------------
    # The mailbox follows the chat from phase 2 on: a reply or "mark as read"
    # sets \Seen, archiving moves a thread's INBOX mails to the archive folder,
    # deleting moves them to Trash. Their place is here, in the adapter, and
    # until then they refuse — nothing in this phase may write to the mailbox.

    def set_seen(self, folder: str, uids) -> None:
        raise MailboxError("the mail mirror is read-only")

    def move(self, folder: str, uid: int, to_folder: str) -> int | None:
        raise MailboxError("the mail mirror is read-only")


# ══════════════════════════════════════════════════════════════════════════════
# End mailbox adapter. Everything below is IMAP-agnostic.
# ══════════════════════════════════════════════════════════════════════════════

# ── The mailbox index ─────────────────────────────────────────────────────────
# Where each mail lives, which record mirrors it, and what is still owed to the
# rail. A cache: the ledger says what was mirrored and the mailbox what exists,
# and Mirror._prepare rebuilds this from the first and finds the second again.
#
#   folders    one row per mirrored role: its name, UIDVALIDITY, a generation
#              that is bumped whenever the UIDs stop meaning what they meant,
#              the backfill ceiling (UIDs at or below it are old mail) and the
#              live high-water mark (every UID up to it has been looked at).
#   locations  (role, generation, UID) → Message-ID. `skipped` says why a UID
#              has no record ("news", "unreadable"), so it is not read again.
#              Rows of an older generation are stale: mails to find again.
#   mails      one row per mirrored mail: its record file, direction, chat and
#              the facts a Thread answer needs. An empty `record` is a claim —
#              the ledger write was begun and not confirmed.
#   notes      who authored a mail that will turn up in Sent (/outbound-note).
#   outbox     rail events not yet taken by the web gateway, in order.

INDEX_SCHEMA_VERSION = 1
_INDEX_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS folders (
    role TEXT PRIMARY KEY, name TEXT NOT NULL, uidvalidity INTEGER NOT NULL,
    gen INTEGER NOT NULL, ceiling INTEGER NOT NULL, hwm INTEGER NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS locations (
    role TEXT NOT NULL, gen INTEGER NOT NULL, uid INTEGER NOT NULL,
    message_id TEXT, skipped TEXT,
    PRIMARY KEY (role, gen, uid)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS locations_mid ON locations (message_id);
CREATE TABLE IF NOT EXISTS mails (
    message_id TEXT PRIMARY KEY, record TEXT NOT NULL, direction TEXT NOT NULL,
    chat TEXT NOT NULL, ts REAL NOT NULL, sender TEXT, sender_name TEXT,
    recipients TEXT, cc TEXT, removed INTEGER NOT NULL DEFAULT 0
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS mails_chat ON mails (chat, ts);
CREATE TABLE IF NOT EXISTS notes (
    message_id TEXT PRIMARY KEY, author TEXT NOT NULL, created REAL NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS outbox (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, payload TEXT NOT NULL
);
"""


class MailboxIndex:
    """The sqlite file behind the mirror: one connection, one lock, shared by
    the poll thread (the only writer of everything but notes) and the HTTP
    threads. Autocommit; a write that must be atomic is one statement."""

    def __init__(self, path):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False,
                                     isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_INDEX_SCHEMA)
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                self._conn.execute(f"PRAGMA user_version = {INDEX_SCHEMA_VERSION}")
            elif version != INDEX_SCHEMA_VERSION:
                raise RuntimeError(f"mailbox index {path}: schema {version}, "
                                   f"expected {INDEX_SCHEMA_VERSION}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def run(self, sql: str, args=()) -> int:
        with self._lock:
            return self._conn.execute(sql, args).rowcount

    def rows(self, sql: str, args=()) -> list:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    def one(self, sql: str, args=()):
        with self._lock:
            return self._conn.execute(sql, args).fetchone()

    def get(self, key: str, default=None):
        row = self.one("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else default

    def set(self, key: str, value) -> None:
        self.run("INSERT INTO meta (key, value) VALUES (?, ?) "
                 "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, str(value)))


# ── The rail ──────────────────────────────────────────────────────────────────
# Three events go to the web gateway: a mirrored mail (the existing
# /internal/chats/inbound, with mail's optional fields), a thread merge and a
# removal. They are posted here rather than through chat_ingest.notify_chat_event
# because that function's field list is fixed and its answer says nothing about
# why a call failed — and a queue that must not lose an event needs to know
# whether to retry it. The URL and the token are chat_ingest's.

def _rail_url(kind: str) -> str:
    base = _chats.CHATS_INGEST_URL
    if kind == "inbound":
        return base
    head, _slash, _last = base.rstrip("/").rpartition("/")
    return f"{head}/{kind}"


def _rail_send(kind: str, payload: dict) -> str:
    """POST one event. ``"ok"`` — taken; ``"drop"`` — refused as malformed, a
    repeat would be refused again; ``"later"`` — this event cannot be taken
    yet, others may; ``"down"`` — the web gateway is not answering."""
    request = urllib.request.Request(
        _rail_url(kind), data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json",
                 "X-Conversation-Backend-Token": _chats.CHATS_INGEST_TOKEN})
    try:
        with urllib.request.urlopen(request, timeout=MAIL_RAIL_TIMEOUT) as response:
            response.read()
        return "ok"
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            print(f"[mail-gateway] the chats rail refused the token (HTTP {exc.code}); "
                  f"check CHATS_INGEST_TOKEN on both sides", flush=True)
            return "down"
        if exc.code in (404, 408, 429) or exc.code >= 500:
            # A merge or removal an older web gateway does not know yet must
            # not hold the mails behind it back.
            return "down" if kind == "inbound" else "later"
        print(f"[mail-gateway] the chats rail refused a {kind} event "
              f"(HTTP {exc.code}); dropped", flush=True)
        return "drop"
    except Exception as exc:  # noqa: BLE001 - unreachable, timed out
        print(f"[mail-gateway] the chats rail did not answer ({exc}); will retry", flush=True)
        return "down"


# The verdict every mirrored mail is offered with in this phase: held, nobody's
# VIP, nobody's instruction — so the rail pushes nothing and starts no turn.
MIRROR_GATE = {"forward": False, "vip": False, "instructed": False, "reason": "mirror"}


def _delivered_on_write(direction: str) -> bool:
    """Whether a mail's record starts out ``delivered``.

    THE read-only phase's handover decision, in one place: every record is
    written delivered, because nothing is owed for it — e-mail triage still
    owns the handling of mail, and the rail is offered each mail without a
    handover. The gate phase replaces this with the messengers' flow: persist
    ``delivered: false``, offer the rail a handover, and flip the flag only on
    its acceptance (docs/mail-chats.md, "The gate")."""
    return True


# ── The mirror ────────────────────────────────────────────────────────────────

class Mirror:
    """One account's mailbox mirrored into the ledger.

    ``poll`` is one pass: follow the folders (new mail, vanished mail), look
    again for mails that lost their place, backfill a batch, and hand the rail
    what it is owed. Everything it needs to resume is in the two indexes; a
    poll that fails midway is simply run again."""

    def __init__(self, mailbox, clock=time.time):
        self.mailbox = mailbox
        self._clock = clock
        MAIL_STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.index = self._open(MAIL_STATE_DIR / "mailbox.sqlite", MailboxIndex)
        self.threads = self._open(MAIL_STATE_DIR / "threads.sqlite", _threads.ThreadIndex)
        self.fault = None         # why the mailbox cannot be mirrored as configured
        self.notice = None        # for /health while the mirror works
        # One pass at a time, and no note corrected while one runs: both
        # rewrite ledger records, and a record has one writer at a time.
        self._lock = threading.RLock()
        self._folders: dict = {}
        self._settled = False     # the last look at the folders was complete
        self._plan = None         # the backfill still to do, oldest first
        self._merges_due: set = set()
        self._prepare()

    @staticmethod
    def _open(path: Path, kind):
        try:
            return kind(path)
        except (RuntimeError, sqlite3.DatabaseError) as exc:
            # Another schema, or not a database: both indexes are caches of the
            # ledger, so the cure is the same — start over and rebuild.
            print(f"[mail-gateway] {path.name} is unusable ({exc}); rebuilding it", flush=True)
            path.unlink(missing_ok=True)
            return kind(path)

    # -- startup: the indexes agree with the ledger ----------------------------

    def _ledger(self) -> list:
        """This account's mail records, one per Message-ID, oldest first."""
        seen, out = set(), []
        for path, fields in _ibstore.iter_records(INBOUND_STORE_DIR):
            message_id = fields.get("message_id")
            if (fields.get("channel") != CHANNEL or (fields.get("account") or "") != ACCOUNT
                    or not message_id or not fields.get("chat") or message_id in seen):
                continue
            seen.add(message_id)
            out.append((path, fields))
        return out

    @staticmethod
    def _record_time(fields: dict) -> float:
        stamp = fields.get("sent_at") or fields.get("received_at")
        return _threads.to_epoch(stamp) or 0.0

    def _adopt(self, path: Path, fields: dict) -> None:
        """Index a ledger record the mailbox index does not know (yet)."""
        outbound = fields.get("type") == _ibstore.T_OUTBOUND
        self.index.run(
            "INSERT OR REPLACE INTO mails (message_id, record, direction, chat, ts, "
            "sender, sender_name, recipients, cc, removed) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (fields["message_id"], path.name, "out" if outbound else "in", fields["chat"],
             self._record_time(fields), fields.get("sender") or "",
             fields.get("sender_name"),
             json.dumps([[a, None] for a in fields.get("recipients") or []]),
             json.dumps([[a, None] for a in fields.get("cc") or []]),
             1 if fields.get("removed_at") else 0))

    def _prepare(self) -> None:
        """Make both indexes agree with the ledger, before the mailbox is asked
        anything. The ledger is the record of what was mirrored; the indexes
        are caches of it and may be missing (a lost state volume), behind (an
        ingest a crash cut short, a volume restored from a backup) or ahead (a
        record that is gone). Reading the whole ledger costs a start a second
        or two per ten thousand mails, and buys never recording a mail twice."""
        ledger = self._ledger()
        by_id = {fields["message_id"]: (path, fields) for path, fields in ledger}
        if ledger and (not self.threads.chats()
                       or any(self.threads.chat_of(mid) is None for mid in by_id)):
            # The ledger's own chat keys are honoured; keys the headers prove
            # to be one thread come back as merges due, below.
            self.threads.rebuild([
                (f["message_id"],
                 list(dict.fromkeys((f.get("references") or "").split()
                                    + [f.get("in_reply_to") or ""])),
                 f["chat"], self._record_time(f), f.get("subject_line"))
                for _path, f in ledger])
            print(f"[mail-gateway] thread index rebuilt from {len(ledger)} ledger records",
                  flush=True)
        indexed = {row["message_id"]: row["record"] for row in
                   self.index.rows("SELECT message_id, record FROM mails")}
        # Records the index does not know — all of them after a lost volume,
        # or the one whose ingest was cut short after the ledger write (its
        # claim has no record name yet). Their mails are looked for again.
        adopted = [mid for mid, (path, _f) in by_id.items() if indexed.get(mid) != path.name]
        for mid in adopted:
            self._adopt(*by_id[mid])
        # Rows whose record is not in the ledger. A claim that never became a
        # record just goes; a record that is gone takes its mail's places with
        # it, and the folders are read again so a mail still there is mirrored
        # anew (everything up to the high-water mark becomes the backfill's).
        lost = [mid for mid in indexed if mid not in by_id]
        for mid in lost:
            self.index.run("DELETE FROM mails WHERE message_id = ?", (mid,))
            self.index.run("DELETE FROM locations WHERE message_id = ?", (mid,))
        if any(indexed[mid] for mid in lost):
            self.index.run("UPDATE folders SET ceiling = hwm")
            self.index.set("backfill_done", "0")
        if adopted or lost:
            print(f"[mail-gateway] mailbox index reconciled with the ledger: "
                  f"{len(adopted)} record(s) adopted, {len(lost)} row(s) dropped", flush=True)
        # Where the thread index and the records disagree, a merge was begun
        # and not finished (or the rebuild just found one).
        for row in self.index.rows("SELECT message_id, chat FROM mails"):
            chat = self.threads.chat_of(row["message_id"])
            if chat and chat != row["chat"]:
                self._merges_due.add(row["chat"])

    # -- what /health and the thread endpoints read ----------------------------

    def backfill_done(self) -> bool:
        return self.index.get("backfill_done") == "1"

    def backfill_status(self) -> dict:
        plan = self._plan
        return {"done": self.backfill_done(),
                "pending": 0 if self.backfill_done() else (len(plan) if plan is not None else None),
                "days": MAIL_BACKFILL_DAYS}

    def thread(self, chat: str) -> dict | None:
        """The ``Thread`` answer for one chat key, or None for no such chat."""
        rows = self.index.rows(
            "SELECT * FROM mails WHERE chat = ? AND record != '' "
            "ORDER BY ts DESC, message_id DESC", (chat,))
        if not rows:
            return None
        names: dict = {}

        def note(address, name):
            if not address or address in OWN_ADDRESSES:
                return
            if address not in names or (name and not names[address]):
                names[address] = name or names.get(address)

        def listed(row, column):
            try:
                return [(a, n) for a, n in json.loads(row[column] or "[]")]
            except (ValueError, TypeError):
                return []

        # Most recent sender first, then everyone only ever written to.
        for row in rows:
            note(row["sender"], row["sender_name"])
        for row in rows:
            for address, name in listed(row, "recipients") + listed(row, "cc"):
                note(address, name)
        peer = None
        # The person the chat is with: whoever wrote last — not the user, whose
        # own mail can arrive in INBOX too (a copy to oneself).
        inbound = next((r for r in rows if r["direction"] == "in" and r["sender"]
                        and r["sender"] not in OWN_ADDRESSES), None)
        if inbound is not None:
            peer = {"address": inbound["sender"],
                    "name": names.get(inbound["sender"]) or inbound["sender_name"]}
        else:
            # A thread the user started: whom they wrote to first.
            for row in reversed(rows):
                first = listed(row, "recipients") + listed(row, "cc")
                others = [(a, n) for a, n in first if a not in OWN_ADDRESSES] or first
                if others:
                    peer = {"address": others[0][0],
                            "name": names.get(others[0][0]) or others[0][1]}
                    break
        return {
            "chat": chat,
            "name": self.threads.subject_of(chat),
            "group": len(names) > 1,
            "participants": [{"address": a, "name": n} for a, n in names.items()],
            "peer": peer,
            "last_ts": rows[0]["ts"],
            "messages": len(rows),
            "live": sum(1 for r in rows if not r["removed"]),
        }

    def recent_chats(self, limit: int = MAIL_RECENT_CHATS_MAX) -> list:
        rows = self.index.rows(
            "SELECT chat, MAX(ts) AS last FROM mails WHERE record != '' "
            "GROUP BY chat ORDER BY last DESC, chat LIMIT ?", (limit,))
        return [t for t in (self.thread(row["chat"]) for row in rows) if t]

    def note_author(self, message_id: str, author: str) -> None:
        """Remember who authored a mail that is, or will be, in Sent.

        A mail already recorded as the own-device echo has its record
        corrected; the reader learns it from the ledger."""
        self.index.run(
            "INSERT INTO notes (message_id, author, created) VALUES (?, ?, ?) "
            "ON CONFLICT (message_id) DO UPDATE SET author = excluded.author, "
            "created = excluded.created", (message_id, author, self._clock()))
        self.index.run(
            "DELETE FROM notes WHERE message_id NOT IN "
            "(SELECT message_id FROM notes ORDER BY created DESC LIMIT ?)", (MAIL_NOTES_MAX,))
        with self._lock:
            row = self.index.one(
                "SELECT record FROM mails WHERE message_id = ? AND direction = 'out' "
                "AND record != '' AND removed = 0", (message_id,))
            if row is not None:
                _ibstore.update_message(self._record_path(row["record"]), author=author)

    # -- one pass --------------------------------------------------------------

    def poll(self, *, sync: bool = True, backfill_limit: int | None = None) -> bool:
        """One pass over the mailbox. Returns whether work is waiting that the
        next pass should not wait a whole poll interval for.

        ``sync`` looks at the folders (new and vanished mail); a pass without
        it only works on what an earlier look left to do — the loop runs
        those between polls while a backfill is under way."""
        with self._lock:
            rail_up = self._flush_rail()
            policy = self._policy()
            self._apply_merges()
            if sync:
                self._settled = self._sync(policy)
            more = False
            if self._settled:
                # In this order: a mail must be found again before it can be
                # called gone, and known mail before the backfill reads the rest.
                more = (self._reidentify(RELOCATE_BATCH)
                        or self._settle_unlocated(RELOCATE_BATCH)
                        or self._backfill(policy, backfill_limit))
            if rail_up:
                self._flush_rail()
            return more

    @staticmethod
    def _policy():
        try:
            return _triage.load_email_policy(POLICY_PATH)
        except Exception as exc:  # noqa: BLE001 - no flags is the safe reading
            print(f"[mail-gateway] e-mail policy unreadable ({exc}); mirroring "
                  f"as with no flags", flush=True)
            return _triage.EmailPolicy(frozenset(), frozenset())

    def _record_path(self, name: str) -> Path:
        return _ibstore.messages_dir(INBOUND_STORE_DIR) / name

    def _folder(self, role: str):
        return self.index.one("SELECT * FROM folders WHERE role = ?", (role,))

    # -- following the folders -------------------------------------------------

    def _sync(self, policy) -> bool:
        """Look at the three folders. True when every folder a mail could be
        in was seen — only then may a mail be called gone."""
        self._folders = self.mailbox.folders()
        if self.index.get("horizon") is None:
            # Fixed once, so a restart continues the same backfill instead of
            # sliding its window.
            self.index.set("horizon", self._clock() - MAIL_BACKFILL_DAYS * 86400)
            self.index.set("backfill_done", "0" if MAIL_BACKFILL_DAYS else "1")
        settled, missing = True, []
        # Sent before INBOX: a mail in both (a mail to oneself, the list's
        # copy of one's own post) is then recorded as what the user sent.
        for role in ("sent", "inbox", "archive"):
            name = self._folders.get(role)
            if not name or not self._sync_folder(role, name, policy):
                missing.append(role)
                settled = settled and role == "archive"
        # Without INBOX or Sent the mirror is not the mailbox: what the other
        # folder holds is still read, but nothing is called gone and no old
        # mail is read until the configuration is put right.
        self.fault = None if settled else (
            "the " + " and the ".join(r for r in missing if r != "archive")
            + " folder could not be found on the server"
            + (f" — set SENT_FOLDER (configured: {MAIL_CONFIG.sent_folder!r})"
               if "sent" in missing and MAIL_CONFIG is not None else ""))
        self.notice = (
            f"no archive folder ({MAIL_ARCHIVE_FOLDER!r}) on the server: a mail moved "
            f"out of INBOX shows as removed — set MAIL_ARCHIVE_FOLDER"
            if "archive" in missing else None)
        return settled

    def _sync_folder(self, role: str, name: str, policy) -> bool:
        folder = self._folder(role)
        if folder is None:
            # First sight. Everything in it now is old mail, the backfill's to
            # read (or, in the archive, nobody's); what arrives later is new.
            listing = self.mailbox.list_uids(name, since=self._clock())
            if listing is None:
                return False
            newest = self._newest(name, listing)
            self.index.run(
                "INSERT INTO folders (role, name, uidvalidity, gen, ceiling, hwm) "
                "VALUES (?, ?, ?, 1, ?, ?)", (role, name, listing.uidvalidity, newest, newest))
            return True
        gen, hwm = folder["gen"], folder["hwm"]
        lowest = self.index.one(
            "SELECT MIN(uid) AS uid FROM locations WHERE role = ? AND gen = ?",
            (role, gen))["uid"]
        first = min(lowest or hwm + 1, hwm + 1)
        listing = self.mailbox.list_uids(name, first_uid=first)
        if listing is None:
            return False
        if listing.uidvalidity != folder["uidvalidity"] or name != folder["name"]:
            # The UIDs no longer mean what they meant (the server renumbered
            # the folder, or the role now names another folder). The old rows
            # stay, a generation behind, as the list of mails to find again by
            # Message-ID; what is here now and unknown is the backfill's.
            newest = self._newest(name, listing)
            self.index.run(
                "UPDATE folders SET name = ?, uidvalidity = ?, gen = gen + 1, "
                "ceiling = ?, hwm = ? WHERE role = ?",
                (name, listing.uidvalidity, newest, newest, role))
            if role != "archive":
                self.index.set("backfill_done", "0")
                self._plan = None
            print(f"[mail-gateway] {name!r}: the UIDs changed (UIDVALIDITY "
                  f"{folder['uidvalidity']} -> {listing.uidvalidity}); finding its "
                  f"mails again by Message-ID", flush=True)
            return True
        present = set(listing.uids)
        for row in self.index.rows(
                "SELECT uid FROM locations WHERE role = ? AND gen = ? AND uid >= ?",
                (role, gen, first)):
            if row["uid"] not in present:
                self.index.run("DELETE FROM locations WHERE role = ? AND gen = ? AND uid = ?",
                               (role, gen, row["uid"]))
        if role == "archive":
            return True   # followed, never read for new mail
        for uid in sorted(uid for uid in present if uid > hwm):
            if self.index.one("SELECT 1 FROM locations WHERE role = ? AND gen = ? AND uid = ?",
                              (role, gen, uid)) is None:
                self._ingest(role, name, uid, policy, backfill=False)
            # Only now: a mail whose ingest raised is read again next poll.
            self.index.run("UPDATE folders SET hwm = ? WHERE role = ?", (uid, role))
        return True

    def _newest(self, name: str, listing: Listing) -> int:
        """The highest UID the folder has handed out so far."""
        if listing.uidnext:
            return listing.uidnext - 1
        whole = self.mailbox.list_uids(name)
        return max(whole.uids, default=0) if whole else 0

    # -- finding a mail again --------------------------------------------------

    def _find(self, role: str, message_id: str, ts: float | None) -> int | None:
        name = self._folders.get(role)
        folder = self._folder(role)
        if not name or folder is None:
            return None
        if _searchable(message_id):
            return self.mailbox.find(name, message_id)
        # No header to search for: read the headers of the mails that arrived
        # around then and are not accounted for, and compare ids.
        listing = self.mailbox.list_uids(name, since=(ts or self._clock()) - 2 * 86400)
        if listing is None or listing.uidvalidity != folder["uidvalidity"]:
            return None
        known = {row["uid"] for row in self.index.rows(
            "SELECT uid FROM locations WHERE role = ? AND gen = ?", (role, folder["gen"]))}
        for uid in [u for u in sorted(listing.uids) if u not in known][:SCAN_MAX]:
            got = self.mailbox.fetch(name, uid, limit=HEADER_PEEK_BYTES)
            if got is not None and _identify(email.message_from_bytes(got.raw)) == message_id:
                return uid
        return None

    def _locate(self, role: str, uid: int, message_id: str | None, skipped=None) -> None:
        folder = self._folder(role)
        self.index.run(
            "INSERT OR REPLACE INTO locations (role, gen, uid, message_id, skipped) "
            "VALUES (?, ?, ?, ?, ?)", (role, folder["gen"], uid, message_id, skipped))

    def _reidentify(self, limit: int) -> bool:
        """Find again, by Message-ID, the mails of a folder whose UIDs changed.
        True while some are left for the next pass."""
        stale = self.index.rows(
            "SELECT l.role, l.gen, l.uid, l.message_id, l.skipped, m.ts FROM locations l "
            "JOIN folders f ON f.role = l.role AND f.gen != l.gen "
            "LEFT JOIN mails m ON m.message_id = l.message_id LIMIT ?", (limit + 1,))
        for row in stale[:limit]:
            if row["message_id"]:
                uid = self._find(row["role"], row["message_id"], row["ts"])
                if uid is not None:
                    self._locate(row["role"], uid, row["message_id"], row["skipped"])
            # Not found: the row goes, and a mail left with no place at all is
            # looked for in every folder before it is called gone.
            self.index.run("DELETE FROM locations WHERE role = ? AND gen = ? AND uid = ?",
                           (row["role"], row["gen"], row["uid"]))
        return len(stale) > limit

    def _settle_unlocated(self, limit: int) -> bool:
        """Mails with a record and no place: look in every mirrored folder —
        this is how a move to the archive is followed — and tombstone the ones
        that are in none. True while some are left for the next pass."""
        # (When the gateway sends mail itself, a record will exist before its
        # Sent copy is visible; that phase must keep such a mail out of here
        # until the copy has had time to appear.)
        waiting = self.index.rows(
            "SELECT * FROM mails m WHERE m.removed = 0 AND m.record != '' AND NOT EXISTS "
            "(SELECT 1 FROM locations l WHERE l.message_id = m.message_id) "
            "ORDER BY m.ts LIMIT ?", (limit + 1,))
        gone: dict = {}
        for row in waiting[:limit]:
            order = ("sent", "archive", "inbox") if row["direction"] == "out" \
                else ("inbox", "archive", "sent")
            for role in order:
                uid = self._find(role, row["message_id"], row["ts"])
                if uid is not None:
                    self._locate(role, uid, row["message_id"])
                    break
            else:
                if not _ibstore.tombstone_message(self._record_path(row["record"]),
                                                  self._clock()):
                    print(f"[mail-gateway] could not tombstone {row['record']}; "
                          f"will retry", flush=True)
                    continue
                # The record lost its recipients; so does the index, which
                # must say what a rebuild from the ledger would.
                self.index.run("UPDATE mails SET removed = 1, recipients = '[]', cc = '[]' "
                               "WHERE message_id = ?", (row["message_id"],))
                gone.setdefault(row["chat"], []).append(row["message_id"])
        for chat, message_ids in gone.items():
            live = self.index.one(
                "SELECT COUNT(*) AS n FROM mails WHERE chat = ? AND removed = 0 "
                "AND record != ''", (chat,))["n"]
            self._enqueue("removed", {"channel": CHANNEL, "account": ACCOUNT, "chat": chat,
                                      "message_ids": message_ids, "live": live})
            print(f"[mail-gateway] {len(message_ids)} mail(s) of chat {chat!r} left the "
                  f"mailbox ({live} still there)", flush=True)
        return len(waiting) > limit

    # -- backfill --------------------------------------------------------------

    def _backfill(self, policy, limit: int | None) -> bool:
        """Read old mail: INBOX and Sent since the horizon, merged, oldest
        first, so parents arrive before their replies. Resumable by
        construction — the plan is what the folders hold minus what the index
        already places — so a restart continues and never repeats."""
        if self.backfill_done():
            return False
        if self._plan is None:
            horizon = float(self.index.get("horizon") or 0.0)
            plan = []
            for order, role in enumerate(("sent", "inbox")):
                folder, name = self._folder(role), self._folders.get(role)
                if folder is None or not name:
                    return False
                listing = self.mailbox.list_uids(name, since=horizon, dates=True)
                if listing is None or listing.uidvalidity != folder["uidvalidity"]:
                    return False   # the next look at the folders sorts it out
                placed = {row["uid"] for row in self.index.rows(
                    "SELECT uid FROM locations WHERE role = ? AND gen = ?",
                    (role, folder["gen"]))}
                plan += [(date or 0.0, order, uid, role, folder["gen"])
                         for uid, date in listing.uids.items()
                         if uid <= folder["ceiling"] and uid not in placed]
            self._plan = sorted(plan)
        done = 0
        while self._plan and (limit is None or done < limit):
            _date, _order, uid, role, gen = self._plan[0]
            folder = self._folder(role)
            if folder is None or folder["gen"] != gen:
                self._plan = None   # the UIDs changed under the plan
                return True
            self._ingest(role, folder["name"], uid, policy, backfill=True)
            self._plan.pop(0)
            done += 1
        if self._plan:
            return True
        self.index.set("backfill_done", "1")
        self._plan = None
        print("[mail-gateway] backfill complete", flush=True)
        return False

    # -- one mail --------------------------------------------------------------

    def _ingest(self, role: str, name: str, uid: int, policy, *, backfill: bool) -> None:
        """Mirror the mail at one UID: a record, or one more place for a mail
        already recorded, or a note of why it has neither."""
        fetched = self.mailbox.fetch(name, uid, limit=MAIL_MAX_MAIL_BYTES)
        if fetched is None:
            return   # expunged between the listing and now
        try:
            mail = parse_mail(fetched.raw, fetched.date)
        except Exception as exc:  # noqa: BLE001 - one unreadable mail must not stop the folder
            print(f"[mail-gateway] could not read the mail at {name!r} UID {uid}: {exc}\n"
                  f"{traceback.format_exc()}", flush=True)
            self._locate(role, uid, None, "unreadable")
            return
        outbound = role == "sent"
        known = self.index.one("SELECT * FROM mails WHERE message_id = ? AND record != ''",
                               (mail.message_id,))
        revived = False
        if known is not None:
            if known["removed"]:
                # Back from wherever it had gone (out of the Trash, say): the
                # tombstone gives way to the mail.
                revived = True
            elif not (outbound and known["direction"] == "in"):
                self._locate(role, uid, mail.message_id)
                return
            # (else: recorded from its INBOX copy before the Sent copy was
            # visible — it is the user's own mail, and is recorded as that.)
            self._drop_record(known)
        if not outbound and known is None and self._is_news(mail, policy):
            self._locate(role, uid, mail.message_id, "news")
            return
        self._write(mail, fetched, outbound=outbound,
                    backfill=backfill and not revived)
        self._locate(role, uid, mail.message_id)

    def _is_news(self, mail: Mail, policy) -> bool:
        """A news sender or list starts no chat (the news rail has the mail) —
        unless the mail continues a thread that is already mirrored, normally
        because the user wrote in it: then it is the answer to their post."""
        try:
            news = _triage.email_gate_decision(mail.sender, mail.list_id, pol=policy)["news"]
        except Exception as exc:  # noqa: BLE001 - when in doubt, mirror
            print(f"[mail-gateway] e-mail gate failed ({exc}); mirroring the mail", flush=True)
            return False
        return bool(news) and not any(self.threads.chat_of(cited) for cited in mail.cited)

    def _drop_record(self, row) -> None:
        """Take a mail's record out of the ledger so it can be written anew."""
        path = self._record_path(row["record"])
        _ibstore.tombstone_message(path)   # unlinks its blobs, with care for shared ones
        path.unlink(missing_ok=True)
        self.index.run("DELETE FROM mails WHERE message_id = ?", (row["message_id"],))

    def _store_parts(self, mail: Mail, fetched: Fetched, stored: list) -> dict:
        """Store a mail's parts as blobs; returns the record's references.
        Every blob id written is appended to ``stored`` as it is, so the
        caller can undo them if the record itself cannot be written."""
        def keep(data: bytes, content_type: str, file_name=None) -> str:
            media_id = _ibstore.store_media(INBOUND_STORE_DIR, data, content_type,
                                            file_name=file_name)
            stored.append(media_id)
            return _MEDIA_REF + media_id

        refs: dict = {"attachments": [], "content_ids": {}}
        # The three parts of the original, each normalised to UTF-8 text so a
        # reader needs no charset but the one stated.
        if mail.plain is not None:
            refs["body_part"] = keep(mail.plain.encode("utf-8", "replace"),
                                     "text/plain; charset=utf-8")
        if mail.html is not None:
            refs["html_part"] = keep(mail.html.encode("utf-8", "replace"),
                                     "text/html; charset=utf-8")
        refs["header_part"] = keep(mail.headers.encode("utf-8", "replace"),
                                   "text/plain; charset=utf-8")
        if fetched.size > MAIL_MAX_MAIL_BYTES:
            # Only the head of the mail was fetched: its text parts come
            # first and are whole, its attachments are cut off or missing.
            data, file_name = _oversized_mail_note(fetched.size, mail.parts)
            refs["attachments"].append(keep(data, "text/plain; charset=utf-8", file_name))
            return refs
        for part in mail.parts:
            if len(part.data) > MAIL_MAX_PART_BYTES:
                data, file_name = _not_mirrored_note(part)
                refs["attachments"].append(keep(data, "text/plain; charset=utf-8", file_name))
                continue
            # The type and the name are the sender's claims; store_media keeps
            # each as one clean line and a bare base name, never as a path.
            ref = keep(part.data, part.content_type, part.file_name)
            if part.inline_image:
                meta = _ibstore.media_meta(INBOUND_STORE_DIR, stored[-1]) or {}
                if _trim.is_tracking_pixel(meta.get("width"), meta.get("height")):
                    _ibstore.discard_media(INBOUND_STORE_DIR, stored.pop())
                    continue   # a pixel is not an attachment
            refs["attachments"].append(ref)
            if part.content_id:
                refs["content_ids"][ref] = part.content_id
        return refs

    def _write(self, mail: Mail, fetched: Fetched, *, outbound: bool, backfill: bool) -> None:
        """Thread one mail, store its parts, write its record, tell the rail."""
        now = self._clock()
        ts = min(mail.date, now) if mail.date is not None else now
        resolution = self.threads.resolve(mail.message_id, mail.cited, ts, mail.subject)
        chat = resolution.chat
        trimmed = _trim.trim(mail.plain, mail.html, subject=mail.subject)
        direction = "out" if outbound else "in"
        # The claim goes in before the ledger write, so a crash between that
        # write and the index update leaves a row for the next start to match
        # to the record (_prepare) instead of a second record of the same mail.
        self.index.run(
            "INSERT OR REPLACE INTO mails (message_id, record, direction, chat, ts, sender, "
            "sender_name, recipients, cc, removed) VALUES (?, '', ?, ?, ?, ?, ?, ?, ?, 0)",
            (mail.message_id, direction, chat, ts, mail.sender, mail.sender_name,
             json.dumps(mail.to), json.dumps(mail.cc)))
        stored: list = []
        author = None
        try:
            refs = self._store_parts(mail, fetched, stored)
            common = dict(
                channel=CHANNEL, chat=chat, account=ACCOUNT, text=trimmed.text,
                message_id=mail.message_id, timestamp=ts,
                attachment_urls=refs["attachments"], sender_name=mail.sender_name,
                subject=mail.subject, recipients=[a for a, _n in mail.to],
                cc=[a for a, _n in mail.cc], in_reply_to=mail.in_reply_to,
                references=mail.references,
                list_id=_triage.email_list_id(mail.list_id) or None,
                body_part=refs.get("body_part"), html_part=refs.get("html_part"),
                header_part=refs["header_part"], trim=trimmed.state,
                content_ids=refs["content_ids"])
            if outbound:
                noted = self.index.one("SELECT author FROM notes WHERE message_id = ?",
                                       (mail.message_id,))
                # Whichever client sent it: the own-device echo, unless
                # someone said who wrote it.
                author = noted["author"] if noted else "device"
                _urn, path = _ibstore.write_outbound(
                    INBOUND_STORE_DIR, author=author, sender=mail.sender or None, **common)
            else:
                _urn, path = _ibstore.write_message(
                    INBOUND_STORE_DIR, sender=mail.sender,
                    delivered=_delivered_on_write(direction), **common)
        except Exception:
            for media_id in stored:
                _ibstore.discard_media(INBOUND_STORE_DIR, media_id)
            self.index.run("DELETE FROM mails WHERE message_id = ? AND record = ''",
                           (mail.message_id,))
            raise
        self.index.run("UPDATE mails SET record = ? WHERE message_id = ?",
                       (path.name, mail.message_id))
        if resolution.merged:
            self._merges_due.update(resolution.merged)
            self._apply_merges()
        thread = self.thread(chat) or {}
        event = {
            "direction": direction, "channel": CHANNEL, "chat": chat, "account": ACCOUNT,
            "sender": mail.sender or None, "sender_name": mail.sender_name,
            "chat_name": thread.get("name") or None, "group": bool(thread.get("group")),
            "message_id": mail.message_id, "ts": ts,
            "text": trimmed.text[:MAIL_RAIL_TEXT_CHARS],
            "attachments": refs["attachments"], "author": author,
            "gate": dict(MIRROR_GATE), "subject": mail.subject or None,
            "participants": thread.get("participants") or [], "peer": thread.get("peer"),
        }
        if backfill:
            # Old mail: the rail must not treat it as an arrival, and may
            # start the chat's read mark from what the mailbox says was read.
            event["backfill"] = True
            event["seen"] = "\\Seen" in fetched.flags
        self._enqueue("inbound", event)

    # -- merges ----------------------------------------------------------------

    def _apply_merges(self) -> None:
        """Re-key the records of chats the thread index merged away, then tell
        the web gateway — only once the ledger is clean (rekey_chat fails
        closed), so a merge a full disk interrupts is finished by a later poll."""
        merged: dict = {}
        for old in sorted(self._merges_due):
            member = self.index.one("SELECT message_id FROM mails WHERE chat = ? LIMIT 1", (old,))
            into = self.threads.chat_of(member["message_id"]) if member else None
            if not into or into == old:
                self._merges_due.discard(old)
                continue
            result = _ibstore.rekey_chat(INBOUND_STORE_DIR, old, ACCOUNT, into)
            if result["errors"]:
                print(f"[mail-gateway] merging chat {old!r} into {into!r}: "
                      f"{result['errors']} record(s) could not be re-keyed; will retry",
                      flush=True)
                continue
            self.index.run("UPDATE mails SET chat = ? WHERE chat = ?", (into, old))
            self._merges_due.discard(old)
            merged.setdefault(into, []).append(old)
        for into, absorbed in merged.items():
            self._enqueue("merge", {"channel": CHANNEL, "account": ACCOUNT,
                                    "from": absorbed, "into": into})
            print(f"[mail-gateway] merged chat(s) {absorbed!r} into {into!r}", flush=True)

    # -- the rail's queue ------------------------------------------------------

    def _enqueue(self, kind: str, payload: dict) -> None:
        if not _chats.chats_enabled():
            return   # no rail configured: the reader has the ledger
        self.index.run("INSERT INTO outbox (kind, payload) VALUES (?, ?)",
                       (kind, json.dumps(payload)))

    def _flush_rail(self) -> bool:
        """Offer the web gateway everything it has not taken, in order. An
        event it cannot take now stays queued for the next poll. False when
        the web gateway is not answering at all."""
        if not _chats.chats_enabled():
            return True
        after = 0
        while True:
            batch = self.index.rows(
                "SELECT seq, kind, payload FROM outbox WHERE seq > ? ORDER BY seq LIMIT 200",
                (after,))
            if not batch:
                return True
            for row in batch:
                after = row["seq"]
                try:
                    payload = json.loads(row["payload"])
                except ValueError:
                    outcome = "drop"
                else:
                    outcome = _rail_send(row["kind"], payload)
                if outcome in ("ok", "drop"):
                    self.index.run("DELETE FROM outbox WHERE seq = ?", (row["seq"],))
                elif outcome == "down":
                    return False


# ── The mirror loop ───────────────────────────────────────────────────────────

def _policy_wait(mirror: Mirror, loop: dict, now: float) -> str | None:
    """Before the first backfill, wait for the e-mail policy file: read without
    it, every newsletter of ninety days would become a chat. The retinue side
    creates the file (empty if it has nothing to say) at its own start; if it
    never does, the mirror proceeds as with no flags rather than never."""
    if mirror.backfill_done() or POLICY_PATH.exists():
        return None
    if now - loop["started"] >= MAIL_POLICY_WAIT_SECONDS:
        if not loop.get("gave_up"):
            loop["gave_up"] = True
            print(f"[mail-gateway] no e-mail policy file at {POLICY_PATH} after "
                  f"{int(MAIL_POLICY_WAIT_SECONDS)}s; mirroring as with no flags", flush=True)
        return None
    return (f"waiting for the e-mail policy file ({POLICY_PATH}) before the first "
            f"backfill; the retinue container writes it at start")


def _loop_step(mirror: Mirror, loop: dict) -> float:
    """One turn of the mirror loop; returns how long to sleep before the next.
    Never raises: a failure is a health state, and the next turn tries again."""
    now = time.time()
    loop.setdefault("started", now)
    try:
        mirror.mailbox.ensure()
        waiting = _policy_wait(mirror, loop, now)
        if waiting:
            _set_state(connected=True, error=None, notice=waiting)
            return min(MAIL_POLL_SECONDS, 5.0)
        sync = now - loop.get("synced", 0.0) >= MAIL_POLL_SECONDS
        more = mirror.poll(sync=sync, backfill_limit=MAIL_BACKFILL_BATCH)
        if sync:
            loop["synced"] = now
        _set_state(connected=mirror.fault is None, error=mirror.fault, notice=mirror.notice,
                   last_poll=time.time())
        if more:
            return MAIL_BACKFILL_PAUSE_SECONDS
        return max(1.0, MAIL_POLL_SECONDS - (time.time() - loop.get("synced", now)))
    except MailboxError as exc:
        mirror.mailbox.close()
        _set_state(connected=False, error=str(exc)[:500])
        print(f"[mail-gateway] poll failed: {exc}", flush=True)
    except Exception as exc:  # noqa: BLE001 - reported, and retried next turn
        _set_state(connected=False,
                   error=f"the mirror stopped on an internal error: {exc}"[:500])
        print(f"[mail-gateway] poll failed: {exc}\n{traceback.format_exc()}", flush=True)
    # A whole interval after a failure: a refused login retried every few
    # seconds is how a mailbox gets locked.
    return float(MAIL_POLL_SECONDS)


def _mirror_loop(mirror: Mirror) -> None:
    loop: dict = {}
    while True:
        time.sleep(_loop_step(mirror, loop))


# ── HTTP API ──────────────────────────────────────────────────────────────────

READ_ONLY_ERROR = {"error": "the mail mirror is read-only"}
# Present so a caller gets a reason, not a 404: each is a later phase's.
_READ_ONLY_POSTS = ("/send", "/chats/delete", "/chats/read", "/chats/archive")
_NOTE_AUTHORS = ("agent", "user")


def _is_pending_sends(path: str) -> bool:
    return path == "/pending-sends" or path.startswith("/pending-sends/")


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def _reply(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _reply_raw(self, status: int, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        # One clean line, whatever the sender declared (load_media). Whether a
        # browser may show the blob as that type is the web gateway's call.
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def _authorized(self) -> bool:
        if not GATEWAY_TOKEN:
            return True
        auth = self.headers.get("Authorization", "")
        token = auth[len("Bearer "):].strip() if auth.startswith("Bearer ") else ""
        return bool(token) and hmac.compare_digest(token, GATEWAY_TOKEN)

    def _read_body(self, limit: int) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            return None
        if length <= 0 or length > limit:
            return None
        return self.rfile.read(length)

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if path in ("", "/health"):
            self._reply(200, _health_snapshot())
            return
        if not self._authorized():
            self._reply(401, {"error": "unauthorized"})
            return
        media = re.match(r"^/media/([^/?]+)$", path)
        if media:
            # Attachments and the three part blobs; load_media validates the
            # id before it touches the disk.
            got = _ibstore.load_media(INBOUND_STORE_DIR, media.group(1))
            if got is None:
                self._reply(404, {"error": "not found"})
            else:
                self._reply_raw(200, got[0], got[1])
            return
        if _is_pending_sends(path):
            self._reply(501, READ_ONLY_ERROR)
            return
        if path == "/recent-chats":
            self._reply(200, {"recent_chats": MIRROR.recent_chats() if MIRROR else []})
            return
        if path.startswith("/threads/"):
            chat = unquote(path[len("/threads/"):])
            thread = MIRROR.thread(chat) if MIRROR and chat else None
            if thread is None:
                self._reply(404, {"error": "not found"})
            else:
                self._reply(200, thread)
            return
        self._reply(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if not self._authorized():
            self._reply(401, {"error": "unauthorized"})
            return
        if path in _READ_ONLY_POSTS or _is_pending_sends(path):
            self._reply(501, READ_ONLY_ERROR)
            return
        if path != "/outbound-note":
            self._reply(404, {"error": "not found"})
            return
        raw = self._read_body(MAX_BODY_BYTES)
        try:
            body = json.loads(raw.decode("utf-8")) if raw else None
        except (ValueError, UnicodeDecodeError):
            body = None
        if not isinstance(body, dict):
            self._reply(400, {"error": "body must be a JSON object"})
            return
        message_id = _threads.normalize_id(str(body.get("message_id") or ""))
        author = str(body.get("author") or "").strip().lower()
        if not message_id:
            self._reply(400, {"error": "message_id (a Message-ID) is required"})
            return
        if author not in _NOTE_AUTHORS:
            self._reply(400, {"error": "'author' must be one of " + "|".join(_NOTE_AUTHORS)})
            return
        if MIRROR is None:
            self._reply(503, {"error": "the mail gateway is not configured"})
            return
        try:
            MIRROR.note_author(_fit_id(message_id), author)
        except (sqlite3.Error, OSError) as exc:
            print(f"[mail-gateway] could not keep an outbound note: {exc}", flush=True)
            self._reply(503, {"error": "could not keep the note; retry later"})
            return
        self._reply(200, {"ok": True})


def _serve_http() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), _Handler)
    print(f"[mail-gateway] HTTP API listening on port {HTTP_PORT}"
          + (" (token required)" if GATEWAY_TOKEN else ""), flush=True)
    server.serve_forever()


def main() -> None:
    global MIRROR
    if not CONFIGURED:
        # Stay up with /health reporting configured: false, like the other
        # gateways: an unconfigured channel is a deployment choice, not a fault.
        print(f"[mail-gateway] {CONFIG_PROBLEM} — idling (health reports unconfigured)",
              flush=True)
    elif MAIL_CONFIG is None:
        print(f"[mail-gateway] the mailbox is not usable: {CONFIG_PROBLEM}", flush=True)
    else:
        MIRROR = Mirror(Mailbox(MAIL_CONFIG))
        print(f"[mail-gateway] mirroring {ACCOUNT} (read-only; backfill "
              f"{MAIL_BACKFILL_DAYS} days)", flush=True)
        threading.Thread(target=_mirror_loop, args=(MIRROR,), name="mail-mirror",
                         daemon=True).start()
    _serve_http()


if __name__ == "__main__":
    main()
