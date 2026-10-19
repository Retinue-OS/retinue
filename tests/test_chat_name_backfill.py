#!/usr/bin/env python3
"""A 1:1 chat the rail never named is named from its gateway's recent-chats.

The rail writes a chat's name only as a message passes by, so a peer who has
not written since the chat state began shows as a bare key (`…@lid`) although
the gateway remembers their published profile name. The chat list's
background backfill files that name as the peer's roster entry; a name the
rail or a contact card set, and group chats, are left alone. Each channel's
store names its peers by whatever identity that channel keys chats on — a
WhatsApp or SMS number, a Signal number or UUID, a Telegram chat id.

Run: python3 tests/test_chat_name_backfill.py
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_chat_api  # noqa: E402  (reuses its module loader)

LID = "whatsapp:~+41000000000:215165178003711@lid"
PHONE = "whatsapp:~+41000000000:41790000001@s.whatsapp.net"
NAMED = "whatsapp:~+41000000000:152531787317307@lid"
CARD = "whatsapp:~+41000000000:41790000002@s.whatsapp.net"
UNKNOWN = "whatsapp:~+41000000000:99999@lid"

RECENT = {"recent_chats": [
    {"number": "215165178003711", "name": "Renaud", "is_group": False},
    {"number": "+41790000001", "name": "Mara", "is_group": False},
    {"number": "152531787317307", "name": "Renaud Praxis", "is_group": False},
    {"number": "41790000002", "name": "Profile name", "is_group": False},
]}

# Signal keys a 1:1 chat on whichever identity the envelope carried: a UUID
# (the only one a number-hiding peer offers) or the phone number.
SIGNAL_UUID_ID = "5f0c6a2e-1b7d-4c9a-8e3f-2a1b0c9d8e7f"
SIGNAL_UUID = "signal:~+41000000000:" + SIGNAL_UUID_ID
SIGNAL_PHONE = "signal:~+41000000000:+41790000003"
SIGNAL_RECENT = {"recent_chats": [
    {"number": None, "uuid": SIGNAL_UUID_ID, "name": "Uma"},
    {"number": "+41790000003", "uuid": "0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0", "name": "Sven"},
    # Older entry for the same peer: the list is most-recent-first, so it loses.
    {"number": None, "uuid": SIGNAL_UUID_ID, "name": "Uma (old)"},
]}

# Telegram keys a chat on the marked peer id, which its store reports as
# `chat_id` — a string, or a number where the list was seeded from dialogs.
TELEGRAM = "telegram:~+41000000000:777000111"
TELEGRAM_SEEDED = "telegram:~+41000000000:777000222"
TELEGRAM_RECENT = {"recent_chats": [
    {"chat_id": "777000111", "username": "tina", "name": "Tina", "is_group": False},
    {"chat_id": 777000222, "username": None, "name": "Theo", "is_group": False},
    {"chat_id": "-100123456", "username": None, "name": "Some group", "is_group": True},
]}


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        wg = test_chat_api._load_gateway(Path(tmp), 1, 1)
        asked, timeouts = [], {}
        stores = {"http://whatsapp-gateway:8092": RECENT,
                  "http://signal-gateway:8091": SIGNAL_RECENT,
                  "http://telegram-gateway:8093": TELEGRAM_RECENT}

        def fake_request(gw, path, timeout):
            asked.append((gw["base_url"], path))
            timeouts[gw["base_url"]] = timeout
            return _Resp(json.dumps(stores[gw["base_url"]]).encode("utf-8"))

        gateways = {channel: (f"{channel}-gateway", {"base_url": url}, None)
                    for channel, url in (("whatsapp", "http://whatsapp-gateway:8092"),
                                         ("signal", "http://signal-gateway:8091"),
                                         ("telegram", "http://telegram-gateway:8093"))}
        wg._gateway_request = fake_request
        wg._chat_gateway = lambda doc, channel, account=None: gateways[channel]

        wg._CHAT_STATE.note_message(NAMED, name="Physiotherapie Studen GmbH")
        wg._CHAT_STATE.set_contact(CARD, name="Mara Card")

        named = wg._backfill_chat_names([LID, PHONE, NAMED, CARD, UNKNOWN])
        assert named == 2, named
        assert asked == [("http://whatsapp-gateway:8092", "/recent-chats")], asked

        def shown(chat_id):
            channel, key = wg.chat_state_mod.split_chat_id(chat_id)
            return wg._chat_display_name(wg._CHAT_STATE.get(chat_id), channel, key)

        assert shown(LID) == "Renaud", shown(LID)
        assert shown(PHONE) == "Mara", shown(PHONE)
        # The rail's name and the user's card outrank the gateway's memory.
        assert shown(NAMED) == "Physiotherapie Studen GmbH", shown(NAMED)
        assert shown(CARD) == "Mara Card", shown(CARD)
        # Nothing known: the honest fallback stays the key.
        assert shown(UNKNOWN) == "99999@lid", shown(UNKNOWN)

        # Signal: a UUID-keyed chat is named from the entry's `uuid` (its
        # `number` is null), a phone-keyed one from `number` — each by the
        # first, most recent entry that knows it.
        asked.clear()
        assert wg._backfill_chat_names([SIGNAL_UUID, SIGNAL_PHONE]) == 2
        assert asked == [("http://signal-gateway:8091", "/recent-chats")], asked
        assert shown(SIGNAL_UUID) == "Uma", shown(SIGNAL_UUID)
        assert shown(SIGNAL_PHONE) == "Sven", shown(SIGNAL_PHONE)

        # Telegram: named from `chat_id`, matched as-is, string or number.
        # The gateway may first seed its answer from the dialog list, so it is
        # given longer than the short timeout every other gateway gets.
        asked.clear()
        assert wg._backfill_chat_names([TELEGRAM, TELEGRAM_SEEDED]) == 2
        assert asked == [("http://telegram-gateway:8093", "/recent-chats")], asked
        assert shown(TELEGRAM) == "Tina", shown(TELEGRAM)
        assert shown(TELEGRAM_SEEDED) == "Theo", shown(TELEGRAM_SEEDED)
        assert timeouts["http://telegram-gateway:8093"] == wg.CHAT_NAME_BACKFILL_TELEGRAM_TIMEOUT
        assert timeouts["http://signal-gateway:8091"] == wg.CHAT_NAME_BACKFILL_TIMEOUT
        assert wg.CHAT_NAME_BACKFILL_TELEGRAM_TIMEOUT > 30 > wg.CHAT_NAME_BACKFILL_TIMEOUT

        # Once named, a chat is not asked about again.
        asked.clear()
        assert wg._backfill_chat_names([LID, PHONE, SIGNAL_UUID, SIGNAL_PHONE, TELEGRAM]) == 0
        assert asked == [], asked

        # An unreachable gateway names nothing and raises nothing.
        def down(gw, path, timeout):
            raise OSError("connection refused")
        wg._gateway_request = down
        assert wg._backfill_chat_names([UNKNOWN]) == 0
    print("all chat-name backfill tests passed")


if __name__ == "__main__":
    main()
