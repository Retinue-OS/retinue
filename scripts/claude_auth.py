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
  * ``ensure_fresh_credentials()`` is what every framework process calls
    before it starts a ``claude`` subprocess: when the access token is about
    to expire it performs the one refresh itself, under an flock shared by all
    spawners, so concurrent sessions never race each other for the rotation
    (the token pair rotates on every refresh, and the loser of such a race
    clears the file — the "concurrent sessions" way a sign-in ends early).

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
    python3 scripts/claude_auth.py refresh   # what the spawners do, by hand
"""
from __future__ import annotations

import base64
import contextlib
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import threading
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

# Cloudflare fronts the OAuth endpoints and rejects Python's default
# User-Agent outright (HTTP 403, Cloudflare error 1010 — "banned based on
# browser signature"), so the token exchange must present as the client whose
# sign-in flow this is: the Claude Code CLI. Verified empirically 2026-08-22:
# urllib's stock UA is blocked, "claude-cli/<version> (external, cli)" passes
# through to the OAuth app. The version is read from the installed CLI when
# possible so the UA tracks upgrades; the fallback only matters when `claude`
# is unavailable, and CLAUDE_OAUTH_USER_AGENT forces any value.
_FALLBACK_CLI_VERSION = "2.1.240"
_user_agent_cache: str | None = None


def user_agent() -> str:
    global _user_agent_cache
    override = os.environ.get("CLAUDE_OAUTH_USER_AGENT", "").strip()
    if override:
        return override
    if _user_agent_cache is None:
        version = _FALLBACK_CLI_VERSION
        try:
            out = subprocess.run(["claude", "--version"], capture_output=True,
                                 text=True, timeout=10).stdout or ""
            match = re.match(r"\s*(\d+[\w.-]*)", out)
            if match:
                version = match.group(1)
        except Exception:  # noqa: BLE001 - the fallback version serves fine
            pass
        _user_agent_cache = f"claude-cli/{version} (external, cli)"
    return _user_agent_cache


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
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": user_agent(),  # see the Cloudflare note above
    })
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
    """Write a token reply (sign-in exchange or refresh) as a fresh
    ``.credentials.json``.

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


# ── Spawn-time refresh under a cross-process lock ─────────────────────────────
#
# Anthropic rotates the token pair on every refresh, and every ``claude``
# process refreshes on its own once the access token is within five minutes of
# expiry. The framework starts such processes from several places at once —
# the scheduler's jobs, the gateway's conversation turns, the base-job
# scripts, the Ask-Ara server — beside the long-lived remote-control session,
# all on one credential file. Near expiry that is N processes racing for one
# rotation. Claude Code arbitrates among its own processes (verified against
# 2.1.261: a proper-lockfile lock on ``<config-dir>/.oauth_refresh.lock``,
# legacy ``<config-dir>.lock``, stale after 60 s and touched every 5 s while
# held; five 1–2 s retries; a re-read under the lock that adopts tokens another
# process landed meanwhile), but a process that exhausts the retries gives up
# and its turn fails on the expired token, and a loser on an older CLI clears
# the file outright — the entrypoint's restore-and-restart then either recovers
# or, when the backup holds the same dead pair, ends in an early sign-out.
#
# The framework's answer is to never start a child on a token that is about to
# expire: every spawner calls ``ensure_fresh_credentials()`` first. Only when
# the access token is due within REFRESH_AHEAD_SECONDS does it take the flock
# all framework spawners share, then the CLI's own refresh lock (so a claude
# process that wants to refresh meanwhile waits and then adopts, by its own
# protocol, what the spawner wrote), re-read (another process may have
# finished the job), and perform the one refresh itself under both locks —
# the child then starts on a token good for hours and refreshes nothing. This is not the out-of-band refresh the monitor refuses
# to perform: the rotation is exactly the one the child would trigger seconds
# later, only serialized and done before the spawn rather than inside an agent
# turn. Nothing here ever clears credentials, and no failure is fatal — the
# spawn goes ahead and the session refreshes for itself, as it always did.

# Refresh when the access token expires within this many seconds. Wider than
# the CLI's own 300 s margin on purpose, so the spawner gets there before the
# child would; still a small fraction of the token's lifetime, so the
# rotation cadence is unchanged. 0 disables the pre-spawn refresh.
REFRESH_AHEAD_SECONDS = float(os.environ.get("CLAUDE_AUTH_REFRESH_AHEAD_SECONDS", "") or "900")
# How long a spawner waits for the lock (and for a CLI refresh in flight)
# before giving up and leaving the refresh to the session. A refresh is one
# HTTP call bounded by HTTP_TIMEOUT, so this outlasts a full attempt.
LOCK_WAIT_SECONDS = float(os.environ.get("CLAUDE_AUTH_LOCK_WAIT_SECONDS", "") or "60")
# The framework's own lock: an flock on a sibling of the credential file (the
# file itself is replaced atomically on every write, so a lock on it would die
# with the inode). Released by the kernel when the holder exits.
CRED_LOCK = CRED_FILE.with_name(CRED_FILE.name + ".lock")
# Claude Code's refresh lock is a directory (proper-lockfile) whose mtime the
# holder touches every 5 s; one older than this is stale by the CLI's own rule.
CLI_LOCK_STALE_SECONDS = 60.0
# While the framework holds that lock it touches the mtime this often — well
# inside the stale window, so a slow token endpoint never makes the hold look
# abandoned to a claude process that checks it.
CLI_LOCK_TOUCH_SECONDS = 2.0
_LOCK_POLL_SECONDS = 0.1


def _default_log(msg: str) -> None:
    print(f"[claude-auth] {msg}", file=sys.stderr, flush=True)


def access_token_due(block: dict | None, now: int | None = None,
                     ahead_seconds: float | None = None) -> bool:
    """Whether the stored access token expires within ``ahead_seconds`` — the
    point at which a session started now would refresh it at once. A missing
    or unreadable ``expiresAt`` is False: nothing is rotated on a guess, the
    session applies its own rule to such a file."""
    if not block:
        return False
    now = now_ms() if now is None else now
    ahead = REFRESH_AHEAD_SECONDS if ahead_seconds is None else ahead_seconds
    expires = _as_int(block.get("expiresAt"))
    if expires is None:
        return False
    return expires - now <= ahead * 1000


def cli_refresh_locks(cred_file: Path | None = None) -> list[Path]:
    """Where Claude Code itself locks while refreshing: the current lock inside
    the config directory and the legacy one beside it, in the order the CLI
    takes them (2.1.261 takes both)."""
    cred_file = CRED_FILE if cred_file is None else Path(cred_file)
    config_dir = cred_file.parent
    return [config_dir / ".oauth_refresh.lock",
            config_dir.with_name(config_dir.name + ".lock")]


def _lock_dir_live(path: Path, stale_seconds: float) -> bool | None:
    """True while the lock directory exists and is younger than the stale
    window, False when it exists but is stale, None when it does not exist."""
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return None
    return age < stale_seconds


def cli_refresh_in_flight(cred_file: Path | None = None,
                          stale_seconds: float | None = None) -> bool:
    """Whether a ``claude`` process holds its refresh lock right now — a lock
    directory younger than the CLI's own stale window."""
    stale = CLI_LOCK_STALE_SECONDS if stale_seconds is None else stale_seconds
    return any(_lock_dir_live(path, stale) for path in cli_refresh_locks(cred_file))


@contextlib.contextmanager
def cli_refresh_lock(cred_file: Path | None = None, wait_seconds: float | None = None,
                     log=None):
    """Hold Claude Code's own refresh lock for the duration of a refresh.

    The CLI serializes its refreshes with proper-lockfile directories, and a
    process that finds one held retries, then re-reads the credential file
    under the lock and adopts a pair another process landed meanwhile. Taking
    the same directories, in the same order, makes the framework a participant
    in that protocol instead of an observer: a claude process that starts
    refreshing while a spawner is mid-refresh waits on the directory and then
    adopts what the spawner wrote, so two grants are never sent with the same
    refresh token. Acquisition is ``mkdir`` (atomic); a directory whose mtime
    is older than CLI_LOCK_STALE_SECONDS is stale by the CLI's rule and is
    removed; while held, every directory's mtime is touched so a slow token
    endpoint never makes the hold look abandoned. Yields True with the lock
    held, False when the wait ran out — nothing is left behind either way.
    """
    wait = LOCK_WAIT_SECONDS if wait_seconds is None else wait_seconds
    log = log or _default_log
    deadline = time.monotonic() + wait
    held: list[Path] = []
    stop = threading.Event()
    toucher: threading.Thread | None = None
    waited = False
    try:
        for path in cli_refresh_locks(cred_file):
            while True:
                live = _lock_dir_live(path, CLI_LOCK_STALE_SECONDS)
                if live is False:
                    # Stale by the CLI's own rule: its holder is gone.
                    try:
                        os.rmdir(path)
                    except OSError:
                        pass
                if live is not True:
                    try:
                        os.mkdir(path)
                        held.append(path)
                        break
                    except FileExistsError:
                        pass  # a claude process won the race for it: wait
                if time.monotonic() >= deadline:
                    yield False
                    return
                if not waited:
                    log("a claude session is refreshing the token — waiting for it")
                    waited = True
                time.sleep(_LOCK_POLL_SECONDS)

        def _touch() -> None:
            while not stop.wait(CLI_LOCK_TOUCH_SECONDS):
                for path in held:
                    try:
                        os.utime(path, None)
                    except OSError:
                        pass

        toucher = threading.Thread(target=_touch, name="claude-auth-lock-touch", daemon=True)
        toucher.start()
        yield True
    finally:
        stop.set()
        if toucher is not None:
            toucher.join(timeout=1.0)
        for path in reversed(held):
            try:
                os.rmdir(path)
            except OSError:
                pass


@contextlib.contextmanager
def credential_lock(wait_seconds: float | None = None, lock_file: Path | None = None):
    """The cross-process mutex around credential rotation, shared by every
    framework spawner. Yields True when the lock is held, False when the wait
    ran out — the caller must then leave the credentials alone (a refresh
    outside the lock is precisely the race this exists to prevent)."""
    wait = LOCK_WAIT_SECONDS if wait_seconds is None else wait_seconds
    lock_file = CRED_LOCK if lock_file is None else Path(lock_file)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o600)
    held = False
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
                    raise
                if time.monotonic() >= deadline:
                    break
                time.sleep(_LOCK_POLL_SECONDS)
        yield held
    finally:
        if held:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


def refresh_tokens(block: dict, http_post=None) -> dict:
    """One refresh-token grant for the stored pair — the request the CLI sends
    (``grant_type``, ``refresh_token``, ``client_id``, the stored scopes joined
    by spaces). Returns the token reply with ``refresh_token`` defaulted to the
    current one, since the server may keep it. Raises ClaudeAuthError."""
    refresh_token = (block or {}).get("refreshToken")
    if not refresh_token:
        raise ClaudeAuthError("No refresh token is stored — sign in again.")
    scopes = block.get("scopes")
    scope = " ".join(scopes) if isinstance(scopes, list) and scopes else SCOPES
    post = http_post or _http_post_json
    reply = post(TOKEN_URL, {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
        "scope": scope,
    })
    if not isinstance(reply, dict) or not reply.get("access_token"):
        raise ClaudeAuthError(f"Token endpoint sent an unexpected reply: {str(reply)[:300]}")
    reply = dict(reply)
    if not reply.get("refresh_token"):
        reply["refresh_token"] = refresh_token
    return reply


def _signin_expired(block: dict, now: int | None = None) -> str:
    """A reason when the refresh token's own recorded expiry has passed —
    the case only a fresh sign-in helps, so no refresh is worth attempting —
    else an empty string."""
    refresh_exp = _as_int(block.get("refreshTokenExpiresAt"))
    if refresh_exp is None or refresh_exp > (now_ms() if now is None else now):
        return ""
    return f"the sign-in's refresh token expired on {_fmt_ts(refresh_exp)}"


def _outcome(action: str, block: dict | None = None, **extra) -> dict:
    result = {"action": action, "access_expires_at": None}
    if block:
        result["access_expires_at"] = _as_int(block.get("expiresAt"))
    result.update(extra)
    return result


def ensure_fresh_credentials(cred_file: Path | None = None, now: int | None = None,
                             ahead_seconds: float | None = None,
                             wait_seconds: float | None = None,
                             http_post=None, log=None) -> dict:
    """Refresh the access token before a ``claude`` process is started, if it
    is about to expire — once, under the shared lock. Never raises and never
    clears anything: on any failure the spawn goes ahead and the session
    refreshes on its own.

    Returns ``{"action": ..., "access_expires_at": ..., ["reason": ...]}``:
      gateway         no OAuth sign-in in this deployment — nothing to do
      disabled        CLAUDE_AUTH_REFRESH_AHEAD_SECONDS is 0
      no_credentials  nothing stored (the watcher and the re-login own that)
      fresh           the access token outlives the margin — the common case
      adopted         another process refreshed while this one waited
      refreshed       this process performed the refresh
      expired         the refresh token's own expiry has passed — only a
                      fresh sign-in helps; nothing is attempted
      lock_timeout    neither the shared lock nor a live CLI lock freed up in
                      time; left alone rather than sent a competing refresh
      failed          the token endpoint rejected the refresh or was unreachable
    """
    log = log or _default_log
    cred_file = CRED_FILE if cred_file is None else Path(cred_file)
    ahead = REFRESH_AHEAD_SECONDS if ahead_seconds is None else ahead_seconds
    wait = LOCK_WAIT_SECONDS if wait_seconds is None else wait_seconds
    try:
        if not oauth_in_use():
            return _outcome("gateway")
        if ahead <= 0:
            return _outcome("disabled")
        block = oauth_block(_read_json(cred_file))
        if block is None:
            return _outcome("no_credentials")
        if not access_token_due(block, now, ahead):
            return _outcome("fresh", block)
        if _signin_expired(block, now):
            return _outcome("expired", block, reason=_signin_expired(block, now))

        deadline = time.monotonic() + wait
        lock_file = cred_file.with_name(cred_file.name + ".lock")
        with credential_lock(wait, lock_file) as held:
            if not held:
                log(f"credential lock still held after {wait:.0f}s — "
                    "leaving the refresh to the session")
                return _outcome("lock_timeout", block)
            # Then Claude Code's own refresh lock, held across the re-read,
            # the grant and the install: a claude process mid-refresh makes
            # us wait, and one that starts meanwhile waits on us and then
            # adopts what we wrote — no two grants ever carry the same pair.
            # A live lock that outlasts the wait (its holder keeps touching
            # it) means leaving the refresh to the session, exactly like the
            # shared-lock timeout above; a lock that stopped being touched is
            # stale by the CLI's own rule and is taken over.
            remaining = max(0.0, deadline - time.monotonic())
            with cli_refresh_lock(cred_file, remaining, log) as cli_held:
                if not cli_held:
                    log("a claude session has held its own refresh lock for over "
                        f"{wait:.0f}s — leaving the refresh to the session")
                    return _outcome("lock_timeout", block,
                                    reason="a claude session's refresh lock stayed live past the wait")
                # Re-read under both locks: whoever held either before us may
                # have done the work already.
                block = oauth_block(_read_json(cred_file))
                if block is None:
                    return _outcome("no_credentials")
                if not access_token_due(block, now, ahead):
                    log("the access token was refreshed by another process "
                        f"meanwhile — valid until {_fmt_ts(_as_int(block.get('expiresAt')))}")
                    return _outcome("adopted", block)
                try:
                    reply = refresh_tokens(block, http_post)
                    summary = install_tokens(reply, cred_file, now)
                except ClaudeAuthError as exc:
                    log(f"token refresh before spawn failed: {exc} — "
                        "the session will refresh on its own")
                    return _outcome("failed", block, reason=str(exc))
                log("access token refreshed before spawn — valid until "
                    f"{_fmt_ts(summary.get('access_expires_at'))}")
                return _outcome("refreshed", access_expires_at=summary.get("access_expires_at"))
    except Exception as exc:  # noqa: BLE001 - a spawn must never fail on this helper
        log(f"pre-spawn credential check failed: {exc!r} — "
            "the session will refresh on its own")
        return _outcome("failed", reason=repr(exc))


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


def _cli_refresh() -> int:
    """Refresh a near-expiry access token under the shared lock — the same
    step the scheduler and the gateway take before every spawn. Run it before
    starting a ``claude`` process by hand beside the live system."""
    result = ensure_fresh_credentials(log=lambda msg: print(f"[claude-auth] {msg}"))
    action = result["action"]
    print(f"action:          {action}")
    if result.get("reason"):
        print(f"reason:          {result['reason']}")
    if result.get("access_expires_at") is not None:
        print(f"access expires:  {_fmt_ts(result['access_expires_at'])}")
    return 0 if action in ("fresh", "adopted", "refreshed", "gateway", "disabled") else 1


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        raise SystemExit(_cli_status())
    if cmd == "login":
        raise SystemExit(_cli_login())
    if cmd == "refresh":
        raise SystemExit(_cli_refresh())
    print(f"usage: {sys.argv[0]} [status|login|refresh]")
    raise SystemExit(2)
