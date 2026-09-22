#!/usr/bin/env python3
"""Each messenger gateway's chat erasure (POST /chats/delete → _erase_chat).

Deleting a chat in the dashboard asks the channel's gateways to erase it: the
ledger records and their media (inbound_store.delete_chat, tested on its own in
test_inbound_store.py), and each gateway's own traces — pending-send files and
the recent-senders entry. What differs per gateway is only what "this chat"
means for those two files, so that is what this pins, for all three: the
peer's traces go, other peers' stay, a send on the wire right now is left to
finish, and a chat of *another* account leaves this gateway's own files alone.

Loads each gateway with the sandbox loader its send-policy test already uses.

    python3 tests/test_gateway_chat_erase.py
"""
import importlib.util
import json
import tempfile
from pathlib import Path

TESTS = Path(__file__).resolve().parent


def _loader(name, func):
    spec = importlib.util.spec_from_file_location(name, TESTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return getattr(mod, func)


ACCOUNT = "+15551234567"


def _check(gw, prefix, chat, recent_hit, recent_miss, recipient=None):
    """Seed one gateway's stores in `tmp` and erase `chat` from them."""
    recipient = recipient or chat
    store = Path(gw.INBOUND_STORE_DIR)
    ist = gw._ibstore
    ist.write_message(store, channel=gw.INBOUND_CHANNEL, sender=chat, chat=chat,
                      account=ACCOUNT, text="hello", timestamp=100.0)
    ist.write_outbound(store, channel=gw.INBOUND_CHANNEL, chat=chat, account=ACCOUNT,
                       text="hi back", author="device", timestamp=101.0)
    keep = ist.write_message(store, channel=gw.INBOUND_CHANNEL, sender="x", chat="other",
                             account=ACCOUNT, text="unrelated", timestamp=102.0)[1]
    pend = Path(getattr(gw, f"{prefix}_PENDING_SENDS_DIR"))
    pend.mkdir(parents=True, exist_ok=True)
    for rid, to, status in (("a" * 32, recipient, "pending"), ("b" * 32, recipient, "sending"),
                            ("c" * 32, "someone-else", "pending")):
        (pend / f"{rid}.json").write_text(json.dumps(
            {"id": rid, "recipient": to, "status": status}))
        gw._pending_sends[rid] = {"id": rid}
    recent = Path(getattr(gw, f"{prefix}_RECENT_CHATS_PATH"))
    recent.parent.mkdir(parents=True, exist_ok=True)
    recent.write_text(json.dumps([recent_hit, recent_miss]))

    # Another account's chat with the same peer: the shared ledger is matched
    # exactly (nothing of this account's goes), and this gateway's own files
    # are not its to touch.
    got = gw._erase_chat(chat, "+19999999999")
    assert got["messages"] == 0 and got["pending_sends"] == 0 and got["recent"] == 0, got

    got = gw._erase_chat(chat, ACCOUNT)
    assert got["messages"] == 2 and got["errors"] == 0, got
    assert got["pending_sends"] == 1 and got["recent"] == 1, got
    assert [p.name for p in (store / "messages").glob("*.nt")] == [keep.name]
    assert sorted(p.stem for p in pend.glob("*.json")) == ["b" * 32, "c" * 32], \
        "the queued send goes; the one on the wire and another peer's stay"
    assert "a" * 32 not in gw._pending_sends
    assert json.loads(recent.read_text()) == [recent_miss]


def test_signal():
    load = _loader("test_signal_send_policy", "_load_signal_gateway")
    with tempfile.TemporaryDirectory() as tmp:
        gw = load([], Path(tmp) / "pending", account=ACCOUNT)
        gw.INBOUND_STORE_DIR = Path(tmp) / "inbound"
        gw.SIGNAL_RECENT_CHATS_PATH = Path(tmp) / "recent.json"
        _check(gw, "SIGNAL", "+41790000001",
               {"number": "+41790000001", "uuid": "u-1"}, {"number": "+4179", "uuid": "u-2"})
        # A group's recent-senders entries are its members, not the group.
        assert gw._erase_chat("group:abc", ACCOUNT)["recent"] == 0
    print("PASS test_signal")


def test_whatsapp():
    load = _loader("test_whatsapp_send_policy", "_load_whatsapp_gateway")
    with tempfile.TemporaryDirectory() as tmp:
        gw = load([], Path(tmp) / "pending", account=ACCOUNT)
        gw.INBOUND_STORE_DIR = Path(tmp) / "inbound"
        gw.WHATSAPP_RECENT_CHATS_PATH = Path(tmp) / "recent.json"
        # A pending send addressed as a bare number keys as the same chat.
        _check(gw, "WHATSAPP", "41790000001@s.whatsapp.net",
               {"number": "41790000001", "jid": "41790000001@s.whatsapp.net"},
               {"number": "4179", "jid": "4179@s.whatsapp.net"},
               recipient="+41790000001")
    print("PASS test_whatsapp")


def test_telegram():
    load = _loader("test_telegram_send_policy", "_load_telegram_gateway")
    with tempfile.TemporaryDirectory() as tmp:
        gw = load([], Path(tmp) / "pending", account=ACCOUNT)
        gw.INBOUND_STORE_DIR = Path(tmp) / "inbound"
        gw.TELEGRAM_RECENT_CHATS_PATH = Path(tmp) / "recent.json"
        _check(gw, "TELEGRAM", "900900",
               {"chat_id": 900900, "name": "A"}, {"chat_id": 1, "name": "B"})
    print("PASS test_telegram")


if __name__ == "__main__":
    test_signal()
    test_whatsapp()
    test_telegram()
    print("all gateway chat-erase tests passed")
