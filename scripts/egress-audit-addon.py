#!/usr/bin/env python3
"""Mitmproxy addon that writes every HTTP(S) flow to a daily NDJSON log.

Runs inside the egress-audit sidecar. Logs are written to
EGRESS_AUDIT_LOG_DIR (default /var/log/retinue/egress) as one file per UTC
day named YYYY-MM-DD.ndjson.

Bodies are truncated to EGRESS_AUDIT_BODY_LIMIT bytes so the log stays
small enough to review.

Credentials are redacted before anything is written — not only in headers
but in query strings and bodies too. The log is read by the egress log viewer
and the anomaly agent, so a credential that lands in it is a credential
handed to them. That is not hypothetical: Claude Code refreshes its sign-in
by POSTing to an OAuth token endpoint through this proxy, and the response
carries a fresh access and refresh token in its body. Redaction works on the
text, not on a parsed document, so it also covers a body that the size limit
cut off mid-JSON:

- a header whose name says it carries a credential;
- a JSON member (`"name": "value"`) or form/query pair (`name=value`) whose
  name says it carries a credential (`access_token`, `client_secret`,
  `password`, `code_verifier`, ...);
- a value that looks like a credential wherever it sits, the URL path
  included (Anthropic keys, JWTs, `Bearer ...`, Telegram bot tokens), as a
  backstop for names nobody listed.

Redaction keeps the name and replaces only the value, so the log still shows
*that* a credential travelled, and where.
"""
import json
import os
import re
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
# Any other header whose name says it carries a credential (x-api-key,
# x-goog-api-key, x-auth-token, ...). Rate-limit headers that count tokens
# (anthropic-ratelimit-tokens-remaining) are not credentials and stay.
SENSITIVE_HEADER_RE = re.compile(r"token(?!s)|secret|api-?key|password|credential|signature", re.I)

REDACTED = "[redacted]"

# Field names (JSON members, form and query parameters) whose value is a
# credential. Matched case-insensitively against the whole name. Names that
# merely describe a credential or count tokens (token_type, max_tokens,
# input_tokens) are excluded: their values are not secrets, and keeping them
# keeps the log readable.
_SENSITIVE_NAME = (
    r"(?![\w.-]*(?:_type|tokens)(?![\w.-]))"
    r"(?:[\w.-]*(?:token|secret|password|passwd|api[_-]?key|apikey|credential|private[_-]?key)[\w.-]*"
    r"|code|code_verifier|assertion|client_assertion|otp|pin|session|session_?id|sig|signature)"
)
# "name": "value" — the value is a JSON string, escapes included. An unclosed
# string (body cut off by the size limit) is redacted to its end.
_JSON_FIELD_RE = re.compile(
    r'("(?:' + _SENSITIVE_NAME + r')"\s*:\s*)"(?:[^"\\]|\\.)*(?:"|$)', re.I)
# name=value in a query string or form body.
_FORM_FIELD_RE = re.compile(r"(^|[?&;\s])((?:" + _SENSITIVE_NAME + r")=)[^&;\s]*", re.I)
# Credential-shaped values, wherever they appear.
_VALUE_RES = (
    re.compile(r"sk-ant-[A-Za-z0-9_-]+"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"),
    re.compile(r"(\bBearer\s+)[A-Za-z0-9._~+/=-]+", re.I),
    # Telegram bot token, which the Bot API puts in the URL path.
    re.compile(r"(?<!\d)\d{6,}:[A-Za-z0-9_-]{30,}"),
)


def _redact_text(text: str) -> str:
    """Return `text` with every credential value replaced by REDACTED."""
    text = _JSON_FIELD_RE.sub(lambda m: m.group(1) + f'"{REDACTED}"', text)
    text = _FORM_FIELD_RE.sub(lambda m: m.group(1) + m.group(2) + REDACTED, text)
    for pattern in _VALUE_RES:
        text = pattern.sub(
            lambda m: (m.group(1) if pattern.groups else "") + REDACTED, text)
    return text


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
    return {"present": True, "truncated": size > limit, "size": size, "text": _redact_text(text)}


def _headers(headers: http.Headers) -> dict:
    result: dict[str, list[str]] = {}
    for name, value in headers.fields:
        key = name.decode("utf-8", errors="replace").lower()
        val = value.decode("utf-8", errors="replace")
        if key in SENSITIVE_HEADERS or SENSITIVE_HEADER_RE.search(key):
            val = REDACTED
        else:
            val = _redact_text(val)
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
            "path": _redact_text(path_only),
            "query": _redact_text(query) if query else None,
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
