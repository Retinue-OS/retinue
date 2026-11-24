"""Retinue-side chat state and the live message overlay (messenger chats).

A **chat** is one messenger conversation — one peer or group, on one gateway
account — and it takes both halves to name it: the **chat key** is the exact
recipient string that channel's send path accepts (the ``kb:chat`` the gateways
stamp on every ledger record), and the **account** is the identity that holds
the conversation (their ``kb:account``). A key is unique only within an account,
because one channel's message volume is shared by every account on it, so two
accounts writing to the same peer produce the same key.

The id is therefore ``<channel>:~<account>:<chat-key>``, and
``<channel>:<chat-key>`` for records written before ``kb:account`` existed — see
:func:`make_chat_id` for the shape and why the two can always be told apart.
Those older ids are left exactly as they were rather than being guessed at: a
record that does not say which account wrote it cannot be attributed by this
process, and inventing an attribution would put one account's history under
another's name. The practical effect of that honesty is a one-time split — an
ongoing conversation shows as its unattributed history plus the account-named
chat that carries everything from now on — which heals as the new chat fills.

The message history itself lives in the gateways' ledgers and is served over
SPARQL; everything about a chat that is *not* a channel message lives here, in
one JSON document per chat under ``CHAT_STATE_DIR``:

- ``last_read`` — the user's read mark (ISO-8601): everything up to it has
  been dealt with. It is set by a reply and by the explicit *Mark as read*,
  never by opening the chat, and unread counts derive from it. It only ever
  advances — with one guarded exception, the undo of a *Mark as read*
  (:meth:`ChatStateStore.restore_last_read`).
- ``inbound_seq`` / ``read_inbound`` — how many inbound messages have landed,
  and how many of them the newest read covered. A timestamp says when a
  message was stamped, by whichever clock stamped it; these two say in which
  order things happened here, which is what decides whether a message came
  after the user last dealt with the chat (:func:`arrived_since_read`). An
  arrival is counted once: ``inbound_recent`` remembers the message ids of
  the newest ones, so a rail event repeated for the same message is no second
  arrival (:meth:`ChatStateStore.count_arrival`). And a counted arrival that
  is gone from where it came from — a mail deleted in a client, a tombstone
  now — is no unread message: ``inbound_gone`` holds the places in the count
  of those (:meth:`ChatStateStore.forget_arrivals`).
- ``untold`` / ``turn_attempts`` — for a mail, which counted arrivals the
  user has not been told of yet, by message id: counted in the same write
  that counts them, and told once the arrival's attention step and push have
  run to the end (:meth:`ChatStateStore.told`). A mail's gateway offers what
  this side did not accept again, and that offer finds here whether what the
  first one owed was ever done — also after a restart, a merge or a later
  arrival. And how many turns each mail has had (:meth:`note_turn_attempt`),
  so one whose turn keeps failing is not worked for ever.
- ``inbound_ahead`` / ``read_ahead`` — the latest stamp an arrival carried
  from a little ahead of this clock, and how far past ``last_read`` the
  newest read reaches by it: a message stamped by a clock running fast lies
  beyond a mark made after it landed, and a read of the chat as it stands
  covers it all the same (:func:`read_cut`).
- ``draft`` — the shared text area both the user and an agent compose into:
  ``{text, author, agent?, ts, version}``. Writes are version-guarded (the
  project-file sha-guard precedent): a writer names the version it based its
  edit on, and a stale version is rejected so concurrent edits surface as a
  conflict instead of silently clobbering each other.
- ``archived`` / ``muted`` — the dashboard-conversation semantics verbatim: an
  archived chat leaves the active list; a new inbound message un-archives it
  unless it is muted, and ``muted`` also silences that chat's Web Push.
- cached display metadata — the chat's ``name``, its ``group`` flag, the
  originating ``gateway`` service slug (which is what routes a send back
  through the right account) together with ``gateway_source``, how that slug
  was established, and a per-chat ``roster`` of sender handle → display name
  (the ledger stores handles only; names arrive on the notify rail and are
  remembered here).
- for a mail thread (docs/mail-chats.md) — a chat whose ``name`` is its
  subject — the people in it: ``participants`` and ``peer``. See
  :data:`MAIL_CHANNEL`.
- ``pending`` — a copy of the sends of this chat that are not over in its
  gateway's queue (:func:`clean_pending`): each waiting for the user's
  approval, being sent, or failed — with the reason — until the user
  dismisses it. The gateway's queue is the truth; the copy is what the chat
  list counts from, and what the chat page shows while the gateway is away.
- ``refused`` — whom a mail of the user's did not reach: the recipients the
  mail server refused, by the mail's Message-ID (:func:`clean_refused`).
- ``unsent`` — whether the chat is, so far, nothing but a mail of the user's
  own that has not gone out (see :meth:`ChatStateStore.note_message`).

Beside the documents, in a directory of its own, sits the queue of calls a
chat's gateway is still owed (:class:`OwedCalls`) — a read mark or an archive
that did not reach the mailbox behind the chat.

Beside the documents the store keeps one small index, ``merged/into.json``:
which chat ids were folded into which chat
(:meth:`ChatStateStore.note_merged`), so that an id a client, a link or a
list from before the merge still holds can be answered with where the chat
went (:meth:`ChatStateStore.merged_into`).

``gateway_source`` exists because a slug alone says nothing about how much it
can be trusted. Stamps written before the gateways reported their account were
derived by each container *about itself*, from a value that defaults to the
built-in service name — so an extra account of a channel stamped its chats with
the built-in's slug. Only ``GATEWAY_SOURCE_ACCOUNT`` marks a stamp established
from the account a gateway reports; anything else, including a doc written
before this field existed (which simply lacks it), is untrusted and re-derived.

**Single-writer: the web-gateway.** User edits and the token-gated agent
endpoints both go through it, so this store needs no cross-process locking —
one in-process lock plus atomic writes suffice.

The :class:`ChatOverlay` is the other half of the serving design: the chat API
is SPARQL-first, and the store's few seconds of indexing lag are bridged by an
in-memory overlay fed by exactly the two freshness-critical paths — arrivals
(the notify rail) and the dashboard's own sends. Entries expire once the store
has certainly caught up; a restart loses nothing but those few seconds of
freshness. There is deliberately no raw-file fallback behind it.

Stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# kb:author values a draft/message may carry — mirrors inbound_store.AUTHORS
# (not imported: this module lives in the retinue image, that one in the
# gateway images; keeping both stdlib-and-self-contained is the shared-module
# convention).
AUTHORS = ("user", "agent", "device")

# The one provenance that makes a chat's gateway stamp authoritative: it was
# established from the account the sending gateway reports for itself. An
# absent or any other value means "derived some other way" — untrusted, because
# the only other way that ever existed was a container guessing its own
# registry identity, which is what mis-routed sends to the wrong account.
GATEWAY_SOURCE_ACCOUNT = "account"

# How long a cleared draft stays restorable server-side. The client only offers
# the undo for a few seconds; this is the outer bound behind it, generous enough
# that a slow round trip never loses the offer and short enough that a stash
# cannot resurface long after the user has moved on.
UNDO_CLEAR_MAX_AGE = 300.0

# How far ahead of this clock a stamp may lie and still be a clock's error
# rather than an instant nothing can vouch for: a phone running a little
# fast. An arrival stamped within this reach is remembered by its stamp, so
# that a read of the chat as it stands covers it (ChatStateStore.mark_unread,
# _read_up_to), and a client may name its "now" from as far (the web
# gateway's read endpoint).
READ_SKEW_SECONDS = 120.0

# How many of a chat's newest arrivals are remembered by message id, to tell
# a repeated rail event from a new message (ChatStateStore.mark_unread). A
# repeat follows its original within seconds (a gateway's one retry on a
# timeout) or, where a gateway redelivers what it was never told was taken,
# a few messages later; this is far more than either needs, and small enough
# to ride in every state write.
INBOUND_RECENT_MAX = 64

# How many merges the store remembers (ChatStateStore.note_merged), newest
# kept. A merge consumes a chat, so there are never more of them than there
# were chats; the bound only keeps one small file small.
MERGED_MAX = 2000
# How many hops merged_into follows: a chat that absorbed others can be
# absorbed in turn. Far more than any thread's history needs, and the end of
# a walk through a file somebody damaged.
_MERGED_HOPS = 16

# The channel whose chats are mail threads (docs/mail-chats.md). Two things
# about them are this module's business. A mail chat is **named by its
# subject**, not by its peer, so nothing that names a *person* — a contact
# card, a profile name passing by — may write its ``name`` (set_contact, and
# the callers of note_message). And since the name no longer says who the chat
# is with, the people are kept beside it: ``participants`` and ``peer``.
MAIL_CHANNEL = "email"

# How many participants a chat document keeps, and how long an address and a
# display name may be. A thread's participants come from its mails'
# From/To/Cc — text from outside — so they are bounded here, where they are
# stored.
PARTICIPANTS_MAX = 100
_PARTY_NAME_MAX = 200
_PARTY_ADDRESS_MAX = 320
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")


def named_by_subject(channel: str | None) -> bool:
    """Whether a channel's chats carry a subject as their name (see
    :data:`MAIL_CHANNEL`) rather than the name of whoever they are with."""
    return channel == MAIL_CHANNEL


def clean_party(value) -> dict | None:
    """One participant as a chat document keeps it: ``{"address", "name"}``.

    The address is lower-cased and must read as one — a local part, an ``@``,
    a domain, no blank and no control character; anything else is no
    participant. The name is the display name a mail carried, or None: one
    line, control characters dropped, bounded. Both are text from outside and
    are only ever shown or compared, never interpreted."""
    if not isinstance(value, dict):
        return None
    address = value.get("address")
    if not isinstance(address, str):
        return None
    address = address.strip().lower()
    local, at, domain = address.rpartition("@")
    if (not at or not local or not domain or len(address) > _PARTY_ADDRESS_MAX
            or _CONTROL_RE.search(address) or any(ch.isspace() for ch in address)):
        return None
    name = value.get("name")
    name = " ".join(_CONTROL_RE.sub(" ", name).split()) if isinstance(name, str) else ""
    return {"address": address, "name": name[:_PARTY_NAME_MAX] or None}


def clean_parties(values) -> list[dict]:
    """A participants list as stored: cleaned, one entry per address (the
    first wins, so the order the gateway gave — most recent sender first — is
    kept), at most :data:`PARTICIPANTS_MAX`."""
    out: list[dict] = []
    seen: set[str] = set()
    for value in values if isinstance(values, list) else []:
        party = clean_party(value)
        if party is None or party["address"] in seen:
            continue
        seen.add(party["address"])
        out.append(party)
        if len(out) >= PARTICIPANTS_MAX:
            break
    return out


_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")

# A chat's queued sends (``pending``): how many a document keeps a copy of —
# the newest; a queue longer than this is on /sends in full — and how much of
# each. The text is the user's or an agent's own, but an address and a subject
# are repeated from the mail a reply answers, so every string is bounded, and
# one line where it is one line by nature.
PENDING_MAX = 50
_PENDING_TEXT_MAX = 64 * 1024
_PENDING_LINE_MAX = 998
# A queued send's id as it stands in a URL of ours, and the only link a queued
# send carries: the path of its approval page, on this origin. Anything else —
# an absolute URL, another path, a query — is no approval link of the
# dashboard's and is not kept.
_PENDING_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PENDING_URL_RE = re.compile(r"^/sends/[A-Za-z0-9._-]{1,128}/[A-Za-z0-9_-]{1,64}$")
# Where a queued send stands: waiting for the user's decision; allowed (or
# sent directly) and on its way, its outcome not known yet; or ended without
# going out for certain — ``error`` then says why, in one bounded line, and
# ``outcome_unknown`` whether the mail may have gone out all the same.
PENDING_STATUSES = ("pending", "sending", "failed")
_PENDING_ERROR_MAX = 500
# The token one press of Send carries from the browser to the gateway, so a
# repeat of that press sends nothing twice: opaque, printable, no blanks.
REQUEST_KEY_RE = re.compile(r"^[\x21-\x7e]{1,64}$")
# The recipients a mail server refused, per sent mail (``refused``): how many
# mails' worth a document remembers — the newest.
REFUSED_MAX = 50


def iso_z(ts: float | str | None = None) -> str:
    """Normalize a timestamp (epoch seconds, ISO string, or None = now) to the
    UTC ``...Z`` ISO form the ledger uses — lexicographically sortable, so ISO
    string comparison is time comparison everywhere in this module."""
    if ts is None:
        ts = time.time()
    if isinstance(ts, (int, float)):
        return (datetime.fromtimestamp(float(ts), tz=timezone.utc)
                .isoformat(timespec="seconds").replace("+00:00", "Z"))
    text = str(ts).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (dt.astimezone(timezone.utc)
                .isoformat(timespec="seconds").replace("+00:00", "Z"))
    except ValueError:
        return iso_z(None)


def _one_line(value, limit: int = _PENDING_LINE_MAX) -> str:
    """Text that is one line by nature — a subject, an address — as one line:
    control characters and line breaks become blanks, runs of blanks one."""
    if not isinstance(value, str):
        return ""
    return " ".join(_CONTROL_RE.sub(" ", value).split())[:limit]


def clean_pending(value) -> dict | None:
    """One queued send as a chat document keeps it, or None for what is none:

    ``{request_id, approval_url, text, ts, author, to, cc, subject, status,
    error, outcome_unknown, refused}`` — the contract's Pending — with
    ``message_id`` where the gateway minted one already, ``request_key``
    where the send carried one, and ``noted``, the instant this side first
    learned of the entry (:meth:`ChatStateStore.set_pending` reads it).

    ``approval_url`` is kept only as the path of an approval page on this
    origin (``/sends/<gateway>/<id>``) for this very request: the page puts
    it into a link, and a link is the one thing here that could lead
    somewhere. Recipients and the subject are one line each; nothing is
    interpreted.

    ``status`` is one of :data:`PENDING_STATUSES` (anything else reads as
    waiting). Only a failed entry has an ``error`` — the gateway's sentence,
    which may repeat what a mail server said, so it is one bounded line —
    and can be ``outcome_unknown``. ``refused`` are the addresses the mail
    server would not take, where the entry names any."""
    if not isinstance(value, dict):
        return None
    request_id = value.get("request_id")
    url = value.get("approval_url")
    if (not isinstance(request_id, str) or not _PENDING_ID_RE.match(request_id)
            or not isinstance(url, str) or not _PENDING_URL_RE.match(url)
            or not url.endswith("/" + request_id)):
        return None
    text = value.get("text") if isinstance(value.get("text"), str) else ""
    text = text.replace("\x00", "")
    if len(text) > _PENDING_TEXT_MAX:
        text = text[:_PENDING_TEXT_MAX].rstrip() + "…"

    def addresses(field) -> list[str]:
        raw = value.get(field)
        lines = (_one_line(a, _PARTY_ADDRESS_MAX) for a in (raw if isinstance(raw, list) else []))
        return [a for a in lines if a][:PARTICIPANTS_MAX]

    noted = value.get("noted")
    status = value.get("status") if value.get("status") in PENDING_STATUSES else "pending"
    failed = status == "failed"
    pending = {
        "request_id": request_id,
        "approval_url": url,
        "text": text,
        "ts": iso_z(value.get("ts")),
        "author": value.get("author") if value.get("author") in AUTHORS else "agent",
        "to": addresses("to"),
        "cc": addresses("cc"),
        "subject": _one_line(value.get("subject")),
        "status": status,
        "error": (_one_line(value.get("error"), _PENDING_ERROR_MAX) or None) if failed else None,
        "outcome_unknown": failed and value.get("outcome_unknown") is True,
        "refused": addresses("refused"),
        "noted": (float(noted) if isinstance(noted, (int, float))
                  and not isinstance(noted, bool) else time.time()),
    }
    message_id = _one_line(value.get("message_id"))
    if message_id:
        pending["message_id"] = message_id
    request_key = value.get("request_key")
    if isinstance(request_key, str) and REQUEST_KEY_RE.match(request_key):
        pending["request_key"] = request_key
    return pending


def clean_pendings(values, known=None) -> list[dict]:
    """A list of queued sends as stored: cleaned, one per request id, oldest
    first, the newest :data:`PENDING_MAX`. ``known`` is the list held so far:
    an entry it already has keeps the instant it was first noted — and, where
    the new value states no time of its own, the time it has, so that an
    entry a gateway does not date keeps its place and is not rewritten with
    every answer."""
    held = {p.get("request_id"): p for p in known or [] if isinstance(p, dict)}
    out: dict[str, dict] = {}
    for value in values if isinstance(values, list) else []:
        was = held.get(value.get("request_id")) if isinstance(value, dict) else None
        if was is not None:
            value = dict(value, noted=was.get("noted"))
            if value.get("ts") is None:
                value["ts"] = was.get("ts")
        pending = clean_pending(value)
        if pending is not None:
            decided = value.get("decided")
            if isinstance(decided, (int, float)) and not isinstance(decided, bool):
                pending["decided"] = float(decided)
            out.setdefault(pending["request_id"], pending)
    return sorted(out.values(), key=lambda p: (p["ts"], p["noted"]))[-PENDING_MAX:]


def clean_refused(value) -> dict[str, list[str]]:
    """``{message id: [address]}`` as a chat document keeps it (``refused``):
    an id one bounded line, each address one that reads as an address
    (:func:`clean_party`) and nothing else, mails nobody was refused for
    left out, the newest :data:`REFUSED_MAX` mails."""
    out: dict[str, list[str]] = {}
    for message_id, addresses in (value.items() if isinstance(value, dict) else ()):
        message_id = _one_line(message_id)
        parties = (clean_party({"address": a})
                   for a in (addresses if isinstance(addresses, list) else []))
        kept = list(dict.fromkeys(p["address"] for p in parties if p))[:PARTICIPANTS_MAX]
        if message_id and kept:
            out.pop(message_id, None)
            out[message_id] = kept
    return dict(list(out.items())[-REFUSED_MAX:])


def arrived_since_read(doc: dict | None) -> int:
    """How many inbound messages have landed since the chat's newest read (a
    reply, or *Mark as read*) — unread ones, every one of them, whatever their
    timestamps say.

    The read mark is an instant and unread counts are taken by comparing
    stamps with it, which goes wrong exactly when a message's stamp is not
    when it landed: a delayed webhook delivers one stamped minutes ago, a
    sender's clock runs slow, or the message lands in the mark's own second.
    All of those are stamped at or before a mark they arrived after, and no
    count by timestamp finds them. The arrival count does, so a caller takes
    the larger of the two (the web gateway's _chats_payload).

    Landed and still there, that is: an arrival that is gone from where it
    came from (``inbound_gone`` — a mail deleted or moved away in a client)
    is nothing the user can read or answer, and is not counted.

    0 for a chat never read, and for a document from before ``read_inbound``
    existed: with nothing recorded there the timestamps alone decide."""
    covered = (doc or {}).get("read_inbound")
    if covered is None:
        return 0
    covered = int(covered)
    landed = int((doc or {}).get("inbound_seq") or 0) - covered
    return max(0, landed - sum(1 for seq in gone_arrivals(doc) if seq > covered))


def arrivals_after(doc: dict | None, seq: int | None) -> set[str]:
    """The message ids of the arrivals the chat counted after place ``seq``
    in its count (``inbound_recent`` — the newest of them): what landed
    later than some action whose place is known. None for ``seq`` — a place
    nobody recorded — names nothing."""
    if seq is None:
        return set()
    return {str(pair[0]) for pair in (doc or {}).get("inbound_recent") or []
            if isinstance(pair, (list, tuple)) and len(pair) == 2 and pair[0]
            and isinstance(pair[1], int) and not isinstance(pair[1], bool) and pair[1] > seq}


def owes(doc: dict | None, message_id: str | None) -> bool:
    """Whether the counted arrival with this id is still owed what an
    arrival tells the user — its attention step and push
    (:meth:`ChatStateStore.count_arrival`'s ``owe``, :meth:`ChatStateStore.told`).
    False for a message without an id: nothing can be owed by name."""
    return bool(message_id) and str(message_id) in ((doc or {}).get("untold") or [])


def recent_place(doc: dict | None, message_id: str | None) -> int | None:
    """The place in the chat's count of the arrival with this id, as
    ``inbound_recent`` remembers it — or None where it does not (unlike
    :func:`arrival_seq`, which then answers the count as it stands)."""
    if not message_id:
        return None
    for pair in reversed((doc or {}).get("inbound_recent") or []):
        if (isinstance(pair, list) and len(pair) == 2 and pair[0] == message_id
                and isinstance(pair[1], int) and not isinstance(pair[1], bool)):
            return int(pair[1])
    return None


def turn_attempts(doc: dict | None, message_id: str | None) -> int:
    """How many turns the arrival with this id has had
    (:meth:`ChatStateStore.note_turn_attempt`)."""
    for pair in (doc or {}).get("turn_attempts") or []:
        if isinstance(pair, list) and len(pair) == 2 and pair[0] == message_id:
            return int(pair[1] or 0)
    return 0


def gone_arrivals(doc: dict | None) -> list[int]:
    """The places in the chat's arrival count of the counted arrivals that are
    gone from where they came from (``inbound_gone``), ascending — whatever of
    the stored value reads as such."""
    out = set()
    for seq in (doc or {}).get("inbound_gone") or []:
        if isinstance(seq, int) and not isinstance(seq, bool) and seq > 0:
            out.add(seq)
    return sorted(out)


def arrival_seq(doc: dict | None, message_id: str | None) -> int:
    """The place in the chat's arrival count of the message with this id, as
    :meth:`ChatStateStore.mark_unread` counted it: its own, also when the
    event that asks is a repeat and newer messages have been counted since.
    For a message that carries no id, or one no longer remembered, the count
    as it stands — every such event is an arrival of its own."""
    if message_id:
        for pair in reversed((doc or {}).get("inbound_recent") or []):
            if isinstance(pair, list) and len(pair) == 2 and pair[0] == message_id:
                return int(pair[1])
    return int((doc or {}).get("inbound_seq") or 0)


def read_cut(doc: dict | None) -> str:
    """The stamp up to which the chat's own reads cover its messages, "" for
    a chat never read: the read mark, or — where the newest read reached past
    it to a message stamped ahead of this clock — that stamp (``read_ahead``).
    What a count by timestamp compares with; ``last_read`` itself stays the
    instant the read happened at."""
    return max((doc or {}).get("last_read") or "", (doc or {}).get("read_ahead") or "")


# Marker opening an id's optional account segment. A chat id is
# ``<channel>:<key>`` when the account is unknown and
# ``<channel>:~<account>:<key>`` when it is known, and this character is what
# tells the two apart in one pass: no chat key any channel produces begins with
# it (Signal groups begin ``group:``, WhatsApp JIDs and Telegram chat ids with a
# digit or ``-``, phone numbers with ``+``), so a middle segment opening with
# ``~`` can only be an account. The alternative — counting colons — cannot work:
# ``signal:group:abc`` is a legacy two-segment id whose key merely contains one.
# ``~`` is unreserved in RFC 3986, so an id stays readable percent-encoded in a
# URL.
_ACCOUNT_MARK = "~"

# Inside the account segment three characters need hiding: ``:`` would forge a
# segment boundary, a leading ``~`` would look like the doubled marker that
# escapes a key (see :func:`make_chat_id`), and ``%`` must be escaped for the
# encoding to be reversible at all. Everything else — ``+``, ``@``, digits,
# letters — is left as it is so an id reads as the account it names. ``%`` is
# listed first so encoding never re-escapes its own output, and decoding walks
# the list backwards so ``%253A`` comes back as the literal ``%3A``.
_ACCOUNT_ESCAPES = (("%", "%25"), (":", "%3A"), ("~", "%7E"))


def encode_account(account: str) -> str:
    """The account as it appears inside a chat id (see :data:`_ACCOUNT_ESCAPES`)."""
    out = account or ""
    for raw, enc in _ACCOUNT_ESCAPES:
        out = out.replace(raw, enc)
    return out


def decode_account(segment: str) -> str:
    """Inverse of :func:`encode_account`. ``%`` last, so ``%253A`` round-trips."""
    out = segment or ""
    for raw, enc in reversed(_ACCOUNT_ESCAPES):
        out = out.replace(enc, raw)
    return out


def make_chat_id(channel: str, key: str, account: str | None = None) -> str:
    """Compose a chat id from its parts — the one place the shape is written.

    With no account this is the historical ``<channel>:<key>``, which is also
    what a ledger record written before ``kb:account`` existed still yields; with
    one it is ``<channel>:~<account>:<key>``.

    Composing and splitting are inverses for **every** channel-supplied value,
    not merely the ones today's channels produce. No current channel emits a key
    beginning with the marker, but the key is the channel's string and this
    module does not get to assume what a future one will hand it: an accountless
    key that opens with ``~`` gets the marker doubled, which is a form the
    account branch can never produce (an account's own leading ``~`` is
    percent-escaped). So the ambiguity is closed by construction rather than by
    a claim about the channels."""
    if account:
        return f"{channel}:{_ACCOUNT_MARK}{encode_account(account)}:{key}"
    if key.startswith(_ACCOUNT_MARK):
        return f"{channel}:{_ACCOUNT_MARK}{key}"
    return f"{channel}:{key}"


def split_chat_id(chat_id: str) -> tuple[str, str] | None:
    """Split a chat id into ``(channel, key)``, ignoring any account segment.

    The two-value form every caller that only routes or displays a message
    wants: the key is what a send path accepts and what ``kb:chat`` holds, and
    it means the same thing whether or not the id names an account. Callers that
    must distinguish two accounts' chats use :func:`split_chat_ref` instead.
    Returns None when the id has no channel or no key."""
    ref = split_chat_ref(chat_id)
    return None if ref is None else (ref[0], ref[2])


def split_chat_ref(chat_id: str) -> tuple[str, str | None, str] | None:
    """Split a chat id into ``(channel, account, key)``; None if malformed.

    ``account`` is None for an id that names none — a chat whose records predate
    ``kb:account``, or one on a gateway that did not know its own account yet.
    Chat keys contain ``:`` themselves (Signal ``group:<id>``), so the split is
    positional, never a count: the first colon ends the channel, and only a
    second segment opening with :data:`_ACCOUNT_MARK` is an account — everything
    after it is the key, colons and all. A *doubled* marker is the escape
    :func:`make_chat_id` writes for an accountless key that begins with one, and
    is stripped back to that key here."""
    channel, sep, rest = (chat_id or "").partition(":")
    if not sep or not channel or not rest:
        return None
    if rest.startswith(_ACCOUNT_MARK * 2):
        return channel, None, rest[len(_ACCOUNT_MARK):]
    if rest.startswith(_ACCOUNT_MARK):
        account, sep2, key = rest[len(_ACCOUNT_MARK):].partition(":")
        # A marker with no closing colon, or with an empty account or key, is
        # not an id this module ever composed — refuse it rather than silently
        # reading it as a key that happens to start with "~".
        if not sep2 or not account or not key:
            return None
        return channel, decode_account(account), key
    return channel, None, rest


def state_filename(chat_id: str) -> str:
    """Path-safe filename for a chat's state doc.

    Chat keys are channel-native strings (JIDs, ``group:<base64>``, phone
    numbers) that may contain ``/``, ``=`` and other path-hostile characters,
    so the filename is a digest of the id — never the raw key — and the id is
    kept inside the document."""
    return hashlib.sha256((chat_id or "").encode("utf-8")).hexdigest()[:32] + ".json"


class ChatStateStore:
    """One JSON document per chat; atomic writes behind one in-process lock."""

    def __init__(self, directory: str | Path):
        self._dir = Path(directory)
        self._lock = threading.Lock()

    # -- plumbing -------------------------------------------------------------

    def _path(self, chat_id: str) -> Path:
        return self._dir / state_filename(chat_id)

    @staticmethod
    def _default(chat_id: str) -> dict:
        parts = split_chat_ref(chat_id) or ("", None, chat_id)
        return {
            "id": chat_id,
            "channel": parts[0],
            # The gateway account this chat belongs to, or None for one whose
            # records predate kb:account. Denormalized from the id purely so a
            # state document is readable on its own; the id stays the truth.
            "account": parts[1],
            "key": parts[2],
            # The read mark: the user replied, or said "read", up to here.
            # Looking at a chat moves nothing — see advance_last_read.
            "last_read": None,
            # How many reads advance_last_read has taken, whether or not the
            # mark moved: a read is an event even when its whole-second
            # mark equals the one held (see the web gateway's
            # _instructed_read_since).
            "read_seq": 0,
            # The arrival count (``inbound_seq``) the newest read covered:
            # whatever has been counted since landed after it and is unread
            # (see arrived_since_read). None until the chat is first read.
            "read_inbound": None,
            # How far past ``last_read`` the newest read reaches, by the
            # stamps of the messages it covered: ``inbound_ahead`` as a read
            # of the chat as it stands found it, where that lies later than
            # the mark. None otherwise — nearly always. See _read_up_to and
            # read_cut.
            "read_ahead": None,
            # What the newest *Mark as read* replaced beside the mark, as
            # ``{"read_inbound": …, "read_ahead": …}``, so that its undo puts
            # both back; None when there is nothing to undo — never marked,
            # the mark undone already, or a reply since (which no undo
            # reaches behind). See restore_last_read.
            "read_undo": None,
            # What the newest *Mark as read* settled: the ``done_at`` of the
            # attention item it marked done, or None when it settled nothing.
            # Kept so that undoing that mark reopens exactly the item it
            # closed and no other (see set_read_settled).
            "read_settled": None,
            "draft": None,
            # What the newest clear removed, kept so it can be put back exactly
            # — see set_draft and undo_clear. None when nothing is recoverable.
            "cleared": None,
            # Monotonic counter surviving draft clears, so a version can never
            # repeat and a stale writer is always detectable.
            "draft_version": 0,
            "archived": False,
            "muted": False,
            # The user's mark: the chat sorts above the rest of its list.
            # Independent of the two above — muting, restoring and an arrival
            # all leave it as it is.
            "starred": False,
            "name": None,
            "group": None,
            "gateway": None,
            # How `gateway` was established; see GATEWAY_SOURCE_ACCOUNT. A doc
            # written before this field existed lacks it and so is untrusted by
            # construction — the marker cannot be forged by an old stamp.
            "gateway_source": None,
            "roster": {},
            "roster_refreshed": None,
            # A mail thread's people (see MAIL_CHANNEL): everyone on its
            # mails' From/To/Cc but the account itself, as ``[{"address",
            # "name"}]``, and ``peer`` — the one the thread is with: its
            # latest inbound sender, or the first recipient of a thread the
            # user started. Both None until the rail (or the name backfill)
            # has said; an empty list is an answer, None is not knowing.
            "participants": None,
            "peer": None,
            # The sends of this chat that wait in its gateway's queue for the
            # user's approval, oldest first (clean_pending). A copy: the
            # gateway's queue is the truth and this follows it (set_pending).
            "pending": [],
            # The recipients a mail server would not take, per mail of the
            # user's that went out all the same: ``{message id: [address]}``,
            # the newest REFUSED_MAX (note_refused). The mail's record names
            # whom it reached; this is who it did not.
            "refused": {},
            # True for a chat that so far is nothing but a mail of the user's
            # own that has not gone out — queued, or its outcome not known —
            # (note_message's ``unsent``); False once any mail of it is known.
            # Such a chat is forgotten when that send is denied, retracted or
            # dismissed: there was never anything else in it.
            "unsent": False,
            # First-unread watermark: set when an inbound lands on a fully-read
            # chat, cleared when the user catches up. Its presence is what
            # classifies the next notification as "reply" rather than "new".
            "unread_since": None,
            # How many inbound messages have landed (see mark_unread), and how
            # many of them the attention model had taken in when it last wrote
            # this chat's block (set_attention's ``attended``). The two differ
            # while an arrival is on its way into the model — between the
            # rail's unarchive and the arrival's decision, or for minutes
            # while an instructed sender's turn works — and an expiry timer
            # must not archive a message it has never seen (see
            # archive_unless_arrived).
            "inbound_seq": 0,
            "attended_seq": 0,
            # The newest counted arrivals that carried a message id, oldest
            # first, each as ``[message id, its inbound_seq]`` — at most
            # INBOUND_RECENT_MAX. What makes a repeated rail event for one
            # message a repeat and not a second arrival, also across a
            # restart of the web gateway (see mark_unread, arrival_seq).
            "inbound_recent": [],
            # The places in the count (``inbound_seq`` values) of counted
            # arrivals that are gone from where they came from: mails the
            # gateway turned into tombstones. They stay arrivals — the count
            # never goes back — and are no longer unread ones
            # (arrived_since_read, forget_arrivals).
            "inbound_gone": [],
            # A mail's counted arrivals the user has not been told of yet, by
            # message id, oldest first, at most INBOUND_RECENT_MAX: written
            # with the count (count_arrival's ``owe``) and taken out once the
            # arrival's attention step and push have run to the end, or its
            # ring that waited on a turn went out (told). One per arrival,
            # never a high-water mark: an instructed sender's mail waits for
            # its turn while later mail of the thread is attended, and a
            # repeated offer of it must still find it owed.
            "untold": [],
            # How many turns each mail has had, as ``[message id, n]``, at
            # most INBOUND_RECENT_MAX (note_turn_attempt): the web gateway
            # stops starting turns for one whose turn keeps failing.
            "turn_attempts": [],
            # The latest stamp a counted arrival carried from ahead of this
            # clock, no further than READ_SKEW_SECONDS: a sender's clock, or
            # a receiving phone's, running a little fast. The rail takes
            # such a stamp as now for the overlay, but the ledger record
            # keeps it, and a read has to reach it (``read_ahead``). None
            # for a chat no such message has landed in.
            "inbound_ahead": None,
            # Whether the last arrival's sender is one the dashboard knows
            # nothing about from the rail: True for a direct message from
            # anyone who is not a VIP, False for a VIP, None for a group or
            # a chat no gated message has arrived in yet. (The delivery gate no longer
            # tells strangers from acquaintances — the messenger whitelist is
            # retired, docs/triage-delivery-gate.md.) The attention model
            # screens such a sender unless something else says where they
            # belong: a sphere the user taught the profile, or `contact` below.
            "unknown_sender": None,
            # The contact card the user filled in for this chat, once they
            # have: {"name", "sphere", "tags", "at"}. None while the chat is
            # only a handle. Naming someone is what turns a screened stranger
            # into a correspondent — see `set_contact`.
            "contact": None,
            # Id of this chat's companion conversation — the dashboard thread
            # where the user works out a reply with Ara. None until one is
            # first asked for; see `set_companion`.
            "companion": None,
            # The attention model's block for this chat (scripts/attention.py,
            # `item_from_doc`): importance, deadline, lead, sphere, tags, kind
            # and the delivery state. Written by the web-gateway when an
            # inbound message arrives and when the user acts on the home
            # screen; None for a chat the model has not seen.
            "attention": None,
            # The newest message a sender's standing instruction was applied
            # to (docs/contacts.md, "The instruction"): who it was for, the
            # instruction as applied, whether the turn is still working, and
            # its verdict — importance, deadline, what it did, what it
            # flagged — with how the message was delivered. None for a chat
            # no instructed message has arrived in. See `update_instructed`.
            "instructed": None,
        }

    def _read(self, chat_id: str) -> dict:
        try:
            data = json.loads(self._path(chat_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self._default(chat_id)
        if not isinstance(data, dict):
            return self._default(chat_id)
        doc = self._default(chat_id)
        doc.update(data)
        # The id is the truth about a chat's identity, and channel/account/key
        # are only ever denormalized from it for readability — so they are
        # re-derived here rather than taken from the file. The digest filename
        # is not invertible, so the caller's id is what we have; a stored copy
        # that ever disagreed with it would silently route or filter by the
        # wrong half.
        doc["id"] = chat_id
        channel, account, key = split_chat_ref(chat_id) or ("", None, chat_id)
        doc["channel"], doc["account"], doc["key"] = channel, account, key
        return doc

    def _write(self, doc: dict) -> None:
        path = self._path(doc["id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def _merged_path(self) -> Path:
        # A directory of its own: all() reads every *.json beside the chat
        # documents as one.
        return self._dir / "merged" / "into.json"

    def _merged_read(self) -> dict[str, dict]:
        try:
            data = json.loads(self._merged_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): v for k, v in data.items()
                if isinstance(v, dict) and isinstance(v.get("into"), str) and v["into"]}

    def _merged_write(self, index: dict[str, dict]) -> None:
        path = self._merged_path()
        if not index:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def _merged_target(self, index: dict[str, dict], chat_id: str) -> str | None:
        """Where ``chat_id`` went, by ``index`` — the chat at the end of its
        merges — or None for an id that is a chat (it has a document: a new
        conversation under an old id outranks the note of a merge) or that
        nothing was noted for."""
        seen = {chat_id}
        current = chat_id
        for _hop in range(_MERGED_HOPS):
            if self._path(current).exists():
                break
            nxt = (index.get(current) or {}).get("into")
            if not nxt or nxt in seen:
                break
            seen.add(nxt)
            current = nxt
        return current if current != chat_id else None

    # -- reads ----------------------------------------------------------------

    def get(self, chat_id: str) -> dict:
        with self._lock:
            return self._read(chat_id)

    def exists(self, chat_id: str) -> bool:
        """Whether a document is stored for this chat (a default is not)."""
        return self._path(chat_id).exists()

    def all(self) -> dict[str, dict]:
        """Every stored chat doc, keyed by chat id."""
        out: dict[str, dict] = {}
        with self._lock:
            try:
                paths = sorted(self._dir.glob("*.json"))
            except OSError:
                return out
            for path in paths:
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if isinstance(data, dict) and data.get("id"):
                    doc = self._default(str(data["id"]))
                    doc.update(data)
                    out[doc["id"]] = doc
        return out

    def merged_into(self, chat_id: str) -> str | None:
        """The chat this id was folded into (see :meth:`note_merged`), through
        every later merge of that chat; None for an id that is a chat itself
        or that was never merged away."""
        with self._lock:
            return self._merged_target(self._merged_read(), chat_id)

    def merged_away(self) -> dict[str, str]:
        """``{absorbed chat id: the chat it is part of now}`` for every merge
        on record that still stands — one read for a caller that has a whole
        list to check."""
        with self._lock:
            index = self._merged_read()
            out = {}
            for chat_id in index:
                target = self._merged_target(index, chat_id)
                if target:
                    out[chat_id] = target
            return out

    # -- writes ---------------------------------------------------------------

    def note_merged(self, absorbed_id: str, survivor_id: str) -> None:
        """Remember that ``absorbed_id`` is part of ``survivor_id`` now.

        Kept apart from :meth:`merge`, which has nothing to fold where the
        absorbed chat never had a document — its messages can be in the
        ledger all the same, and its id in a client's hands. What is noted
        says where an old id went (:meth:`merged_into`) and nothing else: no
        state hangs on it, an id that has a document again is a chat again
        whatever is noted, and :meth:`delete` forgets it with either chat.
        Idempotent. A survivor that is a chat again after having once been
        absorbed somewhere loses its own note — it leads nowhere any more."""
        if not absorbed_id or not survivor_id or absorbed_id == survivor_id:
            return
        with self._lock:
            index = self._merged_read()
            was = dict(index)
            if self._path(survivor_id).exists():
                index.pop(survivor_id, None)
            if (index.get(absorbed_id) or {}).get("into") != survivor_id:
                index[absorbed_id] = {"into": survivor_id, "at": iso_z()}
            if len(index) > MERGED_MAX:
                newest = sorted(index, key=lambda k: str(index[k].get("at") or ""))[-MERGED_MAX:]
                index = {k: index[k] for k in newest}
            if index != was:
                self._merged_write(index)

    def delete(self, chat_id: str) -> dict | None:
        """Forget a chat entirely; returns the document it had, or None.

        Not a reset to defaults: the file goes, so ``all()`` no longer lists
        the chat and a later message about the same peer starts from a fresh
        document — the chat-delete contract (see web-gateway's chat delete).
        The notes of merges that name the chat go with it, on either side.
        """
        with self._lock:
            # Nothing remembers a deleted chat: neither that it was merged
            # away, nor which chats were folded into it.
            index = self._merged_read()
            kept = {k: v for k, v in index.items()
                    if k != chat_id and v.get("into") != chat_id
                    and self._merged_target(index, k) != chat_id}
            if kept != index:
                self._merged_write(kept)
            path = self._path(chat_id)
            if not path.exists():
                return None
            doc = self._read(chat_id)
            path.unlink(missing_ok=True)
            return doc

    def note_message(self, chat_id: str, *, name: str | None = None,
                     group: bool | None = None, gateway: str | None = None,
                     gateway_source: str | None = None,
                     sender: str | None = None,
                     sender_name: str | None = None,
                     unknown_sender: bool | None = None,
                     participants=None, peer=None,
                     unsent: bool | None = None) -> dict:
        """Cache display metadata learned from a message event (the rail).

        The ledger persists handles, never names — names are remembered here as
        they pass by, so group bubbles can label their senders and a send can
        route back through the exact gateway account the chat lives on.

        ``participants`` (a list) and ``peer`` are a mail thread's people as
        its gateway states them (see MAIL_CHANNEL); each replaces what was
        kept, cleaned (:func:`clean_parties`). None leaves them as they are,
        and a ``peer`` that does not read as one is not stored.

        ``unsent`` says whether the chat is, so far, only a mail of the
        user's own that has not gone out (True — a queued new mail), or
        holds a mail that exists (False — any message event). None leaves
        it."""
        with self._lock:
            doc = self._read(chat_id)
            if unsent is not None:
                doc["unsent"] = bool(unsent)
            if participants is not None:
                doc["participants"] = clean_parties(participants)
            if peer is not None:
                doc["peer"] = clean_party(peer) or doc.get("peer")
            if name:
                doc["name"] = name
            if group is not None:
                doc["group"] = bool(group)
            if gateway:
                # Slug and provenance are written as one: a stamp can never end
                # up marked trusted by a path that did not establish it.
                doc["gateway"] = gateway
                doc["gateway_source"] = gateway_source or None
            if sender and sender_name:
                roster = doc.get("roster") or {}
                if roster.get(sender) != sender_name:
                    roster[sender] = sender_name
                    doc["roster"] = roster
                    doc["roster_refreshed"] = time.time()
            if unknown_sender is not None:
                # Stored as the rail said it, card or no card: a contact the
                # user filed outranks it where it is read (attention_store.
                # chat_is_unknown), so removing the card falls back on this.
                doc["unknown_sender"] = bool(unknown_sender)
            self._write(doc)
            return doc

    def set_gateway(self, chat_id: str, slug: str | None,
                    source: str | None = None) -> dict:
        """Set — or clear, with None — which gateway account owns this chat.

        Separate from :meth:`note_message`, which only ever fills metadata in
        and so cannot undo a wrong stamp. Clearing is what the web-gateway's
        repair pass needs: a stamp that cannot be shown to be this chat's
        account is dropped, and the next account-attributed rail event
        establishes the right one. Provenance is written with the slug and
        cleared with it, so the two can never disagree."""
        with self._lock:
            doc = self._read(chat_id)
            doc["gateway"] = slug or None
            doc["gateway_source"] = (source or None) if slug else None
            self._write(doc)
            return doc

    def set_contact(self, chat_id: str, *, name: str,
                    sphere: str | None = None, tags=(), at: str | None = None,
                    person: str | None = None, chamber: str | None = None,
                    path: str | None = None) -> dict:
        """File this chat's peer in the address book, or clear the card with
        an empty name.

        One write for what naming someone means to this store: the chat is
        called by their name from now on, and the card records which spheres
        they belong to. The rest of what the card does — the attention
        profile, the life-store record — is the web-gateway's business; this
        is only what a chat document knows about it.

        A mail thread is the exception to the first half (see MAIL_CHANNEL):
        its name is its subject and stays that, card or no card — the card
        says who the thread is *with*, which the document keeps as ``contact``
        beside the name.

        ``unknown_sender`` is deliberately left alone: it is what the rail
        last said about the sender, and the card is the *user's* word about
        the person. A named chat is not screened because a card exists (see
        `chat_is_unknown`), so removing the card falls back on what the rail
        said rather than a guess made here.

        ``person``, ``chamber`` and ``path`` point at the contact the card is
        filed under (scripts/contacts.py): the person is the truth, and the
        name and spheres here are this chat's copy of it."""
        with self._lock:
            doc = self._read(chat_id)
            clean = " ".join(str(name or "").split())
            titled = named_by_subject(doc["channel"])
            if not clean:
                # Back to a handle: the name went with the card, and whatever
                # the channel itself passes by next will name the chat again.
                doc["contact"] = None
                if not titled:
                    doc["name"] = None
                self._write(doc)
                return doc
            if not titled:
                doc["name"] = clean
            doc["contact"] = {
                "name": clean,
                "sphere": (str(sphere).strip().lower() or None) if sphere else None,
                "tags": [t for t in (str(x).strip().lower() for x in tags) if t],
                "at": at or iso_z(),
            }
            if person:
                doc["contact"].update(person=str(person), chamber=chamber or None, path=path or None)
            self._write(doc)
            return doc

    def merge(self, absorbed_id: str, survivor_id: str) -> tuple[dict, dict | None]:
        """Fold one chat's state into another's and forget the first; returns
        ``(survivor, absorbed)`` — the surviving document as it now stands and
        the absorbed one as it was, or None where there was none to fold.

        For two chats that turned out to be one conversation: mail threads a
        later mail linked (docs/mail-chats.md, "Threading"). The messages are
        the gateway's to re-key; this is everything else, and nothing the
        user did to either chat may be lost in it:

        - the **read mark** is the later of the two, and the arrival counts
          add up — what had landed unread in either is still unread, by its
          place in the count (:func:`arrived_since_read`), wherever the other
          chat's mark falls. That holds for a chat never read as well: the
          user said "read" about the other one, having seen only that, so
          every arrival of the unread one stays unread behind the mark it
          inherits. (Two chats never read have no mark between them, and
          their timestamps go on deciding.) The arrivals remembered by
          message id (``inbound_recent``) and those that are gone
          (``inbound_gone``) come along, renumbered into the one count so
          that a place in it still says read or unread: what either chat's
          read covered first, then what had landed since; so do the
          arrivals either still owed the user (``untold``) and the turns
          each mail had (``turn_attempts``). Where the mark
          moved, the undo of the survivor's last *Mark as read* is dropped —
          it names a mark that no longer stands;
        - the **flags**: starred if either was; muted if either was (the user
          silenced this conversation, whichever half they did it on), and
          muting archives, as everywhere; otherwise archived only if both
          were — the half that was in view keeps the whole in view;
        - the **draft** stays the survivor's; an absorbed draft moves over
          only where the survivor has none, under a version later than any
          either chat has handed out, so a writer holding an old one is
          refused;
        - the **companion** stays the survivor's; an absorbed one moves over
          only where the survivor has none. What becomes of a second
          companion thread is the caller's business (it holds the
          conversations) — the absorbed document it gets back names it;
        - the **queued sends** (``pending``) are the union: a reply waiting
          for approval in either half waits in the whole;
        - the **attention block** in force is the open one, the survivor's
          first. A settled block of the absorbed chat is never taken: it
          was the user's word about that chat's mail, and on a survivor
          that has none it would speak for mail it was never about. The
          ``instructed`` block is the survivor's, else the absorbed chat's;
        - the survivor keeps its own name, people and contact card, and takes
          the absorbed chat's only where it has none; the participants and
          the roster are the union.

        One step under the lock, the survivor written before the absorbed
        file goes: a crash in between leaves both, and a repeat folds again —
        read mark, flags and people come out the same, the arrival counts
        higher than the truth by the absorbed chat's, which shows at worst a
        read chat as unread, never the reverse. Idempotent otherwise: with no
        absorbed document (a repeat, a chat nothing was ever noted for)
        nothing is written."""
        with self._lock:
            if absorbed_id == survivor_id or not self._path(absorbed_id).exists():
                return self._read(survivor_id), None
            gone = self._read(absorbed_id)
            doc = self._read(survivor_id)

            def covered(d: dict) -> int:
                # The arrivals a chat's newest read covered: none, for a chat
                # never read.
                return int(d.get("read_inbound") or 0)

            mark = max(doc.get("last_read") or "", gone.get("last_read") or "") or None
            if mark != doc.get("last_read"):
                doc["read_undo"] = None
                doc["read_settled"] = None
            read_before = (doc.get("read_inbound") is not None
                           or gone.get("read_inbound") is not None)

            # One count out of two, in an order that still means something:
            # the arrivals the survivor's read covered keep their places,
            # those the absorbed chat's read covered follow, then the
            # survivor's unread ones and last the absorbed chat's. A place at
            # or below the merged ``read_inbound`` is then a read arrival and
            # one above it an unread one, as in any chat.
            mine_seq, mine_read = int(doc.get("inbound_seq") or 0), covered(doc)
            their_seq, their_read = int(gone.get("inbound_seq") or 0), covered(gone)

            def placed(seq: int, theirs: bool) -> int:
                if theirs:
                    return seq + (mine_read if seq <= their_read else mine_seq)
                return seq if seq <= mine_read else seq + their_read

            recent: dict[str, int] = {}
            for source, theirs in ((gone, True), (doc, False)):
                for pair in source.get("inbound_recent") or []:
                    if (isinstance(pair, list) and len(pair) == 2 and pair[0]
                            and isinstance(pair[1], int)):
                        recent[str(pair[0])] = placed(pair[1], theirs)
            doc["inbound_recent"] = [
                [mid, seq] for mid, seq in sorted(recent.items(), key=lambda kv: kv[1])
            ][-INBOUND_RECENT_MAX:]
            doc["inbound_gone"] = sorted(
                {placed(seq, False) for seq in gone_arrivals(doc)}
                | {placed(seq, True) for seq in gone_arrivals(gone)})[-INBOUND_RECENT_MAX:]
            # What either half still owed its user, by message id: owed by
            # the whole — a fold tells nobody of anything. And the turns
            # each mail had, the most either half counted.
            untold = list(dict.fromkeys(
                [str(m) for m in (gone.get("untold") or []) if m]
                + [str(m) for m in (doc.get("untold") or []) if m]))
            doc["untold"] = untold[-INBOUND_RECENT_MAX:]
            attempts: dict[str, int] = {}
            for source in (gone, doc):
                for pair in source.get("turn_attempts") or []:
                    if isinstance(pair, list) and len(pair) == 2 and pair[0]:
                        attempts[str(pair[0])] = max(attempts.get(str(pair[0]), 0),
                                                     int(pair[1] or 0))
            doc["turn_attempts"] = [[m, n] for m, n in attempts.items()][-INBOUND_RECENT_MAX:]

            doc["read_inbound"] = mine_read + their_read if read_before else None
            doc["last_read"] = mark
            for key in ("read_seq", "inbound_seq", "attended_seq"):
                doc[key] = int(doc.get(key) or 0) + int(gone.get(key) or 0)
            marks = [m for m in (doc.get("unread_since"), gone.get("unread_since")) if m]
            doc["unread_since"] = min(marks) if marks else None
            if doc["unread_since"] and mark and mark >= doc["unread_since"]:
                doc["unread_since"] = None

            doc["starred"] = bool(doc.get("starred") or gone.get("starred"))
            doc["muted"] = bool(doc.get("muted") or gone.get("muted"))
            doc["archived"] = doc["muted"] or bool(doc.get("archived") and gone.get("archived"))

            version = max(int(doc.get("draft_version") or 0),
                          int(gone.get("draft_version") or 0))
            if (gone.get("draft") or {}).get("text") and not (doc.get("draft") or {}).get("text"):
                version += 1
                doc["draft"] = dict(gone["draft"], version=version)
                doc["cleared"] = None
            # Never behind a version the absorbed chat handed out: its clients
            # are about to be looking at this document.
            doc["draft_version"] = version

            doc["companion"] = doc.get("companion") or gone.get("companion")
            # The queued sends of both: they are this conversation's now.
            if doc.get("pending") or gone.get("pending"):
                doc["pending"] = clean_pendings(
                    list(doc.get("pending") or []) + list(gone.get("pending") or []))
            if doc.get("refused") or gone.get("refused"):
                doc["refused"] = clean_refused({**(gone.get("refused") or {}),
                                                **(doc.get("refused") or {})})
            # A whole that holds a mail is no chat of one unsent mail.
            doc["unsent"] = bool(doc.get("unsent") and gone.get("unsent"))
            mine, theirs = doc.get("attention") or None, gone.get("attention") or None
            if (theirs and theirs.get("state", "open") == "open"
                    and (not mine or mine.get("state", "open") != "open")):
                # The open one is the one still asking for something.
                doc["attention"] = theirs
            for key in ("instructed", "name", "peer", "contact"):
                if not doc.get(key) and gone.get(key):
                    doc[key] = gone[key]
            if not doc.get("gateway") and gone.get("gateway"):
                # Slug and provenance travel as one, as everywhere.
                doc["gateway"] = gone["gateway"]
                doc["gateway_source"] = gone.get("gateway_source")
            if doc.get("unknown_sender") is None:
                doc["unknown_sender"] = gone.get("unknown_sender")
            if doc.get("participants") is not None or gone.get("participants") is not None:
                doc["participants"] = clean_parties(
                    list(doc.get("participants") or []) + list(gone.get("participants") or []))
            if doc.get("group") is not None or gone.get("group") is not None:
                doc["group"] = bool(doc.get("group") or gone.get("group")
                                    or len(doc.get("participants") or []) > 1)
            roster = dict(gone.get("roster") or {})
            roster.update(doc.get("roster") or {})
            if roster != (doc.get("roster") or {}):
                doc["roster"] = roster
                doc["roster_refreshed"] = time.time()

            self._write(doc)
            self._path(absorbed_id).unlink(missing_ok=True)
            return doc, gone

    def set_attention(self, chat_id: str, block: dict | None, *,
                      attended: int | None = None) -> dict:
        """Store — or clear, with None — the attention block of a chat.

        ``attended`` is the ``inbound_seq`` of the newest arrival this block
        was decided from, passed by the arrival that wrote it; it only moves
        forward, so an older arrival settling late cannot claim a newer one."""
        with self._lock:
            doc = self._read(chat_id)
            doc["attention"] = dict(block) if block else None
            if attended is not None and int(attended) > int(doc.get("attended_seq") or 0):
                doc["attended_seq"] = int(attended)
            self._write(doc)
            return doc

    def update_instructed(self, chat_id: str, patch: dict, *,
                          fresh: bool = False, only_jobs=None) -> dict:
        """Merge ``patch`` into the chat's ``instructed`` block; ``fresh``
        starts a new one (a new instructed message) instead of merging into
        the last. ``only_jobs`` limits the merge to a block that still belongs
        to one of those arrivals (its ``job``): the block shows the newest
        instructed message, and the outcome of an older one that settles
        later must not be written over it."""
        with self._lock:
            doc = self._read(chat_id)
            if (not fresh and only_jobs is not None
                    and (doc.get("instructed") or {}).get("job") not in only_jobs):
                return doc
            base = {} if fresh else dict(doc.get("instructed") or {})
            base.update(patch)
            doc["instructed"] = base
            self._write(doc)
            return doc

    def set_companion(self, chat_id: str, conv_id: str | None) -> dict:
        """Record — or clear, with None — this chat's companion conversation.

        A plain setter, as the caller is the web-gateway, which is the only
        writer of this store and serializes create-or-get itself; making the
        store arbitrate instead would still leave the losing caller holding an
        already-created conversation."""
        with self._lock:
            doc = self._read(chat_id)
            doc["companion"] = conv_id or None
            self._write(doc)
            return doc

    def set_pending(self, chat_id: str, pendings, *,
                    since: float | None = None) -> tuple[dict, bool]:
        """Replace the chat's copy of its queued sends with what its gateway
        lists now; returns ``(doc, changed)``.

        The gateway's queue is the truth, so an entry it no longer lists is
        gone from the copy — sent, rejected or retracted. With one exception:
        an entry this side noted at or after ``since``, the instant the
        gateway was asked. A send queued while that answer was on its way is
        not in it, and dropping it would take a bubble the user has just been
        shown off the screen until the next answer puts it back.

        A chat with no document and nothing queued gets none: asking about a
        chat must not create its state."""
        with self._lock:
            exists = self._path(chat_id).exists()
            doc = self._read(chat_id)
            held = [p for p in doc.get("pending") or [] if isinstance(p, dict)]
            fresh = clean_pendings(pendings, known=held)
            if since is not None:
                # Nor does an answer asked before a decision was noted here
                # (update_pending) take that decision back: the entry stands
                # as decided until an answer from after it says otherwise.
                later = {p.get("request_id"): p for p in held
                         if isinstance(p.get("decided"), (int, float)) and p["decided"] >= since}
                listed = {p["request_id"] for p in fresh}
                fresh = clean_pendings(
                    [later.get(p["request_id"], p) for p in fresh] + [
                        p for p in held if p.get("request_id") not in listed
                        and isinstance(p.get("noted"), (int, float)) and p["noted"] >= since],
                    known=held)
            if fresh == held or (not exists and not fresh):
                return doc, False
            doc["pending"] = fresh
            self._write(doc)
            return doc, True

    def add_pending(self, chat_id: str, pending) -> dict:
        """Note one send that was queued just now (the send path, which has
        the gateway's answer in hand); an entry with the same request id is
        replaced. What does not read as a queued send is not stored."""
        with self._lock:
            doc = self._read(chat_id)
            clean = clean_pending(pending)
            if clean is None:
                return doc
            held = [p for p in doc.get("pending") or []
                    if isinstance(p, dict) and p.get("request_id") != clean["request_id"]]
            doc["pending"] = clean_pendings(held + [clean])
            self._write(doc)
            return doc

    def update_pending(self, chat_id: str, request_id: str, **changes) -> tuple[dict, bool]:
        """Say where one queued send of the copy stands now — ``status``,
        ``error``, ``outcome_unknown``, ``refused`` — for a decision or an
        outcome this side learned before the gateway's queue was read again;
        returns ``(doc, changed)``. The instant is kept on the entry
        (``decided``), so an answer of the queue asked before it does not
        undo it (:meth:`set_pending`). Nothing is written for a chat that
        holds no such entry."""
        with self._lock:
            doc = self._read(chat_id)
            held = [p for p in doc.get("pending") or [] if isinstance(p, dict)]
            fresh, changed = [], False
            for p in held:
                if p.get("request_id") == request_id:
                    moved = clean_pending({**p, **changes})
                    if moved is not None and any(
                            moved.get(k) != p.get(k)
                            for k in ("status", "error", "outcome_unknown", "refused")):
                        moved["decided"] = time.time()
                        p, changed = moved, True
                fresh.append(p)
            if not changed:
                return doc, False
            doc["pending"] = fresh
            self._write(doc)
            return doc, True

    def note_refused(self, chat_id: str, message_id: str | None, addresses) -> dict:
        """Remember whom a mail of the user's did not reach: the addresses
        the mail server refused while it took the mail for the others. Kept
        by the mail's Message-ID, the newest :data:`REFUSED_MAX` mails; a
        mail without an id, or with nobody refused, notes nothing."""
        with self._lock:
            doc = self._read(chat_id)
            if not isinstance(message_id, str) or not message_id.strip():
                return doc
            noted = clean_refused({**(doc.get("refused") or {}), message_id: addresses})
            if noted == (doc.get("refused") or {}):
                return doc
            doc["refused"] = noted
            self._write(doc)
            return doc

    def drop_pending(self, chat_id: str, *, request_id: str | None = None,
                     message_id: str | None = None) -> tuple[dict, bool]:
        """Take one queued send out of the copy — by its request id (a
        decision on /sends) or by the Message-ID its mail went out under (the
        rail's word that it was sent); returns ``(doc, dropped)``. Nothing is
        written for a chat that holds no such entry."""
        with self._lock:
            doc = self._read(chat_id)
            held = [p for p in doc.get("pending") or [] if isinstance(p, dict)]
            kept = [p for p in held
                    if not ((request_id and p.get("request_id") == request_id)
                            or (message_id and p.get("message_id") == message_id))]
            if len(kept) == len(held):
                return doc, False
            doc["pending"] = kept
            self._write(doc)
            return doc, True

    def advance_last_read(self, chat_id: str, ts: str | float | None, *,
                          everything: bool = False) -> dict:
        """Advance the read mark for the user's own reply — forward only, so
        a stale client (or an out-of-order echo) can never resurrect
        already-read messages.

        A reply is one of the two things that make a chat read, the explicit
        *Mark as read* (:meth:`mark_read`) being the other. Opening a chat
        calls nothing. ``ts`` is when the reply was written; ``everything``
        says it was written here and now (the dashboard's own send), so it
        answers every message that has landed, whatever clock stamped it. An
        echo from the user's device is taken by its stamp instead — see
        :meth:`_read_up_to`. No undo reaches behind a reply, so the record
        of the newest mark's undo is dropped."""
        with self._lock:
            doc = self._read(chat_id)
            if self._read_up_to(doc, iso_z(ts), everything=everything):
                doc["read_undo"] = None
            self._write(doc)
            return doc

    @staticmethod
    def _read_up_to(doc: dict, instant: str, *, everything: bool,
                    cap: str | None = None) -> bool:
        """One read, applied to ``doc``: the mark moves forward to
        ``instant`` (never back, and no further than ``cap`` where one is
        given), the read is counted, and — where this read covers the
        messages that have landed so far — ``read_inbound`` takes the arrival
        count. Returns whether it covered them.

        It does when it reads ``everything``, and otherwise when its instant
        is not behind the mark it found: a read naming an older instant (a
        stale client, a late echo of an old reply) says nothing about what
        landed after the newer read, and must not hide it.

        A read of ``everything`` also reaches the one message the mark
        cannot: an arrival stamped a little ahead of this clock
        (``inbound_ahead``), whose ledger record lies later than a mark made
        after it landed. Its stamp is kept beside the mark as ``read_ahead``
        — not in it: the mark stays the instant the read happened at, so
        that what a later reply or mark is compared with is a time of this
        clock. A read up to a named instant reaches no further than that
        instant: the stamp where the instant is not short of it (a caller
        naming the stamp of the message it showed, which the mark itself is
        not lifted to), and otherwise ``read_ahead`` stays as it was found.
        Either way it is dropped once the mark has passed it."""
        previous = doc["last_read"]
        mark = instant if cap is None else min(instant, cap)
        if previous is None or mark > previous:
            doc["last_read"] = mark
        doc["read_seq"] = int(doc.get("read_seq") or 0) + 1
        covers = everything or previous is None or instant >= previous
        if covers:
            doc["read_inbound"] = int(doc.get("inbound_seq") or 0)
        ahead = doc.get("inbound_ahead") or ""
        if everything or ahead <= instant:
            doc["read_ahead"] = ahead or None
        if (doc.get("read_ahead") or "") <= doc["last_read"]:
            doc["read_ahead"] = None
        if doc.get("unread_since") and (everything
                                        or doc["last_read"] >= doc["unread_since"]):
            doc["unread_since"] = None
        return covers

    def mark_read(self, chat_id: str,
                  ts: str | float | None = None) -> tuple[dict, str | None]:
        """*Mark as read*: ``(doc, previous)``, ``previous`` being the mark
        before this call — None for a chat never read. The pair is what an
        undo hands back to :meth:`restore_last_read`, so it is taken under
        the same lock as the write it describes.

        Without ``ts`` the chat is read as it stands: the mark moves to this
        clock's now, and every message that has landed is covered, whatever
        its stamp. With one it is read up to that instant, forward only —
        what is stamped later stays unread.

        Either way the mark is never set past this clock's now. A read cannot
        have happened later than that, and an instant from ahead of it — the
        stamp of a message from a fast clock, named by a client — would
        stand as the cut-off for whatever arrives until then. Such a
        message, where the mark covers it, is covered beside the mark
        instead (``read_ahead``, see :meth:`_read_up_to`). (A reply is
        different: :meth:`advance_last_read` keeps the instant it was
        written at, which is the reply's own place in the conversation.)"""
        return self.mark_read_unless_arrived(chat_id, ts, None)[1:]

    def mark_read_unless_arrived(self, chat_id: str, ts: str | float | None,
                                 seen: int | None) -> tuple[bool, dict, str | None]:
        """:meth:`mark_read` by a caller that says what it showed the user:
        ``seen`` is the ``inbound_seq`` its view of the chat carried. Returns
        ``(marked, doc, previous)``.

        A message that has landed since (``inbound_seq`` past ``seen``) is
        one the user was never shown, and "read" was not said about it: the
        mark is refused and nothing is written. One decision under the lock,
        like :meth:`archive_unless_arrived` — an arrival counts itself under
        the same lock, so either it is seen here or it lands after the mark
        and is unread by its place in the count. ``seen`` None marks
        unconditionally."""
        now = iso_z()
        instant = now if ts is None else iso_z(ts)
        with self._lock:
            doc = self._read(chat_id)
            if seen is not None and int(doc.get("inbound_seq") or 0) > seen:
                return False, doc, doc["last_read"]
            previous = doc["last_read"]
            found = {"read_inbound": doc.get("read_inbound"),
                     "read_ahead": doc.get("read_ahead")}
            if self._read_up_to(doc, instant, everything=ts is None, cap=now):
                doc["read_undo"] = found
            self._write(doc)
            return True, doc, previous

    def restore_last_read(self, chat_id: str, *, restore: str | None,
                          marked: str) -> tuple[bool, dict]:
        """Undo a *Mark as read*: put the mark back to ``restore`` (None for
        a chat that had none) — the one way ``last_read`` ever moves
        backwards. Returns ``(restored, doc)``.

        Guarded twice, because moving this mark back resurrects messages as
        unread. It happens only while the mark is still exactly ``marked``,
        the value the call being undone answered: a reply or a later mark in
        between has said something newer about the chat, and an undo that
        arrives after it must not take that back (refused, nothing changes).
        And ``restore`` may not lie past ``marked``, which would be an
        advance wearing the undo's name — that raises ValueError.

        ``read_inbound`` and ``read_ahead`` go back with the mark, to what
        the mark being undone found (``read_undo``): the messages that had
        landed unread before it are unread again, with any that came since —
        one stamped ahead of this clock among them. Where a reply fell into
        the mark's own second — the one way past the first guard — nothing
        is recorded to put back, and the reply's cover stands.

        ``read_seq`` stays: it counts reads, and the read that is being
        undone happened. ``unread_since`` is not put back either; the next
        arrival sets it afresh."""
        marked = iso_z(marked)
        if restore is not None:
            restore = iso_z(restore)
            if restore > marked:
                raise ValueError("restore must not be later than the mark it undoes")
        with self._lock:
            doc = self._read(chat_id)
            if doc["last_read"] != marked:
                return False, doc
            doc["last_read"] = restore
            undo = doc.get("read_undo")
            if isinstance(undo, dict):
                doc["read_inbound"] = undo.get("read_inbound")
                # (A record written before ``read_ahead`` existed holds none:
                # the mark it describes changed none.)
                if "read_ahead" in undo:
                    doc["read_ahead"] = undo["read_ahead"]
            doc["read_undo"] = None
            self._write(doc)
            return True, doc

    def set_read_settled(self, chat_id: str, done_at: str | None) -> dict:
        """Record — or clear, with None — which attention item the newest
        *Mark as read* settled, by that item's ``done_at``.

        The mark and the item are settled in two steps (the web-gateway first
        has to see whether the mark left anything unread), and an undo comes
        as a separate request, so the link between them has to be stored. An
        identity rather than a flag: an item that was reopened by a new
        message and settled again by something else carries another
        ``done_at``, and undoing the old mark then leaves it alone."""
        with self._lock:
            doc = self._read(chat_id)
            doc["read_settled"] = done_at or None
            self._write(doc)
            return doc

    def mark_unread(self, chat_id: str, ts: str | float | None, *,
                    message_id: str | None = None,
                    stamp: str | float | None = None) -> tuple[dict, bool]:
        """:meth:`count_arrival` for a caller that does not ask whether the
        event was a repeat: ``(doc, had_unread)``."""
        return self.count_arrival(chat_id, ts, message_id=message_id, stamp=stamp)[:2]

    def count_arrival(self, chat_id: str, ts: str | float | None, *,
                      message_id: str | None = None,
                      stamp: str | float | None = None,
                      owe: bool = False) -> tuple[dict, bool, bool]:
        """Note one inbound landing unread; returns ``(doc, had_unread,
        counted)``.

        ``had_unread`` says whether the chat already held unread messages
        *before* this one — the notification-mode split (False = "new",
        True = "reply") without querying the store on the hot path.

        Every message counts as an arrival (``inbound_seq``), read or not —
        and the count is what makes it unread: past ``read_inbound`` it
        landed after the newest read, wherever its stamp puts it
        (:func:`arrived_since_read`).

        Every message, not every call: the rail delivers at least once (a
        gateway retries a POST that timed out, and redelivers what it was
        never told was taken), and a second event for one message is no
        second arrival. ``message_id`` is what tells: one that is among the
        chat's newest counted arrivals (``inbound_recent``) is a repeat, and
        nothing is counted or written for it — ``counted`` is False,
        ``had_unread`` is then simply how the chat stands, and
        :func:`arrival_seq` gives the place the message was counted at. The
        record is in the document, so the answer is the same after a
        restart. An event without an id cannot be told from a new message
        and is counted.

        One repeat is an arrival: a message that was gone (its place is in
        ``inbound_gone`` — :meth:`forget_arrivals`) and is announced again
        has come back, a mail moved out of the Trash. Its old place stays
        gone and it is counted anew, like the new message it is to the chat.

        ``stamp`` is the message's own stamp where ``ts`` is not it — the
        rail hands on a stamp from ahead of this clock as now. One that lies
        ahead by no more than READ_SKEW_SECONDS is kept as
        ``inbound_ahead``, for the read that covers this message
        (:meth:`_read_up_to`); one further ahead is no clock's error anyone
        can vouch for, and is not.

        ``owe`` (a mail's arrival) notes a counted arrival with an id as
        owed to the user (``untold``) in the same write: whatever ends the
        request between this count and the arrival's attention step — a
        restart, a full disk — leaves it owed, and a repeated offer finds
        it so (:func:`owes`, :meth:`told`)."""
        mark = iso_z(ts)
        stamp = mark if stamp is None else iso_z(stamp)
        now = time.time()
        with self._lock:
            doc = self._read(chat_id)
            had_unread = bool(doc.get("unread_since"))
            recent = [pair for pair in doc.get("inbound_recent") or []
                      if isinstance(pair, list) and len(pair) == 2]
            if message_id:
                places = [seq for mid, seq in recent if mid == message_id]
                if places and places[-1] not in gone_arrivals(doc):
                    return doc, had_unread, False
                recent = [pair for pair in recent if pair[0] != message_id]
            if not had_unread:
                doc["unread_since"] = mark
            doc["inbound_seq"] = int(doc.get("inbound_seq") or 0) + 1
            if message_id:
                recent.append([message_id, doc["inbound_seq"]])
                doc["inbound_recent"] = recent[-INBOUND_RECENT_MAX:]
                if owe:
                    untold = [m for m in doc.get("untold") or [] if m != message_id]
                    doc["untold"] = (untold + [message_id])[-INBOUND_RECENT_MAX:]
            if (iso_z(now) < stamp <= iso_z(now + READ_SKEW_SECONDS)
                    and stamp > (doc.get("inbound_ahead") or "")):
                doc["inbound_ahead"] = stamp
            self._write(doc)
            return doc, had_unread, True

    def forget_arrivals(self, chat_id: str, message_ids) -> dict:
        """Messages of the chat are gone from where they came from — mails
        deleted or moved away in a client, tombstones in the ledger now.
        Those among the counted arrivals stop being unread ones: their
        places go into ``inbound_gone``, which :func:`arrived_since_read`
        leaves out. Returns the document.

        By message id, through ``inbound_recent``: an arrival older than
        that remembers keeps its place, and at worst a chat shows unread
        until it is marked — never the reverse. The count itself does not
        move (a client's ``seen``, the attention model's ``attended_seq``
        and every remembered place stay true), so the call is safe to
        repeat. Nothing is written for ids the chat never counted, and no
        document is made for a chat that has none."""
        wanted = {str(m) for m in message_ids or [] if m}
        with self._lock:
            doc = self._read(chat_id)
            gone = set(gone_arrivals(doc))
            found = {pair[1] for pair in doc.get("inbound_recent") or []
                     if isinstance(pair, list) and len(pair) == 2
                     and str(pair[0]) in wanted and isinstance(pair[1], int)}
            if found <= gone:
                return doc
            doc["inbound_gone"] = sorted(gone | found)[-INBOUND_RECENT_MAX:]
            if (doc.get("unread_since") and doc.get("read_inbound") is not None
                    and arrived_since_read(doc) == 0):
                # The watermark of the first unread message, and none is left.
                doc["unread_since"] = None
            self._write(doc)
            return doc

    def told(self, chat_id: str, message_id: str | None) -> dict:
        """The arrival with this id has been told to the user — its
        attention step and push ran to the end, or the ring that waited on
        its turn went out (or was put away by that turn): it is owed no
        more. Returns the document; nothing is written where nothing was
        owed, so a messenger's arrival, which never is, costs a read."""
        with self._lock:
            doc = self._read(chat_id)
            if not owes(doc, message_id):
                return doc
            doc["untold"] = [m for m in doc.get("untold") or [] if m != str(message_id)]
            self._write(doc)
            return doc

    def note_turn_attempt(self, chat_id: str, message_id: str | None) -> int:
        """One more turn started for the arrival with this id; returns how
        many it has had now (0 for a message without an id, which is never
        offered again by it)."""
        if not message_id:
            return 0
        with self._lock:
            doc = self._read(chat_id)
            pairs = [p for p in doc.get("turn_attempts") or []
                     if isinstance(p, list) and len(p) == 2 and p[0] != message_id]
            count = turn_attempts(doc, message_id) + 1
            doc["turn_attempts"] = (pairs + [[str(message_id), count]])[-INBOUND_RECENT_MAX:]
            self._write(doc)
            return count

    def archive_through(self, chat_id: str, cut: int, *,
                        muted: bool = False) -> tuple[dict, bool]:
        """Archive — and with ``muted`` mute — a chat for an agent's turn
        that was started for the arrivals up to place ``cut`` in its count,
        unless one has been counted since (``inbound_seq`` past ``cut``):
        the turn never saw it, and archiving would put it away unseen.
        Returns ``(doc, done)``.

        One decision under the lock, like :meth:`archive_unless_arrived`:
        an arrival counts itself before it unarchives, so either this sees
        the count and leaves the chat be, or it archives first and the
        arrival's unarchive brings the chat back (unless this muted it)."""
        with self._lock:
            doc = self._read(chat_id)
            if int(doc.get("inbound_seq") or 0) > int(cut):
                return doc, False
            doc["archived"] = True
            if muted:
                doc["muted"] = True
            self._write(doc)
            return doc, True

    def archive_unless_arrived(self, chat_id: str) -> tuple[dict, bool]:
        """Archive a chat on its attention timer's behalf — unless a message
        has landed that the attention model has not taken in yet
        (``inbound_seq`` ahead of ``attended_seq``). Returns ``(doc,
        archived)``; True also when it was archived already.

        One decision under the lock, like :meth:`unarchive_unless_muted`, and
        its mirror image: an arrival counts itself (mark_unread) before it
        unarchives, so either this sees the count and leaves the chat be, or
        it archives first and the arrival's unarchive brings the chat back."""
        with self._lock:
            doc = self._read(chat_id)
            if int(doc.get("inbound_seq") or 0) > int(doc.get("attended_seq") or 0):
                return doc, False
            if not doc.get("archived"):
                doc["archived"] = True
                self._write(doc)
            return doc, True

    def unarchive_unless_muted(self, chat_id: str) -> dict:
        """Bring an archived chat back for a new message — unless it is muted.

        The check and the write are one operation under the lock because they
        are one decision. Taken apart, a flag write landing between them is
        read back stale and half-undone: a mute sets `muted` AND `archived`,
        and an arrival deciding from a snapshot taken before it would clear
        `archived` while `muted` stayed, putting a chat the user just muted
        back in the active list. Nothing wrote these flags concurrently with
        the rail until the dashboard could, which is why the split version
        stood for as long as it did.
        """
        with self._lock:
            doc = self._read(chat_id)
            if doc.get("archived") and not doc.get("muted"):
                doc["archived"] = False
                self._write(doc)
            return doc

    def set_flags(self, chat_id: str, *, archived: bool | None = None,
                  muted: bool | None = None, starred: bool | None = None) -> dict:
        """Set a chat's user-facing flags, which say where the user wants
        the chat on their screen and nothing else. `starred` puts it at the
        top of whichever list it is in, and is independent of the other two. (Whether a message is
        *worked* follows the sender's VIP status in the triage policy: a chat
        is a place, and the user's interest is in a person.)

        **Muting archives.** The two are not independent settings to combine:
        archiving is "out of my way until it speaks again", and muting is "out
        of my way, and do not let it speak" — which only means anything if the
        chat is out of the way to begin with. So setting `muted` sets
        `archived` too, and a caller passing both never has to think about the
        order. Unmuting deliberately leaves the chat archived: it stays put
        until the next message brings it back, which is what un-muting is for.

        The inverse rule lives in :meth:`unarchive_unless_muted`, which an
        arrival calls: a new message un-archives an unmuted chat."""
        with self._lock:
            doc = self._read(chat_id)
            if archived is not None:
                doc["archived"] = bool(archived)
            if muted is not None:
                doc["muted"] = bool(muted)
                if muted:
                    doc["archived"] = True
            if starred is not None:
                doc["starred"] = bool(starred)
            self._write(doc)
            return doc

    def set_draft(self, chat_id: str, text: str, *, author: str,
                  agent: str | None = None,
                  base_version: int | None = None,
                  require_free: bool = False) -> tuple[bool, dict]:
        """Write (or clear) the shared draft; returns ``(accepted, doc)``.

        ``base_version`` is the draft version the writer based its edit on —
        anything but the current version is rejected (the caller answers 409
        with the current doc, sha-guard style). ``base_version=None`` writes
        unconditionally **unless** ``require_free`` is set, which additionally
        refuses to overwrite a non-empty user-authored draft — the agent
        staging path, where silently clobbering what the user is typing would
        be worse than a conflict.

        Empty ``text`` clears the draft (the composer's ✕), dropping the
        author tag with it; the version counter still advances so the clear is
        itself guarded against. What was cleared is kept aside as ``cleared``
        so :meth:`undo_clear` can put it back exactly — text, author and agent
        name — because a draft an agent staged is not something the user can
        retype, and re-sending it as their own words would lose the one marker
        that says whose words they are.
        """
        if author not in AUTHORS:
            raise ValueError(f"author must be one of {'|'.join(AUTHORS)}, got {author!r}")
        text = (text or "").strip()
        with self._lock:
            doc = self._read(chat_id)
            current = int(doc.get("draft_version") or 0)
            if base_version is not None and int(base_version) != current:
                return False, doc
            if base_version is None and require_free:
                existing = doc.get("draft") or {}
                if existing.get("text") and existing.get("author") == "user":
                    return False, doc
            doc["draft_version"] = current + 1
            if not text:
                previous = doc.get("draft") or None
                # Only a clear that actually removed something is undoable, and
                # only the newest one: an older stash would offer to restore
                # text the user has long since moved past.
                doc["cleared"] = ({**previous, "at": iso_z(None)}
                                  if previous and previous.get("text") else None)
                doc["draft"] = None
            else:
                draft = {
                    "text": text,
                    "author": author,
                    "ts": iso_z(None),
                    "version": doc["draft_version"],
                }
                if agent:
                    draft["agent"] = agent
                doc["draft"] = draft
                # Writing a draft settles the question of what the composer
                # holds; whatever was cleared before it is no longer the thing
                # to go back to.
                doc["cleared"] = None
            self._write(doc)
            return True, doc

    def clear_draft(self, chat_id: str, *, only_text: str | None = None) -> dict:
        """Unconditional clear — the send path, where the draft just went out.

        A draft that has just been sent is not something to offer back, so this
        leaves nothing to undo.

        With ``only_text``, the clear is made only while the draft still holds
        exactly those words (compared trimmed) — the repeat of a send, whose
        words went out with its first request: a draft that still holds them
        is that mail, put back into the box when its answer was lost, and one
        written since is left as it is. Guarded by the draft's version, so a
        save that lands between the look and the clear is never lost."""
        if only_text is not None:
            with self._lock:
                doc = self._read(chat_id)
                held = ((doc.get("draft") or {}).get("text") or "").strip()
                version = int(doc.get("draft_version") or 0)
            if not held or held != only_text.strip():
                return doc
            accepted, doc = self.set_draft(chat_id, "", author="user", base_version=version)
            if not accepted:
                return doc
        else:
            _, doc = self.set_draft(chat_id, "", author="user")
        with self._lock:
            doc = self._read(chat_id)
            if doc.get("cleared"):
                doc["cleared"] = None
                self._write(doc)
            return doc

    def undo_clear(self, chat_id: str, *,
                   max_age_seconds: float = UNDO_CLEAR_MAX_AGE) -> tuple[bool, dict]:
        """Put the newest cleared draft back, verbatim; ``(restored, doc)``.

        The restore happens here rather than by the client re-submitting the
        text, for two reasons. It keeps the draft's **author** — a draft an
        agent staged comes back marked as the agent's, where a client re-write
        could only claim it as the user's, and that marker is the one thing
        saying whose words these are about to be sent as. And it is a single
        guarded step, so it cannot race the clear that produced it: whichever
        order the two requests arrive in, the state they leave is coherent.

        Refused when there is nothing to restore, or when the stash is older
        than ``max_age_seconds`` — an undo offered long after the fact would be
        putting back text the user has moved on from."""
        with self._lock:
            doc = self._read(chat_id)
            stash = doc.get("cleared") or None
            if not stash or not stash.get("text"):
                return False, doc
            try:
                age = time.time() - datetime.fromisoformat(
                    str(stash.get("at") or "").replace("Z", "+00:00")).timestamp()
            except ValueError:
                age = None
            if age is None or age > max_age_seconds:
                doc["cleared"] = None
                self._write(doc)
                return False, doc
            doc["draft_version"] = int(doc.get("draft_version") or 0) + 1
            draft = {
                "text": stash["text"],
                "author": stash.get("author") if stash.get("author") in AUTHORS else "user",
                "ts": iso_z(None),
                "version": doc["draft_version"],
            }
            if stash.get("agent"):
                draft["agent"] = stash["agent"]
            doc["draft"] = draft
            doc["cleared"] = None
            self._write(doc)
            return True, doc


# ── Calls owed to a chat's gateway ────────────────────────────────────────────

class OwedCalls:
    """A small durable queue of calls a chat's gateway is still owed.

    What the user did to a chat, its mailbox is told (the web gateway's
    "mailbox follows the chat"): best-effort, after the chat's state changed.
    A call that did not go through is kept here as ``(chat id, action)`` with
    the instant it was first owed, in one JSON file written atomically — so a
    restart of the web gateway forgets none of them, and a timer there makes
    them again. Bounded both ways: the newest ``maxlen`` pairs, and none
    older than ``keep_seconds`` (a call nobody could make for that long is
    given up; the chat's state stays the truth).

    A call may carry its ``scope`` — which messages of the chat it is about,
    as the list of their ids — so that one made late is still about what the
    user acted on and not about what has arrived since.

    Reads like a set of pairs: ``in``, ``len``, iteration (oldest first)."""

    def __init__(self, path: str | Path, maxlen: int = 500,
                 keep_seconds: float = 7 * 86400.0):
        self._path = Path(path)
        self._maxlen = int(maxlen)
        self._keep = float(keep_seconds)
        self._lock = threading.Lock()
        self._owed: dict[tuple[str, str], float] | None = None
        self._scopes: dict[tuple[str, str], list[str]] = {}

    def _load(self) -> dict[tuple[str, str], float]:
        if self._owed is None:
            owed: dict[tuple[str, str], float] = {}
            self._scopes = {}
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = None
            for row in (data.get("owed") if isinstance(data, dict) else None) or []:
                if (isinstance(row, list) and len(row) in (3, 4) and isinstance(row[0], str)
                        and isinstance(row[1], str) and isinstance(row[2], (int, float))
                        and not isinstance(row[2], bool)):
                    owed[(row[0], row[1])] = float(row[2])
                    if len(row) == 4 and isinstance(row[3], list):
                        self._scopes[(row[0], row[1])] = [x for x in row[3] if isinstance(x, str)]
            self._owed = owed
        cutoff = time.time() - self._keep
        for pair in [p for p, since in self._owed.items() if since < cutoff]:
            self._owed.pop(pair, None)
            self._scopes.pop(pair, None)
        return self._owed

    def _save(self) -> None:
        rows = [[chat_id, action, since] + ([self._scopes[(chat_id, action)]]
                                             if (chat_id, action) in self._scopes else [])
                for (chat_id, action), since in self._owed.items()]
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"owed": rows}, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as exc:
            # Still owed in memory; only a restart would forget it.
            print(f"[chat-state] owed calls could not be written ({exc})", flush=True)

    def add(self, chat_id: str, action: str, scope: list[str] | None = None) -> bool:
        """Owe one call; True when it was not owed already (its first
        instant stands otherwise). A ``scope`` given replaces the one kept:
        the newest action is what the chat says now."""
        with self._lock:
            owed = self._load()
            pair = (chat_id, action)
            fresh = pair not in owed
            if not fresh and (scope is None or self._scopes.get(pair) == scope):
                return False
            owed.setdefault(pair, time.time())
            if scope is not None:
                self._scopes[pair] = [x for x in scope if isinstance(x, str)]
            while len(owed) > self._maxlen:
                self._scopes.pop(next(iter(owed)), None)
                owed.pop(next(iter(owed)))
            self._save()
            return fresh

    def scope(self, chat_id: str, action: str) -> list[str] | None:
        """The scope an owed call carries, or None where it carries none."""
        with self._lock:
            self._load()
            kept = self._scopes.get((chat_id, action))
            return list(kept) if kept is not None else None

    def discard(self, chat_id: str, action: str | None = None) -> int:
        """One call is owed no more — or, without ``action``, none of the
        chat's; returns how many went."""
        with self._lock:
            owed = self._load()
            doomed = [p for p in owed if p[0] == chat_id and action in (None, p[1])]
            for pair in doomed:
                owed.pop(pair, None)
                self._scopes.pop(pair, None)
            if doomed:
                self._save()
            return len(doomed)

    def clear(self) -> None:
        with self._lock:
            if self._load():
                self._owed, self._scopes = {}, {}
                self._save()

    def pairs(self) -> list[tuple[str, str]]:
        """What is owed, oldest first."""
        with self._lock:
            return list(self._load())

    def __contains__(self, pair) -> bool:
        with self._lock:
            return pair in self._load()

    def __len__(self) -> int:
        with self._lock:
            return len(self._load())

    def __iter__(self):
        return iter(self.pairs())


# ── Live overlay ──────────────────────────────────────────────────────────────

class ChatOverlay:
    """In-memory overlay of the last seconds of chat traffic.

    The chat API serves the life store's answer plus whatever entries the store
    has not caught up to yet — the message a push notification announced is by
    construction in the view the tap opens, because the same rail event
    produced both. Entries expire after ``ttl`` seconds (by then the store
    holds them) and are deduplicated on the chat and the channel message id,
    falling back to ``(chat, ts, text)`` when a producer captured no id. The
    chat is part of either key: a message id is unique within a conversation,
    not across them — one mail to two of the user's addresses is one
    ``Message-ID`` in two chats. Disposable by design:
    a web-gateway restart loses only those seconds of freshness, never a
    message — the ledger is the record.
    """

    def __init__(self, ttl: float = 90.0, maxlen: int = 500):
        self._ttl = float(ttl)
        self._maxlen = int(maxlen)
        self._lock = threading.Lock()
        self._entries: dict[tuple, dict] = {}  # dedup key -> entry (+_inserted, _first)

    @staticmethod
    def entry_key(entry: dict) -> tuple:
        mid = entry.get("message_id")
        if mid:
            return ("id", entry.get("chat_id"), str(mid))
        return ("txt", entry.get("chat_id"), entry.get("ts"), entry.get("text"))

    def insert(self, entry: dict) -> None:
        """Add one message event; a re-delivery of the same key replaces it
        (and keeps the instant the message was first seen: ``first_seen``)."""
        now = time.time()
        stored = dict(entry)
        stored["_inserted"] = now
        with self._lock:
            self._prune(now)
            key = self.entry_key(entry)
            stored["_first"] = (self._entries.get(key) or {}).get("_first", now)
            self._entries[key] = stored
            while len(self._entries) > self._maxlen:
                oldest = min(self._entries, key=lambda k: self._entries[k]["_inserted"])
                self._entries.pop(oldest)

    def first_seen(self, chat_id: str, message_id: str | None) -> float | None:
        """When a live entry of this chat with this message id was first
        inserted (this process's clock), or None where there is none. What
        tells a message the chat already had from one that arrived while
        something was waiting for it — a send answered again long after its
        mail went out, against the rail announcing a mail a moment before
        the request that sent it gets its answer."""
        if not message_id:
            return None
        now = time.time()
        with self._lock:
            self._prune(now)
            entry = self._entries.get(("id", chat_id, str(message_id)))
            return entry.get("_first") if entry else None

    def _prune(self, now: float) -> None:
        expired = [k for k, e in self._entries.items()
                   if now - e["_inserted"] > self._ttl]
        for k in expired:
            self._entries.pop(k, None)

    def forget(self, chat_id: str) -> int:
        """Drop every live entry of one chat (a deleted chat); returns the count."""
        with self._lock:
            doomed = [k for k, e in self._entries.items() if e.get("chat_id") == chat_id]
            for k in doomed:
                self._entries.pop(k, None)
        return len(doomed)

    def rekey(self, old_chat_id: str, new_chat_id: str) -> int:
        """Move every live entry of one chat to another (two chats that
        turned out to be one — ChatStateStore.merge); returns the count.

        An entry's dedup key names its chat, so the entries are filed anew;
        one that already stands under the new chat's key keeps its place."""
        with self._lock:
            moving = [(k, e) for k, e in self._entries.items()
                      if e.get("chat_id") == old_chat_id]
            for key, entry in moving:
                self._entries.pop(key, None)
                entry["chat_id"] = new_chat_id
                self._entries.setdefault(self.entry_key(entry), entry)
        return len(moving)

    def forget_messages(self, chat_id: str, message_ids) -> int:
        """Drop the live entries of one chat that carry one of these message
        ids (messages gone from where they came from); returns the count."""
        wanted = {str(m) for m in message_ids or [] if m}
        with self._lock:
            doomed = [k for k, e in self._entries.items()
                      if e.get("chat_id") == chat_id
                      and str(e.get("message_id") or "") in wanted]
            for k in doomed:
                self._entries.pop(k, None)
        return len(doomed)

    def entries(self, chat_id: str | None = None) -> list[dict]:
        """Live entries (optionally one chat's), ascending by ts — insertion
        order breaks same-second ties, since the seconds-precision timestamps
        cannot order a quick exchange themselves."""
        now = time.time()
        with self._lock:
            self._prune(now)
            items = [dict(e) for e in self._entries.values()
                     if chat_id is None or e.get("chat_id") == chat_id]
        items.sort(key=lambda e: (e.get("ts") or "", e.get("_inserted") or 0.0))
        for e in items:
            e.pop("_inserted", None)
            e.pop("_first", None)
        return items
