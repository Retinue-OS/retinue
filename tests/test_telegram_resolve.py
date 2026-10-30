#!/usr/bin/env python3
"""GET /resolve on the Telegram gateway: a username or phone number → the
user's numeric id, the only identity an inbound message carries — what the
address book files next to the name (docs/contacts.md, "Telegram ids").

Over HTTP against the gateway's own handler, with a stand-in for the Telethon
client: the token gate, the query, the phone that must not be read as an id,
the people-only filter, and the 404/502 split — then contacts.py's reading of
those same answers, so both ends of the contract are held to it.

    python3 tests/test_telegram_resolve.py
"""
import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import threading
import types
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
TOKEN = "tg-token"


class BadRequestError(Exception):
    """telethon.errors.BadRequestError: Telegram's 400 about the request."""
    code = 400


class FloodWaitError(Exception):
    """A 420 — the gateway's trouble, not the query's."""
    code = 420


# The bridge imports Telethon lazily, and this suite runs without it: stand
# in for the one name /resolve imports.
_telethon = types.ModuleType("telethon")
_telethon.errors = types.ModuleType("telethon.errors")
_telethon.errors.BadRequestError = BadRequestError
sys.modules.update({"telethon": _telethon, "telethon.errors": _telethon.errors})


class User:
    """Stand-in for telethon.tl.types.User (the gateway goes by the class name)."""

    def __init__(self, id, username=None, phone=None, first_name=None, last_name=None):
        self.id, self.username, self.phone = id, username, phone
        self.first_name, self.last_name = first_name, last_name


class Channel:
    def __init__(self, id, username):
        self.id, self.username, self.title = id, username, username


class FakeClient:
    """get_entity as Telethon answers it: an int is an id; text is a phone
    number (looked up among the account's contacts) or a username."""

    def __init__(self):
        mara = User(4242, "Mara_K", "41791234567", "Mara", "Keller")
        self.ids = {4242: mara,
                    # Somebody whose id is Mara's number read as digits.
                    41791234567: User(41791234567, "someone_else", None, "Someone", "Else")}
        self.texts = {"@mara_k": mara, "+41791234567": mara, "@news_ch": Channel(-1001234, "news_ch")}
        self.asked = []

    async def get_entity(self, peer):
        self.asked.append(peer)
        if isinstance(peer, int):
            if peer in self.ids:
                return self.ids[peer]
            raise ValueError(f"Could not find the input entity for PeerUser(user_id={peer})")
        if peer == "@gone_away":
            raise BadRequestError("Nobody is using this username or the username is unacceptable")
        if peer == "@flooding":
            raise FloodWaitError("A wait of 300 seconds is required")
        if peer.lower() in self.texts:
            return self.texts[peer.lower()]
        raise ValueError(f'No user has "{peer}" as username')


def _load_gateway(tmpdir):
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    os.environ["TELEGRAM_SEND_POLICY"] = ""
    os.environ["TELEGRAM_ACCOUNT"] = ""
    os.environ["TELEGRAM_PENDING_SENDS_DIR"] = str(Path(tmpdir) / "pending")
    os.environ["TELEGRAM_DATA_DIR"] = str(Path(tmpdir) / "data")
    os.environ["TELEGRAM_TMP_DIR"] = str(Path(tmpdir) / "tmp")
    spec = importlib.util.spec_from_file_location("telegram_gateway_resolve_test",
                                                  SCRIPTS_DIR / "telegram-gateway.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _get(url, token=TOKEN):
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_resolve_over_http(gw, client, base):
    # Token-gated like /contacts: the answer names the account's contacts.
    assert _get(f"{base}/resolve?q=@mara_k", token=None)[0] == 401
    assert _get(f"{base}/resolve?q=@mara_k", token="wrong")[0] == 401
    for query in ("", "?q=", "?q=%20"):
        assert _get(f"{base}/resolve{query}")[0] == 400, query

    status, body = _get(f"{base}/resolve?q=@mara_k")
    assert (status, body) == (200, {"id": 4242, "username": "Mara_K", "phone": "41791234567",
                                    "name": "Mara Keller"}), (status, body)

    # A phone number is looked up as a number: int() would read "+41…" as
    # the id 41791234567 — somebody else entirely.
    client.asked.clear()
    status, body = _get(f"{base}/resolve?q=%2B41791234567")
    assert status == 200 and body["id"] == 4242, (status, body)
    assert client.asked == ["+41791234567"], client.asked
    # An id still resolves as an id.
    assert _get(f"{base}/resolve?q=4242") == (200, body)

    # No such user: unknown, USERNAME_INVALID (also Telegram's word for a
    # name nobody uses), or a channel — an address book files people.
    for name in ("@nobody", "@gone_away", "@news_ch"):
        status, body = _get(f"{base}/resolve?q={name}")
        assert status == 404 and name in body["error"], (name, status, body)
    # The gateway's own trouble is a 502, never "no such user".
    assert _get(f"{base}/resolve?q=@flooding")[0] == 502
    gw._client = None
    try:
        status, body = _get(f"{base}/resolve?q=@mara_k")
        assert status == 502 and "not connected" in body["error"], (status, body)
    finally:
        gw._client = client
    print("ok: /resolve answers over HTTP")


def test_contacts_reads_the_answers(base):
    import contacts
    os.environ["TELEGRAM_GATEWAY_BASE_URL"] = base
    os.environ["TELEGRAM_GATEWAY_TOKEN"] = TOKEN
    try:
        assert contacts._telegram_lookup("@mara_k")["id"] == 4242
        assert contacts._telegram_lookup("+41791234567")["id"] == 4242, "the + survives the query string"
        assert contacts._telegram_lookup("@nobody") is None
        assert contacts.with_telegram_ids([contacts.parse_handle("telegram:@Mara_K")]) == [
            ("telegram", "@mara_k"), ("telegram", "4242")]
        try:
            contacts.with_telegram_ids([("telegram", "+41790000000")])
            raise AssertionError("filed a number Telegram does not know")
        except contacts.ContactError as exc:
            assert exc.status == 404 and "contacts" in str(exc), exc
        # A gateway that refuses or fails is refused as such, never taken for
        # an unknown name.
        for token, query in (("wrong", "@mara_k"), (TOKEN, "@flooding")):
            os.environ["TELEGRAM_GATEWAY_TOKEN"] = token
            try:
                contacts._telegram_lookup(query)
                raise AssertionError(f"{query} resolved")
            except contacts.ContactError as exc:
                assert exc.status == 502, exc
    finally:
        os.environ.pop("TELEGRAM_GATEWAY_BASE_URL", None)
        os.environ.pop("TELEGRAM_GATEWAY_TOKEN", None)
    print("ok: contacts.py reads the answers as the gateway means them")


def main():
    with tempfile.TemporaryDirectory() as tmp:
        gw = _load_gateway(tmp)
        gw.GATEWAY_TOKEN = TOKEN
        client = FakeClient()
        loop = asyncio.new_event_loop()
        threading.Thread(target=loop.run_forever, daemon=True).start()
        gw._client, gw._LOOP = client, loop
        server = ThreadingHTTPServer(("127.0.0.1", 0), gw._PushHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            test_resolve_over_http(gw, client, base)
            test_contacts_reads_the_answers(base)
        finally:
            server.shutdown()
            loop.call_soon_threadsafe(loop.stop)
    print("all telegram resolve checks passed")


if __name__ == "__main__":
    main()
