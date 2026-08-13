#!/usr/bin/env python3
"""Focused checks for the triage delivery-gate policy library.

`scripts/triage_policy.py` is the single source of truth for who is worth a model
turn — e-mail whitelist (exact addresses + `*@domain` wildcards) and per-channel
messenger policy (whitelist / blacklist / group-block), all persisted as
deterministic N-Triples the gateways read raw and qlever indexes. This exercises
wildcard semantics (especially the freemail guarantee), the `.nt` round-trip,
write-if-changed, Sent-folder derivation, and handle classification.

Standalone, no third-party deps:

    python3 tests/test_triage_policy.py
"""
import importlib.util
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "triage_policy.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("triage_policy", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tp = _load_module()


def test_email_exact_and_wildcard():
    addresses = {"alice@gmail.com"}
    wildcards = {"*@factsmission.com", "*@*.epfl.ch"}
    # Exact match, case-insensitive.
    assert tp.email_whitelisted("Alice@Gmail.com", addresses, wildcards)
    # Freemail guarantee: another gmail address is NOT trusted by the exact entry.
    assert not tp.email_whitelisted("mallory@gmail.com", addresses, wildcards)
    # Plain domain wildcard.
    assert tp.email_whitelisted("bob@factsmission.com", addresses, wildcards)
    assert not tp.email_whitelisted("bob@evil.com", addresses, wildcards)
    # Subdomain wildcard covers both apex and any subdomain.
    assert tp.email_whitelisted("carol@epfl.ch", addresses, wildcards)
    assert tp.email_whitelisted("dan@cs.epfl.ch", addresses, wildcards)
    assert not tp.email_whitelisted("dan@notepfl.ch", addresses, wildcards)
    # Garbage inputs never match.
    assert not tp.email_whitelisted("", addresses, wildcards)
    assert not tp.email_whitelisted("no-at-sign", addresses, wildcards)
    print("PASS test_email_exact_and_wildcard")


def test_email_nt_roundtrip_and_determinism():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "gen" / "email-whitelist.nt"
        addresses = {"b@x.com", "a@x.com"}
        wildcards = {"*@z.com"}
        content = tp.render_email_whitelist(addresses, wildcards)
        # Deterministic: sorted, well-formed N-Triples, nested mkdir on write.
        lines = [l for l in content.splitlines() if l]
        assert lines == sorted(lines), "output not sorted"
        assert all(l.endswith(" .") for l in lines)
        assert tp.write_if_changed(content, path) is True
        assert path.parent.is_dir()
        got_addr, got_wild = tp.load_email_whitelist(path)
        assert got_addr == {a.lower() for a in addresses}
        assert got_wild == wildcards
    print("PASS test_email_nt_roundtrip_and_determinism")


def test_write_if_changed():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "p.nt"
        content = tp.render_email_whitelist({"a@x.com"}, set())
        assert tp.write_if_changed(content, path) is True
        mtime = path.stat().st_mtime_ns
        assert tp.write_if_changed(content, path) is False, "identical content rewrote"
        assert path.stat().st_mtime_ns == mtime, "file was touched"
        assert tp.write_if_changed(content + "\n", path) is True
    print("PASS test_write_if_changed")


def test_recipients_from_sent():
    messages = [
        {"to": "Alice <alice@x.com>, bob@y.com"},
        {"to": "carol@z.com", "cc": "dan@z.com", "bcc": "eve@w.com"},
        {"to": ""},          # empty header ignored
        {"subject": "no recipients"},
    ]
    got = tp.recipients_from_sent(messages)
    assert got == {"alice@x.com", "bob@y.com", "carol@z.com", "dan@z.com", "eve@w.com"}
    print("PASS test_recipients_from_sent")


def test_messenger_policy_roundtrip_and_classify():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "signal" / "policy.nt"
        wl = {"+41791112233"}
        bl = {"+41790000000"}
        grp = {"group.abc123=="}
        content = tp.render_messenger_policy("signal", wl, bl, grp)
        lines = [l for l in content.splitlines() if l]
        assert lines == sorted(lines), "output not sorted"
        tp.write_if_changed(content, path)
        gwl, gbl, ggrp = tp.load_messenger_policy("signal", path)
        assert gwl == wl and gbl == bl and ggrp == grp
        # Classification, with blacklist winning over whitelist.
        assert tp.handle_status("+41791112233", gwl, gbl) == "whitelisted"
        assert tp.handle_status("+41790000000", gwl, gbl) == "blacklisted"
        assert tp.handle_status("+41799999999", gwl, gbl) == "unknown"
        both_wl, both_bl = {"x"}, {"x"}
        assert tp.handle_status("x", both_wl, both_bl) == "blacklisted"
        # Group blocking.
        assert tp.group_blocked("group.abc123==", ggrp)
        assert not tp.group_blocked("group.other==", ggrp)
        assert not tp.group_blocked("", ggrp)
    print("PASS test_messenger_policy_roundtrip_and_classify")


def test_missing_file_is_empty():
    got_addr, got_wild = tp.load_email_whitelist(Path("/nonexistent/x.nt"))
    assert got_addr == set() and got_wild == set()
    wl, bl, grp = tp.load_messenger_policy("signal", Path("/nonexistent/x.nt"))
    assert wl == set() and bl == set() and grp == set()
    print("PASS test_missing_file_is_empty")


def test_literal_escaping_roundtrip():
    # A handle/label with quotes and a backslash must survive the .nt round-trip.
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "p.nt"
        weird = 'odd"name\\x'
        content = tp.render_messenger_policy("signal", {weird}, set(), set())
        tp.write_if_changed(content, path)
        wl, _bl, _grp = tp.load_messenger_policy("signal", path)
        assert wl == {weird.lower()}
    print("PASS test_literal_escaping_roundtrip")


if __name__ == "__main__":
    test_email_exact_and_wildcard()
    test_email_nt_roundtrip_and_determinism()
    test_write_if_changed()
    test_recipients_from_sent()
    test_messenger_policy_roundtrip_and_classify()
    test_missing_file_is_empty()
    test_literal_escaping_roundtrip()
    print("all triage_policy tests passed")
