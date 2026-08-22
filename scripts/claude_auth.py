#!/usr/bin/env python3
"""Claude Code OAuth credentials: status, browser re-login, shared constants.

Claude Code authenticates the whole system (the remote-control session, every
scheduled ``claude -p`` job, dashboard conversation turns) through one OAuth
credential file. When that sign-in dies — the refresh token reaches its expiry,
or a concurrent session rotates the tokens and the stale side clears the file —
every agent in the deployment silently stops working until someone SSHes into
the host and re-runs ``claude`` in an interactive container. This module makes
that recoverable (and observable) without a console:

  * ``credential_status()`` classifies the stored credentials — including the
    entrypoint's backup/rejected-marker protocol — into one line of truth the
    dashboard page and the auth monitor both render.
  * ``new_login_attempt()`` / ``exchange_code()`` / ``install_tokens()``
    implement the same authorization-code + PKCE flow the Claude Code CLI
    itself performs on ``/login``: the user opens the authorize URL (on any
    device), approves, and pastes the displayed ``code#state`` back; the token
    exchange then writes a fresh ``.credentials.json``.

Endpoint constants, the credential-file schema (``claudeAiOauth`` with
``accessToken`` / ``refreshToken`` / ``expiresAt`` / ``refreshTokenExpiresAt``
/ ``scopes`` / ``subscriptionType`` / ``rateLimitTier`` / ``clientId``) and the
token-response fields (``expires_in``, ``refresh_token_expires_in``, ``scope``)
were verified against Claude Code 2.1.240. They are not a published API, so
every value is env-overridable — if Anthropic moves an endpoint, the fix is a
variable in the deployment override, not a code change.

The file paths and the ``.restored-expiry`` marker protocol are shared with the
entrypoint's credential watcher (scripts/entrypoint.sh): the watcher restores a
cleared file from ``.credentials.json.bak`` and records the backup's expiry in
the marker when it does; a marker matching the backup means those credentials
were already tried and rejected by the server — the state that previously
required the console login. ``install_tokens()`` clears the marker and renews
the backup, so a web re-login resets the whole protocol.

Also usable standalone (e.g. over SSH, without stopping the stack):

    python3 scripts/claude_auth.py status
    python3 scripts/claude_auth.py login
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ── Credential file locations (shared with scripts/entrypoint.sh) ─────────────

CRED_FILE = Path(os.environ.get("CLAUDE_CRED_FILE", "") or "/root/.claude/.credentials.json")
CRED_BAK = CRED_FILE.with_name(CRED_FILE.name + ".bak")
CRED_MARKER = CRED_FILE.with_name(CRED_FILE.name + ".restored-expiry")

# ── OAuth constants (verified against Claude Code 2.1.240; env-overridable) ───

CLIENT_ID = os.environ.get("CLAUDE_OAUTH_CLIENT_ID", "") or "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
# The claude.ai-account authorize endpoint — the subscription sign-in, which is
# what a remote-control deployment uses (the entrypoint unsets ANTHROPIC_API_KEY
# precisely to get this mode).
AUTHORIZE_URL = os.environ.get("CLAUDE_OAUTH_AUTHORIZE_URL", "") \
    or "https://claude.com/cai/oauth/authorize"
TOKEN_URL = os.environ.get("CLAUDE_OAUTH_TOKEN_URL", "") \
    or "https://platform.claude.com/v1/oauth/token"
# Manual-mode redirect: the callback page displays the code for copy/paste
# instead of hitting a localhost listener — the only mode that works when the
# browser is on the phone and the CLI is in a headless container.
REDIRECT_URI = os.environ.get("CLAUDE_OAUTH_REDIRECT_URI", "") \
    or "https://platform.claude.com/oauth/code/callback"
SCOPES = os.environ.get("CLAUDE_OAUTH_SCOPES", "") \
    or "user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload"

HTTP_TIMEOUT = float(os.environ.get("CLAUDE_OAUTH_HTTP_TIMEOUT", "") or "30")

# ── Status thresholds ─────────────────────────────────────────────────────────

# Warn this many days before the refresh token itself expires — the point at
# which the system *will* stop working and only a fresh sign-in helps.
WARN_DAYS = float(os.environ.get("CLAUDE_AUTH_WARN_DAYS", "") or "3")
# An access token whose expiry lies further than this in the past means the
# running sessions have not managed to refresh for that long — the refresh is
# most likely failing even though the file still holds tokens.
STALE_HOURS = float(os.environ.get("CLAUDE_AUTH_STALE_HOURS", "") or "24")

DAY_MS = 24 * 3600 * 1000


class ClaudeAuthError(Exception):
    """A login step failed in a way the user can act on (message is shown)."""


def now_ms() -> int:
    return int(time.time() * 1000)


def oauth_in_use() -> bool:
    """Whether this deployment authenticates Claude Code via OAuth at all.

    Mirrors the entrypoint's remote-control condition: a configured
    Claude-compatible gateway (ANTHROPIC_BASE_URL) replaces the OAuth login —
    unless RETINUE_GATEWAY_USES_CLAUDE_OAUTH declares the gateway itself relies
    on the subscription credentials.
    """
    if not os.environ.get("ANTHROPIC_BASE_URL", "").strip():
        return True
    return os.environ.get("RETINUE_GATEWAY_USES_CLAUDE_OAUTH", "").strip().lower() == "true"


# ── Reading and classifying stored credentials ────────────────────────────────

def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def oauth_block(data: dict | None) -> dict | None:
    """The ``claudeAiOauth`` section, or None when it holds no refresh token —
    the same validity test the entrypoint's ``_cred_has_token`` applies."""
    if not isinstance(data, dict):
        return None
    block = data.get("claudeAiOauth")
    if not isinstance(block, dict) or not block.get("refreshToken"):
        return None
    return block


def credential_status(now: int | None = None,
                      cred_file: Path | None = None,
                      warn_days: float | None = None,
                      stale_hours: float | None = None) -> dict:
    """Classify the stored credentials into one actionable state.

    States, in decreasing severity:
      needs_login — nothing recoverable is stored: no credentials anywhere,
                    the backup was already rejected by the server (marker
                    protocol), or the refresh token's own expiry has passed.
                    Only a fresh sign-in helps.
      expiring    — the refresh token expires within ``warn_days``: the system
                    still works, but will stop; re-login now, at leisure.
      stale       — something is off but may recover on its own: the live file
                    is cleared while a valid backup exists (the entrypoint
                    watcher restores those), or the access token has not been
                    refreshed for ``stale_hours`` past its expiry.
      ok          — a refresh token is stored and none of the above applies.

    Pure function over the files' content plus the clock, so it is equally
    valid in the web gateway, the monitor daemon, and tests.
    """
    now = now_ms() if now is None else now
    cred_file = CRED_FILE if cred_file is None else Path(cred_file)
    bak_file = cred_file.with_name(cred_file.name + ".bak")
    marker_file = cred_file.with_name(cred_file.name + ".restored-expiry")
    warn_days = WARN_DAYS if warn_days is None else warn_days
    stale_hours = STALE_HOURS if stale_hours is None else stale_hours

    cred = oauth_block(_read_json(cred_file))
    bak = oauth_block(_read_json(bak_file))
    try:
        marker = marker_file.read_text(encoding="utf-8").strip()
    except OSError:
        marker = ""
    backup_rejected = bool(bak) and marker != "" and str(bak.get("expiresAt", "")) == marker

    status = {
        "credentials_present": cred is not None,
        "backup_present": bak is not None,
        "backup_rejected": backup_rejected,
        "access_expires_at": None,
        "refresh_expires_at": None,
        "subscription": None,
        "rate_limit_tier": None,
        "scopes": [],
    }

    effective = cred or (bak if not backup_rejected else None)
    if effective:
        status["access_expires_at"] = _as_int(effective.get("expiresAt"))
        status["refresh_expires_at"] = _as_int(effective.get("refreshTokenExpiresAt"))
        status["subscription"] = effective.get("subscriptionType")
        status["rate_limit_tier"] = effective.get("rateLimitTier")
        scopes = effective.get("scopes")
        status["scopes"] = list(scopes) if isinstance(scopes, list) else []

    refresh_exp = status["refresh_expires_at"]
    access_exp = status["access_expires_at"]

    if effective is None:
        status["state"] = "needs_login"
        if backup_rejected:
            status["reason"] = ("The stored credentials were cleared and the backup was "
                                "already rejected by the server — a fresh sign-in is required.")
        else:
            status["reason"] = "No Claude credentials are stored — sign in to get started."
    elif refresh_exp is not None and refresh_exp <= now:
        status["state"] = "needs_login"
        status["reason"] = ("The sign-in's refresh token expired on "
                            f"{_fmt_ts(refresh_exp)} — a fresh sign-in is required.")
    elif refresh_exp is not None and refresh_exp - now <= warn_days * DAY_MS:
        status["state"] = "expiring"
        days_left = max(0, (refresh_exp - now) / DAY_MS)
        status["reason"] = (f"The sign-in expires in about {days_left:.1f} day(s) "
                            f"(on {_fmt_ts(refresh_exp)}) — re-login now to avoid an outage.")
    elif cred is None:
        # Live file cleared, valid backup present: the entrypoint watcher
        # restores this on its own; flag it only as worth watching.
        status["state"] = "stale"
        status["reason"] = ("The live credentials file is cleared; a valid backup exists "
                            "and should be restored automatically. If this persists, sign in again.")
    elif access_exp is not None and now - access_exp > stale_hours * 3600 * 1000:
        status["state"] = "stale"
        hours = (now - access_exp) / 3600 / 1000
        status["reason"] = (f"The access token expired {hours:.0f} hour(s) ago and has not "
                            "been refreshed since — token refresh may be failing.")
    else:
        status["state"] = "ok"
        if refresh_exp is not None:
            status["reason"] = f"Signed in; the sign-in is valid until {_fmt_ts(refresh_exp)}."
        else:
            status["reason"] = "Signed in."
    return status


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _fmt_ts(ms: int | None) -> str:
    if ms is None:
        return "unknown"
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ms / 1000))


# ── Browser login flow (authorization code + PKCE, manual code paste) ─────────

def new_login_attempt() -> dict:
    """One login attempt: PKCE verifier, CSRF state, and the URL to open.

    The caller keeps the attempt (in memory — it contains the code verifier,
    which together with the pasted code yields tokens) and hands only ``url``
    and ``id`` to the browser.
    """
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    state = base64.urlsafe_b64encode(secrets.token_bytes(24)).rstrip(b"=").decode("ascii")
    query = urllib.parse.urlencode({
        # code=true selects the manual flow: the callback page displays the
        # authorization code for copy/paste instead of redirecting anywhere.
        "code": "true",
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    })
    return {
        "id": secrets.token_urlsafe(16),
        "verifier": verifier,
        "state": state,
        "created": time.time(),
        "url": f"{AUTHORIZE_URL}?{query}",
    }


def parse_pasted_code(text: str) -> tuple[str, str | None]:
    """Extract (code, state) from whatever the user pasted.

    The callback page displays ``<code>#<state>``; users also paste the bare
    code, or the full callback URL with ``?code=…&state=…``. Anything else is
    passed through as-is and fails the exchange with the server's message.
    """
    text = (text or "").strip()
    if not text:
        raise ClaudeAuthError("No code was pasted.")
    if "://" in text:
        parsed = urllib.parse.urlsplit(text)
        params = urllib.parse.parse_qs(parsed.query)
        code = (params.get("code") or [""])[0]
        state = (params.get("state") or [None])[0]
        if not code:
            raise ClaudeAuthError("The pasted URL carries no ?code= parameter.")
        return code, state
    if "#" in text:
        code, _, state = text.partition("#")
        return code.strip(), (state.strip() or None)
    return text, None


def _http_post_json(url: str, payload: dict, timeout: float | None = None) -> dict:
    """POST JSON, return the parsed JSON reply; HTTP errors raise with the
    server's error body attached so the user sees *why* an exchange failed."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout or HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:  # noqa: BLE001 - the status line alone still helps
            detail = ""
        raise ClaudeAuthError(f"Token endpoint returned HTTP {exc.code}: {detail}") from exc
    except Exception as exc:  # noqa: BLE001 - DNS, TLS, proxy, timeout
        raise ClaudeAuthError(f"Could not reach the token endpoint: {exc}") from exc


def exchange_code(pasted: str, attempt: dict, http_post=None) -> dict:
    """Trade the pasted authorization code for tokens. Raises ClaudeAuthError."""
    code, pasted_state = parse_pasted_code(pasted)
    if pasted_state is not None and pasted_state != attempt.get("state"):
        raise ClaudeAuthError(
            "This code belongs to a different login attempt — start the sign-in "
            "again and use the newest link."
        )
    post = http_post or _http_post_json
    reply = post(TOKEN_URL, {
        "grant_type": "authorization_code",
        "code": code,
        "state": attempt.get("state"),
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": attempt.get("verifier"),
    })
    if not isinstance(reply, dict) or not reply.get("access_token") or not reply.get("refresh_token"):
        raise ClaudeAuthError(f"Token endpoint sent an unexpected reply: {str(reply)[:300]}")
    return reply


def install_tokens(reply: dict, cred_file: Path | None = None, now: int | None = None) -> dict:
    """Write the token-exchange reply as a fresh ``.credentials.json``.

    Merges over the existing file (unknown sibling keys and, when the reply
    lacks a field, the previous ``subscriptionType`` / ``rateLimitTier`` /
    ``refreshTokenExpiresAt`` survive — the same merge the CLI applies),
    writes atomically with mode 0600, renews the entrypoint's backup, and
    clears the rejected-restore marker so the watcher protocol starts over.
    """
    now = now_ms() if now is None else now
    cred_file = CRED_FILE if cred_file is None else Path(cred_file)
    bak_file = cred_file.with_name(cred_file.name + ".bak")
    marker_file = cred_file.with_name(cred_file.name + ".restored-expiry")

    existing = _read_json(cred_file) or {}
    old_block = existing.get("claudeAiOauth")
    old_block = old_block if isinstance(old_block, dict) else {}

    block = dict(old_block)
    block["accessToken"] = reply["access_token"]
    block["refreshToken"] = reply["refresh_token"]
    expires_in = _as_int(reply.get("expires_in"))
    if expires_in is not None:
        block["expiresAt"] = now + expires_in * 1000
    refresh_expires_in = _as_int(reply.get("refresh_token_expires_in"))
    if refresh_expires_in is not None:
        block["refreshTokenExpiresAt"] = now + refresh_expires_in * 1000
    scope = reply.get("scope")
    if isinstance(scope, str) and scope.strip():
        block["scopes"] = scope.split()
    elif "scopes" not in block:
        block["scopes"] = SCOPES.split()
    block.setdefault("subscriptionType", None)
    block.setdefault("rateLimitTier", None)
    block["clientId"] = CLIENT_ID

    data = dict(existing)
    data["claudeAiOauth"] = block

    cred_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = cred_file.with_name(cred_file.name + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(cred_file)
    # Renew the entrypoint's backup and reset its rejected-restore marker: the
    # new tokens are known-good by construction.
    bak_file.write_text(json.dumps(data), encoding="utf-8")
    os.chmod(bak_file, 0o600)
    try:
        marker_file.unlink()
    except OSError:
        pass
    return {
        "access_expires_at": block.get("expiresAt"),
        "refresh_expires_at": block.get("refreshTokenExpiresAt"),
        "scopes": block.get("scopes", []),
    }


# ── Console entry point (SSH fallback / debugging) ────────────────────────────

def _cli_status() -> int:
    status = credential_status()
    mode = "oauth" if oauth_in_use() else "gateway"
    print(f"mode:            {mode}")
    for key in ("state", "reason", "subscription", "rate_limit_tier"):
        print(f"{key + ':':17}{status.get(key)}")
    print(f"access expires:  {_fmt_ts(status.get('access_expires_at'))}")
    print(f"sign-in expires: {_fmt_ts(status.get('refresh_expires_at'))}")
    print(f"backup present:  {status.get('backup_present')}"
          + (" (rejected by server)" if status.get("backup_rejected") else ""))
    return 0 if status.get("state") in ("ok", "expiring") else 1


def _cli_login() -> int:
    attempt = new_login_attempt()
    print("Open this URL in any browser, sign in, and approve:\n")
    print(f"  {attempt['url']}\n")
    pasted = input("Paste the displayed code here: ")
    try:
        reply = exchange_code(pasted, attempt)
        summary = install_tokens(reply)
    except ClaudeAuthError as exc:
        print(f"Sign-in failed: {exc}")
        return 1
    print("Signed in. Credentials written to", CRED_FILE)
    print("Sign-in valid until:", _fmt_ts(summary.get("refresh_expires_at")))
    print("Restart the retinue container (or wait for the next scheduled job) "
          "to pick the new credentials up everywhere.")
    return 0


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        raise SystemExit(_cli_status())
    if cmd == "login":
        raise SystemExit(_cli_login())
    print(f"usage: {sys.argv[0]} [status|login]")
    raise SystemExit(2)
