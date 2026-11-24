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
        self.quiet_flags = []  # one per append, in order
        self._next_id = 0

    def open_thread(self, title, message, attention=None):
        if self.fail:
            return None
        self.opened.append((title, message))
        self._next_id += 1
        return f"thread{self._next_id}"

    def append(self, thread_id, message, attention=None, quiet=False):
        if self.fail:
            return False
        self.appended.append((thread_id, message))
        self.quiet_flags.append(quiet)
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
    # Not working, and the gateway says it is not the link: held by its setup.
    verdict, reason = gm.classify_health(200, {"configured": True, "connected": False,
                                               "blocked": "policy", "error": "no policy file"})
    assert (verdict, reason) == ("blocked", "no policy file")
    # (A gateway that names nothing, or says null, is simply down.)
    assert gm.classify_health(200, {"connected": False, "blocked": None})[0] == "down"
    # Healthy link.
    assert gm.classify_health(200, {"configured": True, "connected": True})[0] == "up"
    assert gm.classify_health(200, {"connected": True, "blocked": "policy"})[0] == "up"
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
    # The all-clear is a record, not news: no push, and an archived thread
    # stays archived.
    assert n.quiet_flags == [True]
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
    assert n.quiet_flags == [False]
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
                "TELEGRAM_GATEWAY_BASE_URL", "MAIL_GATEWAY_BASE_URL", "MESSENGER_GATEWAYS",
                "SIGNAL_GATEWAY_TOKEN"):
        os.environ.pop(key, None)
    os.environ["SIGNAL_GATEWAY_BASE_URL"] = "http://signal-gateway:8090"
    os.environ["SIGNAL_GATEWAY_TOKEN"] = "s3cret"
    os.environ["MESSENGER_GATEWAYS"] = json.dumps([
        {"base_url": "http://signal-gateway-personal:8090", "label": "Signal (personal)"},
        {"base_url": "http://mail-gateway-ari:8096", "label": "E-mail (Ari)",
         "channel": " Email "},
        {"base_url": "http://odd-gateway:1", "channel": 7},  # kept; its channel is not
        {"base_url": ""},           # skipped: no base_url
        "not-an-object",            # skipped: malformed
    ])
    registry = messenger_gateways.channel_gateways("[test]")
    assert set(registry) == {"signal-gateway", "signal-gateway-personal",
                             "mail-gateway-ari", "odd-gateway"}
    assert registry["signal-gateway"]["token"] == "s3cret"
    assert registry["signal-gateway-personal"]["label"] == "Signal (personal)"
    # Every entry carries its channel: a built-in its own, an extra what it
    # declares (normalized), and None when it declares nothing usable — the
    # registry does not guess one from the name.
    assert all("channel" in gw for gw in registry.values()), registry
    assert registry["signal-gateway"]["channel"] == "signal"
    assert registry["signal-gateway-personal"]["channel"] is None
    assert registry["mail-gateway-ari"]["channel"] == "email"
    assert registry["odd-gateway"]["channel"] is None
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
            "TELEGRAM_GATEWAY_BASE_URL", "MAIL_GATEWAY_BASE_URL", "MESSENGER_GATEWAYS",
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


def test_sms_enrols_only_with_its_base_url():
    """SMS is opt-in: the base compose never sets SMS_GATEWAY_BASE_URL, so an
    unset channel list must not drag a never-started sms-gateway into the
    monitor (which would report it as an outage on every boot)."""
    import messenger_gateways
    keys = ("SIGNAL_GATEWAY_BASE_URL", "WHATSAPP_GATEWAY_BASE_URL",
            "TELEGRAM_GATEWAY_BASE_URL", "SMS_GATEWAY_BASE_URL", "MAIL_GATEWAY_BASE_URL",
            "SMS_GATEWAY_TOKEN", "MESSENGER_GATEWAYS", "MESSENGER_BUILTIN_CHANNELS")
    for key in keys:
        os.environ.pop(key, None)
    os.environ["SIGNAL_GATEWAY_BASE_URL"] = "http://signal-gateway:8090"
    try:
        assert set(messenger_gateways.channel_gateways("[test]")) == {"signal-gateway"}
        os.environ["SMS_GATEWAY_BASE_URL"] = "http://sms-gateway:8095"
        os.environ["SMS_GATEWAY_TOKEN"] = "t"
        registry = messenger_gateways.channel_gateways("[test]")
        assert set(registry) == {"signal-gateway", "sms-gateway"}
        assert registry["sms-gateway"] == {
            "base_url": "http://sms-gateway:8095", "token": "t", "label": "SMS",
            "channel": "sms"}
        # An explicit channel list still has the final say.
        os.environ["MESSENGER_BUILTIN_CHANNELS"] = "signal"
        assert set(messenger_gateways.channel_gateways("[test]")) == {"signal-gateway"}
        os.environ["MESSENGER_BUILTIN_CHANNELS"] = "signal,sms"
        assert set(messenger_gateways.channel_gateways("[test]")) == {
            "signal-gateway", "sms-gateway"}
    finally:
        for key in keys:
            os.environ.pop(key, None)


def test_mail_enrols_only_with_its_base_url():
    """E-mail is a built-in channel whose gateway is the mail gateway: its
    variables are MAIL_*, its slug is the service name, and its entry says
    "email" because neither the slug nor the label does."""
    import messenger_gateways
    keys = ("SIGNAL_GATEWAY_BASE_URL", "WHATSAPP_GATEWAY_BASE_URL",
            "TELEGRAM_GATEWAY_BASE_URL", "SMS_GATEWAY_BASE_URL", "MAIL_GATEWAY_BASE_URL",
            "MAIL_GATEWAY_TOKEN", "EMAIL_GATEWAY_BASE_URL", "MESSENGER_GATEWAYS",
            "MESSENGER_BUILTIN_CHANNELS")
    for key in keys:
        os.environ.pop(key, None)
    try:
        assert "email" in messenger_gateways.BUILTIN_CHANNELS
        # Named after the channel is not how this one enrols.
        os.environ["EMAIL_GATEWAY_BASE_URL"] = "http://email-gateway:8096"
        assert messenger_gateways.channel_gateways("[test]") == {}
        os.environ["MAIL_GATEWAY_BASE_URL"] = "http://mail-gateway:8096/"
        os.environ["MAIL_GATEWAY_TOKEN"] = " m "
        registry = messenger_gateways.channel_gateways("[test]")
        assert registry == {"mail-gateway": {
            "base_url": "http://mail-gateway:8096", "token": "m", "label": "E-mail",
            "channel": "email"}}, registry
        # The channel list has the final say, by the channel's name.
        os.environ["MESSENGER_BUILTIN_CHANNELS"] = "signal"
        assert messenger_gateways.channel_gateways("[test]") == {}
        os.environ["MESSENGER_BUILTIN_CHANNELS"] = "signal,email"
        assert set(messenger_gateways.channel_gateways("[test]")) == {"mail-gateway"}
    finally:
        for key in keys:
            os.environ.pop(key, None)


def test_a_mail_gateway_not_opted_in_is_never_an_outage():
    """The mail gateway's opt-ins — MAIL_GATEWAY_TOKEN set, `email` in a
    pinned MESSENGER_BUILTIN_CHANNELS — leave a running container that
    mirrors nothing and says why on /health with configured: false. An
    upgrade that sets neither must not open a "disconnected" thread that
    blames the mail server, however long it idles."""
    n = FakeNotifier()
    e = gm.MonitorEngine(n, fail_threshold=1, remind_seconds=1)
    for error in ("MAIL_GATEWAY_TOKEN is not set",
                  "the email channel is not in MESSENGER_BUILTIN_CHANNELS"):
        body = {"status": "ok", "configured": False, "connected": False,
                "mode": "inbox", "error": error}
        verdict, reason = gm.classify_health(200, body)
        assert (verdict, reason) == ("unconfigured", None), (verdict, reason)
        for now in (1000, 2000, 90000):
            e.step("mail-gateway", "E-mail", verdict, reason, now=now, channel="email")
    assert n.opened == [] and n.appended == [] and "mail-gateway" not in e.state


# ── Wording follows the channel ───────────────────────────────────────────────

_PAIRING_WORDS = ("re-pair", "QR", "phone", "messenger")


def test_mail_notices_never_talk_about_pairing():
    """A mail gateway is down because the IMAP server refused or is out of
    reach. Every notice for it must say that, and none may send the user to
    re-pair a phone or scan a code that does not exist."""
    notices = [
        gm.outage_message("E-mail", "IMAP login failed", since=1000, channel="email"),
        gm.reminder_message("E-mail", 1000, channel="email"),
        gm.recovery_message("E-mail", channel="email"),
    ]
    for text in notices:
        for word in _PAIRING_WORDS:
            assert word.lower() not in text.lower(), (word, text)
    assert "mail server" in notices[0] and "login" in notices[0], notices[0]
    assert "IMAP login failed" in notices[0]
    assert "/gateways)" in notices[0] and "/gateways)" in notices[1]
    # The channel is matched as the registry states it, case and all.
    assert gm.outage_message("E-mail", None, channel=" Email ") == \
        gm.outage_message("E-mail", None, channel="email")


_IMAP_WORDS = ("mail server", "login", "password", "provider", "lost its connection",
               "disconnected", "connected again")


def test_a_mail_gateway_waiting_for_its_setup_is_not_an_imap_outage():
    """The mail gateway's mirror does not start without the e-mail policy
    file, which the retinue side places. While it waits its mailbox is
    reachable: /health says ``connected: false`` with ``blocked: "policy"``
    and what to check. The notices must say that — in the gateway's words —
    and must not send the user to an app password or the provider's status
    page; the reminders carry the reason too."""
    reason = ("the mirror is waiting for the e-mail policy: there is no e-mail policy file "
              "at /inbound/policy/email-policy.nt yet — check the retinue container's "
              "start log")
    body = {"status": "ok", "configured": True, "connected": False, "mode": "inbox",
            "blocked": "policy", "error": reason}
    verdict, said = gm.classify_health(200, body)
    assert (verdict, said) == ("blocked", reason)
    n = FakeNotifier()
    e = gm.MonitorEngine(n, fail_threshold=2, remind_seconds=3600)
    e.step("mail-gateway", "E-mail", verdict, said, now=1000, channel="email")
    assert n.opened == []                       # debounced like any bad check
    e.step("mail-gateway", "E-mail", verdict, said, now=1060, channel="email")
    e.step("mail-gateway", "E-mail", verdict, said, now=1060 + 3700, channel="email")
    e.step("mail-gateway", "E-mail", "up", None, now=1060 + 3800, channel="email")
    (title, outage), = n.opened
    assert title == "E-mail gateway waiting for its setup", title
    texts = [title, outage] + [m for _t, m in n.appended]
    assert len(texts) == 4, texts
    for text in texts:
        for word in _IMAP_WORDS + _PAIRING_WORDS:
            assert word.lower() not in text.lower(), (word, text)
    # The notice and the reminder both say what the gateway reports, and
    # that the chats are missing mail meanwhile.
    assert reason in outage and reason in texts[2], texts
    assert "waiting for its setup" in outage and "still waiting for its setup" in texts[2]
    assert "NOT reaching the chats" in outage and "/gateways)" in outage
    assert "/gateways)" in texts[2] and texts[3].startswith("The E-mail gateway is running again")
    assert n.quiet_flags == [False, True]
    # An outage that turns out to be the mailbox after all is worded as one,
    # in the same thread.
    n = FakeNotifier()
    e = gm.MonitorEngine(n, fail_threshold=1, remind_seconds=10)
    e.step("mail-gateway", "E-mail", "blocked", reason, now=1000, channel="email")
    e.step("mail-gateway", "E-mail", "down", "IMAP login failed", now=1100, channel="email")
    e.step("mail-gateway", "E-mail", "up", None, now=1200, channel="email")
    assert len(n.opened) == 1 and [t for t, _m in n.appended] == ["thread1", "thread1"]
    assert "still disconnected" in n.appended[0][1] and "mail server" in n.appended[0][1]
    assert "connected again" in n.appended[1][1]
    # A messenger that reported it would be worded as held too, not as unlinked.
    text = gm.outage_message("Signal", "waiting for X", channel="signal", blocked=True)
    assert "waiting for its setup" in text and "QR" not in text and "waiting for X" in text


def test_messenger_notices_keep_their_wording():
    for channel in (None, "signal", "telegram", "unknown"):
        outage = gm.outage_message("Signal", "unlinked", channel=channel)
        assert "messenger gateway" in outage and "pairing QR code" in outage, outage
        assert "Re-pair it on" in gm.reminder_message("Signal", 0, channel=channel)
        assert "messenger gateway is connected again" in gm.recovery_message("Signal", channel)


def test_engine_words_each_notice_by_channel():
    """The state machine hands the gateway's channel to every notice it posts."""
    n = FakeNotifier()
    e = gm.MonitorEngine(n, fail_threshold=1, remind_seconds=3600)
    e.step("mail-gateway", "E-mail", "down", "server unreachable", now=1000, channel="email")
    e.step("mail-gateway", "E-mail", "down", "server unreachable", now=1000 + 3700,
           channel="email")
    e.step("mail-gateway", "E-mail", "up", None, now=1000 + 3800, channel="email")
    title, outage = n.opened[0]
    assert title == "E-mail gateway disconnected", title
    texts = [outage] + [m for _t, m in n.appended]
    assert len(texts) == 3, texts
    for text in texts:
        for word in _PAIRING_WORDS:
            assert word.lower() not in text.lower(), (word, text)
    assert "still disconnected" in texts[1] and "connected again" in texts[2]


def test_the_loop_passes_the_registry_channel():
    """main() is where the registry entry meets the engine: the channel the
    registry states must reach the notice, or the mail gateway would be
    worded as a messenger after all."""
    n = FakeNotifier()
    ticks = []

    class Stop(Exception):
        pass

    def sleep(_seconds):
        ticks.append(1)
        if len(ticks) >= gm.FAIL_THRESHOLD:
            raise Stop

    saved = (gm.messenger_gateways.channel_gateways, gm.check_gateway, gm.time.sleep,
             gm.ConversationNotifier, gm._load_state, gm._save_state)
    gm.messenger_gateways.channel_gateways = lambda _prefix: {
        "mail-gateway": {"base_url": "http://mail-gateway:8096", "token": "",
                         "label": "E-mail", "channel": "email"}}
    gm.check_gateway = lambda gw: ("down", "IMAP login refused")
    gm.time.sleep = sleep
    gm.ConversationNotifier = lambda **_kw: n
    gm._load_state = dict
    gm._save_state = lambda state: None
    try:
        try:
            gm.main()
        except Stop:
            pass
    finally:
        (gm.messenger_gateways.channel_gateways, gm.check_gateway, gm.time.sleep,
         gm.ConversationNotifier, gm._load_state, gm._save_state) = saved
    assert len(n.opened) == 1, n.opened
    assert "mail server" in n.opened[0][1] and "QR" not in n.opened[0][1], n.opened[0][1]


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
