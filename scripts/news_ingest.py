#!/usr/bin/env python3
"""Forward a broadcast-style messenger message onto the news rail.

A group can be flagged ``news`` in the triage policy (see ``triage_policy``): its
messages are references worth keeping in the news feed, independent of whether
any of them is worth a triage turn. The messenger gateways run in their own
containers and cannot touch ``NEWS_DIR`` (the web-gateway owns it), so a
news-flagged message is handed to the web-gateway's ``POST /internal/news``,
which shapes it into a news item and files it via ``news_store``. The Herald
scores it on the next curation tick. That endpoint is open unless the deployment
sets ``NEWS_INGEST_TOKEN`` on both sides.

This is the deterministic, credit-free half of the news pipeline — it spends no
model turn and runs immediately on arrival, in parallel to the agent-driven
``news-add.py`` path a triage turn can still take.

``NEWS_INGEST_URL`` defaults to the in-network web-gateway address in the base
compose file, so the rail works with no deployment configuration; with it
explicitly emptied the forward is a no-op that returns ``False``. Stdlib-only
(urllib), so every gateway can import it regardless of what else is on its
container.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

NEWS_INGEST_URL = os.environ.get("NEWS_INGEST_URL", "").strip()
# Optional shared secret. The endpoint is open when neither side sets one (see
# _news_ingest_authorized in web-gateway.py); a deployment that wants it locked
# down sets NEWS_INGEST_TOKEN here and on the web-gateway. CONVERSATION_BACKEND_TOKEN
# stays accepted as a fallback so a deployment wired before NEWS_INGEST_TOKEN
# existed keeps working.
NEWS_INGEST_TOKEN = (os.environ.get("NEWS_INGEST_TOKEN")
                     or os.environ.get("CONVERSATION_BACKEND_TOKEN", "")).strip()


def news_enabled() -> bool:
    """True when a news-ingest endpoint is configured for this gateway."""
    return bool(NEWS_INGEST_URL)


def forward_news(
    *,
    channel: str,
    source: str,
    text: str,
    url: str | None = None,
    lang: str | None = None,
    group: str | None = None,
    timeout: float = 8.0,
) -> bool:
    """Best-effort hand-off of one news-flagged message to the news feed.

    Returns ``True`` when the web-gateway accepted the item, ``False`` on any
    miss (endpoint unset, network error, non-2xx) — never raises, so a failing
    news rail can never break the gateway's inbound handling.
    """
    if not NEWS_INGEST_URL:
        return False
    payload = {
        "channel": channel,
        "source": source,
        "text": text,
        "url": url or "",
        "lang": lang or None,
        "group": group or None,
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        NEWS_INGEST_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Conversation-Backend-Token": NEWS_INGEST_TOKEN,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception as exc:  # noqa: BLE001 — best-effort, must never propagate
        print(f"[news_ingest] forward failed ({exc})", file=sys.stderr, flush=True)
        return False
