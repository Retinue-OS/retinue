#!/usr/bin/env python3
"""Focused checks for the Telegram outbound send-control (TELEGRAM_SEND_POLICY).

The gateway talks to the Telegram Bot API over plain HTTP only inside its bridge
adapter, so the policy-category resolution and pending-send file store load and
run in isolation (no network, no bot token).

    python3 tests/test_telegram_send_policy.py
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_telegram_gateway(send_policy, pending_dir, account=""):
    """Load scripts/telegram-gateway.py with the given config in a sandbox.

    `account` is this gateway's own sending identity (TELEGRAM_ACCOUNT — the
    logged-in account); the send-control category is resolved from it, mirroring
    EMAIL_SEND_POLICY. Telethon is imported lazily by the gateway, so the policy
    and pending-store logic load here without it.
    """
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    os.environ["TELEGRAM_SEND_POLICY"] = json.dumps(send_policy) if send_policy is not None else ""
    os.environ["TELEGRAM_ACCOUNT"] = account
    os.environ["TELEGRAM_PENDING_SENDS_DIR"] = str(pending_dir)
    os.environ["TELEGRAM_DATA_DIR"] = str(Path(pending_dir) / "data")
    os.environ["TELEGRAM_TMP_DIR"] = str(Path(pending_dir) / "tmp")

    spec = importlib.util.spec_from_file_location(
        "telegram_gateway_under_test", SCRIPTS_DIR / "telegram-gateway.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The category is a property of the SENDING identity (this account), never the
# recipient chat — exactly like EMAIL_SEND_POLICY keys off the sender.
_POLICY = [
    {"number": "@me", "category": "verify"},          # the user's own account
    {"number": "@ara_agent", "category": "allow"},    # a dedicated agent account
]


def test_category_resolves_from_sending_account():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_telegram_gateway(_POLICY, tmp, account="@me")
        assert wg._outbound_policy_category() == "verify"
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_telegram_gateway(_POLICY, tmp, account="@ara_agent")
        assert wg._outbound_policy_category() == "allow"
    print("ok: category resolves from sending account")


def test_wildcard_applies_to_unlisted_account():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_telegram_gateway(
            [{"number": "@ara_agent", "category": "allow"}, {"number": "*", "category": "trust"}],
            tmp, account="@other",
        )
        assert wg._outbound_policy_category() == "trust"
    print("ok: wildcard applies to unlisted account")


def test_default_verify_without_wildcard():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_telegram_gateway(
            [{"number": "@ara_agent", "category": "allow"}], tmp, account="@other",
        )
        # No wildcard and this account is unlisted → verify (fail-safe). Same
        # default as e-mail.
        assert wg._outbound_policy_category() == "verify"
    print("ok: default verify without wildcard")


def test_default_verify_with_no_policy_or_account():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_telegram_gateway(None, tmp, account="")
        assert wg._outbound_policy_category() == "verify"
    print("ok: default verify with no policy or account")


def test_pending_send_store_lifecycle():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_telegram_gateway([{"number": "*", "category": "verify"}], tmp)

        # Record what _push would have sent instead of touching the Bot API.
        sent = []
        wg._push = lambda recipient, message, **kw: sent.append((recipient, message, kw))

        rid = wg._new_pending_send(
            "123456789", "hello", "en", images=[], voice=True, category="verify"
        )
        assert len(rid) == 32
        assert (Path(tmp) / f"{rid}.json").exists()
        listed = wg._list_pending_sends_store()
        assert [e["id"] for e in listed] == [rid]
        assert "images" not in listed[0]

        detail = wg._get_pending_send_detail(rid)
        assert detail["recipient"] == "123456789"
        assert detail["status"] == "pending"

        entry = wg._complete_pending_send(rid, approved=True)
        assert entry["status"] == "approved"
        assert sent == [("123456789", "hello", {"lang": "en", "images": [], "voice": True})]
        assert wg._list_pending_sends_store() == []
        again = wg._complete_pending_send(rid, approved=True)
        assert again["status"] == "approved"
        assert len(sent) == 1
    print("ok: pending send store lifecycle (approve)")


def test_pending_send_reject_does_not_send():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_telegram_gateway([{"number": "*", "category": "verify"}], tmp)
        sent = []
        wg._push = lambda recipient, message, **kw: sent.append((recipient, message))

        rid = wg._new_pending_send(
            "123456789", "hello", None, images=[], voice=True, category="verify"
        )
        entry = wg._complete_pending_send(rid, approved=False)
        assert entry["status"] == "rejected"
        assert sent == []
        assert wg._list_pending_sends_store() == []
    print("ok: pending send reject does not send")


def test_malformed_request_id_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_telegram_gateway([], tmp)
        bad_ids = [
            "../../etc/passwd", "..", "/etc/passwd",
            "abc", "0" * 31, "0" * 33,
            "AAAA" + "0" * 28, "g" * 32, "",
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
    test_wildcard_applies_to_unlisted_account()
    test_default_verify_without_wildcard()
    test_default_verify_with_no_policy_or_account()
    test_pending_send_store_lifecycle()
    test_pending_send_reject_does_not_send()
    test_malformed_request_id_rejected()
    print("\nAll Telegram send-policy checks passed.")


if __name__ == "__main__":
    main()
