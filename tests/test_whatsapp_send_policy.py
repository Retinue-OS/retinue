#!/usr/bin/env python3
"""Focused checks for the WhatsApp outbound send-control (WHATSAPP_SEND_POLICY).

Runnable without the WhatsApp bridge library: whatsapp-gateway.py imports neonize
lazily (only in the bridge-adapter functions), so the policy-category resolution
and the pending-send file store can be exercised in isolation.

    python3 tests/test_whatsapp_send_policy.py
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_whatsapp_gateway(send_policy, pending_dir, account=""):
    """Load scripts/whatsapp-gateway.py with the given config in a sandbox.

    `account` is this gateway's own sending number (WHATSAPP_ACCOUNT); the
    send-control category is resolved from it, mirroring EMAIL_SEND_POLICY.
    """
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    os.environ["WHATSAPP_SEND_POLICY"] = json.dumps(send_policy) if send_policy is not None else ""
    os.environ["WHATSAPP_ACCOUNT"] = account
    os.environ["WHATSAPP_PENDING_SENDS_DIR"] = str(pending_dir)
    # Redirect writable dirs the module creates at import time into the sandbox.
    os.environ["WHATSAPP_DATA_DIR"] = str(Path(pending_dir) / "data")
    os.environ["WHATSAPP_TMP_DIR"] = str(Path(pending_dir) / "tmp")

    spec = importlib.util.spec_from_file_location(
        "whatsapp_gateway_under_test", SCRIPTS_DIR / "whatsapp-gateway.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The category is a property of the SENDING identity (this gateway's own number),
# never the recipient — exactly like EMAIL_SEND_POLICY keys off the from-address.
_POLICY = [
    {"number": "+15551234567", "category": "verify"},  # the user's own number
    {"number": "+15559999999", "category": "trust"},   # a semi-trusted number
    {"number": "+15558888888", "category": "allow"},   # a dedicated agent number
]


def test_category_resolves_from_sending_account():
    with tempfile.TemporaryDirectory() as tmp:
        # A gateway linked as the user's own number → verify (needs approval).
        wg = _load_whatsapp_gateway(_POLICY, tmp, account="+15551234567")
        assert wg._outbound_policy_category() == "verify"
    with tempfile.TemporaryDirectory() as tmp:
        # A gateway linked as a dedicated agent number → allow (posts autonomously).
        wg = _load_whatsapp_gateway(_POLICY, tmp, account="+15558888888")
        assert wg._outbound_policy_category() == "allow"
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_whatsapp_gateway(_POLICY, tmp, account="+15559999999")
        assert wg._outbound_policy_category() == "trust"
    print("ok: category resolves from sending account")


def test_category_normalizes_account():
    with tempfile.TemporaryDirectory() as tmp:
        # Spaces / tel: prefixes on the configured account resolve to the entry.
        wg = _load_whatsapp_gateway(
            [{"number": "+15551234567", "category": "verify"}], tmp,
            account="tel:+1 555 123 4567",
        )
        assert wg._outbound_policy_category() == "verify"
    print("ok: category normalizes account")


def test_wildcard_applies_to_unlisted_account():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_whatsapp_gateway(
            [{"number": "+15551234567", "category": "verify"}, {"number": "*", "category": "allow"}],
            tmp, account="+15550000000",
        )
        # An account matching no explicit entry falls through to the wildcard.
        assert wg._outbound_policy_category() == "allow"
    print("ok: wildcard applies to unlisted account")


def test_default_verify_without_wildcard():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_whatsapp_gateway(
            [{"number": "+15558888888", "category": "allow"}], tmp, account="+15550000000",
        )
        # No wildcard and this account is unlisted → verify (fail-safe): an
        # undeclared account can never post autonomously. Same default as e-mail.
        assert wg._outbound_policy_category() == "verify"
    print("ok: default verify without wildcard")


def test_default_verify_with_no_policy_or_account():
    with tempfile.TemporaryDirectory() as tmp:
        # No policy and no account configured → verify.
        wg = _load_whatsapp_gateway(None, tmp, account="")
        assert wg._outbound_policy_category() == "verify"
    print("ok: default verify with no policy or account")


def test_pending_send_store_lifecycle():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_whatsapp_gateway([{"number": "*", "category": "verify"}], tmp)

        # Record what _push would have sent instead of touching the bridge.
        sent = []
        wg._push = lambda recipient, message, **kw: sent.append((recipient, message, kw))

        rid = wg._new_pending_send(
            "+15551234567", "hello", "en", images=[], voice=True, category="verify"
        )
        assert len(rid) == 32
        assert (Path(tmp) / f"{rid}.json").exists()
        listed = wg._list_pending_sends_store()
        assert [e["id"] for e in listed] == [rid]
        assert "images" not in listed[0]

        detail = wg._get_pending_send_detail(rid)
        assert detail["recipient"] == "+15551234567"
        assert detail["status"] == "pending"

        entry = wg._complete_pending_send(rid, approved=True)
        assert entry["status"] == "approved"
        assert sent == [("+15551234567", "hello", {"lang": "en", "images": [], "voice": True})]
        assert wg._list_pending_sends_store() == []
        # Double-completion is a no-op (idempotent), does not resend.
        again = wg._complete_pending_send(rid, approved=True)
        assert again["status"] == "approved"
        assert len(sent) == 1
    print("ok: pending send store lifecycle (approve)")


def test_pending_send_reject_does_not_send():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_whatsapp_gateway([{"number": "*", "category": "verify"}], tmp)
        sent = []
        wg._push = lambda recipient, message, **kw: sent.append((recipient, message))

        rid = wg._new_pending_send(
            "+15551234567", "hello", None, images=[], voice=True, category="verify"
        )
        entry = wg._complete_pending_send(rid, approved=False)
        assert entry["status"] == "rejected"
        assert sent == []
        assert wg._list_pending_sends_store() == []
    print("ok: pending send reject does not send")


def test_unknown_request_id():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_whatsapp_gateway([], tmp)
        assert wg._get_pending_send_detail("0" * 32) is None
        assert wg._complete_pending_send("0" * 32, approved=True) is None
    print("ok: unknown request id handled")


def test_malformed_request_id_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_whatsapp_gateway([], tmp)
        bad_ids = [
            "../../etc/passwd", "..", "/etc/passwd",  # path traversal / separators
            "abc", "0" * 31, "0" * 33,                # wrong length
            "AAAA" + "0" * 28, "g" * 32,              # invalid characters
            "",                                       # empty
        ]
        for bad in bad_ids:
            assert wg._lookup_existing_path(bad) is None
            assert wg._get_pending_send_detail(bad) is None
            assert wg._complete_pending_send(bad, approved=True) is None
        good = "a" * 32
        assert wg._lookup_existing_path(good) is None
        (Path(tmp) / f"{good}.json").write_text('{"status": "pending"}', encoding="utf-8")
        p = wg._lookup_existing_path(good)
        assert p is not None and p.parent == Path(tmp)
    print("ok: malformed request id rejected")


def main():
    test_category_resolves_from_sending_account()
    test_category_normalizes_account()
    test_wildcard_applies_to_unlisted_account()
    test_default_verify_without_wildcard()
    test_default_verify_with_no_policy_or_account()
    test_pending_send_store_lifecycle()
    test_pending_send_reject_does_not_send()
    test_unknown_request_id()
    test_malformed_request_id_rejected()
    print("\nAll WhatsApp send-policy checks passed.")


if __name__ == "__main__":
    main()
