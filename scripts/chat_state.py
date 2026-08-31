"""Retinue-side chat state and the live message overlay (messenger chats).

A **chat** is one messenger conversation (one peer or group, on one gateway
account), identified as ``<channel>:<chat-key>`` — the chat key being the exact
recipient string that channel's send path accepts, the same ``kb:chat`` value
the gateways stamp on every ledger record. The message history itself lives in
the gateways' ledgers and is served over SPARQL; everything about a chat that
is *not* a channel message lives here, in one JSON document per chat under
``CHAT_STATE_DIR``:

- ``last_read`` — the user's read watermark (ISO-8601), from which unread
  badges derive; only ever advances.
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

_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")


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


def split_chat_id(chat_id: str) -> tuple[str, str] | None:
    """Split ``<channel>:<chat-key>`` at the FIRST colon.

    Chat keys themselves contain ``:`` (Signal ``group:<id>``), ``@`` and
    ``+``, so only the first separator is structural. Returns None when the id
    has no channel or no key."""
    channel, sep, key = (chat_id or "").partition(":")
    if not sep or not channel or not key:
        return None
    return channel, key


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
        parts = split_chat_id(chat_id) or ("", chat_id)
        return {
            "id": chat_id,
            "channel": parts[0],
            "key": parts[1],
            "last_read": None,
            "draft": None,
            # Monotonic counter surviving draft clears, so a version can never
            # repeat and a stale writer is always detectable.
            "draft_version": 0,
            "archived": False,
            "muted": False,
            "name": None,
            "group": None,
            "gateway": None,
            # How `gateway` was established; see GATEWAY_SOURCE_ACCOUNT. A doc
            # written before this field existed lacks it and so is untrusted by
            # construction — the marker cannot be forged by an old stamp.
            "gateway_source": None,
            "roster": {},
            "roster_refreshed": None,
            # First-unread watermark: set when an inbound lands on a fully-read
            # chat, cleared when the user catches up. Its presence is what
            # classifies the next notification as "reply" rather than "new".
            "unread_since": None,
            # Id of this chat's companion conversation — the dashboard thread
            # where the user works out a reply with Ara. None until one is
            # first asked for; see `set_companion`.
            "companion": None,
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
        doc["id"] = chat_id  # the digest filename is not invertible; trust the caller's id
        return doc

    def _write(self, doc: dict) -> None:
        path = self._path(doc["id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    # -- reads ----------------------------------------------------------------

    def get(self, chat_id: str) -> dict:
        with self._lock:
            return self._read(chat_id)

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

    # -- writes ---------------------------------------------------------------

    def note_message(self, chat_id: str, *, name: str | None = None,
                     group: bool | None = None, gateway: str | None = None,
                     gateway_source: str | None = None,
                     sender: str | None = None,
                     sender_name: str | None = None) -> dict:
        """Cache display metadata learned from a message event (the rail).

        The ledger persists handles, never names — names are remembered here as
        they pass by, so group bubbles can label their senders and a send can
        route back through the exact gateway account the chat lives on."""
        with self._lock:
            doc = self._read(chat_id)
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

    def advance_last_read(self, chat_id: str, ts: str | float | None) -> dict:
        """Advance the read watermark — forward only, so a stale client (or an
        out-of-order echo) can never resurrect already-read messages."""
        mark = iso_z(ts)
        with self._lock:
            doc = self._read(chat_id)
            if doc["last_read"] is None or mark > doc["last_read"]:
                doc["last_read"] = mark
            if doc.get("unread_since") and doc["last_read"] >= doc["unread_since"]:
                doc["unread_since"] = None
            self._write(doc)
            return doc

    def mark_unread(self, chat_id: str, ts: str | float | None) -> tuple[dict, bool]:
        """Note one inbound landing unread; returns ``(doc, had_unread)``.

        ``had_unread`` says whether the chat already held unread messages
        *before* this one — the notification-mode split (False = "new",
        True = "reply") without querying the store on the hot path."""
        mark = iso_z(ts)
        with self._lock:
            doc = self._read(chat_id)
            had_unread = bool(doc.get("unread_since"))
            if not had_unread:
                doc["unread_since"] = mark
                self._write(doc)
            return doc, had_unread

    def set_flags(self, chat_id: str, *, archived: bool | None = None,
                  muted: bool | None = None) -> dict:
        with self._lock:
            doc = self._read(chat_id)
            if archived is not None:
                doc["archived"] = bool(archived)
            if muted is not None:
                doc["muted"] = bool(muted)
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
        itself guarded against.
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
            self._write(doc)
            return True, doc

    def clear_draft(self, chat_id: str) -> dict:
        """Unconditional clear — the send path, where the draft just went out."""
        _, doc = self.set_draft(chat_id, "", author="user")
        return doc


# ── Live overlay ──────────────────────────────────────────────────────────────

class ChatOverlay:
    """In-memory overlay of the last seconds of chat traffic.

    The chat API serves the life store's answer plus whatever entries the store
    has not caught up to yet — the message a push notification announced is by
    construction in the view the tap opens, because the same rail event
    produced both. Entries expire after ``ttl`` seconds (by then the store
    holds them) and are deduplicated on the channel message id, falling back to
    ``(chat, ts, text)`` when a producer captured no id. Disposable by design:
    a web-gateway restart loses only those seconds of freshness, never a
    message — the ledger is the record.
    """

    def __init__(self, ttl: float = 90.0, maxlen: int = 500):
        self._ttl = float(ttl)
        self._maxlen = int(maxlen)
        self._lock = threading.Lock()
        self._entries: dict[tuple, dict] = {}  # dedup key -> entry (+_inserted)

    @staticmethod
    def entry_key(entry: dict) -> tuple:
        mid = entry.get("message_id")
        if mid:
            return ("id", str(mid))
        return ("txt", entry.get("chat_id"), entry.get("ts"), entry.get("text"))

    def insert(self, entry: dict) -> None:
        """Add one message event; a re-delivery of the same key replaces it."""
        now = time.time()
        stored = dict(entry)
        stored["_inserted"] = now
        with self._lock:
            self._prune(now)
            self._entries[self.entry_key(entry)] = stored
            while len(self._entries) > self._maxlen:
                oldest = min(self._entries, key=lambda k: self._entries[k]["_inserted"])
                self._entries.pop(oldest)

    def _prune(self, now: float) -> None:
        expired = [k for k, e in self._entries.items()
                   if now - e["_inserted"] > self._ttl]
        for k in expired:
            self._entries.pop(k, None)

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
        return items
