"""Per-message store for the messaging gateways (message ledger).

Every inbound message that reaches an *inbox*-mode gateway (Signal, WhatsApp,
Telegram) is persisted here as **one N-Triples file per message**, on the
gateway's own data volume, before any routing decision is made. Two things fall
out of that single act:

1. **A browsable history.** The life store indexes the volume read-only, so the
   whole message stream is queryable over SPARQL (``kb:InboundMessage``) — "what
   came in from X last week?" is a plain ``SELECT``, no model turn, no gateway
   round-trip. Because it is one file per message the store never rewrites a
   shared index, so a gateway writing a new message can never race a SPARQL read
   or another gateway.

2. **A delivery ledger.** Each message carries a ``kb:delivered`` flag. This is
   **not** "read" — it records only whether the message has yet been *handed to
   triage*. The flag is owned solely by the gateway and flipped ``false → true``
   by exactly two operations, both here: :func:`undelivered`, which returns the
   held messages **and marks them delivered as a side effect** (the daily drain),
   and :func:`mark_delivered`, which flips one already-written message the gateway
   persisted up front (the persist-before-forward path — see below). Nothing else
   — no SPARQL query, no ad-hoc read — ever touches it, so browsing history never
   silently "consumes" a message. The daily triage skill drains the backlog by
   calling the gateway's ``/undelivered`` endpoint (which calls this), so a
   message that arrived while its sender was not yet whitelisted is caught the
   next day instead of being lost.

The delivered flag lets a gateway persist a message it deliberately did **not**
forward — a blacklisted or no-action-class sender is written straight to
``delivered: true`` (already accounted for, never drained). A message that *is*
forwarded takes the never-drop path: the gateway writes it ``delivered: false``
the instant it arrives (before the gate, before the forward — so a crash or a
throwing forward cannot lose it), then calls :func:`mark_delivered` once the
triage turn has actually **run**. That last part is the whole point: the forward
POST answers 202 (accepted), not "handled", so the flip waits on the job's
``status: done`` (see ``job_delivery.py``). Any message that was persisted but
never reached a completed turn — a failed forward, a job that errored or
expired, a gateway that died mid-dispatch — stays ``delivered: false`` and is
picked up by the daily drain (at-least-once: a rare duplicate surface beats a
silent loss).

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
delivered flag and are invisible to :func:`undelivered` — the drain is triage
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

import json
import os
import re
import secrets
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
    except OSError:
        return None
    return (fields or {}).get("subject")


def thread_key(channel: str, account: str, chat: str | None,
               message_id: str | None, subject: str | None = None) -> str:
    """The canonical idempotency key for one inbound message's dashboard thread.

    Opening a thread is a side effect, and the same inbound can legitimately be
    handled twice — an escalation re-runs the turn's prompt, a channel
    redelivers a stanza after a reconnect, a live turn dies before finishing and
    the daily drain picks the record up again. All of those must land on one
    thread, so the key has to name the *message*, identically on the live path
    and at the drain.

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
# literal, and an xsd:dateTime typed literal. Nothing here is a general RDF
# parser — it round-trips only what write_message emits.

def _esc(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
        .replace('"', '\\"')
    )


def _unesc(value: str) -> str:
    out, i = [], 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
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
_TRIPLE_RE = re.compile(
    r'^<([^>]+)>\s+<([^>]+)>\s+'
    r'(?:<([^>]+)>|"((?:[^"\\]|\\.)*)"(?:\^\^<([^>]+)>)?)\s*\.\s*$'
)


def _render(fields: dict) -> str:
    """Deterministic sorted N-Triples for one message, either direction.

    ``fields`` carries the resolved values; the record's ``type`` selects the
    direction-specific triples (delivered/receivedAt/sender for inbound,
    author/sentAt for outbound). Optional fields may be absent."""
    subj = fields["subject"]
    rtype = fields.get("type") or T_INBOUND
    lines = [
        _iri(subj, RDF_TYPE, rtype),
        _lit(subj, P_CHANNEL, fields["channel"]),
        _lit(subj, P_TEXT, fields["text"]),
    ]
    if rtype == T_OUTBOUND:
        lines.append(_lit(subj, P_AUTHOR, fields["author"]))
        lines.append(_lit(subj, P_SENT_AT, fields["sent_at"], XSD_DATETIME))
    else:
        lines.append(_lit(subj, P_DELIVERED, "true" if fields["delivered"] else "false"))
        lines.append(_lit(subj, P_RECEIVED_AT, fields["received_at"], XSD_DATETIME))
        lines.append(_lit(subj, P_SENDER, fields["sender"]))
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
    # P_CONTENT_TYPE). Only for references the record actually carries.
    for url, meta in sorted((fields.get("attachment_meta") or {}).items()):
        if url not in (fields.get("attachments") or []) or not meta:
            continue
        if meta.get("content_type"):
            lines.append(_lit(url, P_CONTENT_TYPE, str(meta["content_type"])))
        for key, pred in (("size", P_BYTE_SIZE), ("width", P_WIDTH), ("height", P_HEIGHT)):
            if isinstance(meta.get(key), int) and meta[key] >= 0:
                lines.append(_lit(url, pred, str(meta[key]), XSD_INTEGER))
        if meta.get("file_name"):
            lines.append(_lit(url, P_FILE_NAME, str(meta["file_name"])))
    return "".join(l + "\n" for l in sorted(lines))


def _parse(text: str) -> dict | None:
    """Read a message file back into a ``fields`` dict, or None if unparseable.

    The record ``type`` defaults to inbound: every file written before
    ``kb:OutboundMessage`` existed carries an explicit inbound type, so an
    absent type triple can only be a legacy hand-crafted record — it keeps the
    pre-outbound contract (drainable) rather than being mistaken for a send.
    """
    fields: dict = {"type": T_INBOUND, "delivered": False, "group": None,
                    "message_id": None, "chat": None, "account": None,
                    "author": None, "sent_at": None, "attachments": [],
                    "media": None, "attachment_meta": {}}
    triples = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _TRIPLE_RE.match(line)
        if not m:
            return None
        triples.append(m.groups())
    # The record's own subject is the one that carries kb:channel; a file
    # holds one message, so every other subject is a blob the message
    # references, and its triples are what the gateway stated about it.
    subject = next((t[0] for t in triples if t[1] == P_CHANNEL), None)
    if subject is None:
        return None
    for subj, pred, obj_iri, lit, _dtype in triples:
        value = obj_iri if obj_iri is not None else _unesc(lit)
        if subj != subject:
            meta = fields["attachment_meta"].setdefault(subj, {})
            if pred == P_CONTENT_TYPE:
                meta["content_type"] = value
            elif pred == P_FILE_NAME:
                meta["file_name"] = value
            elif pred in (P_BYTE_SIZE, P_WIDTH, P_HEIGHT):
                try:
                    meta[{P_BYTE_SIZE: "size", P_WIDTH: "width",
                          P_HEIGHT: "height"}[pred]] = int(value)
                except ValueError:
                    pass
            continue
        if pred == RDF_TYPE:
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
    # Statements about a blob the record does not reference are noise, never
    # re-emitted; statements about one it does are kept whether or not the
    # blob still exists here — they are the record's, not the filesystem's.
    fields["attachment_meta"] = {
        url: meta for url, meta in fields["attachment_meta"].items()
        if url in fields["attachments"] and meta}
    fields["subject"] = subject
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
) -> tuple[str, Path]:
    """Persist one inbound message as a deterministic N-Triples file.

    Returns ``(subject_uri, path)``. ``delivered=False`` (the default) marks the
    message as still owed to triage; pass ``delivered=True`` for a message the
    gateway is deliberately *not* forwarding (blacklisted, group-blocked or
    no-action-class) so the daily drain never re-surfaces it.

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
    """
    ts = time.time() if timestamp is None else float(timestamp)
    token = secrets.token_hex(8)
    subject = f"urn:retinue:inbound:{_slug(channel)}:{token}"
    fields = {
        "type": T_INBOUND,
        "subject": subject,
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
    fields["attachment_meta"] = _own_attachment_meta(store_dir, fields["attachments"])
    # Filename: zero-padded epoch millis (sortable) + token (unique, IRI-safe).
    fname = f"{int(ts * 1000):016d}-{token}.nt"
    path = messages_dir(store_dir) / fname
    _atomic_write(_render(fields), path)
    return subject, path


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
    """
    if author not in AUTHORS:
        raise ValueError(f"author must be one of {'|'.join(AUTHORS)}, got {author!r}")
    ts = time.time() if timestamp is None else float(timestamp)
    token = secrets.token_hex(8)
    subject = f"urn:retinue:outbound:{_slug(channel)}:{token}"
    fields = {
        "type": T_OUTBOUND,
        "subject": subject,
        "channel": channel,
        "chat": chat or "unknown",
        "account": account or None,
        "text": text or "",
        "author": author,
        "message_id": message_id or None,
        "sent_at": _iso(ts),
        "attachments": [u for u in (attachment_urls or []) if u],
    }
    fields["attachment_meta"] = _own_attachment_meta(store_dir, fields["attachments"])
    fname = f"{int(ts * 1000):016d}-{token}.nt"
    path = messages_dir(store_dir) / fname
    _atomic_write(_render(fields), path)
    return subject, path


def _image_dimensions(data: bytes) -> tuple[int, int] | None:
    """Best-effort (width, height) from an image blob's header, else None.

    A deliberately tiny magic-number parser — PNG (IHDR), GIF (logical screen
    descriptor), JPEG (the first SOF segment), WebP (VP8 / VP8L / VP8X) — so
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


def safe_file_name(name) -> str | None:
    """A sender-supplied file name reduced to something safe to show and save.

    Base name only (no path), control characters dropped, capped in length
    with the extension kept. None when nothing usable remains. Never used to
    address a file here — blobs are keyed by their own id — only to say what
    the sender called it."""
    text = str(name or "").replace("\\", "/").strip()
    text = re.sub(r"[\x00-\x1f\x7f]", "", text.rsplit("/", 1)[-1]).strip()
    if text in ("", ".", ".."):
        return None
    if len(text) > 200:
        stem, dot, ext = text.rpartition(".")
        ext = ("." + ext) if dot and len(ext) <= 12 else ""
        text = (stem if ext else text)[:200 - len(ext)] + ext
    return text


def store_media(store_dir: str | Path, data: bytes, content_type: str | None,
                file_name: str | None = None) -> str:
    """Persist one inbound media blob durably and return its server-generated id.

    The blob is keyed by ``token_hex(16)`` — never by an untrusted filename — so
    the id is path-safe by construction and reveals nothing about the sender. The
    ``content_type`` is written to a ``<id>.type`` sidecar so the serving endpoint
    can set the right ``Content-Type`` without trusting anything client-supplied.
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
    ct = (content_type or "application/octet-stream").strip() or "application/octet-stream"
    _atomic_write(ct + "\n", d / (media_id + ".type"))
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
    missing or unreadable.
    """
    if not _MEDIA_ID_RE.match(media_id or ""):
        return None
    d = media_dir(store_dir)
    try:
        data = (d / media_id).read_bytes()
    except OSError:
        return None
    ct = "application/octet-stream"
    try:
        sidecar = (d / (media_id + ".type")).read_text(encoding="utf-8").strip()
        if sidecar:
            ct = sidecar
    except OSError:
        pass
    return data, ct


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
    meta: dict = {"size": int(size)}
    try:
        ct = (d / (media_id + ".type")).read_text(encoding="utf-8").strip()
    except OSError:
        ct = ""
    meta["content_type"] = ct or "application/octet-stream"
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
    references a blob this store holds but says less about it than
    :func:`media_meta` knows. Idempotent — a second run rewrites nothing —
    and never raises: a record that cannot be read or written is skipped.
    A gateway calls this at startup, on its own store only, so the reader
    never has to fall back to anyone's files. Returns the rewrite count.
    """
    marker = f"<{P_ATTACHMENT}>"
    count = 0
    try:
        paths = sorted(messages_dir(store_dir).glob("*.nt"))
    except OSError:
        return 0
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if marker not in text:
            continue
        fields = _parse(text)
        if not fields:
            continue
        known = _own_attachment_meta(store_dir, fields["attachments"])
        stated = fields.get("attachment_meta") or {}
        if all(stated.get(ref) == meta for ref, meta in known.items()):
            continue
        fields["attachment_meta"] = {**stated, **known}
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
    """Return messages still owed to triage, **marking each delivered**.

    This is the sole mutator of the ``delivered`` flag. It scans the message
    files oldest-first, selects those with ``delivered == false`` (and, when
    ``since`` is given, ``receivedAt >= since``), rewrites each of those files
    with ``delivered = true``, and returns the selected messages as dicts. A
    plain SPARQL read never calls this, so browsing history does not consume
    anything; only the daily triage drain does.

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
        except OSError:
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
    except OSError:
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
                   clear_media: bool = False) -> str | None:
    """Rewrite a persisted message's mutable fields in place; never raises.

    Used by the **persist-before-transcribe** path: a voice note is written up
    front with empty text and a ``media`` reference to its retained audio, then
    once STT succeeds this fills in the transcript (``text=…``) and drops the
    now-superfluous audio reference (``clear_media=True``).

    Returns the ``media`` value present *before* the call — so a caller clearing
    it knows which file to unlink — or ``None`` if there was none or the rewrite
    failed. Only the fields named are touched; ``delivered`` and everything else
    are preserved.
    """
    p = Path(path)
    try:
        fields = _parse(p.read_text(encoding="utf-8"))
    except OSError:
        return None
    if not fields:
        return None
    prev_media = fields.get("media")
    if text is not None:
        fields["text"] = text
    if clear_media:
        fields["media"] = None
    try:
        _atomic_write(_render(fields), p)
    except OSError:
        return None
    return prev_media


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
