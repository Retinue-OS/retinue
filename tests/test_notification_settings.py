#!/usr/bin/env python3
"""Notification-preference tests: subscription storage and notify()'s fan-out.

Since the per-device notification modes were removed, whether a push goes out
is the attention model's decision alone (`push_notify.DEFAULT_PREFERENCES`):
`notify()` reaches every subscribed device, a record left over from #66's
modes included, and the only per-device preference left is the opt-out of
archived conversations. The tests force the module's internals on instead of
calling init(), so they run even where pywebpush (or a working cryptography
build) is unavailable; `webpush` itself is always mocked.

    python3 tests/test_notification_settings.py
"""
import hashlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import types
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
                                      "notify_archived": False}) is True
        record = _read_sub(Path(tmp), sub["endpoint"])
        assert record["notify_archived"] is False
        assert record["keys"]["p256dh"] == "abc"


def test_subscribe_defaults_and_drops_the_retired_mode():
    with tempfile.TemporaryDirectory() as tmp:
        _force_store(Path(tmp))
        sub = {"endpoint": "https://push.example/b",
               "keys": {"p256dh": "abc", "auth": "123"}}
        # notify_archived defaults to True (archived notifies by default); an
        # older client still sending a notification_mode has it dropped, not
        # stored — nothing reads it any more.
        assert push_notify.subscribe({"subscription": sub,
                                      "notification_mode": "new_and_stalled"}) is True
        record = _read_sub(Path(tmp), sub["endpoint"])
        assert "notification_mode" not in record
        assert record["notify_archived"] is True


def test_subscribe_raw_legacy_payload():
    with tempfile.TemporaryDirectory() as tmp:
        _force_store(Path(tmp))
        sub = {"endpoint": "https://push.example/c",
               "keys": {"p256dh": "abc", "auth": "123"}}
        assert push_notify.subscribe(sub) is True
        record = _read_sub(Path(tmp), sub["endpoint"])
        assert "notify_archived" not in record  # unwrapped: the default applies


# ── notify()'s fan-out ─────────────────────────────────────────────────────

def test_notify_reaches_every_subscribed_device():
    """No per-device event filter is left: a stored mode from #66 — 'off'
    included — no longer keeps a device quiet. That filter sat behind the
    attention model and dropped what the model had released."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _force_store(tmp_path)
        for mode in ("all", "new_only", "stalled_only", "new_and_stalled", "off"):
            _write_sub(tmp_path, f"https://push.example/{mode}", notification_mode=mode)
        _write_sub(tmp_path, "https://push.example/plain")  # nothing stored

        assert _sent_endpoints() == {
            f"https://push.example/{m}" for m in
            ("all", "new_only", "stalled_only", "new_and_stalled", "off", "plain")}


def test_notify_archived_optout():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _force_store(tmp_path)
        _write_sub(tmp_path, "https://push.example/optout", notify_archived=False)
        _write_sub(tmp_path, "https://push.example/default")

        # Archived thread: the opted-out device is skipped, the default
        # (no notify_archived on record) still notifies.
        assert _sent_endpoints(archived=True) == {"https://push.example/default"}
        # Non-archived thread: the opt-out is irrelevant.
        assert _sent_endpoints(archived=False) == {
            "https://push.example/optout", "https://push.example/default"}


# ── the gateway's push helpers ─────────────────────────────────────────────

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


# ── #61: the subscriber count an agent-opened thread reports ────────────────
#
# _push_conv_notification() used to return nothing, so an agent-opened thread
# with zero subscribed devices reported success identically to one that
# actually notified someone. These check the count is now surfaced both from
# the function itself and from the two /internal/conversations POST handlers'
# JSON responses (conversation-push.py's warning reads the latter).

def _fake_agent_request(wg, payload: dict, token: str):
    """A stand-in for the request handler, just enough for
    _agent_conversation_payload()/_read_json_body() and a captured
    _send_json() — no real socket I/O involved."""
    body = json.dumps(payload).encode("utf-8")
    fake = types.SimpleNamespace(
        headers={"Content-Length": str(len(body)),
                 "X-Conversation-Backend-Token": token},
        rfile=io.BytesIO(body),
        status=None,
        response_body=None,
    )

    def _send_json(status, resp_body):
        fake.status = status
        fake.response_body = resp_body

    fake._send_json = _send_json
    fake._read_json_body = lambda: wg.Handler._read_json_body(fake)
    fake._agent_conversation_payload = lambda: wg.Handler._agent_conversation_payload(fake)
    return fake


def test_agent_conversation_reports_zero_subscribers():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        os.environ["CONVERSATION_BACKEND_TOKEN"] = "test-token"
        wg = _load_gateway(tmp_path / "gw")
        _force_store(tmp_path / "push")  # enabled(), zero subscriptions

        fake = _fake_agent_request(wg, {"message": "decide something"}, "test-token")
        wg.Handler._handle_agent_conversation(fake)
        assert fake.status == 201, fake.status
        assert fake.response_body["push_subscribers"] == 0, fake.response_body


def test_agent_conversation_reports_real_subscriber_count():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        os.environ["CONVERSATION_BACKEND_TOKEN"] = "test-token"
        wg = _load_gateway(tmp_path / "gw")
        push_dir = tmp_path / "push"
        _force_store(push_dir)
        _write_sub(push_dir, "https://push.example/one")
        _write_sub(push_dir, "https://push.example/two")

        fake = _fake_agent_request(wg, {"message": "decide something"}, "test-token")
        # The count is what's under test, not delivery — stand in for the
        # actual fan-out so the background thread has nothing real to call
        # (pywebpush is unavailable in this sandbox; see test_push_notify.py).
        with patch.object(push_notify, "notify_async"):
            wg.Handler._handle_agent_conversation(fake)
        assert fake.response_body["push_subscribers"] == 2, fake.response_body


def test_agent_conversation_quiet_omits_push_subscribers():
    """A quiet thread (the cowork audit trail) never calls push_notify, so the
    field it would report is simply absent — not a misleading zero."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        os.environ["CONVERSATION_BACKEND_TOKEN"] = "test-token"
        wg = _load_gateway(tmp_path / "gw")
        _force_store(tmp_path / "push")

        fake = _fake_agent_request(
            wg, {"message": "logged", "kind": "cowork", "quiet": True}, "test-token")
        wg.Handler._handle_agent_conversation(fake)
        assert fake.status == 201, fake.status
        assert "push_subscribers" not in fake.response_body, fake.response_body


def test_agent_conversation_message_reports_subscriber_count():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        os.environ["CONVERSATION_BACKEND_TOKEN"] = "test-token"
        wg = _load_gateway(tmp_path / "gw")
        push_dir = tmp_path / "push"
        _force_store(push_dir)
        _write_sub(push_dir, "https://push.example/one")

        opened = wg._new_conv("agent", "Web", None, "agent", "first message")
        fake = _fake_agent_request(wg, {"message": "a follow-up"}, "test-token")
        with patch.object(push_notify, "notify_async"):
            wg.Handler._handle_agent_conversation_message(fake, opened["id"])
        assert fake.status == 201, fake.status
        assert fake.response_body["push_subscribers"] == 1, fake.response_body


def test_push_conv_notification_return_value():
    """The function itself: 0 with no subscribers, the real count otherwise —
    checked directly since it's what both handlers above now report."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        wg = _load_gateway(tmp_path / "gw")
        push_dir = tmp_path / "push"
        _force_store(push_dir)
        conv = {"id": "c" * 32, "title": "T", "initiator": "agent", "messages": []}
        assert wg._push_conv_notification(conv, "hello") == 0
        _write_sub(push_dir, "https://push.example/x")
        with patch.object(push_notify, "notify_async"):
            assert wg._push_conv_notification(conv, "hello") == 1


# ── runner ─────────────────────────────────────────────────────────────────

TESTS = [
    test_subscribe_stores_preferences,
    test_subscribe_defaults_and_drops_the_retired_mode,
    test_subscribe_raw_legacy_payload,
    test_notify_reaches_every_subscribed_device,
    test_notify_archived_optout,
    test_agent_conversation_reports_zero_subscribers,
    test_agent_conversation_reports_real_subscriber_count,
    test_agent_conversation_quiet_omits_push_subscribers,
    test_agent_conversation_message_reports_subscriber_count,
    test_push_conv_notification_return_value,
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
