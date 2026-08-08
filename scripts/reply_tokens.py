"""Shared reply-token minting for the messaging gateways.

All three linked-device gateways (WhatsApp, Signal, Telegram) share the same
structural defect when they forward an inbound *inbox* message to Ara's triage:
the prompt carries only a human-readable ``sender_label`` (a name, maybe a bare
number), never the exact **origin address** of the conversation the message
arrived in. The gateway knows that address — the WhatsApp chat JID, the Signal
source number/UUID or group id, the Telegram ``chat_id`` — but drops it on the
floor.

So when the user then says "reply", the agent has to reconstruct the address by
resolving the *name*, and name resolution can land on the wrong account entirely
(a correspondent who wrote from an office number whose name maps to their mobile,
say). The reply goes to the wrong conversation.

This module gives the gateways a generic, channel-agnostic fix: when forwarding
an inbound message, mint an opaque reply token that captures the precise origin
address, and hand *that* to the agent instead of asking it to guess. To reply,
the agent passes ``--reply-to <token>`` to the channel's push CLI; the gateway
resolves the token back to the address and sends there. The agent never sees or
types an address, and — crucially — the token fixes only the *destination*: it
flows through the very same ``/send`` path as any other push, so the
``*_SEND_POLICY`` / ``/sends`` approval gate is untouched. A token cannot be
used to bypass verification, only to address a reply correctly.

**The token is self-contained (stateless).** The origin address travels *inside*
the token, authenticated by an HMAC-SHA256 signature over the payload. Resolving
a token means verifying its signature and reading the address back out — there
is **no per-token storage, no lifetime, and nothing to prune**. This is the
property the earlier design lacked: it kept one file per minted token and swept
them on an age cutoff, which meant a token could silently expire and the store
grew with traffic. A signed self-describing token removes both problems at once.

Why a signature rather than a bare base64 of the address? Two reasons, neither a
new security boundary (arbitrary addressing is already possible behind the same
bearer-gated ``/send`` via ``--recipient``; this token is a *convenience*, not a
gate): it keeps the token **opaque**, so the agent handles an address it can
neither read nor mistype; and it makes the token **tamper-evident**, so a
corrupted or hand-edited token resolves to ``None`` (a hard 400 at the gateway)
rather than to some unintended address. The HMAC uses only the Python standard
library (``hmac`` + ``hashlib``) — no third-party crypto, which matters because
the gateway containers ship no ``cryptography`` package.

The one remaining piece of state is a **single signing key per gateway**, not
per token: it is generated once and persisted to one file on the gateway's data
volume so tokens stay resolvable across a restart, and it never grows. Rotating
or deleting that key invalidates all outstanding tokens at once — the only
"expiry" in the design, and an explicit operator action rather than a timer. A
key may also be supplied out-of-band via the ``secret=`` constructor argument or
the ``REPLY_TOKEN_KEY`` environment variable (which, if set, makes all three
gateways share one key — otherwise each gateway persists its own).

The recipient string is stored and returned verbatim — whatever the owning
gateway's own ``_push`` / ``_tg_send`` / ``_signal_send`` already accepts as a
``recipient`` (WhatsApp JID, Signal number/UUID or ``group:<id>``, Telegram
``chat_id``). No gateway-specific address parsing lives here.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

# Token wire format: "<version>.<payload_b64>.<sig_b64>", all url-safe base64
# without padding, so the whole token is shell- and CLI-argument-safe
# (characters limited to [A-Za-z0-9_-] plus the two '.' separators).
_TOKEN_VERSION = "v1"
# 32-byte HMAC-SHA256 signing key. Long enough that a token cannot be forged
# without the key; generated with a CSPRNG.
_KEY_NBYTES = 32
_KEY_FILENAME = ".signing-key"


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


class ReplyTokenStore:
    """Mints and resolves self-contained, signed reply tokens.

    Despite the name (kept for drop-in compatibility with the gateways), this
    holds **no per-token state**: a token carries its own recipient address, and
    ``resolve`` reconstructs it by verifying the signature. The only persisted
    state is a single signing key.

    Thread-safe: ``mint`` (called from a gateway's receive loop) and ``resolve``
    (called from the HTTP ``/send`` handler thread) share no mutable state — the
    key is read once at construction and never mutated — so no lock is needed.
    """

    def __init__(self, directory: str | Path, *,
                 secret: str | bytes | None = None,
                 max_age_seconds: int | None = None) -> None:
        self._dir = Path(directory)
        # ``max_age_seconds`` is accepted for interface compatibility and for
        # deployments that *want* an age cap; the default is None → tokens do
        # not expire. The created-at timestamp travels signed inside the token,
        # so any age check is still stateless.
        self._max_age = int(max_age_seconds) if max_age_seconds else None
        self._key = self._load_or_create_key(secret)

    # -- key management -------------------------------------------------------

    def _load_or_create_key(self, secret: str | bytes | None) -> bytes:
        """Resolve the signing key: an explicit ``secret`` wins, else a
        persisted key file, else a freshly generated key persisted for reuse."""
        if secret:
            return secret.encode("utf-8") if isinstance(secret, str) else secret
        env = os.environ.get("REPLY_TOKEN_KEY")
        if env:
            return env.encode("utf-8")
        key_path = self._dir / _KEY_FILENAME
        try:
            if key_path.is_file():
                data = key_path.read_bytes().strip()
                if data:
                    return data
        except OSError:
            pass
        # Generate and persist. If the volume is unwritable we still return a
        # usable in-process key — tokens then simply do not survive a restart,
        # which degrades to "reply the normal way", never to a wrong address.
        key = secrets.token_bytes(_KEY_NBYTES)
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            key_path.write_bytes(key)
            os.chmod(key_path, 0o600)
        except OSError:
            pass
        return key

    # -- internals ------------------------------------------------------------

    def _sign(self, signing_input: bytes) -> str:
        return _b64e(hmac.new(self._key, signing_input, hashlib.sha256).digest())

    # -- public API -----------------------------------------------------------

    def mint(self, recipient: str, *, channel: str,
             meta: dict | None = None) -> str:
        """Return a fresh opaque token that encodes ``recipient`` (the gateway's
        own native address form), authenticated by HMAC.

        ``channel`` is embedded for observability/debugging; ``meta`` is accepted
        for call-site compatibility but not embedded — resolution needs only the
        recipient, and keeping the token small keeps it a comfortable CLI arg.
        """
        payload = {"r": recipient, "c": channel, "t": int(time.time())}
        payload_b64 = _b64e(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            .encode("utf-8")
        )
        signing_input = f"{_TOKEN_VERSION}.{payload_b64}".encode("ascii")
        return f"{_TOKEN_VERSION}.{payload_b64}.{self._sign(signing_input)}"

    def resolve(self, token: str) -> str | None:
        """Return the recipient encoded in ``token``, or ``None`` if the token is
        malformed, has an invalid signature, or (when an age cap is configured)
        is older than ``max_age_seconds``. Never raises."""
        if not token:
            return None
        try:
            version, payload_b64, sig = token.split(".")
        except ValueError:
            return None
        if version != _TOKEN_VERSION:
            return None
        signing_input = f"{version}.{payload_b64}".encode("ascii")
        expected = self._sign(signing_input)
        # Constant-time comparison: a signed token is the authorization to
        # address that conversation, so do not leak validity via timing.
        if not hmac.compare_digest(sig, expected):
            return None
        try:
            payload = json.loads(_b64d(payload_b64).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        if self._max_age is not None:
            created = int(payload.get("t", 0))
            if (time.time() - created) > self._max_age:
                return None
        recipient = payload.get("r")
        return recipient or None
