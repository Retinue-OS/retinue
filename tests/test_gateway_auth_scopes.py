#!/usr/bin/env python3
"""Checks for host-scoped basic auth in the gateway's forward-auth endpoint.

Several Traefik routers share the one ``/auth`` endpoint, so a credential valid
anywhere used to be valid everywhere. ``GATEWAY_BASIC_AUTH_SCOPES`` confines a
named user to named hosts, which is what makes it safe to hand an MCP connector
its own password. These checks pin that behaviour, and in particular the
backward-compatibility rule that an *unnamed* user stays unrestricted.

    python3 tests/test_gateway_auth_scopes.py
"""
import importlib.util
import base64
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

spec = importlib.util.spec_from_file_location(
    "gateway_auth", SCRIPTS_DIR / "gateway_auth.py"
)
ga = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ga)

# openssl passwd -apr1 -salt abcdefgh secret
APR1_SECRET = "$apr1$abcdefgh$h9FWgUz3n9YxylKLlR5SQ/"
CERT_HEADER = "X-Forwarded-Tls-Client-Cert"
CERT_INFO_HEADER = "X-Forwarded-Tls-Client-Cert-Info"


def basic(user, password):
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return f"Basic {token}"


def decide(headers, users, **kw):
    kw.setdefault("cert_header", CERT_HEADER)
    kw.setdefault("cert_info_header", CERT_INFO_HEADER)
    return ga.decide(headers, users, **kw)


def test_apr1_vector():
    """The pure-stdlib apr1 implementation matches openssl's output."""
    assert ga.apr1_crypt("secret", "abcdefgh") == APR1_SECRET
    assert ga.verify_password("secret", APR1_SECRET)
    assert not ga.verify_password("wrong", APR1_SECRET)
    print("ok: apr1 matches the openssl reference vector")


def test_check_basic_auth_returns_username():
    """The username is what lets the caller apply per-user scoping."""
    users = {"owner": APR1_SECRET}
    assert ga.check_basic_auth(basic("owner", "secret"), users) == "owner"
    # Every failure mode returns None, never a truthy value.
    assert ga.check_basic_auth(basic("owner", "wrong"), users) is None
    assert ga.check_basic_auth(basic("nobody", "secret"), users) is None
    assert ga.check_basic_auth("", users) is None
    assert ga.check_basic_auth("Bearer xyz", users) is None
    assert ga.check_basic_auth("Basic !!!not-base64!!!", users) is None
    assert ga.check_basic_auth(basic("owner", "secret"), {}) is None
    print("ok: check_basic_auth returns the user name, None on any failure")


def test_load_scopes():
    scopes = ga.load_scopes(
        "ara-mcp:ara.example.com,ara-mcp:Alt.Example.COM\nbot:*\n# comment\n\njunk"
    )
    assert scopes == {
        "ara-mcp": {"ara.example.com", "alt.example.com"},
        "bot": {"*"},
    }, scopes
    assert ga.load_scopes("") == {}
    print("ok: scopes parse, lowercase, and accept repeated users")


def test_normalize_host():
    assert ga._normalize_host("Agents.Example.COM:8443") == "agents.example.com"
    # Only the first hop of a comma-joined X-Forwarded-Host is client-facing.
    assert ga._normalize_host("front.example.com, inner:8080") == "front.example.com"
    assert ga._normalize_host("[2001:db8::1]:443") == "[2001:db8::1]"
    assert ga._normalize_host("") == ""
    print("ok: host normalization strips port, case and proxy hops")


def test_unscoped_user_is_unrestricted():
    """Backward compatibility: absence from the map means no restriction."""
    scopes = ga.load_scopes("ara-mcp:ara.example.com")
    assert ga.host_allowed("owner", "agents.example.com", scopes)
    assert ga.host_allowed("owner", "anything.at.all", scopes)
    assert ga.host_allowed("owner", "", scopes)
    assert ga.host_allowed("owner", "x", {})
    print("ok: a user with no scope entry keeps unrestricted access")


def test_scoped_user_confined_to_its_hosts():
    scopes = ga.load_scopes("ara-mcp:ara.example.com")
    assert ga.host_allowed("ara-mcp", "ara.example.com", scopes)
    assert ga.host_allowed("ara-mcp", "ARA.example.com:443", scopes)
    assert not ga.host_allowed("ara-mcp", "agents.example.com", scopes)
    assert not ga.host_allowed("ara-mcp", "", scopes)
    # An explicit wildcard is allowed, so an entry can be present but open.
    assert ga.host_allowed("bot", "whatever.example.com", ga.load_scopes("bot:*"))
    print("ok: a scoped user is confined to its listed hosts")


def test_decide_scoped_credential_on_wrong_host_is_403():
    """Right password, wrong router: refuse rather than re-prompt.

    A 401 would only make the browser offer the same correct credential again,
    which loops.
    """
    users = {"ara-mcp": APR1_SECRET}
    scopes = ga.load_scopes("ara-mcp:ara.example.com")
    auth = basic("ara-mcp", "secret")

    status, _ = decide(
        {"Authorization": auth, "X-Forwarded-Host": "ara.example.com"},
        users, scopes=scopes,
    )
    assert status == 200, status

    status, extra = decide(
        {"Authorization": auth, "X-Forwarded-Host": "agents.example.com"},
        users, scopes=scopes,
    )
    assert status == 403, status
    assert "WWW-Authenticate" not in extra
    print("ok: scoped credential is 200 in scope, 403 out of scope")


def test_decide_falls_back_to_host_header():
    """Without X-Forwarded-Host the plain Host header decides."""
    users = {"ara-mcp": APR1_SECRET}
    scopes = ga.load_scopes("ara-mcp:ara.example.com")
    auth = basic("ara-mcp", "secret")
    assert decide({"Authorization": auth, "Host": "ara.example.com"},
                  users, scopes=scopes)[0] == 200
    assert decide({"Authorization": auth, "Host": "agents.example.com"},
                  users, scopes=scopes)[0] == 403
    print("ok: Host is used when X-Forwarded-Host is absent")


def test_decide_no_credential_still_challenges():
    users = {"owner": APR1_SECRET}
    status, extra = decide({"X-Forwarded-Host": "agents.example.com"}, users,
                           scopes=ga.load_scopes("ara-mcp:ara.example.com"),
                           realm="Retinue")
    assert status == 401, status
    assert extra["WWW-Authenticate"] == 'Basic realm="Retinue"'
    # A wrong password is a challenge too, not a 403 — 403 is reserved for
    # "correct credential, wrong router".
    assert decide({"Authorization": basic("owner", "nope")}, users)[0] == 401
    print("ok: missing or wrong credentials still get a 401 challenge")


def test_certificate_is_never_scoped():
    """The client certificate is the owner's own credential; scoping is for
    passwords handed to third parties."""
    scopes = ga.load_scopes("ara-mcp:ara.example.com")
    status, _ = decide(
        {CERT_HEADER: "-----BEGIN CERTIFICATE-----\nx\n",
         CERT_INFO_HEADER: 'Subject="CN=owner"',
         "X-Forwarded-Host": "anything.example.com"},
        {}, scopes=scopes, allowed_cn="owner",
    )
    assert status == 200, status
    # A CN restriction still applies, and still refuses rather than re-prompts.
    status, _ = decide(
        {CERT_HEADER: "-----BEGIN CERTIFICATE-----\nx\n",
         CERT_INFO_HEADER: 'Subject="CN=someone-else"'},
        {}, scopes=scopes, allowed_cn="owner",
    )
    assert status == 403, status
    print("ok: certificates bypass host scoping, CN check still enforced")


def test_config_from_env():
    keys = ["GATEWAY_BASIC_AUTH_SCOPES", "GATEWAY_BASIC_AUTH_USERS",
            "TRAEFIK_BASIC_AUTH_USERS", "GATEWAY_FORWARDED_HOST_HEADER"]
    saved = {k: os.environ.get(k) for k in keys}
    try:
        for k in keys:
            os.environ.pop(k, None)
        os.environ["GATEWAY_BASIC_AUTH_USERS"] = f"ara-mcp:{APR1_SECRET}"
        os.environ["GATEWAY_BASIC_AUTH_SCOPES"] = "ara-mcp:ara.example.com"
        cfg = ga.config_from_env()
        assert cfg["users"] == {"ara-mcp": APR1_SECRET}
        assert cfg["scopes"] == {"ara-mcp": {"ara.example.com"}}
        assert cfg["host_header"] == "X-Forwarded-Host"
        # Unset scopes must yield an empty map — i.e. nothing restricted.
        os.environ.pop("GATEWAY_BASIC_AUTH_SCOPES")
        assert ga.config_from_env()["scopes"] == {}
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("ok: config_from_env reads scopes and defaults to unrestricted")


def main():
    test_apr1_vector()
    test_check_basic_auth_returns_username()
    test_load_scopes()
    test_normalize_host()
    test_unscoped_user_is_unrestricted()
    test_scoped_user_confined_to_its_hosts()
    test_decide_scoped_credential_on_wrong_host_is_403()
    test_decide_falls_back_to_host_header()
    test_decide_no_credential_still_challenges()
    test_certificate_is_never_scoped()
    test_config_from_env()
    print("\nAll gateway auth scoping checks passed.")


if __name__ == "__main__":
    sys.exit(main())
