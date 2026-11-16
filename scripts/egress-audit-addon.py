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

TLS passthrough: EGRESS_AUDIT_TLS_PASSTHROUGH lists hosts whose TLS
connections are forwarded unopened (comma-separated; `example.com` matches
that host only, `*.example.com` matches the domain and every subdomain). It
is for clients that pin their provider's certificate and refuse the audit CA,
such as end-to-end-encrypted storage tools. Empty by default. Each such
connection is still logged, once when it opens, with `"tls_passthrough":
true`, method `TUNNEL`, the client, host, port and SNI; path, headers and
bodies are not visible to the proxy and stay empty. The match is made on the
proxy's actual destination (the CONNECT target); an SNI, when sent, must be
listed as well, so a client cannot borrow a listed name to tunnel elsewhere.
"""
import json
import os
import re
import time
import urllib.parse
from pathlib import Path

from mitmproxy import http, tls


LOG_DIR = Path(os.environ.get("EGRESS_AUDIT_LOG_DIR", "/var/log/retinue/egress"))
BODY_LIMIT = int(os.environ.get("EGRESS_AUDIT_BODY_LIMIT", "65536"))
SENSITIVE_HEADERS = {
    "authorization",
    "proxy-authorization",
    "x-conversation-backend-token",
    "cookie",
    "set-cookie",
}
# The words that mark a name — header, JSON member, form or query field — as
# carrying a credential. Shared so headers and payload fields cannot drift.
_CREDENTIAL_WORDS = (
    r"token|secret|password|passwd|credential|signature"
    r"|(?:api|access|private|secret)[_-]?key|apikey"
)
# Any other header whose name says it carries a credential (x-api-key,
# x-access-key, x-private-key, x-auth-token, ...). Rate-limit headers that
# count tokens (anthropic-ratelimit-tokens-remaining) are not credentials and
# stay.
SENSITIVE_HEADER_RE = re.compile(r"^(?!.*ratelimit)(?:.*(?:" + _CREDENTIAL_WORDS + r"))", re.I)

REDACTED = "[redacted]"

# Field names (JSON members, form and query parameters) whose value is a
# credential. Matched case-insensitively against the whole name. Two kinds of
# name contain a credential word without carrying one, and are named
# explicitly rather than by pattern — a pattern broad enough to catch them
# (say, anything ending in "tokens") also lets refresh_tokens through:
# token_type and the token counters of LLM APIs (max_tokens, input_tokens,
# cache_read_input_tokens, ...).
_NOT_SENSITIVE = (
    r"[\w.-]*_type"
    r"|(?:[\w.-]*_)?(?:max|min|total|input|output|prompt|completion|budget"
    r"|cached|reasoning)_tokens"
)
_SENSITIVE_NAME = (
    r"(?!(?:" + _NOT_SENSITIVE + r")(?![\w.-]))"
    r"(?:[\w.-]*(?:" + _CREDENTIAL_WORDS + r")[\w.-]*"
    r"|code|code_verifier|assertion|client_assertion|otp|pin|session|session_?id|sig)"
)
_SENSITIVE_NAME_RE = re.compile(r"(?:" + _SENSITIVE_NAME + r")\Z", re.I)
# "name": value — a JSON string (escapes included) or a number, since an OTP
# or PIN is often sent as one. An unclosed string (body cut off by the size
# limit) is redacted to its end.
_JSON_FIELD_RE = re.compile(
    r'("(?:' + _SENSITIVE_NAME + r')"\s*:\s*)'
    r'(?:"(?:[^"\\]|\\.)*(?:"|$)|-?\d[\d.eE+-]*)', re.I)
# name=value in a query string or form body. The name is matched after
# percent-decoding (access%5Ftoken is access_token), the text keeps its
# original spelling.
_FORM_FIELD_RE = re.compile(r"(^|[?&;\s])([^=?&;\s]+=)([^&;\s]*)")
# Credential-shaped values, wherever they appear.
_VALUE_RES = (
    re.compile(r"sk-ant-[A-Za-z0-9_-]+"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"),
    re.compile(r"(\bBearer\s+)[A-Za-z0-9._~+/=-]+", re.I),
    # Telegram bot token, which the Bot API puts in the URL path.
    re.compile(r"(?<!\d)\d{6,}:[A-Za-z0-9_-]{30,}"),
)


def _redact_form_field(m: re.Match) -> str:
    name = urllib.parse.unquote_plus(m.group(2)[:-1])
    if _SENSITIVE_NAME_RE.match(name):
        return m.group(1) + m.group(2) + REDACTED
    return m.group(0)


def _redact_raw(text: str) -> str:
    text = _JSON_FIELD_RE.sub(lambda m: m.group(1) + f'"{REDACTED}"', text)
    text = _FORM_FIELD_RE.sub(_redact_form_field, text)
    for pattern in _VALUE_RES:
        text = pattern.sub(
            lambda m: (m.group(1) if pattern.groups else "") + REDACTED, text)
    return text


def _redact_text(text: str) -> str:
    """Return `text` with every credential value replaced by REDACTED.

    Percent-encoding can hide a credential's shape (a Telegram token with its
    colon written %3A). So the decoded text is checked as well, and when it
    still holds something to redact, the decoded, redacted text is what gets
    logged: the original spelling is given up only where keeping it would
    keep a credential."""
    text = _redact_raw(text)
    if "%" in text:
        decoded = urllib.parse.unquote(text)
        redacted = _redact_raw(decoded)
        if redacted != decoded:
            return redacted
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


def _parse_passthrough(spec: str) -> tuple[frozenset, tuple]:
    """Split the passthrough list into exact hosts and wildcard domains."""
    exact: set[str] = set()
    domains: list[str] = []
    for item in spec.split(","):
        item = item.strip().lower().rstrip(".")
        if not item:
            continue
        if item.startswith("*."):
            domains.append(item[2:])
        else:
            exact.add(item)
    return frozenset(exact), tuple(domains)


PASSTHROUGH_EXACT, PASSTHROUGH_DOMAINS = _parse_passthrough(
    os.environ.get("EGRESS_AUDIT_TLS_PASSTHROUGH", "")
)


def is_passthrough(host: str | None) -> bool:
    """True when TLS to `host` is forwarded without interception."""
    if not host:
        return False
    host = host.lower().rstrip(".")
    if host in PASSTHROUGH_EXACT:
        return True
    return any(host == d or host.endswith("." + d) for d in PASSTHROUGH_DOMAINS)


def _write(entry: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"{time.strftime('%Y-%m-%d')}.ndjson"
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def _empty_body() -> dict:
    return {"present": False, "truncated": False, "size": 0, "text": ""}


class EgressAuditAddon:
    def tls_clienthello(self, data: tls.ClientHelloData) -> None:
        """Forward a passthrough host's TLS stream unopened, and log that."""
        # The decision is bound to the proxy's real destination (the CONNECT
        # target), never to the client-chosen SNI alone: otherwise a client
        # could CONNECT anywhere with an allowlisted SNI and tunnel unopened,
        # logged under the wrong host. An SNI that is present must be listed
        # too; a mismatch is intercepted as usual.
        server = data.context.server
        target = server.address[0] if server.address else None
        sni = data.client_hello.sni
        if not is_passthrough(target):
            return
        if sni and not is_passthrough(sni):
            return
        data.ignore_connection = True
        client = data.context.client
        _write({
            "ts": _now(),
            "client_ip": client.peername[0] if client.peername else None,
            "method": "TUNNEL",
            "scheme": "https",
            "host": target,
            "port": server.address[1] if server.address else None,
            "path": "",
            "query": None,
            "sni": sni,
            "tls_passthrough": True,
            "request_headers": {},
            "request_body": _empty_body(),
            "response_status": 0,
            "response_reason": "",
            "response_headers": {},
            "response_body": _empty_body(),
            "duration_ms": None,
        })

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

        _write(entry)


addons = [EgressAuditAddon()]
