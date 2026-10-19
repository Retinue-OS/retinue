#!/usr/bin/env python3
"""A 1:1 chat the rail never named is named from its gateway's recent-chats.

The rail writes a chat's name only as a message passes by, so a peer who has
not written since the chat state began shows as a bare key (`…@lid`) although
the gateway remembers their published profile name. The chat list's
background backfill files that name as the peer's roster entry; a name the
rail or a contact card set, and group chats, are left alone.

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


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        wg = test_chat_api._load_gateway(Path(tmp), 1, 1)
        asked = []

        def fake_request(gw, path, timeout):
            asked.append((gw["base_url"], path))
            return _Resp(json.dumps(RECENT).encode("utf-8"))

        gw = {"base_url": "http://whatsapp-gateway:8092"}
        wg._gateway_request = fake_request
        wg._chat_gateway = lambda doc, channel, account=None: ("whatsapp-gateway", gw, None)

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

        # Once named, a chat is not asked about again.
        asked.clear()
        assert wg._backfill_chat_names([LID, PHONE]) == 0
        assert asked == [], asked

        # An unreachable gateway names nothing and raises nothing.
        def down(gw, path, timeout):
            raise OSError("connection refused")
        wg._gateway_request = down
        assert wg._backfill_chat_names([UNKNOWN]) == 0
    print("all chat-name backfill tests passed")


if __name__ == "__main__":
    main()
