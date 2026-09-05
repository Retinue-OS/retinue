#!/usr/bin/env python3
"""Checks for the messenger-gateway connection monitor.

Exercises the pure parts — health-verdict classification, the per-gateway
outage state machine, and the shared gateway discovery — with a fake notifier;
no gateway service and no HTTP is needed.

    python3 tests/test_gateway_monitor.py
"""
import importlib.util
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_monitor():
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "gateway_monitor_under_test", SCRIPTS_DIR / "gateway-monitor.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gm = _load_monitor()


class FakeNotifier:
    """Records notification calls; can be told to fail (backend down)."""

    def __init__(self, fail=False):
        self.fail = fail
        self.opened = []    # (title, message)
        self.appended = []  # (thread_id, message)
        self._next_id = 0

    def open_thread(self, title, message, attention=None):
        if self.fail:
            return None
        self.opened.append((title, message))
        self._next_id += 1
        return f"thread{self._next_id}"

    def append(self, thread_id, message, attention=None):
        if self.fail:
            return False
        self.appended.append((thread_id, message))
        return True


# ── classify_health ───────────────────────────────────────────────────────────

def test_classify_health():
    # Unreachable gateway → down.
    verdict, reason = gm.classify_health(None, None, error="connection refused")
    assert verdict == "down" and "unreachable" in reason
    # Non-200 → down.
    verdict, reason = gm.classify_health(500, None)
    assert verdict == "down" and "500" in reason
    # Deliberately unconfigured channel → skipped, never an alarm.
    assert gm.classify_health(200, {"configured": False})[0] == "unconfigured"
    # Broken link with a reported reason.
    verdict, reason = gm.classify_health(200, {"configured": True, "connected": False,
                                               "error": "device unlinked"})
    assert verdict == "down" and reason == "device unlinked"
    # Healthy link.
    assert gm.classify_health(200, {"configured": True, "connected": True})[0] == "up"
    # A gateway that doesn't report link state (older build) counts as up.
    assert gm.classify_health(200, {"status": "ok"})[0] == "up"


# ── MonitorEngine state machine ───────────────────────────────────────────────

def test_debounce_then_notify():
    n = FakeNotifier()
    e = gm.MonitorEngine(n, fail_threshold=2, remind_seconds=3600)
    e.step("signal", "Signal", "down", "receive failed", now=1000)
    assert n.opened == []  # one bad check is not an outage yet
    e.step("signal", "Signal", "down", "receive failed", now=1060)
    assert len(n.opened) == 1
    title, message = n.opened[0]
    assert "Signal" in title and "disconnected" in title
    assert "/gateways" in message
    assert e.state["signal"]["status"] == "down"
    assert e.state["signal"]["thread_id"] == "thread1"


def test_single_blip_never_notifies():
    n = FakeNotifier()
    e = gm.MonitorEngine(n, fail_threshold=2, remind_seconds=3600)
    e.step("whatsapp", "WhatsApp", "down", "blip", now=1000)
    e.step("whatsapp", "WhatsApp", "up", None, now=1060)
    e.step("whatsapp", "WhatsApp", "down", "blip", now=1120)
    e.step("whatsapp", "WhatsApp", "up", None, now=1180)
    assert n.opened == [] and n.appended == []
    assert e.state["whatsapp"]["status"] == "up"


def test_recovery_reports_in_same_thread():
    n = FakeNotifier()
    e = gm.MonitorEngine(n, fail_threshold=1, remind_seconds=3600)
    e.step("telegram", "Telegram", "down", "session revoked", now=1000)
    assert len(n.opened) == 1
    e.step("telegram", "Telegram", "up", None, now=1060)
    assert len(n.appended) == 1
    thread_id, message = n.appended[0]
    assert thread_id == "thread1" and "connected again" in message
    assert e.state["telegram"]["status"] == "up"


def test_reminder_cadence():
    n = FakeNotifier()
    e = gm.MonitorEngine(n, fail_threshold=1, remind_seconds=3600)
    e.step("signal", "Signal", "down", "x", now=1000)
    # Still down but before the remind interval — no second message.
    e.step("signal", "Signal", "down", "x", now=1060)
    assert n.appended == []
    # Past the interval — one reminder, in the incident thread.
    e.step("signal", "Signal", "down", "x", now=1000 + 3700)
    assert len(n.appended) == 1
    assert n.appended[0][0] == "thread1" and "still disconnected" in n.appended[0][1]
    # And not again right away.
    e.step("signal", "Signal", "down", "x", now=1000 + 3760)
    assert len(n.appended) == 1


def test_notify_retries_after_backend_failure():
    n = FakeNotifier(fail=True)
    e = gm.MonitorEngine(n, fail_threshold=1, remind_seconds=3600)
    e.step("signal", "Signal", "down", "x", now=1000)
    assert e.state["signal"]["status"] == "down"
    assert not e.state["signal"].get("notified")
    n.fail = False  # backend comes back — next tick delivers the notice
    e.step("signal", "Signal", "down", "x", now=1060)
    assert len(n.opened) == 1
    assert e.state["signal"]["notified"] is True


def test_unconfigured_is_skipped():
    n = FakeNotifier()
    e = gm.MonitorEngine(n, fail_threshold=1, remind_seconds=3600)
    e.step("telegram", "Telegram", "unconfigured", None, now=1000)
    assert n.opened == [] and "telegram" not in e.state


# ── Shared gateway discovery ──────────────────────────────────────────────────

def test_channel_gateway_discovery():
    import messenger_gateways
    for key in ("SIGNAL_GATEWAY_BASE_URL", "WHATSAPP_GATEWAY_BASE_URL",
                "TELEGRAM_GATEWAY_BASE_URL", "MESSENGER_GATEWAYS",
                "SIGNAL_GATEWAY_TOKEN"):
        os.environ.pop(key, None)
    os.environ["SIGNAL_GATEWAY_BASE_URL"] = "http://signal-gateway:8090"
    os.environ["SIGNAL_GATEWAY_TOKEN"] = "s3cret"
    os.environ["MESSENGER_GATEWAYS"] = json.dumps([
        {"base_url": "http://signal-gateway-personal:8090", "label": "Signal (personal)"},
        {"base_url": ""},           # skipped: no base_url
        "not-an-object",            # skipped: malformed
    ])
    registry = messenger_gateways.channel_gateways("[test]")
    assert set(registry) == {"signal-gateway", "signal-gateway-personal"}
    assert registry["signal-gateway"]["token"] == "s3cret"
    assert registry["signal-gateway-personal"]["label"] == "Signal (personal)"
    # The slug is the service hostname, verbatim — the same name a gateway sees
    # in the Host header of a /send request, so approval links match registry
    # keys with no slug configuration.
    assert messenger_gateways.slug_from_base_url("http://whatsapp-gateway:8092") == "whatsapp-gateway"
    assert messenger_gateways.slug_from_base_url("http://signal-gateway-personal:8090") == "signal-gateway-personal"
    # resolve(): exact keys win; legacy shortened slugs from pre-upgrade links
    # ("signal", "signal-personal") still find their gateway; junk resolves to
    # (None, None) so e-mail account segments fall through untouched.
    assert messenger_gateways.resolve(registry, "signal-gateway")[0] == "signal-gateway"
    assert messenger_gateways.resolve(registry, "signal") == ("signal-gateway", registry["signal-gateway"])
    assert messenger_gateways.resolve(registry, "signal-personal")[0] == "signal-gateway-personal"
    assert messenger_gateways.resolve(registry, "nope") == (None, None)
    for key in ("SIGNAL_GATEWAY_BASE_URL", "SIGNAL_GATEWAY_TOKEN", "MESSENGER_GATEWAYS"):
        os.environ.pop(key, None)


def test_builtin_channels_filter():
    import messenger_gateways
    keys = ("SIGNAL_GATEWAY_BASE_URL", "WHATSAPP_GATEWAY_BASE_URL",
            "TELEGRAM_GATEWAY_BASE_URL", "MESSENGER_GATEWAYS",
            "MESSENGER_BUILTIN_CHANNELS")
    for key in keys:
        os.environ.pop(key, None)
    os.environ["SIGNAL_GATEWAY_BASE_URL"] = "http://signal-gateway:8090"
    os.environ["WHATSAPP_GATEWAY_BASE_URL"] = "http://whatsapp-gateway:8092"
    os.environ["TELEGRAM_GATEWAY_BASE_URL"] = "http://telegram-gateway:8093"
    try:
        # Unset: all three base URLs enrol, exactly today's behaviour.
        assert set(messenger_gateways.channel_gateways("[test]")) == {
            "signal-gateway", "whatsapp-gateway", "telegram-gateway",
        }
        # A deployment naming only the channels it actually runs drops the
        # rest, even though their *_GATEWAY_BASE_URL is still wired (as
        # docker-compose.yml always wires all three) — the case of a
        # container that was never started at all, not merely unpaired.
        os.environ["MESSENGER_BUILTIN_CHANNELS"] = "signal"
        assert set(messenger_gateways.channel_gateways("[test]")) == {"signal-gateway"}
        # Empty string: none of the built-ins enrol.
        os.environ["MESSENGER_BUILTIN_CHANNELS"] = ""
        assert messenger_gateways.channel_gateways("[test]") == {}
    finally:
        for key in keys:
            os.environ.pop(key, None)


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
    print(f"{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
