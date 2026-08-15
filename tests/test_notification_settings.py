#!/usr/bin/env python3
"""Notification-preference tests (#66): subscription storage, notify()'s
per-device filtering, and the gateway's event-mode classification.

The classification tests load scripts/web-gateway.py the same way
test_web_gateway_models.py does, because the previous rounds of this feature
regressed exactly there — the filter worked, but every real push was classified
"new", so nothing was ever filtered. The store/filter tests force the module's
internals on instead of calling init(), so they run even where pywebpush (or a
working cryptography build) is unavailable; `webpush` itself is always mocked.

    python3 tests/test_notification_settings.py
"""
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import push_notify  # noqa: E402


# ── helpers ────────────────────────────────────────────────────────────────

def _force_store(tmp: Path) -> None:
    """Point the module at a temp store without needing pywebpush/VAPID keys."""
    push_notify._AVAILABLE = True
    push_notify._state_dir = tmp
    push_notify._public_key_b64 = "test-key"
    (tmp / "subscriptions").mkdir(parents=True, exist_ok=True)


def _sub_record(endpoint: str, **prefs) -> dict:
    return {"endpoint": endpoint,
            "keys": {"p256dh": "abc", "auth": "123"}, **prefs}


def _write_sub(tmp: Path, endpoint: str, **prefs) -> None:
    digest = hashlib.sha256(endpoint.encode()).hexdigest()
    path = tmp / "subscriptions" / f"{digest}.json"
    path.write_text(json.dumps(_sub_record(endpoint, **prefs)), encoding="utf-8")


def _read_sub(tmp: Path, endpoint: str) -> dict:
    digest = hashlib.sha256(endpoint.encode()).hexdigest()
    return json.loads((tmp / "subscriptions" / f"{digest}.json").read_text(encoding="utf-8"))


def _sent_endpoints(**notify_kwargs) -> set[str]:
    """Run notify() with webpush mocked; return which endpoints were pushed."""
    with patch.object(push_notify, "webpush", create=True) as mock_webpush:
        push_notify.notify(title="T", body="B", url="/", tag="t", **notify_kwargs)
        return {c.kwargs["subscription_info"]["endpoint"]
                for c in mock_webpush.call_args_list}


# ── subscription storage ───────────────────────────────────────────────────

def test_subscribe_stores_preferences():
    with tempfile.TemporaryDirectory() as tmp:
        _force_store(Path(tmp))
        sub = {"endpoint": "https://push.example/a",
               "keys": {"p256dh": "abc", "auth": "123"}}
        assert push_notify.subscribe({"subscription": sub,
                                      "notification_mode": "new_only",
                                      "notify_archived": False}) is True
        record = _read_sub(Path(tmp), sub["endpoint"])
        assert record["notification_mode"] == "new_only"
        assert record["notify_archived"] is False
        assert record["keys"]["p256dh"] == "abc"


def test_subscribe_defaults_and_coercion():
    with tempfile.TemporaryDirectory() as tmp:
        _force_store(Path(tmp))
        sub = {"endpoint": "https://push.example/b",
               "keys": {"p256dh": "abc", "auth": "123"}}
        # An unknown mode from the client is coerced, never stored verbatim;
        # notify_archived defaults to True (#66: archived notifies by default).
        assert push_notify.subscribe({"subscription": sub,
                                      "notification_mode": "definitely_bogus"}) is True
        record = _read_sub(Path(tmp), sub["endpoint"])
        assert record["notification_mode"] == "all"
        assert record["notify_archived"] is True


def test_subscribe_raw_legacy_payload():
    with tempfile.TemporaryDirectory() as tmp:
        _force_store(Path(tmp))
        sub = {"endpoint": "https://push.example/c",
               "keys": {"p256dh": "abc", "auth": "123"}}
        assert push_notify.subscribe(sub) is True
        record = _read_sub(Path(tmp), sub["endpoint"])
        assert "notification_mode" not in record  # legacy record: treated as "all"


# ── notify() filtering ─────────────────────────────────────────────────────

def test_notify_event_filter_matrix():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _force_store(tmp_path)
        for mode in ("all", "new_only", "stalled_only", "new_and_stalled", "off"):
            _write_sub(tmp_path, f"https://push.example/{mode}", notification_mode=mode)
        _write_sub(tmp_path, "https://push.example/legacy")  # no mode stored

        def eps(*modes):
            return {f"https://push.example/{m}" for m in modes}

        assert _sent_endpoints(mode="new") == eps("all", "new_only", "new_and_stalled", "legacy")
        assert _sent_endpoints(mode="stalled") == eps("all", "stalled_only", "new_and_stalled", "legacy")
        # An ordinary reply in an active exchange only reaches "all"-mode devices.
        assert _sent_endpoints(mode="reply") == eps("all", "legacy")
        # An unclassified push (legacy caller) reaches everyone not switched off.
        assert _sent_endpoints(mode=None) == eps("all", "new_only", "stalled_only",
                                                 "new_and_stalled", "legacy")


def test_notify_archived_optout():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _force_store(tmp_path)
        _write_sub(tmp_path, "https://push.example/optout",
                   notification_mode="all", notify_archived=False)
        _write_sub(tmp_path, "https://push.example/default",
                   notification_mode="all")

        # Archived thread: the opted-out device is skipped, the default
        # (no notify_archived on record) still notifies — #66's default.
        assert _sent_endpoints(mode="reply", archived=True) == {"https://push.example/default"}
        # Non-archived thread: the opt-out is irrelevant.
        assert _sent_endpoints(mode="reply", archived=False) == {
            "https://push.example/optout", "https://push.example/default"}


# ── the gateway's event-mode classification ────────────────────────────────

def _load_gateway(tmp: Path):
    """Load scripts/web-gateway.py with sandboxed state, like the other tests."""
    os.environ["CONVERSATIONS_DIR"] = str(tmp / "convs")
    os.environ["CONVERSATION_DIR"] = str(tmp / "convlog")
    os.environ["CHAMBERS_DIR"] = str(tmp / "chambers")
    os.environ["WEB_GATEWAY_STATE"] = str(tmp / "state.json")
    (tmp / "chambers").mkdir(parents=True, exist_ok=True)
    if "markdown_it" not in sys.modules:
        try:
            import markdown_it  # noqa: F401
        except ImportError:
            stub = types.ModuleType("markdown_it")

            class _MarkdownIt:
                def __init__(self, *args, **kwargs):
                    pass

                def enable(self, *args, **kwargs):
                    return self

                def render(self, *args, **kwargs):
                    return ""

            stub.MarkdownIt = _MarkdownIt
            sys.modules["markdown_it"] = stub
    spec = importlib.util.spec_from_file_location(
        "web_gateway_notifications_under_test", SCRIPTS_DIR / "web-gateway.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _iso(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def _conv(initiator: str, message_ages_minutes, read_at_minutes=None) -> dict:
    conv = {
        "id": "cid", "initiator": initiator,
        "messages": [{"role": "agent", "text": "m", "ts": _iso(age)}
                     for age in message_ages_minutes],
    }
    if read_at_minutes is not None:
        conv["read_at"] = _iso(read_at_minutes)
    return conv


def run_event_mode_tests(wg) -> None:
    # First message of an agent-opened thread is the one "new" event.
    assert wg._conv_event_mode(_conv("agent", [0])) == "new"
    # A user-opened thread never produces a "new" event.
    assert wg._conv_event_mode(_conv("user", [0])) == "reply"
    # Second and later turns of an active exchange are replies, not "new" —
    # the regression each previous round of this feature reintroduced.
    assert wg._conv_event_mode(_conv("agent", [2, 0])) == "reply"
    assert wg._conv_event_mode(_conv("agent", [5, 2, 0], read_at_minutes=4)) == "reply"
    # No activity for >10 min before this message → stalled …
    assert wg._conv_event_mode(_conv("agent", [11, 0])) == "stalled"
    assert wg._conv_event_mode(_conv("user", [30, 11, 0], read_at_minutes=25)) == "stalled"
    # … but a recent read keeps the thread active even if the last message is old.
    assert wg._conv_event_mode(_conv("agent", [11, 0], read_at_minutes=2)) == "reply"
    # Unparseable/missing anchors degrade to the quietest class.
    legacy = {"id": "cid", "initiator": "agent",
              "messages": [{"role": "agent", "text": "a"}, {"role": "agent", "text": "b"}]}
    assert wg._conv_event_mode(legacy) == "reply"


def test_conv_event_mode():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp))
        run_event_mode_tests(wg)


# ── runner ─────────────────────────────────────────────────────────────────

TESTS = [
    test_subscribe_stores_preferences,
    test_subscribe_defaults_and_coercion,
    test_subscribe_raw_legacy_payload,
    test_notify_event_filter_matrix,
    test_notify_archived_optout,
    test_conv_event_mode,
]

if __name__ == "__main__":
    failed = False
    for test in TESTS:
        try:
            test()
            print(f"✓ {test.__name__}")
        except Exception:  # noqa: BLE001
            failed = True
            print(f"✗ {test.__name__}")
            import traceback
            traceback.print_exc()
    sys.exit(1 if failed else 0)
