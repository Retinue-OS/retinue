#!/usr/bin/env python3
"""A Telegram broadcast channel must carry a group id, like a group does.

Telethon reports a (super)group as ``is_group`` but a broadcast channel as
``is_channel`` only. Reading ``is_group`` alone made every channel post look
like a 1:1 conversation: no group id reached the delivery gate, so the group
policy flags (news / quieted / ignored) could not match — a news channel never
reached the feed and every post cost an unknown-sender prompt plus a model turn.

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
