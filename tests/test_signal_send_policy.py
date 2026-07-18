#!/usr/bin/env python3
"""Focused checks for the Signal outbound send-control (SIGNAL_SEND_POLICY).

Runnable without the heavy runtime dependencies of signal-gateway.py: the
`langdetect` import is stubbed so the module can be loaded in isolation. Exercises
policy-category resolution and the pending-send file store lifecycle.

    python3 tests/test_signal_send_policy.py
"""
import importlib.util
import json
import os
import sys
import tempfile
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_signal_gateway(send_policy, pending_dir, account=""):
    """Load scripts/signal-gateway.py with stubbed deps and the given config.

    `account` is this gateway's own sending number (SIGNAL_ACCOUNT); the
    send-control category is resolved from it, mirroring EMAIL_SEND_POLICY.
    """
    # Stub langdetect (not needed for the policy/store logic under test).
    if "langdetect" not in sys.modules:
        stub = types.ModuleType("langdetect")
        stub.detect = lambda *a, **k: "en"
        stub.detect_langs = lambda *a, **k: []
        stub.LangDetectException = type("LangDetectException", (Exception,), {})
        sys.modules["langdetect"] = stub

    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    os.environ["SIGNAL_SEND_POLICY"] = json.dumps(send_policy)
    os.environ["SIGNAL_ACCOUNT"] = account
    os.environ["SIGNAL_PENDING_SENDS_DIR"] = str(pending_dir)
    # Redirect writable dirs the module creates at import time into the sandbox.
    os.environ["PIPER_DATA_DIR"] = str(Path(pending_dir) / "models")
    os.environ["SIGNAL_ATTACHMENTS_DIR"] = str(Path(pending_dir) / "attachments")

    spec = importlib.util.spec_from_file_location(
        "signal_gateway_under_test", SCRIPTS_DIR / "signal-gateway.py"
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
        sg = _load_signal_gateway(_POLICY, tmp, account="+15551234567")
        assert sg._outbound_policy_category() == "verify"
    with tempfile.TemporaryDirectory() as tmp:
        # A gateway registered as a dedicated agent number → allow.
        sg = _load_signal_gateway(_POLICY, tmp, account="+15558888888")
        assert sg._outbound_policy_category() == "allow"
    with tempfile.TemporaryDirectory() as tmp:
        sg = _load_signal_gateway(_POLICY, tmp, account="+15559999999")
        assert sg._outbound_policy_category() == "trust"
    with tempfile.TemporaryDirectory() as tmp:
        # Normalization: spaces / tel: prefixes on the account resolve to the entry.
        sg = _load_signal_gateway(
            [{"number": "+15551234567", "category": "verify"}], tmp,
            account="tel:+1 555 123 4567",
        )
        assert sg._outbound_policy_category() == "verify"
    print("ok: category resolves from sending account")


def test_policy_default_verify_without_wildcard():
    with tempfile.TemporaryDirectory() as tmp:
        sg = _load_signal_gateway(
            [{"number": "+15558888888", "category": "allow"}], tmp, account="+15550000000",
        )
        # No wildcard and this account is unlisted → verify (fail-safe): an
        # undeclared account can never post autonomously. Same default as e-mail.
        assert sg._outbound_policy_category() == "verify"
    print("ok: default verify without wildcard")


def test_pending_send_store_lifecycle():
    with tempfile.TemporaryDirectory() as tmp:
        sg = _load_signal_gateway([{"number": "*", "category": "verify"}], tmp)

        # Record what _push would have sent instead of touching signal-cli.
        sent = []
        sg._push = lambda recipient, message, **kw: sent.append((recipient, message, kw))

        rid = sg._new_pending_send(
            "+15551234567", "hello", "en", images=[], voice=True, category="verify"
        )
        assert len(rid) == 32
        # Persisted to disk and listed as pending.
        assert (Path(tmp) / f"{rid}.json").exists()
        listed = sg._list_pending_sends_store()
        assert [e["id"] for e in listed] == [rid]
        # Lean listing omits image payloads.
        assert "images" not in listed[0]

        detail = sg._get_pending_send_detail(rid)
        assert detail["recipient"] == "+15551234567"
        assert detail["status"] == "pending"

        # Approving executes the send and flips the status.
        entry = sg._complete_pending_send(rid, approved=True)
        assert entry["status"] == "approved"
        assert sent == [("+15551234567", "hello", {"lang": "en", "images": [], "voice": True})]
        # No longer pending.
        assert sg._list_pending_sends_store() == []
        # Double-completion is a no-op (idempotent), does not resend.
        again = sg._complete_pending_send(rid, approved=True)
        assert again["status"] == "approved"
        assert len(sent) == 1
    print("ok: pending send store lifecycle (approve)")


def test_pending_send_reject_does_not_send():
    with tempfile.TemporaryDirectory() as tmp:
        sg = _load_signal_gateway([{"number": "*", "category": "verify"}], tmp)
        sent = []
        sg._push = lambda recipient, message, **kw: sent.append((recipient, message))

        rid = sg._new_pending_send(
            "+15551234567", "hello", None, images=[], voice=True, category="verify"
        )
        entry = sg._complete_pending_send(rid, approved=False)
        assert entry["status"] == "rejected"
        assert sent == []
        assert sg._list_pending_sends_store() == []
    print("ok: pending send reject does not send")


def test_unknown_request_id():
    with tempfile.TemporaryDirectory() as tmp:
        sg = _load_signal_gateway([], tmp)
        assert sg._get_pending_send_detail("0" * 32) is None
        assert sg._complete_pending_send("0" * 32, approved=True) is None
    print("ok: unknown request id handled")


def test_malformed_request_id_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        sg = _load_signal_gateway([], tmp)
        # Ids that are not 32-char lowercase hex must never reach the filesystem
        # (path-injection defense) and resolve to "not found". Grouped by class:
        bad_ids = [
            "../../etc/passwd", "..", "/etc/passwd",  # path traversal / separators
            "abc", "0" * 31, "0" * 33,                # wrong length
            "AAAA" + "0" * 28, "g" * 32,              # invalid characters
            "",                                       # empty
        ]
        for bad in bad_ids:
            assert sg._lookup_existing_path(bad) is None
            assert sg._get_pending_send_detail(bad) is None
            assert sg._complete_pending_send(bad, approved=True) is None
        # A well-formed id only resolves once its file actually exists.
        good = "a" * 32
        assert sg._lookup_existing_path(good) is None
        (Path(tmp) / f"{good}.json").write_text('{"status": "pending"}', encoding="utf-8")
        p = sg._lookup_existing_path(good)
        assert p is not None and p.parent == Path(tmp)
    print("ok: malformed request id rejected")


def main():
    test_category_resolves_from_sending_account()
    test_policy_default_verify_without_wildcard()
    test_pending_send_store_lifecycle()
    test_pending_send_reject_does_not_send()
    test_unknown_request_id()
    test_malformed_request_id_rejected()
    print("\nAll Signal send-policy checks passed.")


if __name__ == "__main__":
    main()
