#!/usr/bin/env python3
"""Notify the web-gateway of one chat message event (the chats rail).

The messenger gateways persist every message into their own ledgers; the life
store indexes those within seconds. The two moments that cannot wait those
seconds are exactly the two this rail carries: an arrival that should light up
the chat surface (and Web-Push the user) *now*, and an own-device echo that
should advance the read watermark *now*. The gateway POSTs the event's
metadata to the web-gateway's ``POST /internal/chats/inbound``, which updates
the chat's state and the in-memory live overlay — the deterministic,
credit-free notification path (no model turn).

Fire-and-forget by contract: :func:`notify_chat_event_async` runs the POST on
a daemon thread with a short timeout, never raises, and never blocks or
reorders the gateway's own hot path (persist → gate → triage forward). A lost
rail event costs a notification and a few seconds of freshness, never a
message — the ledger already holds it and the store catches up on its own.

``CHATS_INGEST_URL`` defaults to the in-network web-gateway address in the
base compose file, so the rail works with no deployment configuration; with it
explicitly emptied every call is a no-op. The endpoint is open unless the
deployment sets ``CHATS_INGEST_TOKEN`` on both sides (the news-rail model —
see the web-gateway handler for why open-by-default is the fail-safe here).
Stdlib-only (urllib), like the other modules copied into the gateway images.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import urllib.parse
import urllib.request

CHATS_INGEST_URL = os.environ.get("CHATS_INGEST_URL", "").strip()
# Optional shared secret; CONVERSATION_BACKEND_TOKEN stays accepted as the
# fallback value so a deployment that only sets the generic backend token still
# authenticates when the web-gateway side opts into enforcement.
CHATS_INGEST_TOKEN = (os.environ.get("CHATS_INGEST_TOKEN")
                      or os.environ.get("CONVERSATION_BACKEND_TOKEN", "")).strip()


def chats_enabled() -> bool:
    """True when a chats-rail endpoint is configured for this gateway."""
    return bool(CHATS_INGEST_URL)


def gateway_slug(self_url: str) -> str:
    """This gateway's registry slug: the service hostname of its own base URL.

    The web-gateway keys its messenger-gateway registry by service hostname, so
    sending it with each event lets a chat remember which *account* it lives on
    — the difference between the system Signal number and the user's personal
    one — and route a later send back through that exact gateway."""
    return urllib.parse.urlsplit(self_url or "").hostname or ""


def notify_chat_event(
    *,
    direction: str,
    channel: str,
    chat: str,
    gateway: str | None = None,
    sender: str | None = None,
    sender_name: str | None = None,
    chat_name: str | None = None,
    group: bool = False,
    message_id: str | None = None,
    ts: float | None = None,
    text: str | None = None,
    attachments: list[str] | None = None,
    author: str | None = None,
    gate: dict | None = None,
    timeout: float = 3.0,
) -> bool:
    """Synchronous rail POST; returns True when the web-gateway accepted it.

    ``direction`` is ``in`` for an arrival, ``out`` for an outbound echo (the
    user's own send from another device). ``gate`` carries the delivery-gate
    verdict for inbound events (``{"forward": bool, "reason": str}``) so the
    web-gateway can keep held/no-action classes silent. Never raises.
    """
    if not CHATS_INGEST_URL:
        return False
    payload = {
        "direction": direction,
        "channel": channel,
        "chat": chat,
        "gateway": gateway or None,
        "sender": sender or None,
        "sender_name": sender_name or None,
        "chat_name": chat_name or None,
        "group": bool(group),
        "message_id": message_id or None,
        "ts": ts,
        "text": text or "",
        "attachments": [u for u in (attachments or []) if u],
        "author": author or None,
        "gate": gate or None,
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        CHATS_INGEST_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Conversation-Backend-Token": CHATS_INGEST_TOKEN,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception as exc:  # noqa: BLE001 — best-effort, must never propagate
        print(f"[chat_ingest] notify failed ({exc})", file=sys.stderr, flush=True)
        return False


def notify_chat_event_async(**kwargs) -> None:
    """Fire the rail POST on a daemon thread so the gateway hot path — persist,
    gate, triage forward — is never delayed or reordered by it."""
    if not CHATS_INGEST_URL:
        return
    threading.Thread(
        target=notify_chat_event, kwargs=kwargs, name="chats-rail", daemon=True
    ).start()
