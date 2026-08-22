#!/usr/bin/env python3
"""Checks for the Claude OAuth credential module (scripts/claude_auth.py).

Exercises the pure parts — credential-state classification against the
entrypoint's backup/marker protocol, the PKCE authorize URL, pasted-code
parsing, and the token-install merge — on temp files; no network and no
Claude session is needed.

    python3 tests/test_claude_auth.py
"""
import base64
import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
import urllib.parse
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
        cred = tmp / ".credentials.json"
        # Simulate the dead state: old file with extra keys, rejected backup.
        cred.write_text(json.dumps({
            "claudeAiOauth": {"accessToken": "old", "refreshToken": "old-rt",
                              "expiresAt": 1, "subscriptionType": "max",
                              "rateLimitTier": "t1"},
            "someOtherSection": {"kept": True},
        }))
        (tmp / ".credentials.json.bak").write_text("{}")
        (tmp / ".credentials.json.restored-expiry").write_text("1")

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
        assert json.loads((tmp / ".credentials.json.bak").read_text()) == data
        assert not (tmp / ".credentials.json.restored-expiry").exists()
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
