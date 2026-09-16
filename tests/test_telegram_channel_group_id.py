#!/usr/bin/env python3
"""What the delivery gate is told about a Telegram message: where, and from whom.

Two axes, two bugs, both of the same kind — the gateway handing the gate one
fact where it needed two.

**Where.** Telethon reports a (super)group as ``is_group`` but a broadcast
channel as ``is_channel`` only. Reading ``is_group`` alone made every channel
post look like a 1:1 conversation: no group id reached the gate, so the group
policy flags (news / quieted / ignored) could not match — a news channel never
reached the feed and every post cost an unknown-sender prompt plus a model turn.

**From whom.** The sender axis was keyed on the chat_id too, so in a group it
named the room rather than the person. A group is never whitelisted — only a
sender is — so a whitelisted correspondent writing in a group was never
recognised as one, and the group's quieted/ignored flag decided a message the
sender axis should have won. Signal and WhatsApp always passed the two
separately; only Telegram collapsed them.

    python3 tests/test_telegram_channel_group_id.py
"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


class _Chat:
    """Stand-in for a Telethon event/dialog: only the two flags matter here."""

    def __init__(self, is_group=False, is_channel=False):
        self.is_group = is_group
        self.is_channel = is_channel


def _load_gateway(tmpdir):
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    os.environ["TELEGRAM_SEND_POLICY"] = ""
    os.environ["TELEGRAM_ACCOUNT"] = ""
    os.environ["TELEGRAM_PENDING_SENDS_DIR"] = str(Path(tmpdir) / "pending")
    os.environ["TELEGRAM_DATA_DIR"] = str(Path(tmpdir) / "data")
    os.environ["TELEGRAM_TMP_DIR"] = str(Path(tmpdir) / "tmp")
    spec = importlib.util.spec_from_file_location(
        "telegram_gateway_channel_test", SCRIPTS_DIR / "telegram-gateway.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_shared_chat_covers_groups_and_broadcast_channels(gw):
    assert gw._is_shared_chat(_Chat()) is False, "a 1:1 chat is not shared"
    assert gw._is_shared_chat(_Chat(is_group=True)) is True, "a group is shared"
    assert gw._is_shared_chat(_Chat(is_group=True, is_channel=True)) is True, \
        "a supergroup is shared"
    assert gw._is_shared_chat(_Chat(is_channel=True)) is True, \
        "a broadcast channel is shared — this is the case that regressed"


def test_channel_message_reaches_the_gate_with_a_group_id(gw):
    """The end the fix exists for: the gate sees the channel as a group."""
    seen = {}

    def _fake_gate(sender, group_id):
        seen["sender"] = sender
        seen["group"] = group_id
        return {"forward": False, "flagged_unknown": False,
                "delivered_if_held": True, "reason": "test", "news": False}

    def _fake_persist(question, sender, group_id, delivered=False, media=None,
                      attachment_urls=None, **kw):
        seen["persisted_group"] = group_id
        return None

    gw._inbound_gate_decision = _fake_gate
    gw._persist_inbound = _fake_persist
    gw._mark_delivered = lambda path: None

    channel = _Chat(is_channel=True)
    gw._forward_to_inbox("a channel post", "de", "-1002467043994",
                         is_group=gw._is_shared_chat(channel))

    assert seen["group"] == "-1002467043994", \
        f"gate got group={seen['group']!r}; the policy flags cannot match without it"
    assert seen["persisted_group"] == "-1002467043994", \
        "the ledger record must carry the group too, or the daily drain loses it"


def test_private_message_still_has_no_group_id(gw):
    seen = {}
    gw._inbound_gate_decision = lambda sender, group_id: (
        seen.update(group=group_id)
        or {"forward": False, "flagged_unknown": False,
            "delivered_if_held": True, "reason": "test", "news": False})
    gw._persist_inbound = lambda *a, **k: None
    gw._mark_delivered = lambda path: None

    gw._forward_to_inbox("a direct message", "de", "12345",
                         is_group=gw._is_shared_chat(_Chat()))

    assert seen["group"] is None, "a 1:1 must not be matched by group policy"


def _watch_gate(gw, seen):
    """Capture what the gate and the ledger are told, and forward nothing."""
    def _fake_gate(sender, group_id):
        seen["sender"] = sender
        seen["group"] = group_id
        return {"forward": False, "flagged_unknown": False,
                "delivered_if_held": True, "reason": "test", "news": False}

    def _fake_persist(question, sender, group_id, delivered=False, media=None,
                      attachment_urls=None, **kw):
        seen["persisted_sender"] = sender
        seen["persisted_group"] = group_id
        seen["persisted_chat"] = kw.get("chat")
        return None

    gw._inbound_gate_decision = _fake_gate
    gw._persist_inbound = _fake_persist
    gw._mark_delivered = lambda path: None


def test_a_group_message_is_from_its_poster_not_from_the_room(gw):
    """The case the whitelist could never win: a known person in a group."""
    seen = {}
    _watch_gate(gw, seen)
    gw._forward_to_inbox("kommt ihr?", "de", "-1002467043994", is_group=True,
                         sender_name="Nina", sender_handle="4711")

    assert seen["sender"] == "4711", \
        f"gate got sender={seen['sender']!r} — a group is not a sender"
    assert seen["group"] == "-1002467043994", "and the room is still the group"
    assert seen["persisted_sender"] == "4711", \
        "the ledger must record who wrote, not where"
    assert seen["persisted_chat"] == "-1002467043994", \
        "while the chat stays the chat — it is the reply address"


def test_a_private_message_keys_on_the_same_id_as_before(gw):
    """Back-compat: whitelist entries were written when this keyed on the chat.

    Telethon reports the same id for both in a 1:1, so nothing moves."""
    seen = {}
    _watch_gate(gw, seen)
    gw._forward_to_inbox("hoi", "de", "12345", is_group=False,
                         sender_handle="12345")
    assert seen["sender"] == "12345" and seen["group"] is None
    assert seen["persisted_chat"] == "12345"


def test_a_post_with_no_sender_falls_back_to_the_chat(gw):
    """A broadcast channel post has no individual sender; the channel is it."""
    seen = {}
    _watch_gate(gw, seen)
    gw._forward_to_inbox("a channel post", "de", "-1002467043994",
                         is_group=True, sender_handle=None)
    assert seen["sender"] == "-1002467043994"
    assert seen["group"] == "-1002467043994"


def test_the_poster_id_reaches_the_forward(gw):
    """The dispatch threads it through; without that the fix never arrives."""
    passed = {}
    gw._forward_to_inbox = lambda *a, **kw: passed.update(kw)
    gw._record_recent_sender = lambda *a, **kw: None
    gw._detect_text_language = lambda text: "de"
    gw.TELEGRAM_GATEWAY_MODE = "inbox"
    gw._handle_inbound("kommt ihr?", "de", "-1002467043994", "@nina", True,
                       "Nina", sender_key="4711")
    assert passed.get("sender_handle") == "4711", passed


def main() -> int:
    failures = 0
    with tempfile.TemporaryDirectory() as tmpdir:
        for name, fn in sorted(globals().items()):
            if not name.startswith("test_") or not callable(fn):
                continue
            gw = _load_gateway(tmpdir)
            try:
                fn(gw)
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
            else:
                print(f"ok   {name}")
    print("\n" + ("all passed" if not failures else f"{failures} failed"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
