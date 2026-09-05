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
- POST /chats/<id>/send: the message reaches the gateway as author "user"
  with nothing that skips its send policy — under `verify` it is queued and
  released through the gateway's own approve call in the same request — the
  draft clears,
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
# Two accounts of one channel, for the merge cases: the same chat key under
# each must stay two chats (see test_accounts_do_not_merge).
ACCT_A = "+41791112233"   # the mock gateway's own account
ACCT_B = "+41764445566"   # a second, unregistered account of the same channel
# Its own peer, untouched by the other tests' sends and overlay entries.
MERGE_PEER = "+41791230000"
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
               "gw_mode": "inbox", "gw_account": "+41791112233",
               # The mock account's send policy: "allow" sends on the first
               # hop, anything else queues and must be approved.
               "gw_policy": "allow", "pending": {}, "approved": [],
               # When set, an approved send never leaves "sending" — the
               # gateway that does not confirm in time.
               "gw_never_confirms": False,
               # When set, replaces the canned chat-list rows for one test.
               "list_rows": None,
               # When set, replaces the canned per-chat message rows.
               "msg_rows": None,
               # What each queued send will produce once it completes.
               "outcomes": {}}


def _iso_now(offset=0.0):
    from datetime import datetime, timedelta, timezone
    return ((datetime.now(timezone.utc) + timedelta(seconds=offset))
            .isoformat(timespec="seconds").replace("+00:00", "Z"))


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
        if "VALUES (?chat ?account ?cut)" in query:
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
        if STATE.get("list_rows") is not None:
            return STATE["list_rows"]
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
        # Rows are (key, account, cutoff) — the account is part of the row key,
        # so a count comes back tagged with the account it was asked for.
        import re
        rows = re.findall(
            r'\("((?:[^"\\]|\\.)*)"\s+"((?:[^"\\]|\\.)*)"\s+"([^"]+)"\^\^', query)
        counts = {(MARA, ""): "2", (WA_KEY, ""): "1",
                  (MERGE_PEER, ACCT_A): "2", (MERGE_PEER, ACCT_B): "1"}
        return [_lit_row(chat=key, account=acct, n=counts[(key, acct)])
                for key, acct, cut in rows
                if (key, acct) in counts and cut.startswith("1970-")]

    def _messages(self, query):
        if STATE.get("msg_rows") is not None:
            return STATE["msg_rows"]
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


_PENDING_RE = __import__("re").compile(
    r"^/pending-sends/([0-9a-f]{32})(?:/(approve|reject))?/?$")


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
        m = _PENDING_RE.match(self.path)
        if m and not m.group(2):
            entry = STATE["pending"].get(m.group(1))
            if entry is None:
                self._json(404, {"error": "not found"})
                return
            self._json(200, dict(entry))
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
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        payload = json.loads(raw) if raw else {}
        STATE["gw_requests"].append(("POST", self.path,
                                     self.headers.get("Authorization", "")))
        if self.path.rstrip("/") == "/send":
            STATE["sent"].append(payload)
            outcome = {"message_id": str(776 + len(STATE["sent"])),
                       "sent_at": time.time()}
            if payload.get("images"):
                # The real gateway persists each image into its ledger media
                # store and reports the stored references back.
                outcome["attachments"] = [f"urn:retinue:media:signal:{MID_ATT2}"]
            if STATE["gw_policy"] == "allow":
                self._json(200, {"status": "sent",
                                 "recipient": payload.get("recipient"),
                                 "message_id": outcome["message_id"],
                                 "ts": outcome["sent_at"],
                                 "attachments": outcome.get("attachments", [])})
                return
            # Anything stricter queues, exactly as the real gateway does under
            # `verify`: nothing the caller can put in the body skips this.
            rid = f"{len(STATE['pending']) + 1:032x}"
            # The outcome is NOT on the entry yet: the real gateway records the
            # message id and the sent-at instant only once the send has
            # actually gone out (an approval executes off the request). An
            # entry that carried them from the start would hide exactly the
            # case where a caller has to cope without them.
            STATE["pending"][rid] = {"id": rid, "status": "pending"}
            STATE["outcomes"][rid] = outcome
            self._json(202, {"status": "pending_approval", "request_id": rid,
                             "approval_url": f"/sends/signal/{rid}"})
            return
        m = _PENDING_RE.match(self.path)
        if m and m.group(2) == "approve":
            entry = STATE["pending"].get(m.group(1))
            if entry is None:
                self._json(404, {"error": "pending send not found"})
                return
            # Approval executes off the request at the real gateway, so the
            # caller sees "sending" and reads the outcome back afterwards.
            entry["status"] = "sending"
            STATE["approved"].append(m.group(1))
            self._json(200, dict(entry))
            # A gateway still sending when the caller gives up waiting is the
            # unconfirmed path: it stays "sending" for as long as the test
            # wants, and its ledger row turns up later with its own identity.
            if not STATE.get("gw_never_confirms"):
                entry.update(STATE["outcomes"].get(m.group(1)) or {})
                entry["status"] = "approved"
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
    # These tests drive the handler directly over loopback, which the
    # user-authority gate refuses (see tests/test_chat_send_authority.py).
    # Pinning the harness's own peer is the same knob a deployment uses to name
    # its reverse proxy, so the send paths run exactly as the dashboard reaches
    # them; the gate itself is covered in its own file.
    os.environ["EDGE_PROXY_PEERS"] = "127.0.0.1"
    os.environ["CHAT_STATE_DIR"] = str(tmp / "chat-state")
    os.environ["CHAT_LIST_CACHE_SECONDS"] = "0"
    os.environ["CONVERSATION_BACKEND_TOKEN"] = "agent-token"
    # These checks pin the pre-model push behaviour (every whitelisted arrival
    # notifies); the attention model's gating has its own file.
    os.environ["ATTENTION_PUSH_GATE"] = "0"
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
        assert {"id", "channel", "account", "key", "name", "group", "unread",
                "archived", "muted", "last", "draft", "companion",
                "messages"} <= set(c)
        assert c["companion"] is None, "no companion thread until one is asked for"
    # The two halves of a chat's identity, both surfaced: the peer (stable
    # across accounts, so the client can colour one person one way) and the
    # account, null while the records carry none.
    assert c1["key"] == MARA and c1["account"] is None
    assert c2["key"] == WA_KEY and c2["account"] is None
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
    unread_qs = [q for q in STATE["queries"] if "VALUES (?chat ?account ?cut)" in q]
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
    unread_q = [q for q in STATE["queries"] if "VALUES (?chat ?account ?cut)" in q][-1]
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


def test_draft_undo_endpoint(base, wg):
    """POST /chats/<id>/draft/undo puts back what the ✕ removed, author and all.

    The endpoint exists because the draft POST can only ever stamp "user": a
    client resubmitting the text would silently reattribute a draft Ara staged
    to the user about to send it in their own name.
    """
    draft_path = "/chats/" + _quote(CHAT1) + "/draft"
    undo_path = draft_path + "/undo"
    internal = "/internal/chats/" + _quote(CHAT1) + "/draft"
    tok = {"X-Conversation-Backend-Token": "agent-token"}

    # Nothing cleared yet → nothing to give back, and the current state is
    # returned so the client can settle on the truth rather than guess.
    status, body = _http(base, "POST", undo_path, {})
    assert status == 409 and "draft" in body and "version" in body, body

    # Ara stages a draft; the user's ✕ clears it; the undo restores it.
    status, staged = _http(base, "POST", internal,
                           {"text": "Samstag passt.", "agent": "Ara"}, headers=tok)
    assert status == 200 and staged["draft"]["author"] == "agent"
    status, cleared = _http(base, "POST", draft_path,
                            {"text": "", "version": staged["version"]})
    assert status == 200 and cleared["draft"] is None

    status, back = _http(base, "POST", undo_path, {})
    assert status == 200, back
    assert back["draft"]["text"] == "Samstag passt."
    # The whole point: it is still Ara's, so a reload still says so.
    assert back["draft"]["author"] == "agent", back["draft"]
    assert back["draft"]["agent"] == "Ara", back["draft"]
    assert back["version"] > cleared["version"]
    # And it is on the chat summary, where the list reads it.
    summary = wg._chat_summary(CHAT1)
    assert summary["draft"]["author"] == "agent", summary["draft"]

    # Spent: a second undo refuses and leaves the restored draft alone.
    status, again = _http(base, "POST", undo_path, {})
    assert status == 409 and again["draft"]["text"] == "Samstag passt."

    # Tidy up for the tests that follow.
    _http(base, "POST", draft_path, {"text": "", "version": back["version"]})
    print("PASS test_draft_undo_endpoint")


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
    # Authorship is all it carries: no field in this body asks the gateway to
    # skip its policy, because no such field exists any more.
    assert "edge_verified" not in sent and "user_approved" not in sent
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
    # The event carries an account, so the chat it lands in is that account's:
    # the id is composed from the same value the gateway stamps as kb:account
    # on this very message's ledger record.
    cid = wg.chat_state_mod.make_chat_id("signal", "+41790008888",
                                         STATE["gw_account"])
    assert cid == "signal:~+41791112233:+41790008888", cid
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
    # …but the chat is still that account's own: an id is composed from the
    # account the event asserts, which is a fact about the sender, while the
    # gateway stamp is a lookup in the reader's registry that can simply miss.
    unknown_id = wg.chat_state_mod.make_chat_id("signal", "+41790007777",
                                                "+15559990000")
    unknown = wg._CHAT_STATE.get(unknown_id)
    assert unknown["gateway"] is None and unknown["gateway_source"] is None
    # And it cannot be sent: an account the registry does not serve is refused
    # outright rather than falling back to the channel's other identity.
    slug, gw, err = wg._chat_gateway(unknown, "signal", "+15559990000")
    assert gw is None and slug is None
    assert "+15559990000" in err and "not a configured gateway" in err, err
    print("PASS test_rail_attributes_by_account")


def test_send_under_verify_is_queued_then_approved(base, wg):
    """The dashboard press under `verify`: one action, but through the queue.

    The gateway's policy is not skipped — the send is registered as a pending
    send and released with the gateway's own approve call, in this same
    request. So an agent that merely POSTs a gateway's /send is left with a
    queued message somebody still has to release, while the user's press still
    completes in one go and comes back with the sent Message.
    """
    STATE["sent"].clear()
    STATE["pending"].clear()
    STATE["approved"].clear()
    STATE["gw_requests"].clear()
    STATE["gw_policy"] = "verify"
    try:
        status, msg = _http(base, "POST", "/chats/" + _quote(CHAT1) + "/send",
                            {"text": "unter verify"})
        assert status == 200, msg
    finally:
        STATE["gw_policy"] = "allow"
    # One send registered, and released through the gateway's own endpoint.
    assert len(STATE["sent"]) == 1, STATE["sent"]
    assert len(STATE["approved"]) == 1, STATE["approved"]
    rid = STATE["approved"][0]
    assert STATE["pending"][rid]["status"] == "approved"
    approve_calls = [pth for verb, pth, _ in STATE["gw_requests"]
                     if verb == "POST" and pth.endswith("/approve")]
    assert approve_calls == [f"/pending-sends/{rid}/approve"], approve_calls
    # The outcome survives the queue: the returned Message carries the id the
    # gateway recorded on the approved entry, not a synthetic one.
    assert msg["id"] == STATE["pending"][rid]["message_id"]
    assert msg["text"] == "unter verify" and msg["author"] == "user"
    # And it is in the merged view, exactly as a direct send would be.
    status, body = _http(base, "GET", "/chats/" + _quote(CHAT1) + "/messages")
    assert any(m["id"] == msg["id"] for m in body["messages"])
    print("PASS test_send_under_verify_is_queued_then_approved")


def test_unconfirmed_send_is_not_rendered_twice(base, wg):
    """A send the gateway never confirms is reported, and reported honestly.

    Waiting forever is not an option and reporting a failure is the worse
    error — the words would go back into the composer and the user would send
    them a second time, for real. So the send is reported without an identity,
    marked `unconfirmed`.

    The bug that marking closes: such a send has neither the channel's message
    id nor the instant it accepted the message, so neither of the merge's two
    dedup tests can ever match the ledger row that eventually appears, and the
    user saw their own message twice.
    """
    STATE["sent"].clear()
    STATE["pending"].clear()
    STATE["approved"].clear()
    STATE["gw_policy"] = "verify"
    STATE["gw_never_confirms"] = True
    prev_timeout = wg.CHAT_SEND_CONFIRM_TIMEOUT
    wg.CHAT_SEND_CONFIRM_TIMEOUT = 0.3   # don't sit out the real 30s
    try:
        status, msg = _http(base, "POST", "/chats/" + _quote(CHAT1) + "/send",
                            {"text": "im Flug"})
        assert status == 200, msg
        # Reported as sent — the message is very likely on the wire — but the
        # response says plainly that its identity is not known.
        assert msg["unconfirmed"] is True, msg
        assert msg["text"] == "im Flug" and msg["author"] == "user"
        rid = STATE["approved"][0]
        assert STATE["pending"][rid]["status"] == "sending"

        # The store now indexes the record the gateway did write, with the
        # channel's own id and instant — neither of which the response carried.
        # Stamped a few seconds back, as the real one is: the channel accepted
        # the message while this caller was still waiting, so the record
        # predates the moment it gave up — by roughly the confirm timeout. That
        # gap is the whole point: it is why the (ts, text) fallback cannot match
        # these two, and why the unconfirmed rule has to.
        ledger_ts = _iso_now(-5)
        STATE["msg_rows"] = [
            _lit_row(m="urn:retinue:outbound:signal:777", type=T_OUT,
                     text="im Flug", author="user", mid="7770", ts=ledger_ts),
        ]
        try:
            status, body = _http(base, "GET",
                                 "/chats/" + _quote(CHAT1) + "/messages")
            assert status == 200
            mine = [m for m in body["messages"] if m["text"] == "im Flug"]
            assert len(mine) == 1, [m["id"] for m in mine]
            # And the one that survives is the real record, not the placeholder.
            assert mine[0]["id"] == "7770", mine[0]
        finally:
            STATE["msg_rows"] = None
    finally:
        wg.CHAT_SEND_CONFIRM_TIMEOUT = prev_timeout
        STATE["gw_never_confirms"] = False
        STATE["gw_policy"] = "allow"
        STATE["pending"].clear()
        STATE["approved"].clear()
        wg._CHAT_OVERLAY.clear() if hasattr(wg._CHAT_OVERLAY, "clear") else None
    print("PASS test_unconfirmed_send_is_not_rendered_twice")


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


def test_accounts_do_not_merge(base, wg):
    """Two accounts of one channel writing to the SAME peer are two chats.

    The defect this closes: a chat key identifies a peer only within an
    account, and one channel's message volume is shared by every account on it,
    so grouping by the key alone put a second account's messages into the first
    account's conversation — timeline, unread badge and all.
    """
    STATE["list_rows"] = [
        # One peer (MARA), two accounts, plus the same peer's unattributed
        # history from before kb:account existed. Three chats, not one.
        _lit_row(chat=MERGE_PEER, account=ACCT_A, channel="signal", ts=TS3,
                 type=T_IN, text="von A", sender=MERGE_PEER, atts=""),
        _lit_row(chat=MERGE_PEER, account=ACCT_B, channel="signal", ts=TS2,
                 type=T_OUT, text="von B", author="agent", atts=""),
        _lit_row(chat=MERGE_PEER, account="", channel="signal", ts=TS1,
                 type=T_IN, text="ohne Konto", sender=MERGE_PEER, atts=""),
    ]
    try:
        wg._chats_cache_invalidate()
        _, body = _http(base, "GET", "/chats")
        rows = {c["id"]: c for c in body["chats"]}
        id_a = wg.chat_state_mod.make_chat_id("signal", MERGE_PEER, ACCT_A)
        id_b = wg.chat_state_mod.make_chat_id("signal", MERGE_PEER, ACCT_B)
        id_legacy = "signal:" + MERGE_PEER
        assert set(rows) >= {id_a, id_b, id_legacy}, sorted(rows)
        # Each shows only its own account's last message …
        assert rows[id_a]["last"]["text"] == "von A"
        assert rows[id_b]["last"]["text"] == "von B"
        assert rows[id_legacy]["last"]["text"] == "ohne Konto"
        # … carries the account so the UI can tell same-named rows apart …
        assert rows[id_a]["account"] == ACCT_A
        assert rows[id_b]["account"] == ACCT_B
        assert rows[id_legacy]["account"] is None
        # … and counts unread apart: the canned store gives A two and B one,
        # so a merged count would show three on either row.
        assert rows[id_a]["unread"] == 2, rows[id_a]["unread"]
        assert rows[id_b]["unread"] == 1, rows[id_b]["unread"]

        # The messages query asks for one account's records, never the key's.
        # Serving a page also refreshes the chat summary, so pick the messages
        # query by its own shape rather than taking whatever ran last.
        def _last_messages_query():
            return [q for q in STATE["queries"] if "ORDER BY DESC(?ts)" in q][-1]

        _http(base, "GET", "/chats/" + _quote(id_a) + "/messages")
        q = _last_messages_query()
        assert f'"{ACCT_A}"' in q and f'k:chat "{MERGE_PEER}"' in q, q
        # An unattributed chat asks for exactly the records carrying no
        # account — the empty literal — not for every record of that key.
        _http(base, "GET", "/chats/" + _quote(id_legacy) + "/messages")
        q = _last_messages_query()
        assert 'COALESCE(?acc0, "") = ""' in q, q

        # Sending goes out as the chat's own account. A's is the mock gateway's;
        # B's is not in the registry, so it is refused rather than sent as A.
        doc_a = wg._CHAT_STATE.get(id_a)
        assert wg._chat_gateway(doc_a, "signal", ACCT_A)[0] == "127.0.0.1"
        slug, gw_, err = wg._chat_gateway(wg._CHAT_STATE.get(id_b), "signal", ACCT_B)
        assert gw_ is None and slug is None and ACCT_B in err, err
    finally:
        STATE["list_rows"] = None
        wg._chats_cache_invalidate()
    print("PASS test_accounts_do_not_merge")


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
        test_draft_undo_endpoint(base, wg)
        test_rail_auth_and_notifications(base, wg)
        test_send_user_direct(base, wg)
        test_send_images(base, wg)
        test_send_under_verify_is_queued_then_approved(base, wg)

        test_unconfirmed_send_is_not_rendered_twice(base, wg)
        test_companion_endpoint(base, wg)

        test_control_gateway_refused(base, wg)
        test_rail_attributes_by_account(base, wg)
        test_accounts_do_not_merge(base, wg)
        test_media_proxy(base, wg)
        test_store_down_is_502(base, wg)
        server.shutdown()
    sparql.shutdown()
    gw.shutdown()
    print("all chat-api tests passed")


if __name__ == "__main__":
    main()
