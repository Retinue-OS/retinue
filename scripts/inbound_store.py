"""Per-message inbound store for the messaging gateways (delivery ledger).

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

Stdlib only (``hashlib``/``secrets``/``datetime``): this module is copied into
each gateway image alongside ``triage_policy.py`` and ``reply_tokens.py``, and
those containers ship no third-party RDF library.
"""
from __future__ import annotations

import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

# Same knowledge-base namespace the rest of the triage machinery emits under.
KB = "https://w3id.org/retinue/kb#"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
XSD_DATETIME = "http://www.w3.org/2001/XMLSchema#dateTime"

T_INBOUND = KB + "InboundMessage"
P_CHANNEL = KB + "channel"
P_SENDER = KB + "sender"
P_GROUP = KB + "group"
P_RECEIVED_AT = KB + "receivedAt"
P_TEXT = KB + "text"
P_MESSAGE_ID = KB + "messageId"
P_DELIVERED = KB + "delivered"
# A message's media (voice note, image) is NOT embedded in the graph: consistency
# over data-in-graph means every attachment — regardless of size — is a *reference*
# resolved over HTTP, never an inline data-URI literal. This predicate carries that
# reference as an IRI object (the gateway's own token-gated GET /media/<id> URL);
# it is multi-valued, so a message with several images gets one triple each. The
# attachment's media type is not stored here on purpose — it is returned by the
# HTTP response's Content-Type header when the reference is resolved, which is
# where a media type belongs once the payload lives behind a URL.
P_ATTACHMENT = KB + "attachment"
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
    """Deterministic sorted N-Triples for one message. ``fields`` carries the
    resolved values; only ``group`` and ``message_id`` may be absent."""
    subj = fields["subject"]
    lines = [
        _iri(subj, RDF_TYPE, T_INBOUND),
        _lit(subj, P_CHANNEL, fields["channel"]),
        _lit(subj, P_DELIVERED, "true" if fields["delivered"] else "false"),
        _lit(subj, P_RECEIVED_AT, fields["received_at"], XSD_DATETIME),
        _lit(subj, P_SENDER, fields["sender"]),
        _lit(subj, P_TEXT, fields["text"]),
    ]
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
    return "".join(l + "\n" for l in sorted(lines))


def _parse(text: str) -> dict | None:
    """Read a message file back into a ``fields`` dict, or None if unparseable."""
    fields: dict = {"delivered": False, "group": None, "message_id": None,
                    "attachments": [], "media": None}
    subject = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _TRIPLE_RE.match(line)
        if not m:
            return None
        subj, pred, obj_iri, lit, _dtype = m.groups()
        subject = subj
        if pred == RDF_TYPE:
            continue
        value = obj_iri if obj_iri is not None else _unesc(lit)
        if pred == P_CHANNEL:
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
        elif pred == P_ATTACHMENT:
            # IRI object → obj_iri is set; append the reference URL.
            if value:
                fields["attachments"].append(value)
        elif pred == P_MEDIA:
            fields["media"] = value
        elif pred == P_DELIVERED:
            fields["delivered"] = value.strip().lower() == "true"
    if subject is None or "channel" not in fields:
        return None
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

    ``attachment_urls`` are HTTP-resolvable references to this message's media
    (voice note, image), each emitted as a ``kb:attachment`` IRI. The bytes are
    never inlined into the graph — see :func:`store_media`.

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
        "subject": subject,
        "channel": channel,
        "sender": sender or "unknown",
        "text": text or "",
        "group": group or None,
        "message_id": message_id or None,
        "received_at": _iso(ts),
        "delivered": bool(delivered),
        "attachments": [u for u in (attachment_urls or []) if u],
        "media": media or None,
    }
    # Filename: zero-padded epoch millis (sortable) + token (unique, IRI-safe).
    fname = f"{int(ts * 1000):016d}-{token}.nt"
    path = messages_dir(store_dir) / fname
    _atomic_write(_render(fields), path)
    return subject, path


def store_media(store_dir: str | Path, data: bytes, content_type: str | None) -> str:
    """Persist one inbound media blob durably and return its server-generated id.

    The blob is keyed by ``token_hex(16)`` — never by an untrusted filename — so
    the id is path-safe by construction and reveals nothing about the sender. The
    ``content_type`` is written to a ``<id>.type`` sidecar so the serving endpoint
    can set the right ``Content-Type`` without trusting anything client-supplied.
    Neither file carries an RDF extension, so the life store never indexes them.

    The caller builds the HTTP reference (``…/media/<id>``) and passes it to
    :func:`write_message` as an ``attachment_urls`` entry; the bytes stay on disk
    and out of the graph.
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
    ``message_id``, ``received_at`` (ISO-8601), ``text``, ``attachments`` (a
    possibly-empty list of HTTP media reference URLs) and ``media`` (the local
    path of an as-yet-untranscribed voice note, else None).
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
        if not fields or fields["delivered"]:
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
