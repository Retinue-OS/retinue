#!/usr/bin/env python3
"""Checks that the egress audit log never stores a credential.

The log is readable by the egress log viewer and the anomaly agent, and
Claude Code's OAuth refresh goes through the proxy, so its response body —
an access and a refresh token — used to land in the log in cleartext.
`scripts/egress-audit-addon.py` now redacts credentials in headers, query
strings, URL paths and bodies before writing. What these pin down:

- the OAuth token exchange, request and response, keeps no token value;
- a body cut off by the size limit mid-string is still redacted;
- form bodies, query strings and credential-named headers are covered;
- credential-shaped values are caught under names nobody listed;
- ordinary content is left alone, and the names of redacted fields stay
  visible, so the log still shows that a credential travelled.

    python3 tests/test_egress_audit_redaction.py
"""
import importlib.util
import json
import sys
import tempfile
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ADDON = REPO_ROOT / "scripts" / "egress-audit-addon.py"

ACCESS = "sk-ant-oat01-" + "A" * 40
REFRESH = "sk-ant-ort01-" + "B" * 40
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlLXZhbHVl"


def _load_addon():
    if "mitmproxy" not in sys.modules:
        mitm = types.ModuleType("mitmproxy")
        mitm.http = types.SimpleNamespace(Headers=object, HTTPFlow=object)
        sys.modules["mitmproxy"] = mitm
    spec = importlib.util.spec_from_file_location("egress_audit_addon", ADDON)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


addon = _load_addon()


class _Headers:
    def __init__(self, pairs):
        self.fields = [(k.encode(), v.encode()) for k, v in pairs]


def _flow(path, req_headers, req_body, resp_headers, resp_body):
    req = types.SimpleNamespace(
        method="POST", scheme="https", host="platform.claude.com", port=443,
        path=path, http_version="HTTP/1.1",
        headers=_Headers(req_headers), content=req_body)
    resp = types.SimpleNamespace(
        status_code=200, reason="OK", headers=_Headers(resp_headers), content=resp_body)
    return types.SimpleNamespace(
        request=req, response=resp, timestamp_start=None, timestamp_end=None,
        client_conn=types.SimpleNamespace(peername=("10.0.0.2", 5555)))


def _logged(flow):
    with tempfile.TemporaryDirectory() as tmp:
        addon.LOG_DIR = Path(tmp)
        addon.EgressAuditAddon().response(flow)
        (log,) = Path(tmp).glob("*.ndjson")
        line = log.read_text()
    return line, json.loads(line)


def test_the_oauth_refresh_keeps_no_token():
    req = json.dumps({"grant_type": "refresh_token", "refresh_token": REFRESH,
                      "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e"}).encode()
    resp = json.dumps({"token_type": "Bearer", "access_token": ACCESS,
                       "refresh_token": REFRESH, "expires_in": 28800,
                       "account": {"email_address": "user@example.com"}}).encode()
    line, entry = _logged(_flow("/v1/oauth/token", [("Content-Type", "application/json")],
                                req, [("Content-Type", "application/json")], resp))
    assert ACCESS not in line and REFRESH not in line, line
    body = json.loads(entry["response_body"]["text"])
    assert body["access_token"] == addon.REDACTED
    assert body["refresh_token"] == addon.REDACTED
    assert body["token_type"] == "Bearer" and body["expires_in"] == 28800
    # The grant type names a token but is not one: only the value of a
    # credential-named field goes, and "grant_type" is not such a name.
    assert json.loads(entry["request_body"]["text"])["grant_type"] == "refresh_token"


def test_a_truncated_body_is_still_redacted():
    text = addon._redact_text('{"refresh_token": "' + REFRESH[:20])
    assert REFRESH[:20] not in text, text
    assert '"refresh_token"' in text


def test_form_bodies_and_query_strings():
    form = f"grant_type=authorization_code&code=abc123&code_verifier=xyz789&client_secret=s3cr3t&redirect_uri=x"
    out = addon._redact_text(form)
    for secret in ("abc123", "xyz789", "s3cr3t"):
        assert secret not in out, out
    assert "grant_type=authorization_code" in out and "redirect_uri=x" in out
    line, entry = _logged(_flow("/search?q=weather&api_key=k3y&access_token=t0k", [], b"",
                                [], b""))
    assert "k3y" not in line and "t0k" not in line, line
    assert entry["query"].startswith("q=weather&")


def test_credential_headers():
    line, entry = _logged(_flow("/", [("x-api-key", "plainkey123"),
                                      ("Authorization", "Bearer abc"),
                                      ("X-Custom", "Bearer " + "Z" * 30),
                                      ("User-Agent", "claude-cli/2.0")],
                                b"", [("Set-Cookie", "sid=1")], b""))
    assert "plainkey123" not in line and "Z" * 30 not in line, line
    assert entry["request_headers"]["user-agent"] == "claude-cli/2.0"
    assert entry["response_headers"]["set-cookie"] == addon.REDACTED


def test_credential_shaped_values_under_unlisted_names():
    out = addon._redact_text(json.dumps({"blob": ACCESS, "jwt_like": JWT,
                                         "note": "see /bot123456789:" + "Q" * 35}))
    assert ACCESS not in out and JWT not in out and "Q" * 35 not in out, out
    line, _ = _logged(_flow("/bot123456789:" + "Q" * 35 + "/sendMessage", [], b"", [], b""))
    assert "Q" * 35 not in line, line


def test_ordinary_content_is_untouched():
    text = json.dumps({"model": "claude", "messages": [{"role": "user", "content": "hi"}],
                       "usage": {"input_tokens": 12, "output_tokens": 34}})
    assert addon._redact_text(text) == text


def main() -> int:
    test_the_oauth_refresh_keeps_no_token()
    test_a_truncated_body_is_still_redacted()
    test_form_bodies_and_query_strings()
    test_credential_headers()
    test_credential_shaped_values_under_unlisted_names()
    test_ordinary_content_is_untouched()
    print("all egress-audit redaction tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
