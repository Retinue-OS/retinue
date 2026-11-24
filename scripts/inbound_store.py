"""Per-message store for the messaging gateways (message ledger).

Every inbound message that reaches an *inbox*-mode gateway (Signal, WhatsApp,
Telegram, SMS — and every mail the mail gateway mirrors from a mailbox) is
persisted here as **one N-Triples file per message**, on the gateway's own data
volume, before any routing decision is made. Two things fall out of that single
act:

1. **A browsable history.** The life store indexes the volume read-only, so the
   whole message stream is queryable over SPARQL (``kb:InboundMessage``) — "what
   came in from X last week?" is a plain ``SELECT``, no model turn, no gateway
   round-trip. Because it is one file per message the store never rewrites a
   shared index, so a gateway writing a new message can never race a SPARQL read
   or another gateway.

2. **A delivery ledger.** Each message carries a ``kb:delivered`` flag. This is
   **not** "read" — it records only whether the message has reached the user at
   all: the chat surface shows it, or a turn put it in front of them. The flag is
   owned solely by the gateway and flipped ``false → true`` by exactly two
   operations, both here: :func:`undelivered`, which returns the held messages
   **and marks them delivered as a side effect**, and :func:`mark_delivered`,
   which flips one already-written message the gateway persisted up front (the
   persist-before-forward path — see below). (:func:`tombstone_message` also
   sets it, for a mail found gone from its mailbox: nothing is owed for a
   message that no longer exists.) Nothing else — no SPARQL query, no ad-hoc
   read — ever touches it, so browsing history never silently "consumes" a
   message.

   Since the chat surface shows every arrival the moment it lands, an
   ``undelivered`` backlog is now an **exception**, not a daily inbox: a message
   sits there only when the handover to the chats rail could not be completed.
   The gateway's ``/undelivered`` endpoint is the recovery path for exactly those
   — not a sweep anyone runs on a schedule to see their mail.

The delivered flag lets a gateway persist a message it deliberately did **not**
hand over — one from an ignored group is written straight to ``delivered: true``
(already accounted for, never re-surfaced). A message that *does* go out takes
the never-drop path: the gateway writes it ``delivered: false`` the instant it
arrives (before the gate, before the forward — so a crash or a throwing forward
cannot lose it), then calls :func:`mark_delivered` once the rail has taken it,
or once a turn it bought has actually **run**. That last part is the whole
point: a forward POST answers 202 (accepted), not "handled", so the flip waits
on the job's ``status: done`` (see ``job_delivery.py``). Any message that was
persisted but never got that far — a failed forward, a job that errored or
expired, a gateway that died mid-dispatch — stays ``delivered: false`` and is
recoverable (at-least-once: a rare duplicate surface beats a silent loss).

Inbound is only half the ledger. The store also holds **outbound** messages
(``kb:OutboundMessage``, :func:`write_outbound`) in the same ``messages/``
directory: every send a gateway completed — an agent push, an approved pending
send, and the user's own sends from their other devices captured from the
channel's echo/sync stream. Both directions carry ``kb:chat``, the chat key, and
``kb:account``, the gateway account that wrote the record, so one filter yields a
whole conversation as a single timeline (filenames sort by epoch millis
regardless of direction). It takes **both**: a chat key identifies a peer within
one account, and a channel's message volume is shared by every account on it, so
the pair is the conversation's real identity. Outbound records have **no**
delivered flag and are invisible to :func:`undelivered` — the ledger is
bookkeeping for inbound mail only.

Only **inbox**-mode gateways write here at all, in either direction: a control
account's traffic (prompts in, an agent's replies out) is not the user's
correspondence and is persisted on neither. So every record on a channel's
volume was written by one of that channel's inbox accounts — which is what lets
a reader attribute a record written before ``kb:account`` existed, when the
channel has exactly one such account, without guessing.

Stdlib only (``hashlib``/``secrets``/``datetime``): this module is copied into
each gateway image alongside ``triage_policy.py`` and ``reply_tokens.py``, and
those containers ship no third-party RDF library.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

# Same knowledge-base namespace the rest of the triage machinery emits under.
KB = "https://w3id.org/retinue/kb#"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
XSD_DATETIME = "http://www.w3.org/2001/XMLSchema#dateTime"

T_INBOUND = KB + "InboundMessage"
T_OUTBOUND = KB + "OutboundMessage"
P_CHANNEL = KB + "channel"
P_SENDER = KB + "sender"
P_GROUP = KB + "group"
P_RECEIVED_AT = KB + "receivedAt"
P_TEXT = KB + "text"
P_MESSAGE_ID = KB + "messageId"
P_DELIVERED = KB + "delivered"
# The chat key: the exact recipient string the message's channel accepts on its
# own send path (Signal number/UUID or "group:<id>", WhatsApp chat JID, Telegram
# chat_id). Stamped on BOTH directions by the gateways, so one filter yields a
# whole conversation and a send routes back with the same key.
P_CHAT = KB + "chat"
# The gateway account this record belongs to: the *_ACCOUNT value of the
# container that wrote it, stamped verbatim on BOTH directions. A chat key is
# only unique *within* an account — two accounts of one channel talking to the
# same peer produce the same key — so without this predicate their messages
# merge into one timeline and one unread count, and a reply cannot know which
# identity it should go out as. The account is the only identity a gateway
# asserts about itself (its address and registry slug are the reader's
# configuration; a gateway declaring those would be a second source of truth
# free to drift), so it is what the record carries. Absent on records written
# before this predicate existed, and on a gateway whose account is not yet
# known — Telegram discovers its own only once the session authorizes — so
# readers must treat it as optional and say what they do with an unattributed
# record rather than guessing one.
P_ACCOUNT = KB + "account"
# Outbound only: who composed the message. One of AUTHORS below.
P_AUTHOR = KB + "author"
# Outbound only: when the channel accepted the send. A distinct predicate rather
# than a reuse of kb:receivedAt because the instant means a different thing
# (nothing was received); readers merging both directions into one timeline
# COALESCE the two.
P_SENT_AT = KB + "sentAt"
# Valid kb:author values: "user" (composed in the dashboard), "agent" (an
# agent's own send — the push CLIs' default), "device" (the user's own send from
# another device, captured from the channel's echo/sync stream).
AUTHORS = ("user", "agent", "device")
# A message's media (voice note, image) is NOT embedded in the graph: consistency
# over data-in-graph means every attachment — regardless of size — is a *reference*,
# never an inline data-URI literal. This predicate carries that reference as an IRI
# object: a host-free ``urn:retinue:media:<channel>:<id>`` naming the blob, which
# the gateway serves from its own token-gated GET /media/<id>. The reference states
# *which* blob and deliberately not where to fetch it — a gateway's address is the
# reader's configuration (its messenger registry), and a record that also carried
# one would be a second source of truth free to drift from it. Multi-valued, so a
# message with several images gets one triple each. What the gateway knows about
# the blob is stated on that IRI, in the same record — see P_CONTENT_TYPE.
P_ATTACHMENT = KB + "attachment"
# What the gateway knows about a blob it stored, stated on the media IRI (the
# kb:attachment object) as its subject, inside the message's own record: the
# content type the bytes are served as, their size, and — for an image — the
# pixel size sniffed at ingest. A reader needs these BEFORE fetching (an image
# and a voice note are different elements; a reserved box needs the ratio), and
# they are the gateway's own knowledge about its own store — so the gateway
# states them, and no reader ever looks at another service's files to learn
# them. They are derived from the store's sidecars at write time
# (:func:`media_meta`) and stated on older records by :func:`backfill_media_meta`.
P_CONTENT_TYPE = KB + "contentType"
P_BYTE_SIZE = KB + "byteSize"
P_WIDTH = KB + "width"
P_HEIGHT = KB + "height"
# The name the sender gave the file, when the channel carried one (a document,
# not a photo): what a file row shows and what a download is saved as.
P_FILE_NAME = KB + "fileName"
XSD_INTEGER = "http://www.w3.org/2001/XMLSchema#integer"
# Optional reference to a retained raw-media file (e.g. a voice note's audio),
# recorded when a message is persisted *before* transcription so a failed or
# crashed STT run leaves a re-transcribable artifact instead of a silent drop.
# Cleared once the message is accounted for (transcribed and forwarded). This is
# a *local file path*, not a reference for the reader: unlike P_ATTACHMENT (the
# message's durable, permanent media) it is bookkeeping for the re-transcribe
# retry and disappears the moment the transcript lands.
P_MEDIA = KB + "media"

# -- what a mail record adds ---------------------------------------------------
# The mail gateway mirrors a mailbox into this same ledger (docs/mail-chats.md,
# "The record"). Every one of these is optional, means nothing on another
# channel, and survives every rewrite below; an absent field writes no triple.
#
# The mail's own Subject, decoded. Its field key is "subject_line": "subject"
# has always been the record's own URN, and every caller reads it as that.
P_SUBJECT = KB + "subject"
# The display name on From — on either direction, since a mail the user sent
# from a client is recorded with its From as well (kb:sender then is the
# account's own address).
P_SENDER_NAME = KB + "senderName"
# To and Cc addresses, one triple each. A set: the record is sorted, so header
# order is not kept — which is why kb:references, whose order is its meaning,
# is one literal instead.
P_RECIPIENT = KB + "recipient"
P_CC = KB + "cc"
# The Reply-To addresses, one triple each, where the mail carried the header:
# whom an answer goes to instead of the sender. The mail gateway builds a
# reply from its records of the thread (docs/mail-chats.md, "Writing"), so the
# record has to say it. Addresses, like To and Cc: gone from a tombstone.
P_REPLY_TO = KB + "replyTo"
# Threading, as the headers carried it: the parent's id, and the References
# ids space-separated in header order (followed by any further id the
# In-Reply-To header named beside the parent, so the two together are every
# id the mail cited). Kept on a tombstone too — a thread index rebuilt from
# the ledger needs every id a mail ever cited.
P_IN_REPLY_TO = KB + "inReplyTo"
P_REFERENCES = KB + "references"
# The normalised List-Id, what the news gate decides a list mail by.
P_LIST_ID = KB + "listId"
# The mail's own parts, each a blob in this store referenced like an
# attachment (an IRI naming it, the same blob facts stated on that IRI) but
# never one: a chat shows attachments as files, and a mail's plain body, its
# HTML and its header block are the original, not files the sender attached.
# Stored once, never rewritten: kb:text is derived from them (the trim), they
# are what "Original" shows.
P_BODY_PART = KB + "bodyPart"
P_HTML_PART = KB + "htmlPart"
P_HEADER_PART = KB + "headerPart"
# An inline part's Content-ID, stated on its attachment IRI like the content
# type, so an HTML part's cid: references resolve to the right blob.
P_CONTENT_ID = KB + "contentId"
# How kb:text was derived from the parts: one of TRIMS.
P_TRIM = KB + "trim"
# When the mail was found gone from the mailbox (see tombstone_message).
P_REMOVED_AT = KB + "removedAt"
# When the mailbox server received the mail (IMAP's INTERNALDATE). A mail
# record's own time (kb:receivedAt, kb:sentAt) is the sender's Date header
# where that is the earlier one, so this is the one time on the record its
# sender did not write — what a thread index rebuilt from the ledger must go
# by when it decides which of two chats is the older, and where a mail is
# looked for again in a folder. Kept on a tombstone, like the other times.
P_ARRIVED_AT = KB + "arrivedAt"
# Valid kb:trim values: the deterministic trim settled the text; it did not and
# a model trim is due on first view; a model trim replaced the text.
TRIMS = ("deterministic", "wanting", "model")

# Field key ↔ predicate for the mail fields, by shape. One table each, read by
# the renderer, the parser and the writers alike, so a field cannot be written
# by one of them and forgotten by another.
_MAIL_LITERALS = (("subject_line", P_SUBJECT), ("sender_name", P_SENDER_NAME),
                  ("in_reply_to", P_IN_REPLY_TO), ("references", P_REFERENCES),
                  ("list_id", P_LIST_ID), ("trim", P_TRIM))
_MAIL_LISTS = (("recipients", P_RECIPIENT), ("cc", P_CC), ("reply_to", P_REPLY_TO))
_PARTS = (("body_part", P_BODY_PART), ("html_part", P_HTML_PART),
          ("header_part", P_HEADER_PART))

# Subdirectory (under the gateway's store dir) that holds the per-message files.
# The gateway owns this folder read-write; the life store mounts it read-only.
MESSAGES_SUBDIR = "messages"

# Subdirectory holding the durable media blobs referenced by P_ATTACHMENT. Blobs
# are named by a server-generated hex id (never an untrusted filename) with no
# RDF extension, so qlever-dir — which indexes only .nt/.ttl/.n3 plus declared
# converters — ignores them: the binaries sit on the same volume as the message
# .nt files without ever entering the triple store.
#
# It doubles as the spool for raw media (voice-note audio) retained for a message
# persisted before transcription — same volume, same durability, but referenced
# via P_MEDIA and unlinked once the message is transcribed and accounted for.

MEDIA_SUBDIR = "media"

_SLUG_RE = re.compile(r"[^a-z0-9]+")
# A stored media id is exactly token_hex(16) — 32 lowercase hex chars. Validating
# against this on read makes the GET /media/<id> path traversal-safe: an id that
# is not pure hex can never resolve to a file outside the media dir.
_MEDIA_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def subject_for(path) -> str | None:
    """The stored record's own subject URN, or None if it cannot be read.

    Used to give an id-less message the same :func:`thread_key` on the live
    path as it will get at the drain: the live forward has only the store path
    it just wrote, while the drain carries the parsed subject. Best-effort —
    an unreadable record simply falls back to a fresh random key.
    """
    if not path:
        return None
    try:
        fields = _parse(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None
    return (fields or {}).get("subject")


def thread_key(channel: str, account: str, chat: str | None,
               message_id: str | None, subject: str | None = None) -> str:
    """The canonical idempotency key for one inbound message's dashboard thread.

    Opening a thread is a side effect, and the same inbound can legitimately be
    handled twice — an escalation re-runs the turn's prompt, a channel
    redelivers a stanza after a reconnect, a live turn dies before finishing and
    a recovery sweep picks the record up again. All of those must land on one
    thread, so the key has to name the *message*, identically on the live path
    and on recovery.

    A channel-native message id alone will not do that. Telegram numbers
    messages per chat, Signal identifies one by (source, sent timestamp), and a
    deployment may run several gateways on the same channel (a system account
    and the owner's personal one), so two different messages can share an id.
    The key therefore carries the receiving account and the chat as well —
    together those are unique wherever the native id is.

    Without a native id there is nothing stable to recognise a redelivery by,
    and merging on anything coarser (an arrival second, say) would collapse
    genuinely distinct messages. `subject` — the record's own URN, unique per
    persisted message — is used when available, which keeps the drain's key
    stable for a record it has already seen; otherwise a fresh random value is
    minted, so distinct arrivals never collide even though a redelivery of an
    id-less message cannot be recognised as one.
    """
    if message_id:
        return ":".join((channel, account or "-", str(chat or "-"), str(message_id)))
    return subject or f"{channel}:{account or '-'}:{secrets.token_hex(8)}"


def _slug(value: str) -> str:
    """Lowercase IRI-safe token for a channel name (``signal``, ``whatsapp``…)."""
    return _SLUG_RE.sub("_", (value or "").strip().lower()).strip("_") or "unknown"


def messages_dir(store_dir: str | Path) -> Path:
    return Path(store_dir) / MESSAGES_SUBDIR


def media_dir(store_dir: str | Path) -> Path:
    return Path(store_dir) / MEDIA_SUBDIR


# -- N-Triples serialization --------------------------------------------------
# A tiny, self-contained N-Triples reader/writer. It supports exactly the three
# object shapes this store uses: an IRI object (rdf:type), a plain string
# literal, and a typed literal. Nothing here is a general RDF parser — it reads
# back what write_message emits, and keeps verbatim any other well-formed
# triple of that shape it finds on the record (see _parse).

# Characters a literal must not carry raw: every other C0 control and DEL, and
# the ones Python's str.splitlines() — and many a reader's line splitter —
# takes for a line end (\x85, U+2028, U+2029). A mail body has form feeds and
# pasted line separators; written raw, one would cut its triple in two and
# make the whole record unreadable. They are written as N-Triples \uXXXX.
_RAW_UNSAFE_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\x85  ]")
# A lone surrogate (what a decoder's surrogateescape leaves of an undecodable
# header byte) cannot be written as UTF-8 at all: it becomes U+FFFD.
_SURROGATE_RE = re.compile("[\ud800-\udfff]")


def _esc(value: str) -> str:
    value = _SURROGATE_RE.sub("�", value)
    value = (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
        .replace('"', '\\"')
    )
    return _RAW_UNSAFE_RE.sub(lambda m: f"\\u{ord(m.group(0)):04X}", value)


def _unesc(value: str) -> str:
    out, i = [], 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            # UCHAR: \uXXXX and \UXXXXXXXX, which _esc writes for the
            # characters it must not write raw.
            width = {"u": 4, "U": 8}.get(nxt)
            if width:
                digits = value[i + 2:i + 2 + width]
                try:
                    if len(digits) != width:
                        raise ValueError(digits)
                    out.append(chr(int(digits, 16)))
                    i += 2 + width
                    continue
                except ValueError:
                    pass  # not a well-formed escape: read as before
            out.append(
                {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}.get(nxt, nxt)
            )
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _iri(subject: str, predicate: str, obj_iri: str) -> str:
    return f"<{subject}> <{predicate}> <{obj_iri}> ."


def _lit(subject: str, predicate: str, value: str, datatype: str | None = None) -> str:
    tail = f"^^<{datatype}>" if datatype else ""
    return f'<{subject}> <{predicate}> "{_esc(value)}"{tail} .'


# subject, predicate, then either <iri> or "literal"[^^<datatype>], trailing " ."
#
# The literal is matched possessively: no part of it can be read a second way,
# so the engine has nothing to come back to and need not remember each
# character it passed. Written the plain way, one long literal — a mail's text
# is one line of its record — costs about two hundred bytes of memory per
# character on every read of the record: a gigabyte for five megabytes.
_TRIPLE_SHAPE = (r'^<([^>]+)>\s+<([^>]+)>\s+'
                 r'(?:<([^>]+)>|"(%s)"(?:\^\^<([^>]+)>)?)\s*\.\s*$')
try:
    _TRIPLE_RE = re.compile(_TRIPLE_SHAPE % r'(?:[^"\\]++|\\.)*+')
except re.error:  # Python before 3.11 has no possessive quantifiers
    _TRIPLE_RE = re.compile(_TRIPLE_SHAPE % r'(?:[^"\\]|\\.)*')


def _media_refs(fields: dict) -> list[str]:
    """Every IRI the record's own subject points at, other than its type.

    The attachments and the mail parts, and the IRI object of any triple the
    parser kept without knowing its predicate — a reference a later version
    of this module may add. What the record states about these IRIs is the
    record's own, and is kept through a rewrite; a blob any of them names is
    this record's to unlink (delete_chat, tombstone_message)."""
    subj = fields.get("subject")
    refs = list(fields.get("attachments") or [])
    refs += [fields[key] for key, _pred in _PARTS if fields.get(key)]
    for line in fields.get("extra") or []:
        m = _TRIPLE_RE.match(line)
        if m and m.group(1) == subj and m.group(3):
            refs.append(m.group(3))
    return list(dict.fromkeys(r for r in refs if r))


def _render(fields: dict) -> str:
    """Deterministic sorted N-Triples for one message, either direction.

    ``fields`` carries the resolved values; the record's ``type`` selects the
    direction-specific triples (delivered/receivedAt for inbound, author/sentAt
    for outbound). Optional fields may be absent, and an absent field writes
    no triple. ``extra`` lines — triples the parser read but does not know —
    are written back verbatim, so a rewrite by this version never loses what a
    newer one stated."""
    subj = fields["subject"]
    rtype = fields.get("type") or T_INBOUND
    lines = [
        _iri(subj, RDF_TYPE, rtype),
        _lit(subj, P_CHANNEL, fields["channel"]),
        # Always present, if only empty: the parser and the web gateway's chat
        # query both require the triple.
        _lit(subj, P_TEXT, fields.get("text") or ""),
    ]
    if rtype == T_OUTBOUND:
        lines.append(_lit(subj, P_AUTHOR, fields["author"]))
        if fields.get("sent_at"):
            lines.append(_lit(subj, P_SENT_AT, fields["sent_at"], XSD_DATETIME))
    else:
        lines.append(_lit(subj, P_DELIVERED, "true" if fields["delivered"] else "false"))
        if fields.get("received_at"):
            lines.append(_lit(subj, P_RECEIVED_AT, fields["received_at"], XSD_DATETIME))
    # Required on inbound (the writer fills in "unknown"); on outbound only a
    # mail states it — its From, the account's own address.
    if fields.get("sender") is not None:
        lines.append(_lit(subj, P_SENDER, fields["sender"]))
    for key, pred in _MAIL_LITERALS:
        if fields.get(key):
            lines.append(_lit(subj, pred, fields[key]))
    for key, pred in _MAIL_LISTS:
        for value in dict.fromkeys(fields.get(key) or []):
            if value:
                lines.append(_lit(subj, pred, value))
    for key, pred in _PARTS:
        if fields.get(key):
            lines.append(_iri(subj, pred, fields[key]))
    if fields.get("removed_at"):
        lines.append(_lit(subj, P_REMOVED_AT, fields["removed_at"], XSD_DATETIME))
    if fields.get("arrived_at"):
        lines.append(_lit(subj, P_ARRIVED_AT, fields["arrived_at"], XSD_DATETIME))
    if fields.get("chat"):
        lines.append(_lit(subj, P_CHAT, fields["chat"]))
    # Both directions: the chat key alone does not identify a conversation
    # across accounts (see P_ACCOUNT).
    if fields.get("account"):
        lines.append(_lit(subj, P_ACCOUNT, fields["account"]))
    if fields.get("group"):
        lines.append(_lit(subj, P_GROUP, fields["group"]))
    if fields.get("message_id"):
        lines.append(_lit(subj, P_MESSAGE_ID, fields["message_id"]))
    # Multi-valued: one IRI triple per attachment reference (deduped, order-free
    # since the whole record is sorted before serialization).
    for url in dict.fromkeys(fields.get("attachments") or []):
        if url:
            lines.append(_iri(subj, P_ATTACHMENT, url))
    if fields.get("media"):
        lines.append(_lit(subj, P_MEDIA, fields["media"]))
    # What this gateway knows about each blob, on the blob's own IRI (see
    # P_CONTENT_TYPE). Only for references the record actually carries —
    # attachments and mail parts alike.
    refs = _media_refs(fields)
    for url, meta in sorted((fields.get("attachment_meta") or {}).items()):
        if url not in refs or not meta:
            continue
        if meta.get("content_type"):
            lines.append(_lit(url, P_CONTENT_TYPE, str(meta["content_type"])))
        for key, pred in (("size", P_BYTE_SIZE), ("width", P_WIDTH), ("height", P_HEIGHT)):
            if isinstance(meta.get(key), int) and meta[key] >= 0:
                lines.append(_lit(url, pred, str(meta[key]), XSD_INTEGER))
        if meta.get("file_name"):
            lines.append(_lit(url, P_FILE_NAME, str(meta["file_name"])))
        if meta.get("content_id"):
            lines.append(_lit(url, P_CONTENT_ID, str(meta["content_id"])))
    # Triples the parser kept without knowing them, verbatim: those on the
    # record itself, and those on an IRI it still references (a statement
    # about a blob the record let go of goes with it). A line that no longer
    # reads as a triple is never written — one would make the file unreadable.
    for line in fields.get("extra") or []:
        m = _TRIPLE_RE.match(line)
        if m and (m.group(1) == subj or m.group(1) in refs):
            lines.append(line)
    return "".join(l + "\n" for l in sorted(dict.fromkeys(lines)))


def _parse(text: str) -> dict | None:
    """Read a message file back into a ``fields`` dict, or None if unparseable.

    The record ``type`` defaults to inbound: every file written before
    ``kb:OutboundMessage`` existed carries an explicit inbound type, so an
    absent type triple can only be a legacy hand-crafted record — it keeps the
    pre-outbound contract (drainable) rather than being mistaken for a send.

    Strict about what it accepts as a record — one line that is no triple of
    the shapes above makes the whole file unreadable (None), exactly as
    before — and lenient about what a record may say: a well-formed triple on
    the record's subject, or on an IRI it references, whose predicate this
    version does not know is kept verbatim in ``extra`` and written back by
    :func:`_render`, so no rewrite here ever drops a newer writer's facts.

    The mail fields (see :data:`P_SUBJECT`) read as ``subject_line``,
    ``sender_name``, ``recipients``, ``cc`` and ``reply_to`` (lists),
    ``in_reply_to``,
    ``references`` (the space-separated literal as stored), ``list_id``,
    ``body_part``/``html_part``/``header_part`` (media IRIs), ``trim``,
    ``removed_at`` and ``arrived_at`` (ISO-8601); an inline part's Content-ID is
    ``attachment_meta[<reference>]["content_id"]``. ``attachment_meta`` is
    keyed by every media IRI the record references, the parts included.
    """
    fields: dict = {"type": T_INBOUND, "delivered": False, "group": None,
                    "message_id": None, "chat": None, "account": None,
                    "author": None, "sent_at": None, "attachments": [],
                    "media": None, "attachment_meta": {}, "sender": None,
                    "removed_at": None, "arrived_at": None, "extra": []}
    for key, _pred in _MAIL_LITERALS + _PARTS:
        fields[key] = None
    for key, _pred in _MAIL_LISTS:
        fields[key] = []
    literals = {pred: key for key, pred in _MAIL_LITERALS}
    lists = {pred: key for key, pred in _MAIL_LISTS}
    parts = {pred: key for key, pred in _PARTS}
    triples = []
    # Lines are what the writer ends with "\n", and nothing else: a character
    # str.splitlines() would also break on (a form feed, U+2028 in a literal
    # some other writer left raw) is part of its triple, not a line end.
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        m = _TRIPLE_RE.match(line)
        if not m:
            return None
        triples.append((line, m.groups()))
    # The record's own subject is the one that carries kb:channel; a file
    # holds one message, so every other subject is a blob the message
    # references, and its triples are what the gateway stated about it.
    subject = next((g[0] for _line, g in triples if g[1] == P_CHANNEL), None)
    if subject is None:
        return None
    fields["subject"] = subject
    others = []
    for line, (subj, pred, obj_iri, lit, _dtype) in triples:
        value = obj_iri if obj_iri is not None else _unesc(lit)
        if subj != subject:
            others.append((line, subj, pred, value))
            continue
        if pred in literals:
            fields[literals[pred]] = value
        elif pred in lists:
            if value:
                fields[lists[pred]].append(value)
        elif pred in parts:
            fields[parts[pred]] = value or None
        elif pred == P_REMOVED_AT:
            fields["removed_at"] = value
        elif pred == P_ARRIVED_AT:
            fields["arrived_at"] = value
        elif pred == RDF_TYPE:
            fields["type"] = value
        elif pred == P_CHANNEL:
            fields["channel"] = value
        elif pred == P_SENDER:
            fields["sender"] = value
        elif pred == P_GROUP:
            fields["group"] = value
        elif pred == P_RECEIVED_AT:
            fields["received_at"] = value
        elif pred == P_TEXT:
            fields["text"] = value
        elif pred == P_MESSAGE_ID:
            fields["message_id"] = value
        elif pred == P_CHAT:
            fields["chat"] = value
        elif pred == P_ACCOUNT:
            fields["account"] = value
        elif pred == P_AUTHOR:
            fields["author"] = value
        elif pred == P_SENT_AT:
            fields["sent_at"] = value
        elif pred == P_ATTACHMENT:
            # IRI object → obj_iri is set; append the reference URL.
            if value:
                fields["attachments"].append(value)
        elif pred == P_MEDIA:
            fields["media"] = value
        elif pred == P_DELIVERED:
            fields["delivered"] = value.strip().lower() == "true"
        else:
            fields["extra"].append(line)
    # Statements about a blob the record does not reference are noise, never
    # re-emitted; statements about one it does are kept whether or not the
    # blob still exists here — they are the record's, not the filesystem's.
    refs = set(_media_refs(fields))
    for line, subj, pred, value in others:
        if subj not in refs:
            continue
        meta = fields["attachment_meta"].setdefault(subj, {})
        if pred == P_CONTENT_TYPE:
            meta["content_type"] = value
        elif pred == P_FILE_NAME:
            meta["file_name"] = value
        elif pred == P_CONTENT_ID:
            meta["content_id"] = value
        elif pred in (P_BYTE_SIZE, P_WIDTH, P_HEIGHT):
            try:
                meta[{P_BYTE_SIZE: "size", P_WIDTH: "width",
                      P_HEIGHT: "height"}[pred]] = int(value)
            except ValueError:
                fields["extra"].append(line)  # kept as it was, not guessed at
        elif pred != P_CHANNEL:
            # Not kb:channel: a second subject carrying it would make the
            # record's own subject ambiguous on the next read.
            fields["extra"].append(line)
    fields["attachment_meta"] = {
        url: meta for url, meta in fields["attachment_meta"].items() if meta}
    return fields


def _atomic_write(content: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _iso(ts: float) -> str:
    """UTC ISO-8601 with a trailing Z — lexicographically sortable and a valid
    ``xsd:dateTime``."""
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _parse_since(since: str | float | None) -> float | None:
    """Normalize a ``since`` filter (ISO-8601 string or epoch) to epoch seconds."""
    if since is None:
        return None
    if isinstance(since, (int, float)):
        return float(since)
    text = str(since).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        norm = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(norm)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def _received_epoch(fields: dict) -> float:
    """Epoch seconds of a parsed message's receivedAt (0.0 if unreadable)."""
    val = fields.get("received_at") or ""
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _one(value) -> str | None:
    """A single-valued optional field: its text trimmed, None when empty."""
    if value is None:
        return None
    return str(value).strip() or None


def _many(values) -> list[str]:
    """A multi-valued optional field: one string or several, trimmed, empties
    dropped, duplicates dropped with the first position kept."""
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    return list(dict.fromkeys(v for v in (_one(x) for x in values) if v))


def _check_trim(trim) -> str | None:
    trim = _one(trim)
    if trim is not None and trim not in TRIMS:
        raise ValueError(f"trim must be one of {'|'.join(TRIMS)}, got {trim!r}")
    return trim


def _mail_fields(*, subject=None, sender_name=None, recipients=None, cc=None,
                 in_reply_to=None, references=None, list_id=None, body_part=None,
                 html_part=None, header_part=None, trim=None, arrived_at=None,
                 reply_to=None) -> dict:
    """The mail fields of a new record, normalised (see :data:`P_SUBJECT`).

    ``references`` is the References header's ids in header order, as one
    string or as a sequence; either way it is stored as one literal, the ids
    separated by single spaces. Ids, addresses and names are stored as given
    (trimmed): normalising them — angle brackets, case — is the gateway's.
    ``arrived_at`` is epoch seconds (see :data:`P_ARRIVED_AT`)."""
    if references is not None and not isinstance(references, str):
        references = " ".join(str(r) for r in references if r)
    return {
        "arrived_at": None if arrived_at is None else _iso(float(arrived_at)),
        "subject_line": _one(subject),
        "sender_name": _one(sender_name),
        "recipients": _many(recipients),
        "cc": _many(cc),
        "reply_to": _many(reply_to),
        "in_reply_to": _one(in_reply_to),
        "references": " ".join(str(references or "").split()) or None,
        "list_id": _one(list_id),
        "body_part": _one(body_part),
        "html_part": _one(html_part),
        "header_part": _one(header_part),
        "trim": _check_trim(trim),
    }


def _record_blob_meta(store_dir, fields: dict, content_ids) -> dict:
    """``attachment_meta`` for a new record: what this store knows about every
    blob it references (attachments and parts), plus each inline
    attachment's Content-ID as the caller gave it."""
    meta = _own_attachment_meta(store_dir, _media_refs(fields))
    for ref, cid in (content_ids or {}).items():
        cid = _one(cid)
        if cid and ref in fields["attachments"]:
            meta.setdefault(ref, {})["content_id"] = cid
    return meta


# -- public API ---------------------------------------------------------------

def write_message(
    store_dir: str | Path,
    *,
    channel: str,
    sender: str,
    text: str,
    group: str | None = None,
    chat: str | None = None,
    account: str | None = None,
    message_id: str | None = None,
    timestamp: float | None = None,
    delivered: bool = False,
    attachment_urls: list[str] | None = None,
    media: str | None = None,
    subject: str | None = None,
    sender_name: str | None = None,
    recipients: list[str] | None = None,
    cc: list[str] | None = None,
    in_reply_to: str | None = None,
    references: str | list[str] | None = None,
    list_id: str | None = None,
    body_part: str | None = None,
    html_part: str | None = None,
    header_part: str | None = None,
    trim: str | None = None,
    content_ids: dict | None = None,
    arrived_at: float | None = None,
    reply_to: list[str] | None = None,
) -> tuple[str, Path]:
    """Persist one inbound message as a deterministic N-Triples file.

    Returns ``(subject_uri, path)``. ``delivered=False`` (the default) marks the
    message as not yet in front of the user; pass ``delivered=True`` for a
    message the gateway is deliberately *not* handing over (an ignored group) so
    a recovery sweep never re-surfaces it.

    ``chat`` is the chat key (see :data:`P_CHAT`): the exact recipient string
    this channel's own send path accepts, computed by the gateway and persisted
    verbatim, so this message and any reply sent back to it carry the same key.
    ``account`` is the gateway's own ``*_ACCOUNT`` (see :data:`P_ACCOUNT`) — the
    other half of the identity, because a chat key is unique only within one
    account. ``message_id`` is the channel-native message identifier, which is
    what a reaction or quoted reply later targets.

    ``attachment_urls`` are references to this message's media (voice note,
    image), each emitted as a ``kb:attachment`` IRI — see :data:`P_ATTACHMENT`
    for the shape. The bytes are never inlined into the graph; see
    :func:`store_media`. For every reference naming a blob in *this* store,
    what the store knows about it (type, size, pixel size) is stated on the
    reference in the same record — see :data:`P_CONTENT_TYPE`.

    ``media`` optionally records a reference (a durable file path) to raw media
    retained alongside this message — used by the persist-before-transcribe path
    so a voice note survives a failed or crashed STT run. Unlike
    ``attachment_urls`` it is transient bookkeeping, cleared by
    :func:`update_message` once the transcript is in.

    The remaining arguments are a mail's (see :data:`P_SUBJECT` and
    docs/mail-chats.md, "The record"): ``subject`` its Subject, ``sender_name``
    the display name on From, ``recipients``/``cc`` the To/Cc addresses,
    ``reply_to`` the Reply-To addresses (see :data:`P_REPLY_TO`),
    ``in_reply_to``/``references`` its threading headers, ``list_id``,
    ``body_part``/``html_part``/``header_part`` the media references of its
    parts (stored with :func:`store_media` first, like attachments, so their
    blob facts are stated), ``trim`` one of :data:`TRIMS` (anything else is a
    programming error and raises ``ValueError``), ``content_ids``
    ``{attachment reference: Content-ID}`` for its inline parts, and
    ``arrived_at`` when the mailbox server received the mail (epoch seconds;
    see :data:`P_ARRIVED_AT` — ``timestamp`` is the mail's time on record,
    which its sender may have written). Every one defaults to absent, and an
    absent one writes no triple.
    """
    ts = time.time() if timestamp is None else float(timestamp)
    token = secrets.token_hex(8)
    urn = f"urn:retinue:inbound:{_slug(channel)}:{token}"
    fields = {
        **_mail_fields(subject=subject, sender_name=sender_name, recipients=recipients,
                       cc=cc, in_reply_to=in_reply_to, references=references,
                       list_id=list_id, body_part=body_part, html_part=html_part,
                       header_part=header_part, trim=trim, arrived_at=arrived_at,
                       reply_to=reply_to),
        "type": T_INBOUND,
        "subject": urn,
        "channel": channel,
        "sender": sender or "unknown",
        "text": text or "",
        "group": group or None,
        "chat": chat or None,
        "account": account or None,
        "message_id": message_id or None,
        "received_at": _iso(ts),
        "delivered": bool(delivered),
        "attachments": [u for u in (attachment_urls or []) if u],
        "media": media or None,
    }
    fields["attachment_meta"] = _record_blob_meta(store_dir, fields, content_ids)
    # Filename: zero-padded epoch millis (sortable) + token (unique, IRI-safe).
    fname = f"{int(ts * 1000):016d}-{token}.nt"
    path = messages_dir(store_dir) / fname
    _atomic_write(_render(fields), path)
    return urn, path


def write_outbound(
    store_dir: str | Path,
    *,
    channel: str,
    chat: str,
    text: str,
    author: str = "agent",
    account: str | None = None,
    message_id: str | None = None,
    timestamp: float | None = None,
    attachment_urls: list[str] | None = None,
    sender: str | None = None,
    sender_name: str | None = None,
    subject: str | None = None,
    recipients: list[str] | None = None,
    cc: list[str] | None = None,
    in_reply_to: str | None = None,
    references: str | list[str] | None = None,
    list_id: str | None = None,
    body_part: str | None = None,
    html_part: str | None = None,
    header_part: str | None = None,
    trim: str | None = None,
    content_ids: dict | None = None,
    arrived_at: float | None = None,
    reply_to: list[str] | None = None,
) -> tuple[str, Path]:
    """Persist one successfully sent outbound message as its own N-Triples file.

    The sibling of :func:`write_message`: one deterministic file per message, in
    the same ``messages/`` directory, so a chat's two directions form a single
    timeline (filenames sort by epoch millis regardless of direction). A gateway
    writes this only once a send has actually gone out — queued pending sends
    are not messages and never reach the store.

    ``chat`` is the chat key (see :data:`P_CHAT`) — for a send, the resolved
    recipient — and ``account`` the identity it went out as (see
    :data:`P_ACCOUNT`), which for a send is not bookkeeping but the record of
    *who the recipient saw*: the same text to the same peer from two accounts
    are two different events. ``author`` must be one of :data:`AUTHORS`;
    anything else is a programming error and raises ``ValueError``. ``timestamp`` is the sent-at
    instant (``kb:sentAt`` — see the predicate comment for why it is not
    ``kb:receivedAt``), defaulting to now; pass the channel-reported send
    timestamp when the client returns one, along with its ``message_id``.

    There is deliberately **no** delivered flag: the delivery ledger tracks what
    is owed to *triage*, and an outbound message never is — :func:`undelivered`
    and :func:`mark_delivered` filter by record type, so these files can never
    surface in the drain.

    A mail also states its From — ``sender`` (the account's own address, or
    the alias it went out as) and ``sender_name`` — and takes the same mail
    arguments as :func:`write_message`; a messenger send passes none of them
    and its record is what it always was.
    """
    if author not in AUTHORS:
        raise ValueError(f"author must be one of {'|'.join(AUTHORS)}, got {author!r}")
    ts = time.time() if timestamp is None else float(timestamp)
    token = secrets.token_hex(8)
    urn = f"urn:retinue:outbound:{_slug(channel)}:{token}"
    fields = {
        **_mail_fields(subject=subject, sender_name=sender_name, recipients=recipients,
                       cc=cc, in_reply_to=in_reply_to, references=references,
                       list_id=list_id, body_part=body_part, html_part=html_part,
                       header_part=header_part, trim=trim, arrived_at=arrived_at,
                       reply_to=reply_to),
        "type": T_OUTBOUND,
        "subject": urn,
        "channel": channel,
        "chat": chat or "unknown",
        "account": account or None,
        "text": text or "",
        "author": author,
        "sender": _one(sender),
        "message_id": message_id or None,
        "sent_at": _iso(ts),
        "attachments": [u for u in (attachment_urls or []) if u],
    }
    fields["attachment_meta"] = _record_blob_meta(store_dir, fields, content_ids)
    fname = f"{int(ts * 1000):016d}-{token}.nt"
    path = messages_dir(store_dir) / fname
    _atomic_write(_render(fields), path)
    return urn, path


def _image_dimensions(data: bytes) -> tuple[int, int] | None:
    """Best-effort (width, height) from an image blob's header, else None.

    A deliberately tiny magic-number parser — PNG (IHDR), GIF (logical screen
    descriptor), JPEG (the first SOF segment), WebP (VP8 / VP8L / VP8X), AVIF
    (the first ``ispe`` image-spatial-extents property) — so
    the chat surface can tell the client an image's intrinsic size and the
    bubble reserves its box before the bytes arrive. Detection is by magic, not
    by declared content type, so non-images (voice notes) simply miss. Never
    raises: any malformed or truncated header is an honest None.
    """
    try:
        if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
            w = int.from_bytes(data[16:20], "big")
            h = int.from_bytes(data[20:24], "big")
            return (w, h) if w and h else None
        if len(data) >= 10 and data[:6] in (b"GIF87a", b"GIF89a"):
            w = int.from_bytes(data[6:8], "little")
            h = int.from_bytes(data[8:10], "little")
            return (w, h) if w and h else None
        if len(data) >= 4 and data[:2] == b"\xff\xd8":
            # Walk the JPEG segments to the first start-of-frame. SOF markers
            # are 0xC0–0xCF minus DHT (C4), JPG (C8) and DAC (CC); its payload
            # is precision(1), height(2 BE), width(2 BE).
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker == 0xFF:
                    i += 1  # fill byte
                    continue
                if marker in (0x00, 0x01) or 0xD0 <= marker <= 0xD9:
                    i += 2  # standalone marker, no length field
                    continue
                seg_len = int.from_bytes(data[i + 2:i + 4], "big")
                if seg_len < 2:
                    return None
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    h = int.from_bytes(data[i + 5:i + 7], "big")
                    w = int.from_bytes(data[i + 7:i + 9], "big")
                    return (w, h) if w and h else None
                i += 2 + seg_len
            return None
        if len(data) >= 30 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            fourcc = data[12:16]
            if fourcc == b"VP8X":  # extended: 24-bit canvas size minus one
                w = int.from_bytes(data[24:27], "little") + 1
                h = int.from_bytes(data[27:30], "little") + 1
                return w, h
            if fourcc == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":  # lossy
                w = int.from_bytes(data[26:28], "little") & 0x3FFF
                h = int.from_bytes(data[28:30], "little") & 0x3FFF
                return (w, h) if w and h else None
            if fourcc == b"VP8L" and data[20] == 0x2F:  # lossless: 14+14 bits
                bits = int.from_bytes(data[21:25], "little")
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        if len(data) >= 16 and data[4:8] == b"ftyp":
            # AVIF: an ISO-BMFF file whose ftyp box lists an AVIF brand. The
            # size lives in an `ispe` property (version/flags(4), width(4 BE),
            # height(4 BE)) in the meta box near the start; the first one is
            # the primary item's in the files encoders write.
            ftyp_len = int.from_bytes(data[:4], "big")
            brands = data[8:12] + data[16:max(16, min(ftyp_len, 256))]
            if not any(brands[i:i + 4] in (b"avif", b"avis")
                       for i in range(0, len(brands) - 3, 4)):
                return None
            at = data.find(b"ispe", 0, 1 << 16)
            if at < 0 or at + 16 > len(data):
                return None
            w = int.from_bytes(data[at + 8:at + 12], "big")
            h = int.from_bytes(data[at + 12:at + 16], "big")
            return (w, h) if w and h else None
        return None
    except Exception:  # noqa: BLE001 - sniffing must never cost the blob
        return None


def media_kind(content_type: str | None) -> str:
    """``image`` / ``audio`` / ``video`` / ``file`` from a content type.

    The one reading every gateway applies when deciding what to do with an
    inbound medium: images and documents are forwarded to the agent when
    they fit, audio is a voice note to transcribe, a video is kept for the
    chat only. Anything unlabeled or unrecognised is a ``file``."""
    ct = (content_type or "").strip().lower()
    for kind in ("image", "audio", "video"):
        if ct.startswith(kind + "/"):
            return kind
    return "file"


# What a stated content type is kept as: printable ASCII on one line. Any run
# of anything else — a line break, a tab, a control character, a character
# outside ASCII — is one blank.
_NOT_TYPE_TEXT_RE = re.compile(r"[^\x21-\x7e]+")
# As long as a type/subtype pair can be (RFC 6838, 4.2: 127 characters each).
_CONTENT_TYPE_MAX_LEN = 255


def clean_content_type(value) -> str:
    """A stated content type reduced to one clean line.

    The type of an inbound medium is what the sender's client declared: a line
    of text from outside. It is written to the blob's sidecar, stated in the
    record and sent back as the ``Content-Type`` of ``GET /media/<id>``, and a
    header is exactly one line. So a line break, a tab, a control character
    or anything outside ASCII becomes a blank, the ends are trimmed and
    overlong text is cut; when nothing is left, the type is the generic one.

    That makes the text safe to keep and to put in a header. It does not make
    it a valid media type: what a blob may be shown as is decided where it is
    served to a browser, in the web gateway's media proxy."""
    text = _NOT_TYPE_TEXT_RE.sub(" ", str(value or "")).strip()
    return text[:_CONTENT_TYPE_MAX_LEN].rstrip() or "application/octet-stream"


def _stored_content_type(d: Path, media_id: str) -> str:
    """The content type a blob's ``.type`` sidecar holds, as one clean line.

    Cleaned on the way out as on the way in (:func:`clean_content_type`),
    because a sidecar written before the store did that holds whatever was
    declared. A missing or unreadable sidecar is the generic type."""
    try:
        raw = (d / (media_id + ".type")).read_text(encoding="utf-8", errors="replace")
    except OSError:
        raw = ""
    return clean_content_type(raw)


def safe_file_name(name) -> str | None:
    """A sender-supplied file name reduced to something safe to show and save.

    Base name only (no path), control characters dropped, capped in length
    with the extension kept. None when nothing usable remains. Never used to
    address a file here — blobs are keyed by their own id — only to say what
    the sender called it. A lone surrogate (a name decoded under a charset
    its sender chose can hold one) becomes U+FFFD: the name is written to a
    sidecar and stated in a record, and neither can hold one."""
    text = _SURROGATE_RE.sub("�", str(name or "")).replace("\\", "/").strip()
    text = re.sub(r"[\x00-\x1f\x7f]", "", text.rsplit("/", 1)[-1]).strip()
    if text in ("", ".", ".."):
        return None
    if len(text) > 200:
        stem, dot, ext = text.rpartition(".")
        ext = ("." + ext) if dot and len(ext) <= 12 else ""
        text = (stem if ext else text)[:200 - len(ext)] + ext
    return text


# Each outbound attachment is materialized in a directory of its own, so the
# file can carry the sender's name for it: the channel shows the recipient the
# basename of what it was handed, and a document called "wa-push-k3x9.pdf"
# says nothing.
OUTBOUND_DIR_PREFIX = "retinue-out-"


def materialize_outbound(entry, tmp_dir: str | Path | None = None) -> tuple[Path, str, str | None]:
    """Write one /send attachment entry to disk, for the channel to send.

    ``entry`` is an item of the /send payload's ``images`` list — the key is
    historical, it carries a file of any type: ``{"data": <base64>,
    "filename": ..., "content_type": ...}``, the last two optional. Returns
    ``(path, content_type, name)``: the file, under its own (sanitized) name
    so the recipient sees what the sender called it; its content type, the
    declared one or else read off the name; and the name as given, None when
    the entry carried none. An entry with neither name nor type is treated as
    a JPEG, which is what such entries were before other files could be sent.

    Raises ValueError on a malformed entry. Remove the file again with
    :func:`discard_outbound`."""
    if not isinstance(entry, dict):
        raise ValueError("each attachment must be an object with base64 'data'")
    data_b64 = entry.get("data")
    if not data_b64 or not isinstance(data_b64, str):
        raise ValueError("attachment entry missing base64 'data'")
    try:
        raw = base64.b64decode(data_b64, validate=True)
    except ValueError as exc:  # binascii.Error subclasses ValueError
        raise ValueError(f"invalid base64 attachment data: {exc}") from exc
    name = safe_file_name(entry.get("filename"))
    declared = entry.get("content_type")
    declared = declared.split(";", 1)[0].strip().lower() if isinstance(declared, str) else ""
    if name:
        on_disk = name
    elif declared:
        on_disk = "attachment" + (mimetypes.guess_extension(declared) or ".bin")
    else:
        on_disk = "image.jpg"
    content_type = declared or mimetypes.guess_type(on_disk)[0] or "application/octet-stream"
    folder = Path(tempfile.mkdtemp(prefix=OUTBOUND_DIR_PREFIX,
                                   dir=str(tmp_dir) if tmp_dir else None))
    path = folder / on_disk
    try:
        path.write_bytes(raw)
    except OSError:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return path, content_type, name


def discard_outbound(path: str | Path) -> None:
    """Remove a file :func:`materialize_outbound` wrote, with its directory.

    Any other path is simply unlinked, so a caller can hand over every
    temporary file of a send without telling the two kinds apart."""
    path = Path(path)
    if path.parent.name.startswith(OUTBOUND_DIR_PREFIX):
        shutil.rmtree(path.parent, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


def store_media(store_dir: str | Path, data: bytes, content_type: str | None,
                file_name: str | None = None) -> str:
    """Persist one inbound media blob durably and return its server-generated id.

    The blob is keyed by ``token_hex(16)`` — never by an untrusted filename — so
    the id is path-safe by construction and reveals nothing about the sender. The
    ``content_type`` is written to a ``<id>.type`` sidecar so the serving endpoint
    can set the ``Content-Type`` without asking the requesting client. It is
    what the sender's client declared, so it is kept as one clean line
    (:func:`clean_content_type`).
    For an image blob (detected by magic — see :func:`_image_dimensions`) the
    intrinsic size is written to a ``<id>.meta`` JSON sidecar
    (``{"width", "height"}``) so the chat surface can reserve the image box
    before the bytes arrive; an absent sidecar means unknown, exactly the
    pre-sidecar behaviour. ``file_name``, when the channel carried one, goes to a
    ``<id>.name`` sidecar (see :func:`safe_file_name`). None of these files
    carries an RDF extension, so the life store never indexes them.

    The caller builds the reference — a host-free
    ``urn:retinue:media:<channel>:<id>``, see :data:`P_ATTACHMENT` — and passes
    it to :func:`write_message` as an ``attachment_urls`` entry; the bytes stay
    on disk and out of the graph, and the reader resolves the reference through
    the account that owns the chat. The sidecars are this store's private
    format: what they hold is stated in the record (:data:`P_CONTENT_TYPE`),
    which is where a reader learns it.
    """
    media_id = secrets.token_hex(16)
    d = media_dir(store_dir)
    d.mkdir(parents=True, exist_ok=True)
    blob = d / media_id
    tmp = blob.with_suffix(".tmp")
    tmp.write_bytes(data or b"")
    os.replace(tmp, blob)
    _atomic_write(clean_content_type(content_type) + "\n", d / (media_id + ".type"))
    name = safe_file_name(file_name)
    if name:
        _atomic_write(name + "\n", d / (media_id + ".name"))
    dims = _image_dimensions(data or b"")
    if dims:
        _atomic_write(json.dumps({"width": dims[0], "height": dims[1]}) + "\n",
                      d / (media_id + ".meta"))
    return media_id


def load_media(store_dir: str | Path, media_id: str) -> tuple[bytes, str] | None:
    """Return ``(bytes, content_type)`` for a stored media id, or None.

    Validates the id against :data:`_MEDIA_ID_RE` before touching the filesystem,
    so a crafted ``media_id`` can never escape the media dir (path traversal). The
    content type falls back to ``application/octet-stream`` if the sidecar is
    missing or unreadable, and is one clean line whatever the sidecar holds
    (:func:`clean_content_type`): the caller sends it as a response header.
    """
    if not _MEDIA_ID_RE.match(media_id or ""):
        return None
    d = media_dir(store_dir)
    try:
        data = (d / media_id).read_bytes()
    except OSError:
        return None
    return data, _stored_content_type(d, media_id)


# A media reference's blob id: the URN a gateway records today, or the
# ``http://<service>:<port>/media/<id>`` form written before it existed.
_MEDIA_REF_RE = re.compile(r"(?:^urn:retinue:media:[^:]+:|/media/)([0-9a-f]{32})/?$")


def media_id_of(reference: str) -> str | None:
    """The blob id a ``kb:attachment`` reference names, or None."""
    m = _MEDIA_REF_RE.search((reference or "").strip())
    return m.group(1) if m else None


def media_meta(store_dir: str | Path, media_id: str) -> dict | None:
    """What this store knows about one of its blobs, or None if it holds none.

    ``{"content_type", "size"}`` plus ``{"width", "height"}`` when the
    ``.meta`` sidecar carries them and ``"file_name"`` when the ``.name`` one
    does — the same facts :func:`store_media` wrote,
    read back by the store that wrote them. This is the only reader of the
    sidecars besides :func:`load_media`: a record states these on the media
    IRI (see :data:`P_CONTENT_TYPE`) so nothing outside the gateway needs to.
    """
    if not _MEDIA_ID_RE.match(media_id or ""):
        return None
    d = media_dir(store_dir)
    try:
        size = (d / media_id).stat().st_size
    except OSError:
        return None
    meta: dict = {"size": int(size), "content_type": _stored_content_type(d, media_id)}
    try:
        name = safe_file_name((d / (media_id + ".name")).read_text(encoding="utf-8"))
        if name:
            meta["file_name"] = name
    except OSError:
        pass
    try:
        dims = json.loads((d / (media_id + ".meta")).read_text(encoding="utf-8"))
        w, h = dims.get("width"), dims.get("height")
        if isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0:
            meta["width"], meta["height"] = w, h
    except (OSError, ValueError, AttributeError):
        pass
    return meta


def _own_attachment_meta(store_dir: str | Path, references: list) -> dict:
    """``{reference: media_meta}`` for the references naming blobs in this store."""
    out: dict = {}
    for ref in references or []:
        mid = media_id_of(ref)
        meta = media_meta(store_dir, mid) if mid else None
        if meta:
            out[ref] = meta
    return out


def backfill_media_meta(store_dir: str | Path) -> int:
    """State the blob metadata on records written before it was recorded.

    Walks this store's messages once, and rewrites every record that
    references a blob this store holds (an attachment or a mail part) but
    says less about it than
    :func:`media_meta` knows. Idempotent — a second run rewrites nothing —
    and never raises: a record that cannot be read or written is skipped.
    A gateway calls this at startup, on its own store only, so the reader
    never has to fall back to anyone's files. Returns the rewrite count.
    """
    markers = [f"<{p}>" for p in (P_ATTACHMENT, *(pred for _k, pred in _PARTS))]
    count = 0
    try:
        paths = sorted(messages_dir(store_dir).glob("*.nt"))
    except OSError:
        return 0
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not any(marker in text for marker in markers):
            continue
        fields = _parse(text)
        if not fields:
            continue
        known = _own_attachment_meta(store_dir, _media_refs(fields))
        stated = fields.get("attachment_meta") or {}
        # Compared key by key: a record also states what no sidecar holds (a
        # Content-ID), and that must neither force a rewrite on every start
        # nor be lost when one happens.
        if all(stated.get(ref, {}).get(key) == value
               for ref, meta in known.items() for key, value in meta.items()):
            continue
        fields["attachment_meta"] = {
            ref: {**stated.get(ref, {}), **known.get(ref, {})}
            for ref in {**stated, **known}}
        try:
            _atomic_write(_render(fields), path)
        except OSError:
            continue
        count += 1
    return count


def undelivered(
    store_dir: str | Path,
    since: str | float | None = None,
) -> list[dict]:
    """Return messages that never reached the user, **marking each delivered**.

    This is the sole mutator of the ``delivered`` flag. It scans the message
    files oldest-first, selects those with ``delivered == false`` (and, when
    ``since`` is given, ``receivedAt >= since``), rewrites each of those files
    with ``delivered = true``, and returns the selected messages as dicts. A
    plain SPARQL read never calls this, so browsing history does not consume
    anything; only the recovery sweep behind ``/undelivered`` does.

    Each returned dict has: ``subject``, ``channel``, ``sender``, ``group``,
    ``chat``, ``message_id``, ``received_at`` (ISO-8601), ``text``,
    ``attachments`` (a possibly-empty list of HTTP media reference URLs) and
    ``media`` (the local path of an as-yet-untranscribed voice note, else None).
    """
    mdir = messages_dir(store_dir)
    if not mdir.is_dir():
        return []
    cutoff = _parse_since(since)
    out: list[dict] = []
    for path in sorted(mdir.glob("*.nt")):
        try:
            fields = _parse(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        # Inbound records only: outbound messages share this directory and carry
        # no delivered flag (which parses as the False default) — without the
        # type filter every send would be handed to triage as if it were
        # inbound mail. A file with no type triple is legacy inbound (_parse).
        if not fields or fields.get("type") != T_INBOUND or fields["delivered"]:
            continue
        if cutoff is not None and _received_epoch(fields) < cutoff:
            continue
        fields["delivered"] = True
        try:
            _atomic_write(_render(fields), path)
        except OSError:
            # Could not flip the flag — skip it rather than hand it out and risk
            # a second delivery on the next drain. It stays owed for later.
            continue
        out.append({
            "subject": fields["subject"],
            "channel": fields["channel"],
            "sender": fields["sender"],
            "group": fields.get("group"),
            "chat": fields.get("chat"),
            "message_id": fields.get("message_id"),
            "received_at": fields["received_at"],
            "text": fields["text"],
            "attachments": fields.get("attachments") or [],
            "media": fields.get("media"),
        })
    return out


def mark_delivered(path: str | Path) -> bool:
    """Flip one already-written message's ``delivered`` flag to ``true``.

    This exists for the **persist-before-forward** path: a gateway writes an
    inbound message ``delivered = false`` the instant it arrives — before the
    gate, before the forward — so that a later failure (a throwing gate, a crash
    mid-forward, a killed container) leaves the message on disk for the daily
    drain instead of silently dropping it. Once triage actually has the message
    (a live forward succeeded, or it was held in a fully-accounted class), the
    gateway flips the flag here.

    It performs the same single false→true rewrite as :func:`undelivered`, but
    for one known file rather than a scan. Best-effort by design: it returns
    ``True`` on success (or if the flag was already ``true``), ``False`` if the
    file is missing/unreadable/unparseable, and **never raises** — a bookkeeping
    failure must not break message handling. A message left ``false`` by a failed
    flip is simply re-surfaced by the next drain (at-least-once), which is the
    safe direction.
    """
    p = Path(path)
    try:
        fields = _parse(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return False
    if not fields:
        return False
    # Delivered semantics exist for inbound records only; an outbound file has
    # no such flag and must never grow one through a stray flip.
    if fields.get("type") != T_INBOUND:
        return False
    if fields["delivered"]:
        return True
    fields["delivered"] = True
    try:
        _atomic_write(_render(fields), p)
    except OSError:
        return False
    return True


def update_message(path: str | Path, *, text: str | None = None,
                   clear_media: bool = False, chat: str | None = None,
                   trim: str | None = None, author: str | None = None) -> str | None:
    """Rewrite a persisted message's mutable fields in place; never raises
    on file trouble.

    Used by the **persist-before-transcribe** path: a voice note is written up
    front with empty text and a ``media`` reference to its retained audio, then
    once STT succeeds this fills in the transcript (``text=…``) and drops the
    now-superfluous audio reference (``clear_media=True``).

    A mail's record changes in two more ways. ``chat=`` re-keys it when two
    threads turn out to be one and merge (:func:`rekey_chat` does a whole
    chat); ``trim=`` (one of :data:`TRIMS` — anything else raises
    ``ValueError``, a programming error) says how ``text`` now derives from
    the parts, when a better trim replaces it. Nothing here can change a
    record's identity, its timestamps or its parts: what the mail *was* is
    immutable, only the reading of it is not. On a tombstone
    (:func:`tombstone_message`) ``text=`` and ``trim=`` are ignored — a trim
    finishing after the mail was found gone must not put its words back.

    ``author=`` (one of :data:`AUTHORS`, else ``ValueError``) corrects who an
    outbound record says sent it. A mail the mail gateway finds in the Sent
    folder is recorded as the own-device echo until it is told otherwise, and
    the telling can come after the finding (``POST /outbound-note``). It is
    ignored on an inbound record, which has no author to correct.

    Returns the ``media`` value present *before* the call — so a caller clearing
    it knows which file to unlink — or ``None`` if there was none or the rewrite
    failed. Only the fields named are touched; ``delivered`` and everything else
    are preserved.
    """
    trim = _check_trim(trim)
    chat = _one(chat)
    author = _one(author)
    if author is not None and author not in AUTHORS:
        raise ValueError(f"author must be one of {'|'.join(AUTHORS)}, got {author!r}")
    p = Path(path)
    try:
        fields = _parse(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None
    if not fields:
        return None
    prev_media = fields.get("media")
    removed = bool(fields.get("removed_at"))
    if text is not None and not removed:
        fields["text"] = text
    if trim is not None and not removed:
        fields["trim"] = trim
    if chat is not None:
        fields["chat"] = chat
    if author is not None and fields.get("type") == T_OUTBOUND:
        fields["author"] = author
    if clear_media:
        fields["media"] = None
    try:
        _atomic_write(_render(fields), p)
    except OSError:
        return None
    return prev_media


def iter_records(store_dir: str | Path):
    """Every record in this store, as ``(path, fields)``, in timeline order.

    Both directions, oldest first (filenames sort by epoch millis), each
    parsed as :func:`_parse` reads it — the mail fields included. For a
    gateway that must rebuild what it derives from the ledger (a thread index,
    a mailbox index) after losing its own state: the ledger is the record of
    what exists. Tolerant by design: a file that vanishes, cannot be read or
    does not parse is skipped, and a missing store yields nothing. Read-only;
    it never touches the delivered flag.
    """
    try:
        paths = sorted(messages_dir(store_dir).glob("*.nt"))
    except OSError:
        return
    for path in paths:
        try:
            fields = _parse(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        if fields:
            yield path, fields


def read_record(path: str | Path) -> dict | None:
    """One record's fields, as :func:`iter_records` yields them; None when the
    file is gone, cannot be read or does not parse. Read-only."""
    try:
        return _parse(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None


def rekey_chat(store_dir: str | Path, chat: str, account: str | None,
               into: str, *, paths=None) -> dict:
    """Move every record of one chat to another chat key (a thread merge).

    The chat is the exact (``kb:chat``, ``kb:account``) pair, as for
    :func:`delete_chat`; each of its records, both directions, is rewritten
    with ``kb:chat`` = ``into`` through :func:`update_message`, so nothing
    else of it changes. Fails closed in the same way: a record that cannot be
    read, or does not parse but names this chat, or cannot be rewritten, is
    counted in ``errors`` — the caller must not tell anyone the merge is done
    until a repeat comes back clean. Idempotent: a repeat finds nothing left
    under the old key. Returns ``{"messages", "errors"}``.

    Without ``paths`` every record of the store is read, which is the only
    way to find a chat's records from the ledger alone. A writer that keeps
    its own index of which record belongs to which chat (the mail gateway
    does) hands in those record files as ``paths`` and only they are read —
    a merge then costs the chat's size, not the store's. Each is still checked
    against the chat and the account before it is touched, and one of them
    that does not parse is an error whatever it names: the caller said it is
    this chat's.
    """
    wanted_chat = (chat or "").strip()
    wanted_account = (account or "").strip()
    into = (into or "").strip()
    result = {"messages": 0, "errors": 0}
    if not wanted_chat or not into or into == wanted_chat:
        return result
    chat_marker = f'<{P_CHAT}> "{_esc(wanted_chat)}"'
    named = paths is not None
    try:
        paths = (sorted({Path(p) for p in paths}) if named
                 else sorted(messages_dir(store_dir).glob("*.nt")))
    except OSError:
        return result
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except (OSError, UnicodeDecodeError):
            result["errors"] += 1
            continue
        fields = _parse(text)
        if not fields:
            if named or chat_marker in text:
                result["errors"] += 1
            continue
        if (fields.get("chat") != wanted_chat
                or (fields.get("account") or "") != wanted_account):
            continue
        update_message(path, chat=into)
        try:
            moved = _parse(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            moved = None
        if moved and moved.get("chat") == into:
            result["messages"] += 1
        else:
            result["errors"] += 1
    return result


def _blob_ids_referenced_elsewhere(store_dir: Path, ids: set[str],
                                   skip: set[Path]) -> set[str]:
    """Which of ``ids`` a record other than those in ``skip`` still names.

    Read off each record's raw text, not its parse: a record that no longer
    parses still references what it names. A record that cannot be read at
    all might name any of them, so then all of them count as named — a blob
    is never unlinked on a guess."""
    named: set[str] = set()
    if not ids:
        return named
    try:
        paths = sorted(messages_dir(store_dir).glob("*.nt"))
    except OSError:
        return set(ids)
    for path in paths:
        if path.resolve() in skip:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            continue
        except OSError:
            return set(ids)
        named.update(mid for mid in ids if mid in text)
        if named == ids:
            break
    return named


def tombstone_message(path: str | Path, removed_at: str | float | None = None, *,
                      own_blobs: bool = False) -> bool:
    """Strip one record to its identity: the mail is gone from the mailbox.

    For a mail deleted, or moved out of the mirrored folders, in a client
    (docs/mail-chats.md, "The mirror"). The record stays — a chat whose mails
    are all gone is archived, not erased — and keeps who, where and when:
    type, channel, chat, account, group, sender and sender name, author,
    message id, subject, the threading ids and the list id (a thread index
    rebuilt from the ledger needs every id a mail cited), its timestamps,
    and on an inbound record ``delivered`` — set true, since nothing is owed
    for a mail that no longer exists. It loses what the mail said and to
    whom: ``kb:text`` becomes the empty string (the triple stays: the parser
    and the web gateway's chat query require it), and the recipients, the Cc,
    the Reply-To addresses, the trim, the parts, the attachments with what was stated about them, a
    retained voice-note spool, and any triple this version does not know (it
    cannot tell identity from content there, and a tombstone keeps only what
    it knows to be identity). ``kb:removedAt`` is set — ``removed_at`` as
    epoch seconds or ISO-8601, else now.

    The blobs the record referenced are unlinked first, unless another record
    of the store still names one (:func:`_blob_ids_referenced_elsewhere`), and
    the record is rewritten after: if the rewrite fails the call returns
    False and a repeat finishes the job. Idempotent: a tombstone is left as it
    is, its first ``removedAt`` kept. Never raises; False only when the record
    cannot be read, parsed or rewritten.

    Finding out whether another record names a blob means reading every
    record of the store — per call. ``own_blobs=True`` is the caller's word
    that it need not be asked: every blob this record references was minted
    for this record alone, so they are unlinked without a look at the rest.
    Only a store's single writer can say that (the mail gateway stores each
    mail's parts for that mail's record and never cites a blob twice); the
    default keeps the check, which is what every other caller relies on.
    """
    try:
        p = Path(path)
        fields = _parse(p.read_text(encoding="utf-8"))
        if not fields:
            return False
        gone = [key for key, _pred in _MAIL_LISTS] + ["attachments", "extra"]
        stripped = {key for key in gone if fields.get(key)}
        stripped |= {key for key, _pred in _PARTS if fields.get(key)}
        stripped |= {key for key in ("text", "trim", "media", "attachment_meta")
                     if fields.get(key)}
        owed = fields.get("type") == T_INBOUND and not fields.get("delivered")
        if fields.get("removed_at") and not stripped and not owed:
            return True
        # The blobs go first, so a failed rewrite leaves a record a repeat
        # can still finish — never a record pointing at nothing it can name.
        if p.parent.name == MESSAGES_SUBDIR:
            store = p.parent.parent
            ids = set(filter(None, map(media_id_of, _media_refs(fields))))
            keep = set() if own_blobs else _blob_ids_referenced_elsewhere(
                store, ids, {p.resolve()})
            for mid in sorted(ids - keep):
                _unlink_blob(store, mid)
            spool = fields.get("media")
            if spool:
                try:
                    spool_path = Path(spool).resolve()
                    if spool_path.parent == media_dir(store).resolve():
                        spool_path.unlink(missing_ok=True)
                except OSError:
                    pass
        if not fields.get("removed_at"):
            when = _parse_since(removed_at)
            fields["removed_at"] = _iso(time.time() if when is None else when)
        fields["text"] = ""
        for key in gone:
            fields[key] = []
        for key, _pred in _PARTS:
            fields[key] = None
        fields["trim"] = None
        fields["media"] = None
        fields["attachment_meta"] = {}
        if fields.get("type") == T_INBOUND:
            fields["delivered"] = True
        _atomic_write(_render(fields), p)
        return True
    except Exception:  # noqa: BLE001 - a tombstone is bookkeeping; never raise
        return False


def _unlink_blob(store_dir: str | Path, media_id: str) -> tuple[bool, int]:
    """Remove one media blob and its sidecars.

    Returns ``(removed, errors)``: whether the blob itself was there and went,
    and how many of the four files exist but could not be removed — an absent
    sidecar is normal, a stuck one is a trace the caller must report."""
    if not _MEDIA_ID_RE.match(media_id or ""):
        return False, 0
    d = media_dir(store_dir)
    removed, errors = False, 0
    for name in (media_id, media_id + ".type", media_id + ".name", media_id + ".meta"):
        try:
            (d / name).unlink()
            removed = removed or name == media_id
        except FileNotFoundError:
            continue
        except OSError:
            errors += 1
    return removed, errors


def discard_media(store_dir: str | Path, media_id: str) -> bool:
    """Remove one blob :func:`store_media` wrote, with its sidecars.

    For a writer that stored a message's blobs and then could not write the
    record that would have referenced them, or decided against one of them (a
    mail's tracking pixel, known for one only once stored): nothing names the
    blob, so nothing would ever unlink it. Not for a blob a record references — that
    is :func:`tombstone_message`'s and :func:`delete_chat`'s, which check who
    else names it. Never raises; True when nothing of the blob is left."""
    try:
        _removed, errors = _unlink_blob(store_dir, media_id)
    except OSError:
        return False
    return errors == 0


def delete_chat(store_dir: str | Path, chat: str,
                account: str | None) -> dict:
    """Erase one chat from this store: every record of it, in both
    directions, and every blob only those records referenced.

    A chat is the pair (``kb:chat``, ``kb:account``) — see :data:`P_ACCOUNT` —
    so ``account`` is compared exactly, and ``None`` or ``""`` selects the
    records carrying no account (history written before the predicate
    existed), which is what an account-less chat id names. The same peer on
    another account is another chat and is left alone.

    This is a deletion, not a flag: the files go, the life store drops their
    graphs when it next sees the directory, and a later message from the same
    peer starts a chat with no history. A blob is kept if a record outside the
    chat still references it (never the case for blobs this store minted, but
    a reference is only a string and costs nothing to honour). The retained
    raw audio of a voice note still awaiting its transcript (``kb:media``, a
    path inside the media directory) goes too, and so do a mail's parts (its
    plain body, HTML and header blobs), with the same care for a shared blob.

    Never raises on a single unreadable or undeletable file — it is skipped
    and counted in ``errors``, so the caller can tell the erasure was partial
    and must not treat the chat as gone. That includes a file that cannot be
    read at all (it may be one of this chat's), and one that cannot be parsed
    but names this chat key: failing closed there is the point.

    Returns ``{"messages", "media", "parts", "errors", "subjects",
    "message_ids"}``: ``media`` counts every blob unlinked, ``parts`` how many
    of those were mail parts; the last two identify what was erased, so a
    reader still serving an older index can hide exactly those records and
    nothing that arrives afterwards.
    """
    wanted_chat = (chat or "").strip()
    wanted_account = (account or "").strip()
    result: dict = {"messages": 0, "media": 0, "parts": 0, "errors": 0,
                    "subjects": [], "message_ids": []}
    if not wanted_chat:
        return result
    # How this chat key appears in a record, for recognising one that no
    # longer parses (see _lit: the key is an escaped plain literal).
    chat_marker = f'<{P_CHAT}> "{_esc(wanted_chat)}"'
    doomed: list[tuple[Path, dict]] = []
    kept_refs: set[str] = set()
    try:
        paths = sorted(messages_dir(store_dir).glob("*.nt"))
    except OSError:
        return result
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            result["errors"] += 1
            continue
        fields = _parse(text)
        if not fields:
            if chat_marker in text:
                result["errors"] += 1
            continue
        mine = (fields.get("chat") == wanted_chat
                and (fields.get("account") or "") == wanted_account)
        if mine:
            doomed.append((path, fields))
        else:
            kept_refs.update(filter(None, map(media_id_of, _media_refs(fields))))
    media_root = media_dir(store_dir).resolve()
    for path, fields in doomed:
        try:
            path.unlink()
            result["messages"] += 1
            result["subjects"].append(fields["subject"])
            if fields.get("message_id"):
                result["message_ids"].append(fields["message_id"])
        except FileNotFoundError:
            continue
        except OSError:
            result["errors"] += 1
            continue
        parts = {fields[key] for key, _pred in _PARTS if fields.get(key)}
        for ref in _media_refs(fields):
            mid = media_id_of(ref)
            if mid and mid not in kept_refs:
                removed, errors = _unlink_blob(store_dir, mid)
                result["media"] += int(removed)
                result["parts"] += int(removed and ref in parts)
                result["errors"] += errors
        spool = fields.get("media")
        if spool:
            try:
                spool_path = Path(spool).resolve()
                if spool_path.parent == media_root:
                    spool_path.unlink(missing_ok=True)
            except OSError:
                result["errors"] += 1
    return result


# The rest of a chat's erasure is the gateway's own bookkeeping, not the
# ledger's — but every gateway keeps the same two files, so the walk over them
# lives here once and each gateway supplies only its own notion of "this chat"
# (and holds its own lock around the call).

_PENDING_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def purge_pending_sends(pending_dir: str | Path, matches) -> list[str]:
    """Delete the pending-send files ``matches(entry)`` claims; returns their ids.

    Covers every state a file is kept in (queued, sent, rejected, failed) —
    each holds the message text — except ``sending``: that one is on the wire
    right now, and its outcome is written back by the sender thread. Only
    ``<32 hex>.json`` files holding a dict are considered, so a stray foreign
    file in the directory is never touched."""
    removed: list[str] = []
    try:
        paths = sorted(Path(pending_dir).glob("*.json"))
    except OSError:
        return removed
    for path in paths:
        if not _PENDING_ID_RE.match(path.stem):
            continue
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(entry, dict) or entry.get("status") == "sending":
            continue
        try:
            hit = bool(matches(entry))
        except Exception:  # noqa: BLE001 — a matcher bug must not abort the erasure
            hit = False
        if not hit:
            continue
        try:
            path.unlink()
            removed.append(path.stem)
        except OSError:
            continue
    return removed


def purge_recent_chats(path: str | Path, matches) -> int:
    """Drop the recent-senders entries ``matches(entry)`` claims; returns the count.

    The file is a JSON list rewritten atomically, the shape every gateway's
    ``_record_recent_sender`` keeps."""
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if not isinstance(data, list):
        return 0
    kept, dropped = [], 0
    for entry in data:
        try:
            hit = isinstance(entry, dict) and bool(matches(entry))
        except Exception:  # noqa: BLE001
            hit = False
        if hit:
            dropped += 1
        else:
            kept.append(entry)
    if dropped:
        _atomic_write(json.dumps(kept, ensure_ascii=False), p)
    return dropped


# -- echo dedup for outbound recording ----------------------------------------

class RecentSends:
    """Bounded in-process memory of the sends this gateway itself performed.

    A gateway records each outbound message once, at the moment its send
    succeeds. Some client libraries then *echo* that same message back through
    the receive path as an own-account event (Telethon fires an outgoing
    NewMessage for the client's own sends; whether the WhatsApp bridge replays
    them varies by whatsmeow version; signal-cli does not sync its own sends
    back). On the receive path such an echo is indistinguishable from a genuine
    own-device send from the user's phone — which *must* be recorded. This
    memory is how the two are told apart: a send noted here is the gateway's
    own, already in the ledger, and its echo is skipped.

    Keys: the channel-reported message id when the sender captured one, else
    ``(chat, text)`` as an approximate fallback. Matches expire after
    ``window`` seconds so that, on the fallback key, an identical later message
    is never swallowed forever. Deliberately in-process and disposable: a
    restart forgets it, and the worst outcome of forgetting is one duplicate
    ledger record — never a lost one.
    """

    def __init__(self, maxlen: int = 200, window: float = 900.0):
        self._maxlen = int(maxlen)
        self._window = float(window)
        self._lock = threading.Lock()
        self._entries: OrderedDict = OrderedDict()  # key -> noted-at epoch

    def note(self, message_id: str | None = None, *, chat: str | None = None,
             text: str | None = None) -> None:
        """Register one performed send.

        The ``(chat, text)`` fallback key is stored only when no id is known:
        an id-keyed send must not also suppress a coincidentally identical
        own-device message sent moments later.
        """
        if message_id:
            key = ("id", str(message_id))
        elif chat and text:
            key = ("txt", str(chat), text)
        else:
            return
        with self._lock:
            self._entries.pop(key, None)
            self._entries[key] = time.time()
            while len(self._entries) > self._maxlen:
                self._entries.popitem(last=False)

    def seen(self, message_id: str | None = None, *, chat: str | None = None,
             text: str | None = None) -> bool:
        """True when an echoed event matches a noted send (either key form,
        within the freshness window)."""
        keys = []
        if message_id:
            keys.append(("id", str(message_id)))
        if chat and text:
            keys.append(("txt", str(chat), text))
        now = time.time()
        with self._lock:
            return any(
                key in self._entries and now - self._entries[key] <= self._window
                for key in keys
            )
