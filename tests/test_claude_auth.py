#!/usr/bin/env python3
"""Checks for the Claude OAuth credential module (scripts/claude_auth.py).

Exercises the pure parts — credential-state classification against the
entrypoint's backup/marker protocol, the PKCE authorize URL, pasted-code
parsing, the token-install merge, and the pre-spawn refresh under the shared
lock (contention, adoption, yielding to the CLI's own lock) — on temp files
with a fake token endpoint; no network and no Claude session is needed.

    python3 tests/test_claude_auth.py
"""
import base64
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.parse
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS_DIR))
spec = importlib.util.spec_from_file_location("claude_auth_under_test",
                                              SCRIPTS_DIR / "claude_auth.py")
ca = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ca)

NOW = 1_700_000_000_000  # fixed clock (ms)
DAY = 24 * 3600 * 1000


def _write(path: Path, refresh="rt", access="at", expires_at=NOW + 3600_000,
           refresh_expires_at=NOW + 20 * DAY, **extra):
    block = {"accessToken": access, "refreshToken": refresh,
             "expiresAt": expires_at, "scopes": ["user:profile", "user:inference"],
             "subscriptionType": "max", **extra}
    if refresh_expires_at is not None:
        block["refreshTokenExpiresAt"] = refresh_expires_at
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"claudeAiOauth": block}))


def _status(tmp: Path, **kwargs):
    return ca.credential_status(now=NOW, cred_file=tmp / ".credentials.json", **kwargs)


def test_ok_when_fresh():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _write(tmp / ".credentials.json")
        st = _status(tmp)
        assert st["state"] == "ok", st
        assert st["refresh_expires_at"] == NOW + 20 * DAY
        assert st["subscription"] == "max"


def test_missing_needs_login():
    with tempfile.TemporaryDirectory() as d:
        st = _status(Path(d))
        assert st["state"] == "needs_login"
        assert not st["credentials_present"] and not st["backup_present"]


def test_refresh_token_expired_needs_login():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _write(tmp / ".credentials.json", refresh_expires_at=NOW - 1000)
        st = _status(tmp)
        assert st["state"] == "needs_login", st
        assert "expired" in st["reason"]


def test_refresh_token_expiring_warns_ahead():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _write(tmp / ".credentials.json", refresh_expires_at=NOW + 2 * DAY)
        st = _status(tmp, warn_days=3)
        assert st["state"] == "expiring", st
        # Outside the warn window the same file is plain ok.
        st = _status(tmp, warn_days=1)
        assert st["state"] == "ok", st


def test_no_refresh_expiry_recorded_is_ok():
    """Old credential files without refreshTokenExpiresAt must not warn."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _write(tmp / ".credentials.json", refresh_expires_at=None)
        st = _status(tmp)
        assert st["state"] == "ok", st
        assert st["refresh_expires_at"] is None


def test_cleared_with_valid_backup_is_stale():
    """The entrypoint watcher restores this on its own — warn, not dead."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _write(tmp / ".credentials.json.bak")
        st = _status(tmp)
        assert st["state"] == "stale", st
        assert st["backup_present"] and not st["backup_rejected"]


def test_cleared_with_rejected_backup_needs_login():
    """Marker matching the backup's expiresAt = the server rejected it."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _write(tmp / ".credentials.json.bak", expires_at=123456)
        (tmp / ".credentials.json.restored-expiry").write_text("123456")
        st = _status(tmp)
        assert st["state"] == "needs_login", st
        assert st["backup_rejected"]
        assert "rejected" in st["reason"]


def test_stale_access_token_warns():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _write(tmp / ".credentials.json", expires_at=NOW - 30 * 3600 * 1000)
        st = _status(tmp, stale_hours=24)
        assert st["state"] == "stale", st
        # Freshly expired access tokens are normal (sessions refresh lazily).
        _write(tmp / ".credentials.json", expires_at=NOW - 3600 * 1000)
        st = _status(tmp, stale_hours=24)
        assert st["state"] == "ok", st


def test_authorize_url_carries_pkce():
    attempt = ca.new_login_attempt()
    parsed = urllib.parse.urlsplit(attempt["url"])
    params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
    assert params["code"] == "true"
    assert params["client_id"] == ca.CLIENT_ID
    assert params["response_type"] == "code"
    assert params["code_challenge_method"] == "S256"
    assert params["state"] == attempt["state"]
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(attempt["verifier"].encode()).digest()).rstrip(b"=").decode()
    assert params["code_challenge"] == expected
    # Two attempts never share state or verifier.
    other = ca.new_login_attempt()
    assert other["state"] != attempt["state"] and other["verifier"] != attempt["verifier"]


def test_parse_pasted_code_variants():
    assert ca.parse_pasted_code("abc123") == ("abc123", None)
    assert ca.parse_pasted_code("  abc#st  ") == ("abc", "st")
    code, state = ca.parse_pasted_code(
        "https://platform.claude.com/oauth/code/callback?code=xyz&state=st2")
    assert (code, state) == ("xyz", "st2")
    for bad in ("", "   ", "https://example.com/?nope=1"):
        try:
            ca.parse_pasted_code(bad)
        except ca.ClaudeAuthError:
            continue
        raise AssertionError(f"expected ClaudeAuthError for {bad!r}")


def test_exchange_rejects_foreign_state():
    attempt = ca.new_login_attempt()
    try:
        ca.exchange_code("code#WRONG", attempt,
                         http_post=lambda *a, **k: (_ for _ in ()).throw(AssertionError(
                             "must not reach the token endpoint")))
    except ca.ClaudeAuthError as exc:
        assert "different login attempt" in str(exc)
    else:
        raise AssertionError("foreign state accepted")


def test_exchange_posts_expected_payload():
    attempt = ca.new_login_attempt()
    seen = {}

    def fake_post(url, payload):
        seen.update(url=url, payload=payload)
        return {"access_token": "at", "refresh_token": "rt", "expires_in": 60}

    reply = ca.exchange_code(f"thecode#{attempt['state']}", attempt, http_post=fake_post)
    assert reply["access_token"] == "at"
    assert seen["url"] == ca.TOKEN_URL
    p = seen["payload"]
    assert p["grant_type"] == "authorization_code"
    assert p["code"] == "thecode"
    assert p["state"] == attempt["state"]
    assert p["code_verifier"] == attempt["verifier"]
    assert p["redirect_uri"] == ca.REDIRECT_URI


def test_install_tokens_merges_writes_and_resets_marker():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        cred = _cfg(tmp) / ".credentials.json"
        # Simulate the dead state: old file with extra keys, rejected backup.
        cred.write_text(json.dumps({
            "claudeAiOauth": {"accessToken": "old", "refreshToken": "old-rt",
                              "expiresAt": 1, "subscriptionType": "max",
                              "rateLimitTier": "t1"},
            "someOtherSection": {"kept": True},
        }))
        (cred.with_name(".credentials.json.bak")).write_text("{}")
        (cred.with_name(".credentials.json.restored-expiry")).write_text("1")

        summary = ca.install_tokens({
            "access_token": "new-at", "refresh_token": "new-rt",
            "expires_in": 3600, "refresh_token_expires_in": 30 * 86400,
            "scope": "user:profile user:inference",
        }, cred_file=cred, now=NOW)

        data = json.loads(cred.read_text())
        block = data["claudeAiOauth"]
        assert block["accessToken"] == "new-at" and block["refreshToken"] == "new-rt"
        assert block["expiresAt"] == NOW + 3600_000
        assert block["refreshTokenExpiresAt"] == NOW + 30 * 86400 * 1000
        assert block["scopes"] == ["user:profile", "user:inference"]
        # Fields the reply lacks survive from the old file; unknown sections too.
        assert block["subscriptionType"] == "max" and block["rateLimitTier"] == "t1"
        assert data["someOtherSection"] == {"kept": True}
        assert block["clientId"] == ca.CLIENT_ID
        assert (cred.stat().st_mode & 0o777) == 0o600
        # Backup renewed to the new credentials, rejected-marker cleared.
        assert json.loads((cred.with_name(".credentials.json.bak")).read_text()) == data
        assert not (cred.with_name(".credentials.json.restored-expiry")).exists()
        assert summary["refresh_expires_at"] == NOW + 30 * 86400 * 1000


def test_install_tokens_from_scratch():
    with tempfile.TemporaryDirectory() as d:
        cred = Path(d) / "nested" / ".credentials.json"
        ca.install_tokens({"access_token": "a", "refresh_token": "r",
                           "expires_in": 10}, cred_file=cred, now=NOW)
        block = json.loads(cred.read_text())["claudeAiOauth"]
        assert block["refreshToken"] == "r"
        assert block["scopes"] == ca.SCOPES.split()
        assert block["subscriptionType"] is None
        # And the file now classifies as signed-in.
        st = ca.credential_status(now=NOW, cred_file=cred)
        assert st["state"] == "ok", st


def test_user_agent_shape_and_override():
    """Cloudflare blocks urllib's default UA (403 / error 1010); the exchange
    must present as the Claude Code CLI, overridable via env."""
    old = os.environ.pop("CLAUDE_OAUTH_USER_AGENT", None)
    try:
        ua = ca.user_agent()
        assert re.fullmatch(r"claude-cli/\d[\w.-]* \(external, cli\)", ua), ua
        assert ca.user_agent() == ua  # cached, no repeated subprocess
        os.environ["CLAUDE_OAUTH_USER_AGENT"] = "custom/1.0"
        assert ca.user_agent() == "custom/1.0"
    finally:
        os.environ.pop("CLAUDE_OAUTH_USER_AGENT", None)
        if old is not None:
            os.environ["CLAUDE_OAUTH_USER_AGENT"] = old


def test_oauth_in_use_gate():
    old_base = os.environ.pop("ANTHROPIC_BASE_URL", None)
    old_flag = os.environ.pop("RETINUE_GATEWAY_USES_CLAUDE_OAUTH", None)
    try:
        assert ca.oauth_in_use()
        os.environ["ANTHROPIC_BASE_URL"] = "http://litellm:4000"
        assert not ca.oauth_in_use()
        os.environ["RETINUE_GATEWAY_USES_CLAUDE_OAUTH"] = "true"
        assert ca.oauth_in_use()
    finally:
        os.environ.pop("ANTHROPIC_BASE_URL", None)
        os.environ.pop("RETINUE_GATEWAY_USES_CLAUDE_OAUTH", None)
        if old_base is not None:
            os.environ["ANTHROPIC_BASE_URL"] = old_base
        if old_flag is not None:
            os.environ["RETINUE_GATEWAY_USES_CLAUDE_OAUTH"] = old_flag


# ── Pre-spawn refresh under the shared lock ──────────────────────────────────

@contextmanager
def _oauth_env(base_url=None):
    """Pin the deployment mode for a test: OAuth (default) or gateway."""
    saved = {k: os.environ.pop(k, None)
             for k in ("ANTHROPIC_BASE_URL", "RETINUE_GATEWAY_USES_CLAUDE_OAUTH")}
    if base_url:
        os.environ["ANTHROPIC_BASE_URL"] = base_url
    try:
        yield
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def _cfg(tmp: Path) -> Path:
    """A config dir nested in the temp dir, so the CLI's legacy lock path
    (`<config-dir>.lock`) stays inside it too."""
    cfg = tmp / ".claude"
    cfg.mkdir(parents=True, exist_ok=True)
    return cfg


class _Endpoint:
    """Fake token endpoint: records requests, answers with a fresh pair."""

    def __init__(self, reply=None, error=None, delay=0.0):
        self.calls = []
        self.reply = reply if reply is not None else {
            "access_token": "at2", "refresh_token": "rt2", "expires_in": 28800,
            "refresh_token_expires_in": 30 * 24 * 3600,
            "scope": "user:profile user:inference",
        }
        self.error = error
        self.delay = delay

    def __call__(self, url, payload):
        self.calls.append((url, payload))
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise ca.ClaudeAuthError(self.error)
        return dict(self.reply)


def _ensure(cred, ep=None, **kwargs):
    kwargs.setdefault("now", NOW)
    kwargs.setdefault("ahead_seconds", 900)
    kwargs.setdefault("wait_seconds", 5)
    kwargs.setdefault("log", lambda msg: None)
    return ca.ensure_fresh_credentials(cred_file=cred, http_post=ep, **kwargs)


def _hold_flock(lock_file: Path):
    """Hold the spawners' flock through an independent descriptor (a separate
    open() conflicts with flock even inside one process)."""
    fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def test_access_token_due_boundaries():
    due = lambda expires_at, ahead=900: ca.access_token_due(  # noqa: E731
        {"expiresAt": expires_at}, now=NOW, ahead_seconds=ahead)
    assert not due(NOW + 20 * 60_000)          # plenty of life left
    assert due(NOW + 10 * 60_000)              # inside the margin
    assert due(NOW - 1)                        # already expired
    assert due(NOW + 4 * 60_000, ahead=300)    # the CLI's own margin
    assert not ca.access_token_due({"expiresAt": "soon"}, now=NOW)  # unreadable
    assert not ca.access_token_due({}, now=NOW)
    assert not ca.access_token_due(None, now=NOW)


def test_cli_refresh_lock_is_seen_only_while_live():
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(Path(d))
        cred = cfg / ".credentials.json"
        assert not ca.cli_refresh_in_flight(cred)
        current, legacy = ca.cli_refresh_locks(cred)
        assert current == cfg / ".oauth_refresh.lock"
        assert legacy == Path(d) / ".claude.lock"
        current.mkdir()
        assert ca.cli_refresh_in_flight(cred)
        stale = time.time() - 120
        os.utime(current, (stale, stale))
        assert not ca.cli_refresh_in_flight(cred)      # older than the stale window
        assert ca.cli_refresh_in_flight(cred, stale_seconds=600)
        current.rmdir()
        legacy.mkdir()
        assert ca.cli_refresh_in_flight(cred)          # the legacy path counts too


def test_ensure_fresh_is_a_no_op_on_a_live_token():
    with tempfile.TemporaryDirectory() as d, _oauth_env():
        cred = _cfg(Path(d)) / ".credentials.json"
        _write(cred, expires_at=NOW + 2 * 3600_000)
        before = cred.read_bytes()
        ep = _Endpoint()
        res = _ensure(cred, ep)
        assert res["action"] == "fresh", res
        assert res["access_expires_at"] == NOW + 2 * 3600_000
        assert ep.calls == [] and cred.read_bytes() == before
        assert not (cred.parent / ".credentials.json.lock").exists()  # never even locked


def test_ensure_fresh_refreshes_a_token_about_to_expire():
    with tempfile.TemporaryDirectory() as d, _oauth_env():
        cred = _cfg(Path(d)) / ".credentials.json"
        _write(cred, expires_at=NOW + 5 * 60_000, subscriptionType="max", rateLimitTier="t1")
        marker = cred.with_name(".credentials.json.restored-expiry")
        marker.write_text("stale-marker")
        ep = _Endpoint()
        res = _ensure(cred, ep)
        assert res["action"] == "refreshed", res
        assert res["access_expires_at"] == NOW + 28800 * 1000
        # The request is the CLI's own refresh grant, with the stored scopes.
        assert len(ep.calls) == 1
        url, payload = ep.calls[0]
        assert url == ca.TOKEN_URL
        assert payload == {"grant_type": "refresh_token", "refresh_token": "rt",
                           "client_id": ca.CLIENT_ID, "scope": "user:profile user:inference"}
        block = json.loads(cred.read_text())["claudeAiOauth"]
        assert (block["accessToken"], block["refreshToken"]) == ("at2", "rt2")
        assert block["expiresAt"] == NOW + 28800 * 1000
        assert block["refreshTokenExpiresAt"] == NOW + 30 * 24 * 3600 * 1000
        assert block["subscriptionType"] == "max" and block["rateLimitTier"] == "t1"
        # The watcher protocol is reset: backup renewed, rejected-marker gone.
        assert cred.with_name(".credentials.json.bak").read_text() == cred.read_text()
        assert not marker.exists()
        # A second call finds the fresh token and does nothing.
        assert _ensure(cred, ep)["action"] == "fresh" and len(ep.calls) == 1


def test_ensure_fresh_keeps_the_refresh_token_the_server_did_not_rotate():
    with tempfile.TemporaryDirectory() as d, _oauth_env():
        cred = _cfg(Path(d)) / ".credentials.json"
        _write(cred, expires_at=NOW - 1)
        ep = _Endpoint(reply={"access_token": "at2", "expires_in": 3600})
        assert _ensure(cred, ep)["action"] == "refreshed"
        block = json.loads(cred.read_text())["claudeAiOauth"]
        assert (block["accessToken"], block["refreshToken"]) == ("at2", "rt")
        assert block["expiresAt"] == NOW + 3600_000


def test_ensure_fresh_leaves_everything_alone_when_the_endpoint_rejects():
    with tempfile.TemporaryDirectory() as d, _oauth_env():
        cred = _cfg(Path(d)) / ".credentials.json"
        _write(cred, expires_at=NOW - 1)
        before = cred.read_bytes()
        logged = []
        ep = _Endpoint(error='Token endpoint returned HTTP 400: {"error":"invalid_grant"}')
        res = _ensure(cred, ep, log=logged.append)
        assert res["action"] == "failed", res
        assert "invalid_grant" in res["reason"]
        assert cred.read_bytes() == before                      # nothing cleared, nothing written
        assert not cred.with_name(".credentials.json.bak").exists()
        assert any("refresh on its own" in line for line in logged), logged


def test_ensure_fresh_never_tries_an_expired_signin():
    with tempfile.TemporaryDirectory() as d, _oauth_env():
        cred = _cfg(Path(d)) / ".credentials.json"
        _write(cred, expires_at=NOW - 1, refresh_expires_at=NOW - 1000)
        ep = _Endpoint()
        logged = []
        res = _ensure(cred, ep, log=logged.append)
        assert res["action"] == "expired", res
        assert ep.calls == []
        assert "expired" in res["reason"]
        assert any("only a fresh sign-in helps" in line for line in logged), logged


def test_ensure_fresh_skips_without_credentials_or_oauth_or_when_disabled():
    with tempfile.TemporaryDirectory() as d:
        cred = _cfg(Path(d)) / ".credentials.json"
        ep = _Endpoint()
        with _oauth_env():
            assert _ensure(cred, ep)["action"] == "no_credentials"
            _write(cred, expires_at=NOW - 1)
            assert _ensure(cred, ep, ahead_seconds=0)["action"] == "disabled"
        with _oauth_env(base_url="http://litellm:4000"):
            assert _ensure(cred, ep)["action"] == "gateway"
        assert ep.calls == []


def test_ensure_fresh_adopts_a_refresh_another_process_finished_first():
    """A spawner that waited for the lock re-reads before doing anything."""
    with tempfile.TemporaryDirectory() as d, _oauth_env():
        cred = _cfg(Path(d)) / ".credentials.json"
        _write(cred, expires_at=NOW - 1)
        fd = _hold_flock(cred.with_name(".credentials.json.lock"))
        ep = _Endpoint()
        result = {}
        t = threading.Thread(target=lambda: result.update(_ensure(cred, ep)))
        t.start()
        time.sleep(0.3)
        assert not result, "must block on the lock"
        _write(cred, access="at-other", refresh="rt-other", expires_at=NOW + 2 * 3600_000)
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        t.join(5)
        assert result["action"] == "adopted", result
        assert ep.calls == []
        assert json.loads(cred.read_text())["claudeAiOauth"]["refreshToken"] == "rt-other"


def test_ensure_fresh_gives_up_on_a_lock_that_never_frees():
    with tempfile.TemporaryDirectory() as d, _oauth_env():
        cred = _cfg(Path(d)) / ".credentials.json"
        _write(cred, expires_at=NOW - 1)
        before = cred.read_bytes()
        fd = _hold_flock(cred.with_name(".credentials.json.lock"))
        try:
            ep = _Endpoint()
            logged = []
            started = time.monotonic()
            res = _ensure(cred, ep, wait_seconds=0.3, log=logged.append)
            assert res["action"] == "lock_timeout", res
            assert 0.3 <= time.monotonic() - started < 3
            assert ep.calls == [] and cred.read_bytes() == before
            assert any("leaving the refresh to the session" in line for line in logged)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def test_ensure_fresh_yields_to_a_claude_refresh_in_flight():
    """A live CLI lock means a session is mid-refresh: wait for it, then adopt
    its tokens rather than send a competing refresh with the same pair."""
    with tempfile.TemporaryDirectory() as d, _oauth_env():
        cred = _cfg(Path(d)) / ".credentials.json"
        _write(cred, expires_at=NOW - 1)
        cli_lock = cred.parent / ".oauth_refresh.lock"
        cli_lock.mkdir()
        ep = _Endpoint()
        logged = []
        result = {}
        t = threading.Thread(target=lambda: result.update(_ensure(cred, ep, log=logged.append)))
        t.start()
        time.sleep(0.3)
        assert not result, "must wait while the CLI holds its lock"
        _write(cred, access="at-cli", refresh="rt-cli", expires_at=NOW + 2 * 3600_000)
        cli_lock.rmdir()
        t.join(5)
        assert result["action"] == "adopted", result
        assert ep.calls == []
        assert any("waiting for it" in line for line in logged), logged
        # Nothing of ours is left behind once the section ends.
        assert not any(p.exists() for p in ca.cli_refresh_locks(cred))


def test_ensure_fresh_leaves_a_long_held_claude_lock_alone():
    """A CLI lock that stays live past the wait is a refresh still in
    progress: give up, as on the shared lock, never compete with it."""
    with tempfile.TemporaryDirectory() as d, _oauth_env():
        cred = _cfg(Path(d)) / ".credentials.json"
        _write(cred, expires_at=NOW - 1)
        before = cred.read_bytes()
        current, legacy = ca.cli_refresh_locks(cred)
        current.mkdir()
        ep = _Endpoint()
        logged = []
        res = _ensure(cred, ep, wait_seconds=0.3, log=logged.append)
        assert res["action"] == "lock_timeout", res
        assert ep.calls == [] and cred.read_bytes() == before
        assert any("leaving the refresh to the session" in line for line in logged), logged
        assert current.exists(), "a live lock is never taken away from its holder"
        assert not legacy.exists(), "nothing of ours is left behind"


def test_ensure_fresh_ignores_a_stale_claude_lock():
    with tempfile.TemporaryDirectory() as d, _oauth_env():
        cred = _cfg(Path(d)) / ".credentials.json"
        _write(cred, expires_at=NOW - 1)
        cli_lock = cred.parent / ".oauth_refresh.lock"
        cli_lock.mkdir()
        stale = time.time() - 2 * ca.CLI_LOCK_STALE_SECONDS
        os.utime(cli_lock, (stale, stale))
        ep = _Endpoint()
        started = time.monotonic()
        assert _ensure(cred, ep)["action"] == "refreshed"
        assert time.monotonic() - started < 1
        assert len(ep.calls) == 1
        assert not cli_lock.exists(), "the stale lock was taken over and released"


def test_ensure_fresh_holds_the_cli_lock_across_the_refresh():
    """During the grant both of Claude Code's lock directories are held (so a
    session starting a refresh meanwhile waits and then adopts), the
    spawners' own flock is held too, and everything is released after."""
    with tempfile.TemporaryDirectory() as d, _oauth_env():
        cred = _cfg(Path(d)) / ".credentials.json"
        _write(cred, expires_at=NOW - 1)
        locks = ca.cli_refresh_locks(cred)
        seen = {}

        class _Observing(_Endpoint):
            def __call__(self, url, payload):
                seen["held"] = [p.is_dir() and time.time() - p.stat().st_mtime < 5
                                for p in locks]
                seen["in_flight"] = ca.cli_refresh_in_flight(cred)
                return super().__call__(url, payload)

        ep = _Observing()
        assert _ensure(cred, ep)["action"] == "refreshed"
        assert seen["held"] == [True, True], seen
        assert seen["in_flight"] is True
        assert not any(p.exists() for p in locks), "released after the refresh"
        assert json.loads(cred.read_text())["claudeAiOauth"]["refreshToken"] == "rt2"


def test_concurrent_spawners_perform_exactly_one_refresh():
    with tempfile.TemporaryDirectory() as d, _oauth_env():
        cred = _cfg(Path(d)) / ".credentials.json"
        _write(cred, expires_at=NOW - 1)
        ep = _Endpoint(delay=0.2)
        results = []
        lock = threading.Lock()

        def spawner():
            res = _ensure(cred, ep)
            with lock:
                results.append(res["action"])

        threads = [threading.Thread(target=spawner) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        assert sorted(results) == ["adopted"] * 3 + ["refreshed"], results
        assert len(ep.calls) == 1, ep.calls
        block = json.loads(cred.read_text())["claudeAiOauth"]
        assert (block["accessToken"], block["refreshToken"]) == ("at2", "rt2")


def test_install_tokens_waits_for_a_refresh_in_flight():
    """A sign-in completing while a spawner is mid-refresh is installed after
    that refresh, never overwritten by it."""
    with tempfile.TemporaryDirectory() as d:
        cred = _cfg(Path(d)) / ".credentials.json"
        _write(cred, expires_at=NOW - 1)
        fd = _hold_flock(cred.with_name(".credentials.json.lock"))  # a spawner, mid-refresh
        done = {}
        t = threading.Thread(target=lambda: done.update(ca.install_tokens(
            {"access_token": "at-new", "refresh_token": "rt-new", "expires_in": 3600},
            cred_file=cred, now=NOW, log=lambda msg: None)))
        t.start()
        time.sleep(0.3)
        assert not done, "must wait for the spawner's lock"
        # The spawner lands the old family's rotation, then releases.
        _write(cred, access="at-old2", refresh="rt-old2", expires_at=NOW + 8 * 3600_000)
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        t.join(5)
        assert done.get("access_expires_at") == NOW + 3600_000, done
        block = json.loads(cred.read_text())["claudeAiOauth"]
        assert (block["accessToken"], block["refreshToken"]) == ("at-new", "rt-new")
        assert not any(p.exists() for p in ca.cli_refresh_locks(cred))


def test_install_tokens_never_loses_a_signin_to_a_stuck_lock():
    reply = {"access_token": "a", "refresh_token": "r", "expires_in": 60}
    with tempfile.TemporaryDirectory() as d:
        cred = _cfg(Path(d)) / ".credentials.json"
        # The spawners' lock never frees up.
        fd = _hold_flock(cred.with_name(".credentials.json.lock"))
        try:
            logged = []
            ca.install_tokens(reply, cred_file=cred, now=NOW, wait_seconds=0.3,
                              log=logged.append)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        assert json.loads(cred.read_text())["claudeAiOauth"]["refreshToken"] == "r"
        assert any("regardless" in line for line in logged), logged
        # A claude session's own lock stays live past the wait: same outcome,
        # and its lock is left to it.
        current, legacy = ca.cli_refresh_locks(cred)
        current.mkdir()
        logged = []
        ca.install_tokens({**reply, "refresh_token": "r2"}, cred_file=cred, now=NOW,
                          wait_seconds=0.3, log=logged.append)
        assert json.loads(cred.read_text())["claudeAiOauth"]["refreshToken"] == "r2"
        assert any("regardless" in line for line in logged), logged
        assert current.exists() and not legacy.exists()


def test_ensure_fresh_never_raises():
    """A spawn must go ahead whatever this helper runs into."""
    with tempfile.TemporaryDirectory() as d, _oauth_env():
        cred = _cfg(Path(d)) / ".credentials.json"
        _write(cred, expires_at=NOW - 1)
        original = ca.credential_lock
        ca.credential_lock = lambda *a, **k: (_ for _ in ()).throw(OSError("disk on fire"))
        try:
            logged = []
            res = _ensure(cred, _Endpoint(), log=logged.append)
        finally:
            ca.credential_lock = original
        assert res["action"] == "failed" and "disk on fire" in res["reason"], res
        assert any("refresh on its own" in line for line in logged)


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception as exc:  # noqa: BLE001 - report and continue
            failed += 1
            print(f"FAIL {test.__name__}: {exc!r}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
