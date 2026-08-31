#!/usr/bin/env python3
"""Integration checks for the web-gateway's messenger-chat API.

Runs the REAL web-gateway handler on a local port, pointed at a tiny mock
SPARQL server (canned sparql-results+json, so the exact queries the gateway
sends are exercised and captured) and a mock channel gateway (asserting the
direct user-send contract and serving token-gated media). Covers:

- GET /chats and /chats/<id>/messages shaping against the fixture contract
  (webapp/README.md on the chats-ui branch), including ?before paging and the
  media-URL rewrite to the authenticated proxy;
- the notify rail (POST /internal/chats/inbound): open-vs-token auth, the
  un-archive-unless-muted rule, held-gate and muted silence, push mode
  new-vs-reply, and echoes advancing the read watermark;
- the read watermark, the version-guarded draft (409), agent staging;
- POST /chats/<id>/companion: idempotent create-or-get, the id surfacing on the
  ChatSummary, and the thread staying out of the default conversation list;
- POST /chats/<id>/send: author "user" reaches the gateway, the draft clears,
  the watermark advances, and the sent message is returned and visible in the
  merged view before the store knows it (the overlay);
- honest 502 when the life store is down — no raw fallback.

Standalone (stdlib + the gateway module's own deps):

    python3 tests/test_chat_api.py
"""
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import types
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

KB = "https://w3id.org/retinue/kb#"
T_IN = KB + "InboundMessage"
T_OUT = KB + "OutboundMessage"

MARA = "+41794456312"
CHAT1 = "signal:" + MARA
WA_KEY = "123456@g.us"
CHAT2 = "whatsapp:" + WA_KEY
MID_ATT = "ab" * 16   # recorded as a host-free urn:retinue:media:… (today's shape)
MID_ATT2 = "cd" * 16  # the blob the mock gateway "stores" for an images send
MID_ATT3 = "ef" * 16  # a legacy http://<service>/media/<id> record on disk
TS0, TS1, TS2, TS3 = ("2026-08-27T06:00:00Z", "2026-08-27T07:00:00Z",
                      "2026-08-27T07:05:00Z", "2026-08-27T07:12:00Z")
W_TS = "2026-08-26T18:00:00Z"

# Mutable canned state the mock servers consult.
STATE: dict = {"fail": False, "queries": [], "gw_requests": [], "sent": [],
               # The mock gateway's reported identity: chat routing sends only
               # through an inbox-mode account (see test_control_gateway_refused).
               "gw_mode": "inbox", "gw_account": "+41791112233"}


def _cell(value):
    return {"value": value}


def _lit_row(**kw):
    return {k: _cell(v) for k, v in kw.items() if v is not None}


class _MockSparql(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        query = urllib.parse.parse_qs(body).get("query", [""])[0]
        STATE["queries"].append(query)
        if STATE["fail"]:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"boom")
            return
        if "VALUES (?chat ?cut)" in query:
            bindings = self._unread(query)
        elif "MAX(?ts0)" in query:
            bindings = self._chat_list()
        else:
            bindings = self._messages(query)
        payload = json.dumps({"results": {"bindings": bindings}}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/sparql-results+json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _chat_list(self):
        att_url = f"urn:retinue:media:signal:{MID_ATT}"
        return [
            _lit_row(chat=MARA, channel="signal", ts=TS3, type=T_IN,
                     text="Und: chunnsch immer no?", sender=MARA, atts=att_url),
            _lit_row(chat=WA_KEY, channel="whatsapp", ts=W_TS, type=T_IN,
                     text="Letzter Aufruf", sender="4176", atts=""),
        ]

    def _unread(self, query):
        # Canned semantics: a chat whose injected cutoff is still the epoch has
        # never been read (full count); a real cutoff means it was caught up.
        import re
        pairs = dict(re.findall(r'\("((?:[^"\\]|\\.)*)"\s+"([^"]+)"\^\^', query))
        counts = {MARA: "2", WA_KEY: "1"}
        return [_lit_row(chat=key, n=counts[key])
                for key, cut in pairs.items()
                if key in counts and cut.startswith("1970-")]

    def _messages(self, query):
        if f'"{MARA}"' not in query:
            return []
        if "FILTER(?ts <" in query:  # the ?before page
            return [_lit_row(m="urn:retinue:inbound:signal:000", type=T_IN,
                             text="older message", sender=MARA, mid="900", ts=TS0)]
        # Both shapes on one message: the URN a gateway records today and a
        # legacy URL written when gateways still declared their own host.
        att_url = (f"urn:retinue:media:signal:{MID_ATT} "
                   f"http://signal-gateway:8090/media/{MID_ATT3}")
        return [  # newest first, as ORDER BY DESC would
            _lit_row(m="urn:retinue:inbound:signal:003", type=T_IN,
                     text="Und: chunnsch immer no?", sender=MARA, mid="903",
                     ts=TS3, atts=att_url),
            _lit_row(m="urn:retinue:outbound:signal:002", type=T_OUT,
                     text="tönt guet!", author="device", mid="902", ts=TS2),
            _lit_row(m="urn:retinue:inbound:signal:001", type=T_IN,
                     text="Znacht am Samstig?", sender=MARA, mid="901", ts=TS1),
        ]


class _MockGateway(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        STATE["gw_requests"].append(("GET", self.path,
                                     self.headers.get("Authorization", "")))
        if self.path.rstrip("/") in ("", "/health"):
            self._json(200, {"status": "ok", "configured": True,
                             "connected": True, "mode": STATE["gw_mode"],
                             "account": STATE["gw_account"]})
            return
        if self.path.startswith("/media/"):
            if self.headers.get("Authorization", "") != "Bearer gw-secret":
                self._json(401, {"error": "unauthorized"})
                return
            data = b"JPEGBYTES"
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        STATE["gw_requests"].append(("POST", self.path,
                                     self.headers.get("Authorization", "")))
        if self.path.rstrip("/") == "/send":
            STATE["sent"].append(payload)
            answer = {"status": "sent", "recipient": payload.get("recipient"),
                      "message_id": str(776 + len(STATE["sent"])),
                      "ts": time.time()}
            if payload.get("images"):
                # The real gateway persists each image into its ledger media
                # store and reports the stored references back.
                answer["attachments"] = [f"urn:retinue:media:signal:{MID_ATT2}"]
            self._json(200, answer)
            return
        self._json(404, {"error": "not found"})

    def _json(self, status, body):
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _load_gateway(tmp: Path, sparql_port: int, gw_port: int):
    os.environ["QLEVER_LIFE_URL"] = f"http://127.0.0.1:{sparql_port}"
    os.environ["SIGNAL_GATEWAY_BASE_URL"] = f"http://127.0.0.1:{gw_port}"
    os.environ["SIGNAL_GATEWAY_TOKEN"] = "gw-secret"
    os.environ.pop("WHATSAPP_GATEWAY_BASE_URL", None)
    os.environ.pop("TELEGRAM_GATEWAY_BASE_URL", None)
    os.environ.pop("MESSENGER_GATEWAYS", None)
    os.environ.pop("CHATS_INGEST_TOKEN", None)
    os.environ["CHAT_STATE_DIR"] = str(tmp / "chat-state")
    os.environ["CHAT_LIST_CACHE_SECONDS"] = "0"
    os.environ["CONVERSATION_BACKEND_TOKEN"] = "agent-token"
    os.environ["CONVERSATIONS_DIR"] = str(tmp / "convs")
    os.environ["CONVERSATION_DIR"] = str(tmp / "convlog")
    os.environ["CHAMBERS_DIR"] = str(tmp / "chambers")
    os.environ["WEB_GATEWAY_STATE"] = str(tmp / "state.json")
    os.environ["PUSH_DIR"] = str(tmp / "push")
    (tmp / "chambers").mkdir(parents=True, exist_ok=True)
    if "markdown_it" not in sys.modules:
        try:
            import markdown_it  # noqa: F401
        except ImportError:
            stub = types.ModuleType("markdown_it")
            stub.MarkdownIt = object
            sys.modules["markdown_it"] = stub
    sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "web_gateway_chats_under_test", SCRIPTS_DIR / "web-gateway.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _http(base, method, path, body=None, headers=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"raw": raw}


def _quote(chat_id):
    return urllib.parse.quote(chat_id, safe="")


PUSHES: list = []


def test_chat_list_contract(base, wg):
    status, body = _http(base, "GET", "/chats")
    assert status == 200, body
    chats = {c["id"]: c for c in body["chats"]}
    assert set(chats) == {CHAT1, CHAT2}
    # Ordered by last activity, newest first.
    assert [c["id"] for c in body["chats"]] == [CHAT1, CHAT2]
    c1, c2 = chats[CHAT1], chats[CHAT2]
    # ChatSummary contract fields, exactly the fixture's shape.
    for c in (c1, c2):
        assert {"id", "channel", "name", "group", "unread", "archived",
                "muted", "last", "draft", "companion", "messages"} <= set(c)
        assert c["companion"] is None, "no companion thread until one is asked for"
    assert c1["channel"] == "signal" and c1["group"] is False
    assert c2["channel"] == "whatsapp" and c2["group"] is True
    assert c1["unread"] == 2 and c2["unread"] == 1
    assert c1["draft"] is None and not c1["archived"] and not c1["muted"]
    # No name has passed by yet — the honest fallback is the key.
    assert c1["name"] == MARA
    assert c1["last"]["direction"] == "in" and c1["last"]["kind"] == "text"
    assert c1["last"]["ts"] == TS3
    # The messages URL is served here and percent-encoded (keys carry @ : +).
    assert c1["messages"] == "/chats/" + _quote(CHAT1) + "/messages"
    status, doc = _http(base, "GET", c1["messages"])
    assert status == 200
    # The unread query injected the epoch cutoff for never-read chats.
    unread_qs = [q for q in STATE["queries"] if "VALUES (?chat ?cut)" in q]
    assert unread_qs and MARA in unread_qs[-1] and "1970-01-01" in unread_qs[-1]
    print("PASS test_chat_list_contract")


def test_chat_messages_contract(base, wg):
    status, body = _http(base, "GET", "/chats/" + _quote(CHAT1) + "/messages")
    assert status == 200, body
    assert body["chat"]["id"] == CHAT1
    msgs = body["messages"]
    # Ascending, the whole canned page.
    assert [m["ts"] for m in msgs] == [TS1, TS2, TS3]
    assert [m["id"] for m in msgs] == ["901", "902", "903"]
    m_in, m_out, m_att = msgs
    assert m_in["direction"] == "in" and m_in["sender"] == MARA
    assert m_in["chat"] == CHAT1
    assert m_out["direction"] == "out" and m_out["author"] == "device"
    assert "sender" not in m_out
    # The attachment reference is rewritten to the authenticated proxy — never
    # the gateway's internal token-gated URL.
    atts = {a["id"]: a for a in m_att["attachments"]}
    assert set(atts) == {MID_ATT, MID_ATT3}
    # A host-free URN and a legacy URL alike are served through the chat's own
    # account — the legacy record's recorded host ("signal-gateway", the value
    # that was wrong for every extra account) is deliberately overridden.
    assert atts[MID_ATT]["url"] == f"/chats/media/127.0.0.1/{MID_ATT}"
    assert atts[MID_ATT3]["url"] == f"/chats/media/127.0.0.1/{MID_ATT3}"
    # The sniffed intrinsic size rides on the blob that has a .meta sidecar.
    assert atts[MID_ATT]["width"] == 320 and atts[MID_ATT]["height"] == 420
    assert "width" not in atts[MID_ATT3], "no sidecar, no guessed dimensions"
    print("PASS test_chat_messages_contract")


def test_messages_before_paging(base, wg):
    path = "/chats/" + _quote(CHAT1) + "/messages?before=" + urllib.parse.quote(TS1)
    status, body = _http(base, "GET", path)
    assert status == 200, body
    assert [m["text"] for m in body["messages"]] == ["older message"]
    paged = [q for q in STATE["queries"] if "FILTER(?ts <" in q]
    assert paged and TS1 in paged[-1]
    # A malformed cursor is rejected, not interpolated.
    status, _ = _http(base, "GET",
                      "/chats/" + _quote(CHAT1) + "/messages?before=nonsense")
    assert status == 400
    print("PASS test_messages_before_paging")


def test_read_watermark(base, wg):
    status, body = _http(base, "POST", "/chats/" + _quote(CHAT1) + "/read",
                         {"ts": TS3})
    assert status == 200 and body["last_read"] == TS3
    status, body = _http(base, "GET", "/chats")
    assert status == 200
    c1 = next(c for c in body["chats"] if c["id"] == CHAT1)
    # The mock store answers 0 once a real cutoff is injected — and the query
    # carried exactly the new watermark.
    assert c1["unread"] == 0
    unread_q = [q for q in STATE["queries"] if "VALUES (?chat ?cut)" in q][-1]
    assert TS3 in unread_q
    print("PASS test_read_watermark")


def test_draft_guard(base, wg):
    draft_path = "/chats/" + _quote(CHAT1) + "/draft"
    status, body = _http(base, "POST", draft_path, {"text": "user text", "version": 0})
    assert status == 200 and body["version"] == 1
    # Stale version → 409 with the current state (nothing clobbered).
    status, body = _http(base, "POST", draft_path, {"text": "stale", "version": 0})
    assert status == 409 and body["draft"]["text"] == "user text"
    # Agent staging without a version must not overwrite user-typed text…
    internal = "/internal/chats/" + _quote(CHAT1) + "/draft"
    status, _ = _http(base, "POST", internal, {"text": "agent text", "agent": "Ara"})
    assert status == 403, "internal draft must be token-gated"
    tok = {"X-Conversation-Backend-Token": "agent-token"}
    status, body = _http(base, "POST", internal,
                         {"text": "agent text", "agent": "Ara"}, headers=tok)
    assert status == 409 and body["draft"]["text"] == "user text"
    # …the ✕ clear (empty text, current version) frees it, then staging lands
    # with the agent author tag, visible on the summary.
    status, body = _http(base, "POST", draft_path, {"text": "", "version": 1})
    assert status == 200 and body["draft"] is None
    status, body = _http(base, "POST", internal,
                         {"text": "agent text", "agent": "Ara"}, headers=tok)
    assert status == 200 and body["draft"]["author"] == "agent"
    assert body["draft"]["agent"] == "Ara"
    status, body = _http(base, "GET", "/chats")
    c1 = next(c for c in body["chats"] if c["id"] == CHAT1)
    assert c1["draft"] and c1["draft"]["text"] == "agent text"
    print("PASS test_draft_guard")


def test_rail_auth_and_notifications(base, wg):
    rail = "/internal/chats/inbound"
    cid = "telegram:555001"
    PUSHES.clear()
    # Open by default (no CHATS_INGEST_TOKEN configured).
    event = {"direction": "in", "channel": "telegram", "chat": "555001",
             "sender": "555001", "sender_name": "Luca", "group": False,
             "message_id": "e1", "text": "ciao!", "gateway": "127.0.0.1",
             "gate": {"forward": True, "reason": "whitelisted"}}
    status, body = _http(base, "POST", rail, event)
    assert status == 200 and body["pushed"] is True
    # First unread → mode "new"; the push targets the chat page.
    assert len(PUSHES) == 1
    args, kw = PUSHES[0]
    assert args[0] == "Luca"          # the rail's sender_name became the name
    assert "ciao!" in args[1]
    assert kw["mode"] == "new" and kw["tag"] == cid
    assert kw["url"].startswith("/chat.html?id=")
    # Still unread → the next arrival is a "reply".
    status, _ = _http(base, "POST", rail, dict(event, message_id="e2", text="?"))
    assert PUSHES[-1][1]["mode"] == "reply"
    # The overlay alone puts the chat in the list — the store knows nothing of
    # it yet — with the unread count and the last preview from the rail.
    status, body = _http(base, "GET", "/chats")
    c = next(c for c in body["chats"] if c["id"] == cid)
    assert c["unread"] == 2 and c["last"]["text"] == "?" and c["name"] == "Luca"
    assert body["chats"][0]["id"] == cid, "freshest chat sorts first"

    # A held gate class updates the mirror silently.
    n = len(PUSHES)
    held = dict(event, message_id="e3", text="spam",
                gate={"forward": False, "reason": "blacklisted"})
    status, body = _http(base, "POST", rail, held)
    assert status == 200 and body["pushed"] is False and len(PUSHES) == n

    # Muted silences; archived+muted stays archived. Archived alone un-archives.
    wg._CHAT_STATE.set_flags(cid, archived=True, muted=True)
    status, body = _http(base, "POST", rail, dict(event, message_id="e4"))
    assert body["pushed"] is False and len(PUSHES) == n
    assert wg._CHAT_STATE.get(cid)["archived"] is True
    wg._CHAT_STATE.set_flags(cid, muted=False)
    status, body = _http(base, "POST", rail, dict(event, message_id="e5"))
    assert body["pushed"] is True
    assert wg._CHAT_STATE.get(cid)["archived"] is False, \
        "an arrival un-archives unless muted"

    # An own-device echo advances the watermark (no push) — the user was on
    # their phone in that chat.
    echo_ts = time.time()
    n = len(PUSHES)
    status, body = _http(base, "POST", rail,
                         {"direction": "out", "channel": "telegram",
                          "chat": "555001", "author": "device",
                          "message_id": "e6", "text": "ok", "ts": echo_ts})
    assert status == 200 and len(PUSHES) == n
    doc = wg._CHAT_STATE.get(cid)
    assert doc["last_read"] == wg.chat_state_mod.iso_z(echo_ts)

    # Token mode: once configured, a wrong token is refused, the right one not.
    wg.CHATS_INGEST_TOKEN = "railtok"
    try:
        status, _ = _http(base, "POST", rail, dict(event, message_id="e7"))
        assert status == 403
        status, _ = _http(base, "POST", rail, dict(event, message_id="e7"),
                          headers={"X-Conversation-Backend-Token": "railtok"})
        assert status == 200
    finally:
        wg.CHATS_INGEST_TOKEN = ""
    print("PASS test_rail_auth_and_notifications")


def test_send_user_direct(base, wg):
    # This rail event reports no account, so it deliberately stamps nothing:
    # routing rests on the channel having exactly one inbox account, which is
    # the unambiguous case a single-account deployment always hits.
    rail_event = {"direction": "in", "channel": "signal", "chat": MARA,
                  "sender": MARA, "sender_name": "Mara Meier", "group": False,
                  "message_id": "m-in", "text": "hoi", "gateway": "127.0.0.1",
                  "gate": {"forward": True, "reason": "whitelisted"}}
    _http(base, "POST", "/internal/chats/inbound", rail_event)
    STATE["sent"].clear()
    status, msg = _http(base, "POST", "/chats/" + _quote(CHAT1) + "/send",
                        {"text": "bis Samstag!"})
    assert status == 200, msg
    # The one hop carried the user authorship and the chat key verbatim; no
    # voice rendering for a chat send.
    assert len(STATE["sent"]) == 1
    sent = STATE["sent"][0]
    assert sent["author"] == "user" and sent["recipient"] == MARA
    assert sent["voice"] is False and sent["message"] == "bis Samstag!"
    # The returned Message is contract-shaped and carries the gateway's
    # recorded ledger identity.
    assert msg["direction"] == "out" and msg["author"] == "user"
    assert msg["chat"] == CHAT1 and msg["id"] == "777"
    # Draft cleared, watermark advanced to the send. The send writes no
    # gateway stamp: it has no account evidence of its own, and a stamp that is
    # merely "what we resolved last time" is what made the incident sticky.
    doc = wg._CHAT_STATE.get(CHAT1)
    assert doc["draft"] is None
    assert doc.get("gateway") is None, "a send must not stamp an account"
    assert doc["last_read"] == msg["ts"]
    # The sent message is in the merged view before the store indexes it: the
    # list preview flips to the outbound, and the messages page contains it.
    status, body = _http(base, "GET", "/chats")
    c1 = next(c for c in body["chats"] if c["id"] == CHAT1)
    assert c1["last"]["direction"] == "out" and c1["last"]["author"] == "user"
    status, body = _http(base, "GET", "/chats/" + _quote(CHAT1) + "/messages")
    assert any(m["id"] == "777" and m["text"] == "bis Samstag!"
               for m in body["messages"])
    print("PASS test_send_user_direct")


def test_send_images(base, wg):
    """Images ride the chat send: validated here, persisted by the gateway,
    and the stored references come back proxied so the sent image renders
    immediately."""
    send_path = "/chats/" + _quote(CHAT1) + "/send"
    png_b64 = "iVBORw0KGgo="  # tiny valid base64; content is the gateway's concern

    # Validation matrix — each rejected before any gateway round trip.
    n = len(STATE["sent"])
    status, body = _http(base, "POST", send_path, {"text": "x", "images": "nope"})
    assert status == 400, body
    status, body = _http(base, "POST", send_path,
                         {"text": "x", "images": [{"data": png_b64}] * 6})
    assert status == 400 and "at most" in body["error"]
    status, body = _http(base, "POST", send_path,
                         {"text": "x", "images": [{"data": "not!!base64"}]})
    assert status == 400 and "base64" in body["error"]
    status, body = _http(base, "POST", send_path, {"text": "x", "images": ["str"]})
    assert status == 400
    old_cap = wg.MAX_ATTACHMENT_BYTES
    wg.MAX_ATTACHMENT_BYTES = 4
    try:
        status, body = _http(base, "POST", send_path,
                             {"text": "x", "images": [{"data": png_b64}]})
        assert status == 400 and "too large" in body["error"]
    finally:
        wg.MAX_ATTACHMENT_BYTES = old_cap
    assert len(STATE["sent"]) == n, "rejected sends must never reach the gateway"

    # A valid image send: the gateway payload carries the images verbatim with
    # the user authorship; the response Message carries the stored reference,
    # rewritten onto the authenticated proxy.
    status, msg = _http(base, "POST", send_path,
                        {"text": "lueg mal",
                         "images": [{"content_type": "image/png", "data": png_b64}]})
    assert status == 200, msg
    sent = STATE["sent"][-1]
    assert sent["author"] == "user" and sent["message"] == "lueg mal"
    assert sent["images"] == [{"content_type": "image/png", "data": png_b64}]
    assert msg["attachments"][0]["url"] == f"/chats/media/127.0.0.1/{MID_ATT2}"
    assert msg["attachments"][0]["id"] == MID_ATT2
    # The overlay carries the attachment too: the merged view renders the sent
    # image before the store indexes it, and the list preview knows the kind.
    status, body = _http(base, "GET", "/chats/" + _quote(CHAT1) + "/messages")
    mine = next(m for m in body["messages"] if m["id"] == msg["id"])
    assert mine["attachments"][0]["url"] == f"/chats/media/127.0.0.1/{MID_ATT2}"
    # An image-only send (no text) is valid.
    status, only = _http(base, "POST", send_path,
                         {"images": [{"content_type": "image/png", "data": png_b64}]})
    assert status == 200 and only["text"] == "" and only["attachments"]
    print("PASS test_send_images")

def test_control_gateway_refused(base, wg):
    """A control account is never a chat identity: refuse, never mis-send.

    This is the incident in miniature — the chat is stamped with an account
    that turns out to be the system bot. The send must not go out as it."""
    STATE["sent"].clear()
    STATE["gw_mode"] = "control"
    wg._gw_identity.clear()
    try:
        status, body = _http(base, "POST", "/chats/" + _quote(CHAT1) + "/send",
                             {"text": "must not go out as the bot"})
        assert status == 409, (status, body)
        assert "no inbox-mode gateway for channel signal" == body["error"], body
        assert STATE["sent"] == [], "a refused send must never reach a gateway"
    finally:
        STATE["gw_mode"] = "inbox"
        wg._gw_identity.clear()
    # With the account back in inbox mode the same send goes through.
    status, msg = _http(base, "POST", "/chats/" + _quote(CHAT1) + "/send",
                        {"text": "now it may"})
    assert status == 200 and len(STATE["sent"]) == 1
    assert STATE["sent"][0]["author"] == "user"
    print("PASS test_control_gateway_refused")


def test_rail_attributes_by_account(base, wg):
    """A rail event whose self-reported slug is wrong is still attributed to
    the account that actually sent it — the root cause of the incident."""
    cid = "signal:+41790008888"
    status, body = _http(base, "POST", "/internal/chats/inbound",
                         {"direction": "in", "channel": "signal",
                          "chat": "+41790008888", "sender": "+41790008888",
                          "sender_name": "Nina", "message_id": "acct-1",
                          "text": "hoi",
                          # What the mis-defaulted gateway reports: its own
                          # account, but the BUILT-IN's slug.
                          "account": STATE["gw_account"],
                          "gateway": "signal-gateway",
                          "gate": {"forward": True, "reason": "whitelisted"}})
    assert status == 200, body
    # The registry entry for this account is the mock's slug, not the slug the
    # event claimed — and the stamp is marked as account-derived, which is what
    # makes it authoritative later.
    doc = wg._CHAT_STATE.get(cid)
    assert doc["gateway"] == "127.0.0.1"
    assert doc["gateway_source"] == "account"
    # That marked stamp routes even where the channel has several candidates,
    # and survives the repair pass.
    assert wg._chat_gateway(doc, "signal")[0] == "127.0.0.1"
    assert wg.repair_chat_gateway_stamps() == 0
    # An account the registry does not serve leaves the stamp alone rather
    # than writing a wrong one.
    status, _ = _http(base, "POST", "/internal/chats/inbound",
                      {"direction": "in", "channel": "signal",
                       "chat": "+41790007777", "sender": "+41790007777",
                       "message_id": "acct-2", "text": "hi",
                       "account": "+15559990000", "gateway": "signal-gateway",
                       "gate": {"forward": True, "reason": "whitelisted"}})
    assert status == 200
    unknown = wg._CHAT_STATE.get("signal:+41790007777")
    assert unknown["gateway"] is None and unknown["gateway_source"] is None
    print("PASS test_rail_attributes_by_account")


def test_companion_endpoint(base, wg):
    """The chat's linked conversation: created once, returned forever after."""
    path = "/chats/" + _quote(CHAT2) + "/companion"
    status, body = _http(base, "POST", path, {})
    assert status == 201, body
    cid = body["id"]
    assert cid

    # Idempotent: a second open returns the same thread, not a new one.
    status, again = _http(base, "POST", path, {})
    assert status == 200 and again["id"] == cid, again

    # And it is recorded on the chat, so a client can find it without posting.
    status, chats = _http(base, "GET", "/chats")
    summary = next(c for c in chats["chats"] if c["id"] == CHAT2)
    assert summary["companion"] == cid
    # Every other chat is unaffected.
    other = next(c for c in chats["chats"] if c["id"] == CHAT1)
    assert other["companion"] is None

    # It is an ordinary conversation the dashboard drives through
    # /conversations — no second read API for companion threads.
    status, conv = _http(base, "GET", f"/conversations/{cid}")
    assert status == 200, conv
    assert conv["kind"] == "companion" and conv["chat"] == CHAT2
    assert conv["messages"], "the pane would open on an empty thread"

    # But it never shows up where the user browses their own threads.
    status, listing = _http(base, "GET", "/conversations?all=1")
    assert cid not in {c["id"] for c in listing["conversations"]}
    status, listing = _http(base, "GET", "/conversations?all=1&kind=edit")
    assert cid not in {c["id"] for c in listing["conversations"]}
    status, listing = _http(base, "GET", "/conversations?all=1&kind=companion")
    assert cid in {c["id"] for c in listing["conversations"]}

    status, body = _http(base, "POST", "/chats/nothing/companion", {})
    assert status == 404, body
    print("PASS test_companion_endpoint")


def test_media_proxy(base, wg):
    req = urllib.request.Request(base + f"/chats/media/127.0.0.1/{MID_ATT}")
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.status == 200
        assert resp.headers.get("Content-Type") == "image/jpeg"
        assert resp.read() == b"JPEGBYTES"
    # The proxy authenticated with the registry token — the browser never saw it.
    media_reqs = [r for r in STATE["gw_requests"] if r[1].startswith("/media/")]
    assert media_reqs and media_reqs[-1][2] == "Bearer gw-secret"
    # An unknown slug is a 404, and a malformed media id never routes at all.
    status, _ = _http(base, "GET", f"/chats/media/nope/{MID_ATT}")
    assert status == 404
    status, _ = _http(base, "GET", "/chats/media/127.0.0.1/../etc/passwd")
    assert status == 404
    print("PASS test_media_proxy")


def test_store_down_is_502(base, wg):
    STATE["fail"] = True
    try:
        status, body = _http(base, "GET", "/chats")
        assert status == 502 and "life store" in body["error"]
        status, body = _http(base, "GET", "/chats/" + _quote(CHAT1) + "/messages")
        assert status == 502
    finally:
        STATE["fail"] = False
    print("PASS test_store_down_is_502")


def main():
    sparql = _serve(_MockSparql)
    gw = _serve(_MockGateway)
    STATE["gw_port"] = gw.server_address[1]
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp), sparql.server_address[1],
                           gw.server_address[1])
        # Capture pushes instead of talking to a push service.
        wg.push_notify.enabled = lambda: True
        wg.push_notify.notify_async = lambda *a, **k: PUSHES.append((a, k))
        # Local media sidecars for the signal channel mount (type/size hints).
        media_dir = Path(tmp) / "chambers" / "_generated" / "messenger" / "signal" / "media"
        media_dir.mkdir(parents=True)
        (media_dir / MID_ATT).write_bytes(b"x" * 717)
        (media_dir / (MID_ATT + ".type")).write_text("image/jpeg\n")
        (media_dir / (MID_ATT + ".meta")).write_text('{"width": 320, "height": 420}\n')

        server = ThreadingHTTPServer(("127.0.0.1", 0), wg.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"

        test_chat_list_contract(base, wg)
        # The sidecar hints ride on the shaped attachments — including the
        # intrinsic size the store sniffed at ingest.
        status, body = _http(base, "GET", "/chats/" + _quote(CHAT1) + "/messages")
        att = next(a for a in body["messages"][-1]["attachments"]
                   if a["id"] == MID_ATT)
        assert att.get("type") == "image/jpeg" and att.get("size") == 717
        assert att.get("width") == 320 and att.get("height") == 420
        test_chat_messages_contract(base, wg)
        test_messages_before_paging(base, wg)
        test_read_watermark(base, wg)
        test_draft_guard(base, wg)
        test_rail_auth_and_notifications(base, wg)
        test_send_user_direct(base, wg)
        test_send_images(base, wg)

        test_companion_endpoint(base, wg)

        test_control_gateway_refused(base, wg)
        test_rail_attributes_by_account(base, wg)
        test_media_proxy(base, wg)
        test_store_down_is_502(base, wg)
        server.shutdown()
    sparql.shutdown()
    gw.shutdown()
    print("all chat-api tests passed")


if __name__ == "__main__":
    main()
