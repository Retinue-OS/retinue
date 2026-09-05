#!/usr/bin/env python3
"""Posting to the dashboard-conversation backend from monitor daemons.

The one notification channel that reaches the user without spending Claude
credits and without a working Claude login: the web gateway's token-gated
``/internal/conversations`` endpoint (the same one conversation-push.py uses),
which fans every agent→user turn out as a Web Push notification.

Shared by gateway-monitor.py and claude-auth-monitor.py so the two daemons
cannot drift; callers construct it with defaults read from the environment
(CONVERSATION_BACKEND_URL / CONVERSATION_BACKEND_TOKEN, exported by the
entrypoint to every forked service).
"""
from __future__ import annotations

import json
import os
import urllib.request

_PORT = os.environ.get("WEB_GATEWAY_PORT", "8080")
DEFAULT_BACKEND_URL = os.environ.get(
    "CONVERSATION_BACKEND_URL", f"http://localhost:{_PORT}/internal/conversations"
).rstrip("/")
DEFAULT_BACKEND_TOKEN = os.environ.get("CONVERSATION_BACKEND_TOKEN", "").strip()


class ConversationNotifier:
    """Posts to the dashboard-conversation backend (Web Push fans out there)."""

    def __init__(self, base_url: str = DEFAULT_BACKEND_URL,
                 token: str = DEFAULT_BACKEND_TOKEN, timeout: float = 30.0,
                 log_prefix: str = "[monitor]"):
        self.base_url = base_url
        self.token = token
        self.timeout = timeout
        self.log_prefix = log_prefix

    def _post(self, url: str, payload: dict) -> dict | None:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={
            "Content-Type": "application/json",
            "X-Conversation-Backend-Token": self.token,
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - callers retry on the next tick
            print(f"{self.log_prefix} conversation post failed: {exc}", flush=True)
            return None

    def open_thread(self, title: str, message: str,
                    attention: dict | None = None) -> str | None:
        """Open a thread; ``attention`` declares how much it matters to the
        dashboard's attention model (importance, sphere, kind, critical — see
        conversation-push.py). Monitors are system alerts: without a
        declaration the model would list the thread and never push it."""
        payload = {"title": title, "message": message}
        if attention:
            payload["attention"] = dict(attention)
        body = self._post(self.base_url, payload)
        return (body or {}).get("id")

    def append(self, thread_id: str, message: str,
               attention: dict | None = None) -> bool:
        payload = {"message": message}
        if attention:
            payload["attention"] = dict(attention)
        body = self._post(f"{self.base_url}/{thread_id}/messages", payload)
        return body is not None
