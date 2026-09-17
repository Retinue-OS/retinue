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

An event asserts *which account* sent it and nothing about where that account
lives: a gateway's address is configured on the reader's side (the
web-gateway's messenger registry) and is authoritative there. A gateway that
also declared its own address would be a second source of truth free to drift
from the first — which is exactly what once attributed one account's chats to
another.

Fire-and-forget by contract *for the classes the gate holds back*:
:func:`notify_chat_event_async` runs the POST on a daemon thread with a short
timeout, never raises, and never blocks the gateway's own hot path. A lost rail
event costs a notification and a few seconds of freshness, never a message —
the ledger already holds it and the store catches up on its own.

A message the gate **forwards** is different. The web-gateway answers that one
with a job handle for the companion turn it started in the message's own chat
(docs/messenger-chats.md, phase 4), and the gateway needs that handle to learn
whether the message was ever accounted for. So the forward class calls
:func:`notify_chat_event` directly and reads its answer: no handle means the
rail is switched off, unreachable, or could not take the message, and the
caller falls back to the triage forward it has always done.

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


def notify_chat_event(
    *,
    direction: str,
    channel: str,
    chat: str,
    account: str | None = None,
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
    files: list[dict] | None = None,
    handover: bool = False,
    timeout: float = 3.0,
) -> dict | None:
    """Synchronous rail POST; returns the answer body, or None if it failed.

    An accepted event answers with a JSON object. ``{"accepted": true}`` means
    the chat has the message and the caller must not forward it anywhere else —
    the user sees it in the conversation it belongs to, and that is the
    delivery. ``job_url`` comes with it when a companion turn was started too,
    because that one is not finished yet and the caller waits for the job
    before flipping ``delivered``; a plain ``accepted`` is already final.
    Neither key means the rail took nothing, which is the caller's cue to fall
    back. None means the event did not land at all — no endpoint configured, a
    refusal, a connection that never got there.

    ``{"uncertain": True}`` is the third answer, and the one that matters for a
    forwarded message: the request timed out twice, so whether the event landed
    is **unknown**. The handler may well have accepted it and started a turn
    before the answer was lost, and treating that as "did not land" would have
    the caller forward the same message to triage as well — two turns racing
    over one draft, and a dashboard conversation the switch exists to stop. The
    caller must neither confirm delivery nor fall back on it: leaving the
    message undelivered hands it to the daily drain, which is where an unknown
    outcome belongs.

    ``direction`` is ``in`` for an arrival, ``out`` for an outbound echo (the
    user's own send from another device). ``account`` is this gateway's own
    ``*_ACCOUNT`` — how the web-gateway identifies which registry gateway sent
    the event, matched against the accounts the gateways it already knows
    report for themselves. ``gate`` carries the delivery-gate
    verdict for inbound events (``{"forward": bool, "reason": str}``) so the
    web-gateway can keep held/no-action classes silent. It no longer decides
    whether a turn runs: that is the chat's own ``assist`` flag, which the user
    sets per correspondent. ``files``
    are the message's attachments in the ``POST /message`` shape
    (``{"filename", "content_type", "data"}``, base64) so a turn started there
    can open them; they ride along only for a forwarded message.

    ``handover`` is the caller's offer: *if you take this message, I will not
    forward it to triage myself.* Only a call that makes that offer is accepted
    or can buy a companion turn, and that is deliberate — a gateway built before this
    contract existed fires the rail and forwards to triage regardless, so
    starting a turn on its event would have the message handled twice. During
    a rollout where the web-gateway is rebuilt before the gateways, the absent
    offer is what keeps the old behaviour whole instead of doubling it.
    Never raises.
    """
    if not CHATS_INGEST_URL:
        return None
    payload = {
        "direction": direction,
        "channel": channel,
        "chat": chat,
        # The sending gateway's own account identity — the routing key, and
        # the only identity a gateway asserts about itself. Where that account
        # can be reached is the reader's business: the address lives in the
        # web-gateway's messenger registry, which is authoritative.
        "account": account or None,
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
        "files": files or None,
        "handover": True if handover else None,
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
    # One retry, and only for a timeout. The web-gateway mints one handle per
    # message id and hands the same one back for a repeat, so re-sending an
    # event that may already have been accepted cannot start a second turn —
    # which is what makes retrying the right move rather than a risk.
    raw = None
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if not 200 <= resp.status < 300:
                    return None
                raw = resp.read().decode("utf-8", errors="replace")
            break
        except Exception as exc:  # noqa: BLE001 — best-effort, never propagates
            reason = getattr(exc, "reason", None)
            timed_out = isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError)
            if not timed_out:
                print(f"[chat_ingest] notify failed ({exc})", file=sys.stderr,
                      flush=True)
                return None
            if attempt == 1:
                print(f"[chat_ingest] notify timed out ({exc}); retrying once",
                      file=sys.stderr, flush=True)
                continue
            print(f"[chat_ingest] notify timed out twice ({exc}); whether it "
                  "landed is unknown", file=sys.stderr, flush=True)
            return {"uncertain": True}
    try:
        body = json.loads(raw) if raw.strip() else {}
    except ValueError:
        body = {}
    # An accepted event whose body is not an object is still accepted; it just
    # carries no handle.
    return body if isinstance(body, dict) else {}


def notify_chat_event_async(**kwargs) -> None:
    """Fire the rail POST on a daemon thread and discard its answer.

    For the classes the gate holds back, which want the mirror updated and
    nothing more: the gateway's hot path — persist, gate, forward — is never
    delayed or reordered by it. A forwarded message calls the synchronous
    :func:`notify_chat_event` instead, because it needs the answer."""
    if not CHATS_INGEST_URL:
        return
    threading.Thread(
        target=notify_chat_event, kwargs=kwargs, name="chats-rail", daemon=True
    ).start()
