#!/usr/bin/env python3
"""Mitmproxy addon that writes every HTTP(S) flow to a daily NDJSON log.

Runs inside the egress-audit sidecar. Logs are written to
EGRESS_AUDIT_LOG_DIR (default /var/log/retinue/egress) as one file per UTC
day named YYYY-MM-DD.ndjson.

Authorization-like headers are redacted and bodies are truncated to
EGRESS_AUDIT_BODY_LIMIT bytes so the log stays small enough to review.
"""
import json
import os
import time
from pathlib import Path

from mitmproxy import http


LOG_DIR = Path(os.environ.get("EGRESS_AUDIT_LOG_DIR", "/var/log/retinue/egress"))
BODY_LIMIT = int(os.environ.get("EGRESS_AUDIT_BODY_LIMIT", "65536"))
SENSITIVE_HEADERS = {
    "authorization",
    "proxy-authorization",
    "x-conversation-backend-token",
    "cookie",
    "set-cookie",
}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _truncate(body: bytes | None, limit: int = BODY_LIMIT) -> dict:
    if body is None:
        return {"present": False, "truncated": False, "size": 0, "text": ""}
    size = len(body)
    if size == 0:
        return {"present": True, "truncated": False, "size": 0, "text": ""}
    sample = body[:limit]
    try:
        text = sample.decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover - defensive
        text = sample.decode("latin-1", errors="replace")
    return {"present": True, "truncated": size > limit, "size": size, "text": text}


def _headers(headers: http.Headers) -> dict:
    result: dict[str, list[str]] = {}
    for name, value in headers.fields:
        key = name.decode("utf-8", errors="replace").lower()
        val = value.decode("utf-8", errors="replace")
        if key in SENSITIVE_HEADERS:
            val = "[redacted]"
        result.setdefault(key, []).append(val)
    # collapse single-value lists to scalars for readability
    return {k: v[0] if len(v) == 1 else v for k, v in result.items()}


class EgressAuditAddon:
    def response(self, flow: http.HTTPFlow) -> None:
        req = flow.request
        resp = flow.response
        if resp is None:
            return

        started = getattr(flow, "timestamp_start", None)
        ended = getattr(flow, "timestamp_end", None) or time.time()
        duration_ms = int((ended - started) * 1000) if started else None

        path_only, _, query = req.path.partition("?")
        entry = {
            "ts": _now(),
            "client_ip": flow.client_conn.peername[0] if flow.client_conn.peername else None,
            "method": req.method,
            "scheme": req.scheme,
            "host": req.host,
            "port": req.port,
            "path": path_only,
            "query": query or None,
            "http_version": req.http_version,
            "request_headers": _headers(req.headers),
            "request_body": _truncate(req.content),
            "response_status": resp.status_code,
            "response_reason": resp.reason,
            "response_headers": _headers(resp.headers),
            "response_body": _truncate(resp.content),
            "duration_ms": duration_ms,
        }

        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_file = LOG_DIR / f"{time.strftime('%Y-%m-%d')}.ndjson"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


addons = [EgressAuditAddon()]
