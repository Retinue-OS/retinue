#!/usr/bin/env python3
"""Checks the egress audit's TLS passthrough (EGRESS_AUDIT_TLS_PASSTHROUGH).

Some clients pin their provider's certificate and refuse the audit CA. The
passthrough list lets their TLS through unopened. What these pin down:

- `host` matches only that host; `*.domain` matches the domain and every
  subdomain, but not a look-alike (`notdomain`); case and a trailing dot
  do not matter; empty items are ignored;
- a listed host's connection is not intercepted, an unlisted one is;
- a passed-through connection is still logged, once, as a TUNNEL entry
  flagged `tls_passthrough`, with no path or content;
- the list is empty by default, so nothing passes through unless configured.

    python3 tests/test_egress_audit_passthrough.py
"""
import importlib.util
import json
import os
import sys
import tempfile
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ADDON = REPO_ROOT / "scripts" / "egress-audit-addon.py"


def _load_addon(passthrough: str | None, log_dir: str):
    if "mitmproxy" not in sys.modules:
        mitm = types.ModuleType("mitmproxy")
        mitm.http = types.SimpleNamespace(Headers=object, HTTPFlow=object)
        mitm.tls = types.SimpleNamespace(ClientHelloData=object)
        sys.modules["mitmproxy"] = mitm
    os.environ["EGRESS_AUDIT_LOG_DIR"] = log_dir
    if passthrough is None:
        os.environ.pop("EGRESS_AUDIT_TLS_PASSTHROUGH", None)
    else:
        os.environ["EGRESS_AUDIT_TLS_PASSTHROUGH"] = passthrough
    spec = importlib.util.spec_from_file_location("egress_audit_addon_pt", ADDON)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _hello(sni, address=("203.0.113.7", 443)):
    ns = types.SimpleNamespace
    return ns(
        client_hello=ns(sni=sni),
        context=ns(server=ns(address=address), client=ns(peername=("172.18.0.5", 40000))),
        ignore_connection=False,
    )


def _entries(log_dir):
    return [json.loads(line) for f in Path(log_dir).glob("*.ndjson") for line in open(f)]


def test_matching():
    addon = _load_addon(" *.storage.example, api.other.example ,,", tempfile.mkdtemp())
    cases = {
        "storage.example": True,
        "g.api.storage.example": True,
        "G.API.Storage.Example.": True,
        "notstorage.example": False,
        "api.other.example": True,
        "x.api.other.example": False,
        "example.com": False,
        "": False,
        None: False,
    }
    for host, expected in cases.items():
        assert addon.is_passthrough(host) is expected, (host, expected)


def test_listed_host_passes_and_is_logged():
    log_dir = tempfile.mkdtemp()
    addon = _load_addon("*.storage.example", log_dir).EgressAuditAddon()
    listed = _hello("eu.storage.example")
    addon.tls_clienthello(listed)
    assert listed.ignore_connection is True
    other = _hello("api.anthropic.com")
    addon.tls_clienthello(other)
    assert other.ignore_connection is False
    entries = _entries(log_dir)
    assert len(entries) == 1, entries
    e = entries[0]
    assert e["tls_passthrough"] is True and e["method"] == "TUNNEL"
    assert e["host"] == "eu.storage.example" and e["port"] == 443
    assert e["client_ip"] == "172.18.0.5"
    assert e["path"] == "" and e["request_body"]["size"] == 0


def test_no_sni_falls_back_to_address():
    log_dir = tempfile.mkdtemp()
    addon = _load_addon("203.0.113.7", log_dir).EgressAuditAddon()
    hello = _hello(None)
    addon.tls_clienthello(hello)
    assert hello.ignore_connection is True


def test_empty_by_default():
    log_dir = tempfile.mkdtemp()
    module = _load_addon(None, log_dir)
    hello = _hello("eu.storage.example")
    module.EgressAuditAddon().tls_clienthello(hello)
    assert hello.ignore_connection is False
    assert _entries(log_dir) == []


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
