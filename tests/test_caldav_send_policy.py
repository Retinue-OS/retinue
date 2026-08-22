#!/usr/bin/env python3
"""Focused checks for the CalDAV outbound send-control (CALDAV_SEND_POLICY).

Runnable without the `caldav` package (it is import-guarded in the gateway
module itself — see scripts/caldav-gateway.py) or network access: exercises
policy-category resolution, the pending-send file store lifecycle, and the
event-datetime validation, all in isolation from the real CalDAV client.

    python3 tests/test_caldav_send_policy.py
"""
import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_caldav_gateway(send_policy, pending_dir, account=""):
    """Load scripts/caldav-gateway.py with the given config.

    `account` is this gateway's own sending identity (CALDAV_ACCOUNT); the
    send-control category is resolved from it, mirroring SIGNAL_ACCOUNT /
    EMAIL_SEND_POLICY.
    """
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    os.environ["CALDAV_SEND_POLICY"] = json.dumps(send_policy)
    os.environ["CALDAV_ACCOUNT"] = account
    os.environ.pop("SEND_APPROVAL_SLUG", None)
    os.environ["CALDAV_PENDING_SENDS_DIR"] = str(pending_dir)

    spec = importlib.util.spec_from_file_location(
        "caldav_gateway_under_test", SCRIPTS_DIR / "caldav-gateway.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The category is a property of the SENDING identity (this gateway's own
# configured calendar account), never any per-request field — exactly like
# EMAIL_SEND_POLICY keys off the from-address.
_POLICY = [
    {"account": "default", "category": "verify"},   # the user's own calendar
    {"account": "reminders", "category": "trust"},  # a semi-trusted calendar
    {"account": "agent", "category": "allow"},       # a dedicated agent calendar
]


def test_category_resolves_from_sending_account():
    with tempfile.TemporaryDirectory() as tmp:
        cg = _load_caldav_gateway(_POLICY, tmp, account="default")
        assert cg._outbound_policy_category() == "verify"
    with tempfile.TemporaryDirectory() as tmp:
        cg = _load_caldav_gateway(_POLICY, tmp, account="agent")
        assert cg._outbound_policy_category() == "allow"
    with tempfile.TemporaryDirectory() as tmp:
        cg = _load_caldav_gateway(_POLICY, tmp, account="reminders")
        assert cg._outbound_policy_category() == "trust"
    print("ok: category resolves from sending account")


def test_policy_default_verify_without_wildcard():
    with tempfile.TemporaryDirectory() as tmp:
        cg = _load_caldav_gateway(
            [{"account": "agent", "category": "allow"}], tmp, account="unlisted",
        )
        # No wildcard and this account is unlisted → verify (fail-safe): an
        # undeclared account can never write autonomously. Same default as
        # e-mail/Signal.
        assert cg._outbound_policy_category() == "verify"
    print("ok: default verify without wildcard")


def test_policy_wildcard_default():
    with tempfile.TemporaryDirectory() as tmp:
        cg = _load_caldav_gateway(
            [{"account": "*", "category": "allow"}], tmp, account="anything",
        )
        assert cg._outbound_policy_category() == "allow"
    print("ok: wildcard default applies to unlisted accounts")


def test_caldav_account_defaults_when_unset():
    with tempfile.TemporaryDirectory() as tmp:
        # CALDAV_ACCOUNT="" (unset) falls back to "default", same string a
        # policy entry can target explicitly.
        cg = _load_caldav_gateway(
            [{"account": "default", "category": "allow"}], tmp, account="",
        )
        assert cg.CALDAV_ACCOUNT == "default"
        assert cg._outbound_policy_category() == "allow"
    print("ok: CALDAV_ACCOUNT defaults to 'default' when unset")


def _wait_terminal(gw_module, rid, timeout=5.0):
    """Poll the event store until the background write records a terminal status."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        detail = gw_module._get_pending_send_detail(rid)
        if detail and detail.get("status") not in ("pending", "sending"):
            return detail
        time.sleep(0.01)
    raise AssertionError("event write never reached a terminal status")


def test_pending_event_store_lifecycle():
    with tempfile.TemporaryDirectory() as tmp:
        cg = _load_caldav_gateway([{"account": "*", "category": "verify"}], tmp)

        # Record what _create_event_on_server would have written instead of
        # touching a real CalDAV server.
        created = []

        def _fake_create(entry):
            created.append(entry)
            return "fake-uid-123"

        cg._create_event_on_server = _fake_create

        rid = cg._new_pending_send(
            "Dentist", "2026-09-03T14:00:00", "2026-09-03T14:30:00", False,
            "annual checkup", None, category="verify",
        )
        assert len(rid) == 32
        # Persisted to disk and listed as pending.
        assert (Path(tmp) / f"{rid}.json").exists()
        listed = cg._list_pending_sends_store()
        assert [e["id"] for e in listed] == [rid]
        assert listed[0]["subject"] == "Dentist"
        assert listed[0]["to"] == cg.CALDAV_ACCOUNT

        detail = cg._get_pending_send_detail(rid)
        assert detail["summary"] == "Dentist"
        assert detail["status"] == "pending"
        assert "2026-09-03T14:00:00" in detail["body"]

        # Approving executes the write and flips the status.
        # Approval is asynchronous (mirrors issue #116 for the messenger
        # gateways): the caller gets "sending" immediately; a background
        # thread executes the write and records the terminal status.
        entry = cg._complete_pending_send(rid, approved=True)
        assert entry["status"] == "sending"
        final = _wait_terminal(cg, rid)
        assert final["status"] == "approved"
        assert final["event_uid"] == "fake-uid-123"
        assert len(created) == 1 and created[0]["summary"] == "Dentist"
        # No longer pending.
        assert cg._list_pending_sends_store() == []
        # Re-completion after the fact is a no-op (idempotent), does not re-create.
        again = cg._complete_pending_send(rid, approved=True)
        assert again["status"] == "approved"
        assert len(created) == 1
    print("ok: pending event store lifecycle (approve)")


def test_pending_event_reject_does_not_create():
    with tempfile.TemporaryDirectory() as tmp:
        cg = _load_caldav_gateway([{"account": "*", "category": "verify"}], tmp)
        created = []
        cg._create_event_on_server = lambda entry: created.append(entry) or "uid"

        rid = cg._new_pending_send(
            "Dentist", "2026-09-03T14:00:00", "2026-09-03T14:30:00", False,
            "", None, category="verify",
        )
        entry = cg._complete_pending_send(rid, approved=False)
        assert entry["status"] == "rejected"
        assert created == []
        assert cg._list_pending_sends_store() == []
    print("ok: pending event reject does not create")


def test_unknown_request_id():
    with tempfile.TemporaryDirectory() as tmp:
        cg = _load_caldav_gateway([], tmp)
        assert cg._get_pending_send_detail("0" * 32) is None
        assert cg._complete_pending_send("0" * 32, approved=True) is None
    print("ok: unknown request id handled")


def test_malformed_request_id_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        cg = _load_caldav_gateway([], tmp)
        # Ids that are not 32-char lowercase hex must never reach the
        # filesystem (path-injection defense) and resolve to "not found".
        bad_ids = [
            "../../etc/passwd", "..", "/etc/passwd",  # path traversal / separators
            "abc", "0" * 31, "0" * 33,                # wrong length
            "AAAA" + "0" * 28, "g" * 32,              # invalid characters
            "",                                       # empty
        ]
        for bad in bad_ids:
            assert cg._lookup_existing_path(bad) is None
            assert cg._get_pending_send_detail(bad) is None
            assert cg._complete_pending_send(bad, approved=True) is None
        # A well-formed id only resolves once its file actually exists.
        good = "a" * 32
        assert cg._lookup_existing_path(good) is None
        (Path(tmp) / f"{good}.json").write_text('{"status": "pending"}', encoding="utf-8")
        p = cg._lookup_existing_path(good)
        assert p is not None and p.parent == Path(tmp)
    print("ok: malformed request id rejected")


def test_approval_slug_derived_from_host_header():
    with tempfile.TemporaryDirectory() as tmp:
        cg = _load_caldav_gateway(_POLICY, tmp)
        # The slug is the service hostname the caller reached this gateway at,
        # port stripped — the same name the web-gateway keys the gateway by
        # (see messenger_gateways.py), so the approval link resolves for any
        # account with no configuration.
        assert cg._approval_slug("caldav-gateway:8094") == "caldav-gateway"
        assert cg._approval_slug("caldav-gateway-personal:8094") == "caldav-gateway-personal"
        # No Host header (exotic client) → the channel-name fallback.
        assert cg._approval_slug(None) == "caldav"
        assert cg._approval_slug("") == "caldav"
    with tempfile.TemporaryDirectory() as tmp:
        # An explicit SEND_APPROVAL_SLUG still wins when a deployment sets one.
        os.environ["SEND_APPROVAL_SLUG"] = "my-caldav"
        try:
            cg = _load_caldav_gateway(_POLICY, tmp)
            os.environ["SEND_APPROVAL_SLUG"] = "my-caldav"  # loader popped it
            spec = importlib.util.spec_from_file_location(
                "caldav_gateway_slug_override", SCRIPTS_DIR / "caldav-gateway.py"
            )
            cg = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cg)
            assert cg._approval_slug("caldav-gateway-personal:8094") == "my-caldav"
        finally:
            os.environ.pop("SEND_APPROVAL_SLUG", None)
    print("ok: approval slug derived from Host header")


def test_event_datetime_validation():
    with tempfile.TemporaryDirectory() as tmp:
        cg = _load_caldav_gateway([], tmp)
        # Timed events.
        dt = cg._parse_event_datetime("2026-09-03T14:00:00", False)
        assert dt.year == 2026 and dt.month == 9 and dt.day == 3 and dt.hour == 14
        # A trailing "Z" (UTC) is accepted.
        dt_z = cg._parse_event_datetime("2026-09-03T14:00:00Z", False)
        assert dt_z.tzinfo is not None
        # All-day events parse just the date.
        d = cg._parse_event_datetime("2026-09-03", True)
        assert d.year == 2026 and d.month == 9 and d.day == 3
        # Invalid values raise ValueError (mapped to HTTP 400 by the handler).
        for bad in ("not-a-date", "", "2026-13-99"):
            try:
                cg._parse_event_datetime(bad, False)
            except ValueError:
                pass
            else:
                raise AssertionError(f"expected ValueError for {bad!r}")
    print("ok: event datetime validation")


def test_health_reports_unconfigured_by_default():
    with tempfile.TemporaryDirectory() as tmp:
        # No CALDAV_SERVER_URL/USERNAME/PASSWORD set by the loader helper.
        for key in ("CALDAV_SERVER_URL", "CALDAV_USERNAME", "CALDAV_PASSWORD"):
            os.environ.pop(key, None)
        cg = _load_caldav_gateway([], tmp)
        snapshot = cg._health_snapshot()
        assert snapshot["configured"] is False
        assert snapshot["status"] == "ok"
    print("ok: health reports unconfigured without credentials")


def main():
    test_category_resolves_from_sending_account()
    test_policy_default_verify_without_wildcard()
    test_policy_wildcard_default()
    test_caldav_account_defaults_when_unset()
    test_pending_event_store_lifecycle()
    test_pending_event_reject_does_not_create()
    test_unknown_request_id()
    test_malformed_request_id_rejected()
    test_approval_slug_derived_from_host_header()
    test_event_datetime_validation()
    test_health_reports_unconfigured_by_default()
    print("\nAll CalDAV send-policy checks passed.")


if __name__ == "__main__":
    main()
