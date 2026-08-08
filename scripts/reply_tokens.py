"""Shared reply-token store for the messaging gateways.

All three linked-device gateways (WhatsApp, Signal, Telegram) share the same
structural defect when they forward an inbound *inbox* message to Ara's triage:
the prompt carries only a human-readable ``sender_label`` (a name, maybe a bare
number), never the exact **origin address** of the conversation the message
arrived in. The gateway knows that address — the WhatsApp chat JID, the Signal
source number/UUID, the Telegram ``chat_id`` — but drops it on the floor.

So when the user then says "reply", the agent has to reconstruct the address by
resolving the *name*, and name resolution can land on the wrong account entirely
(a correspondent who wrote from an office number whose name maps to their mobile,
say). The reply goes to the wrong conversation.

This module gives the gateways a generic, channel-agnostic fix: when forwarding
an inbound message, mint an **opaque reply token** that captures the precise
origin address, and hand *that* to the agent instead of asking it to guess. To
reply, the agent passes ``--reply-to <token>`` to the channel's push CLI; the
gateway resolves the token back to the stored address and sends there. The agent
never sees or types an address, and — crucially — the token fixes only the
*destination*: it flows through the very same ``/send`` path as any other push,
so the ``*_SEND_POLICY`` / ``/sends`` approval gate is untouched. A token cannot
be used to bypass verification, only to address a reply correctly.

The token is a server-generated ``secrets.token_urlsafe`` string — unguessable,
so possession of a valid token is itself the authorization to address that
conversation (the ``/send`` endpoint is already bearer-token-gated on top). The
resolved recipient string is whatever the owning gateway's own ``_push`` /
``_tg_send`` / ``_signal_send`` already accepts as a ``recipient``, so no
gateway-specific address parsing lives here — each gateway stores the address in
its own native form and gets it back verbatim.

Entries are persisted one-file-per-token under a caller-supplied directory (on
the gateway's persistent data volume) so a token stays resolvable across a
service restart. They are pruned lazily: a token older than ``max_age_seconds``
(default 30 days) is ignored and swept. This is a convenience-addressing store,
not a security boundary — an expired token simply means "reply the normal way".
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from pathlib import Path

# Token id: url-safe base64 of 24 random bytes → 32 chars, no path separators.
_TOKEN_NBYTES = 24
# Default lifetime of a reply token. Long enough that a message the user gets to
# a day or two later is still directly replyable; short enough that the store
# does not grow without bound. Overridable per-store.
DEFAULT_MAX_AGE_SECONDS = 30 * 24 * 3600


class ReplyTokenStore:
    """A persistent, self-pruning map of opaque tokens → reply metadata.

    Thread-safe: the gateways call ``mint`` from their receive loop and
    ``resolve`` from the HTTP ``/send`` handler thread concurrently.
    """

    def __init__(self, directory: str | Path, *,
                 max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._max_age = int(max_age_seconds)
        self._lock = threading.Lock()
        # In-memory cache mirrors the on-disk files; the disk copy is the source
        # of truth across restarts, so a cache miss falls back to a disk read.
        self._cache: dict[str, dict] = {}

    # -- internals ------------------------------------------------------------

    def _path(self, token: str) -> Path:
        return self._dir / f"{token}.json"

    @staticmethod
    def _valid_token(token: str) -> bool:
        # Server-minted tokens are url-safe base64 (letters, digits, - and _).
        # Reject anything else up front so a crafted value cannot escape the
        # store directory via the filename.
        return bool(token) and all(
            c.isalnum() or c in "-_" for c in token
        ) and len(token) <= 128

    def _expired(self, entry: dict) -> bool:
        created = int(entry.get("created", 0))
        return (time.time() - created) > self._max_age

    # -- public API -----------------------------------------------------------

    def mint(self, recipient: str, *, channel: str,
             meta: dict | None = None) -> str:
        """Store ``recipient`` (the gateway's own native address form) and
        return a fresh opaque token addressing it.

        ``channel`` and ``meta`` are recorded for observability/debugging only;
        resolution keys purely on the token.
        """
        token = secrets.token_urlsafe(_TOKEN_NBYTES)
        entry = {
            "token": token,
            "recipient": recipient,
            "channel": channel,
            "meta": meta or {},
            "created": int(time.time()),
        }
        with self._lock:
            self._cache[token] = entry
            try:
                self._path(token).write_text(
                    json.dumps(entry, ensure_ascii=False), encoding="utf-8"
                )
            except OSError:
                # A store write failure must not break message forwarding: the
                # token still works for the life of this process via the cache,
                # and the fallback (reply the normal way) remains available.
                pass
        return token

    def resolve(self, token: str) -> str | None:
        """Return the stored recipient for ``token``, or ``None`` if the token
        is unknown, malformed, or expired."""
        if not self._valid_token(token):
            return None
        with self._lock:
            entry = self._cache.get(token)
            if entry is None:
                path = self._path(token)
                if path.is_file():
                    try:
                        entry = json.loads(path.read_text(encoding="utf-8"))
                        self._cache[token] = entry
                    except (OSError, ValueError):
                        entry = None
            if entry is None:
                return None
            if self._expired(entry):
                self._forget_locked(token)
                return None
            recipient = entry.get("recipient")
            return recipient or None

    def _forget_locked(self, token: str) -> None:
        self._cache.pop(token, None)
        try:
            self._path(token).unlink(missing_ok=True)
        except OSError:
            pass

    def sweep(self) -> int:
        """Delete all expired token files. Returns the number removed. Cheap to
        call opportunistically (e.g. once per receive batch)."""
        removed = 0
        with self._lock:
            for path in list(self._dir.glob("*.json")):
                try:
                    entry = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if self._expired(entry):
                    self._cache.pop(entry.get("token", ""), None)
                    try:
                        path.unlink(missing_ok=True)
                        removed += 1
                    except OSError:
                        pass
        return removed
