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
- the forward path landing in the chat, not in triage: an arrival whose caller
  offers to hand it over is accepted — the chat has it, the gateway stops, held
  classes included — and a turn runs on top of that only for a **VIP sender**,
  which is a fact about the person and holds in a group exactly as in a 1:1;
  the turn's prompt carries the arrival's own text (delimited as data) and the
  chat note, asks for the message to be filed as well as answered, and never
  asks the user to rule on a correspondent, while a non-VIP, an event with no
  handover and a switched-off rail run no turn (and the last answers nothing at
  all, so the gateway keeps its own forward), the same
  message id is only ever worked once however often the rail delivers it,
  arrivals during a turn fold into one follow-up without losing a job, a reply
  that could not be stored fails its job rather than reporting delivery, and
  the turn pushes nothing the arrival has already pushed;
- the read watermark, the version-guarded draft (409), agent staging;
- POST /chats/<id>/flags: archiving takes a chat out of the list and an
  arrival brings it back, muting archives it in the same breath and keeps it
  out however busy it gets, un-muting leaves it archived, restoring clears
  both, and a non-boolean is refused; and a mute racing an arrival never
  half-applies (the server is threaded, so the un-archive decides under the
  state lock rather than from a snapshot);
- POST /chats/<id>/companion: idempotent create-or-get, the id surfacing on the
  ChatSummary, and the thread staying out of the default conversation list;
- POST /chats/<id>/send: the message reaches the gateway as author "user"
  with nothing that skips its send policy — under `verify` it is queued and
  released through the gateway's own approve call in the same request — the
  draft clears,
  the watermark advances, and the sent message is returned and visible in the
  merged view before the store knows it (the overlay);
- the life store down: the last good list is served for a bounded while
  (reads made meanwhile still clear badges), then an honest 502 — never a raw
  fallback.

Standalone (stdlib + the gateway module's own deps):

    python3 tests/test_chat_api.py
"""
import base64
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
# A peer of the store-outage test alone, so its unread count is never read
# down by another test first (see test_store_down_serves_recent_list).
STALE_PEER = "+41791239999"
MID_ATT = "ab" * 16   # recorded as a host-free urn:retinue:media:… (today's shape)
MID_ATT2 = "cd" * 16  # the blob the mock gateway "stores" for an images send
MID_ATT3 = "ef" * 16  # a legacy http://<service>/media/<id> record on disk
MID_SIB = "12" * 16   # a blob only the channel's second account holds
MID_NONE = "34" * 16  # a blob no gateway holds
SIB_ACCOUNT = "+41765550000"  # the second Signal account (see _MockSibling)
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
               # The blobs the mock gateway holds; anything else is a 404,
               # exactly as a real gateway answers for media it never stored.
               "gw_media": {MID_ATT, MID_ATT2, MID_ATT3},
               # The second account's mock (mode, request log).
               "sib_mode": "inbox", "sib_requests": [],
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
            bindings = self._chat_heads()
        elif "VALUES ?m {" in query:
            bindings = self._records(query)
        elif "VALUES ?att" in query:
            bindings = self._media_meta(query)
        else:
            bindings = self._messages(query)
        # The live store's quirk, reproduced: QLever leaves a GROUP_CONCAT over
        # IRI values unbound (the cell is absent, not ""), so attachments only
        # reach a row when the query concatenates STR(?att). Verified on the
        # deployment; without this the mock would keep passing a query that
        # never shows a single picture in production.
        if "GROUP_CONCAT(STR(?att)" not in query:
            bindings = [{k: v for k, v in row.items() if k != "atts"}
                        for row in bindings]
        payload = json.dumps({"results": {"bindings": bindings}}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/sparql-results+json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    # The canned chat list: one row per chat with its latest message, in the
    # flat shape the tests author (and override via STATE["list_rows"]). The
    # gateway reads the list in two queries — the head message per chat, then
    # the heads' records as (m, p, o) rows — so both are derived from here:
    # row i heads the mock message IRI urn:mock:head:<i>.
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

    @staticmethod
    def _head_iri(index):
        return f"urn:mock:head:{index}"

    def _chat_heads(self):
        return [_lit_row(m=self._head_iri(i), chat=row["chat"]["value"],
                         account=(row.get("account") or {"value": ""})["value"],
                         ts=row["ts"]["value"])
                for i, row in enumerate(self._chat_list())]

    def _records(self, query):
        """The (m, p, o) rows of the asked-for head messages, from the flat
        canned rows — one attachment row per URL, as the ledger holds them."""
        import re
        asked = set(re.findall(r"<([^>]+)>", query.split("VALUES ?m {", 1)[1].split("}", 1)[0]))
        rdf_type = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
        flat = {"channel": KB + "channel", "text": KB + "text",
                "sender": KB + "sender", "author": KB + "author",
                "mid": KB + "messageId", "type": rdf_type}
        out = []
        for i, row in enumerate(self._chat_list()):
            m = self._head_iri(i)
            if m not in asked:
                continue
            for key, predicate in flat.items():
                if key in row:
                    out.append(_lit_row(m=m, p=predicate, o=row[key]["value"]))
            ts_pred = KB + ("sentAt" if row.get("type", {}).get("value") == T_OUT
                            else "receivedAt")
            out.append(_lit_row(m=m, p=ts_pred, o=row["ts"]["value"]))
            if (row.get("account") or {}).get("value"):
                out.append(_lit_row(m=m, p=KB + "account", o=row["account"]["value"]))
            for url in (row.get("atts") or {"value": ""})["value"].split(" "):
                if url:
                    out.append(_lit_row(m=m, p=KB + "attachment", o=url))
        return out

    def _media_meta(self, query):
        # What the gateways stated about their blobs, on the media IRIs: the
        # URN record carries a full statement, the legacy URL record none
        # (written before the statements existed, gateway not yet restarted).
        import re
        asked = re.findall(r"<([^>]+)>", query.split("VALUES ?att", 1)[1].split("}", 1)[0])
        stated = {f"urn:retinue:media:signal:{MID_ATT}":
                  dict(ct="image/jpeg", size="717", w="320", h="420", name="IMG_0042.jpg"),
                  f"urn:retinue:media:signal:{MID_ATT2}": dict(ct="image/png", size="2048")}
        return [_lit_row(att=iri, **stated[iri]) for iri in asked if iri in stated]

    def _unread(self, query):
        # Canned semantics: a chat whose injected cutoff is still the epoch has
        # never been read (full count); a real cutoff means it was caught up.
        # Rows are (key, account, cutoff) — the account is part of the row key,
        # so a count comes back tagged with the account it was asked for.
        import re
        rows = re.findall(
            r'\("((?:[^"\\]|\\.)*)"\s+"((?:[^"\\]|\\.)*)"\s+"([^"]+)"\^\^', query)
        counts = {(MARA, ""): "2", (WA_KEY, ""): "1",
                  (MERGE_PEER, ACCT_A): "2", (MERGE_PEER, ACCT_B): "1",
                  (STALE_PEER, ""): "3"}
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
            if self.path.rsplit("/", 1)[1] not in STATE["gw_media"]:
                self._json(404, {"error": "not found"})
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
        if self.path.rstrip("/") == "/chats/delete":
            STATE.setdefault("erase_tokens", []).append(
                self.headers.get("X-Chat-Erase-Token", ""))
            STATE.setdefault("deleted", []).append(payload)
            fail = STATE.get("gw_delete_fail")
            if fail == "http":
                self._json(500, {"error": "store volume unwritable"})
            else:
                # "partial": the gateway answers, but says a file stayed behind.
                self._json(200, {"status": "deleted", "messages": 3, "media": 1,
                                 "errors": 1 if fail == "partial" else 0,
                                 "pending_sends": 0, "recent": 1,
                                 "subjects": ["urn:retinue:inbound:signal:erased1"],
                                 "message_ids": ["d1"]})
            return
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


class _MockSibling(BaseHTTPRequestHandler):
    """The channel's second account: another Signal gateway, another store.

    It holds MID_SIB and nothing else, and answers 404 for the rest — a
    gateway *stating* it lacks a blob is what lets the web-gateway ask the
    next one instead of guessing which account stored a legacy record."""

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        STATE["sib_requests"].append(("GET", self.path,
                                      self.headers.get("Authorization", "")))
        if self.path.rstrip("/") in ("", "/health"):
            self._json(200, {"status": "ok", "configured": True,
                             "connected": True, "mode": STATE["sib_mode"],
                             "account": SIB_ACCOUNT})
            return
        if self.path == f"/media/{MID_SIB}":
            if self.headers.get("Authorization", "") != "Bearer sib-secret":
                self._json(401, {"error": "unauthorized"})
                return
            data = b"OGGBYTES"
            self.send_response(200)
            self.send_header("Content-Type", "audio/ogg")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self._json(404, {"error": "not found"})

    _json = _MockGateway._json


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
    os.environ["MESSAGE_FILES_DIR"] = str(tmp / "message-files")
    # The lint shells out to `claude`; a companion turn now runs on every
    # forwarded arrival, so leaving it on would have this suite spawning
    # sessions. Its own behaviour is covered in
    # tests/test_presentation_lint_deadlock.py.
    os.environ["PRESENTATION_LINT"] = "0"
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
# Model turns the gateway asked for, captured instead of spawning `claude`.
# Every forwarded arrival now runs one, so without this the suite would try to
# start sessions.
TURNS: list = []
# A test sets "hold" to an Event to stall turns inside send_message, which is
# what lets the coalescing case observe a turn that is still running.
TURN_GATE: dict = {"hold": None}


def _stub_send_message(prompt, display_question=None, session_key=None,
                       model=None, restart_message=None, resume=True,
                       reply_attachments=False):
    TURNS.append({"prompt": prompt, "question": display_question,
                  "session": session_key, "resume": resume})
    hold = TURN_GATE.get("hold")
    if hold is not None:
        hold.wait(20)
    return {"response": "Read it and staged a reply in the composer.",
            "model_name": "stub-model", "cost_usd": 0.0}


def _await_job(base, job_url, timeout=20):
    """Poll a job handle the way the messenger gateways' job_delivery does."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, body = _http(base, "GET", job_url)
        if status == 200 and body.get("status") != "pending":
            return body
        time.sleep(0.05)
    raise AssertionError(f"job {job_url} never resolved")


def _wait_for(pred, what, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(0.02)
    raise AssertionError(f"never happened: {what}")


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


def test_chat_list_shows_media_preview(base, wg):
    """A picture-only last message previews as an image in the chat list.

    The head message's attachments reach the skeleton as one records row each
    (see _MockSparql._records) — when the list still read them through a
    GROUP_CONCAT over IRIs, the aggregate came back unbound and every chat
    with a last picture previewed as empty text."""
    STATE["list_rows"] = [
        _lit_row(chat=MARA, channel="signal", ts=TS3, type=T_IN, text="",
                 sender=MARA, atts=f"urn:retinue:media:signal:{MID_ATT}"),
    ]
    try:
        status, body = _http(base, "GET", "/chats")
        assert status == 200, body
        chat = next(c for c in body["chats"] if c["id"] == CHAT1)
        assert chat["last"]["kind"] == "image", chat["last"]
    finally:
        STATE["list_rows"] = None
    print("PASS test_chat_list_shows_media_preview")


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
    # The stated intrinsic size rides on the blob its gateway described.
    assert atts[MID_ATT]["width"] == 320 and atts[MID_ATT]["height"] == 420
    assert atts[MID_ATT]["name"] == "IMG_0042.jpg"
    assert "width" not in atts[MID_ATT3] and "type" not in atts[MID_ATT3], \
        "nothing stated, nothing guessed"
    # The lookup asked about exactly this page's blobs, as recorded.
    meta_qs = [q for q in STATE["queries"] if "VALUES ?att" in q]
    assert meta_qs and f"<urn:retinue:media:signal:{MID_ATT}>" in meta_qs[-1]
    assert f"<http://signal-gateway:8090/media/{MID_ATT3}>" in meta_qs[-1]
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
    # No `handover`, so no turn: these events are about notification, and a
    # caller that has not offered to hand the message over keeps it (see
    # test_arrival_starts_a_companion_turn).
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
    assert "job_url" not in body, "a held message must not buy a model turn"

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
    # ... and what the gateway stated about the blob it just stored, so the
    # bubble renders as a picture right away, not as a file row.
    assert msg["attachments"][0]["type"] == "image/png"
    assert msg["attachments"][0]["size"] == 2048
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
    # Chats that have had a forwarded arrival already have a companion of their
    # own by now — the arrival turn opens one. What must hold is that opening
    # this one changes nothing about them.
    status, before = _http(base, "GET", "/chats")
    others = {c["id"]: c["companion"] for c in before["chats"] if c["id"] != CHAT2}
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
    # Every other chat is unaffected, and none of them was handed this thread.
    after = {c["id"]: c["companion"] for c in chats["chats"] if c["id"] != CHAT2}
    assert after == others, "opening one companion touched another chat"
    assert cid not in others.values()

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


def test_arrival_starts_a_companion_turn(base, wg):
    """The delivery gate's forward path: worked in the chat, not in triage.

    A forwarded arrival used to leave the gateway to spawn a triage session
    whose job was to open a dashboard conversation *about* the message. It now
    starts a turn in the chat's own companion thread, and the rail answers 202
    with that turn's job handle — the same contract POST /message answers with,
    because it is what the gateway's never-drop machinery polls before it dares
    flip the message's `delivered` flag.
    """
    rail = "/internal/chats/inbound"
    chat = "telegram:777001"
    PUSHES.clear()
    TURNS.clear()
    event = {"direction": "in", "channel": "telegram", "chat": "777001",
             "sender": "777001", "sender_name": "Nina", "group": False,
             "message_id": "a1", "text": "Passt Samstag 15 Uhr?",
             "handover": True,
             "gate": {"forward": True, "vip": False, "reason": "unknown"}}
    # The same message from the same person, once the user has said they want
    # to hear from them. Nothing else about it differs.
    vip = lambda **kw: dict(event, gate=dict(event["gate"], vip=True), **kw)
    # Not a VIP: the chat has the message and the user was pushed, both for no
    # model turn at all. `accepted` is what tells the gateway to stop — the
    # message is delivered, and it goes nowhere else.
    status, body = _http(base, "POST", rail, event)
    assert status == 200, body
    assert body["accepted"] is True and "job_url" not in body, body
    assert body["pushed"] is True
    assert TURNS == [], "a message from nobody in particular bought a turn"

    # A VIP's message is worked the moment it lands.
    status, body = _http(base, "POST", rail, vip(message_id="a1b"))
    assert status == 202, body
    assert body["job_url"].startswith("/jobs/") and body["accepted"] is True, body

    done = _await_job(base, body["job_url"])
    assert done["status"] == "done", done
    assert done["chat"] == chat

    # Exactly one turn, and it ran in this chat's companion thread — the same
    # thread the user types into when they ask Ara about the chat themselves.
    assert len(TURNS) == 1, TURNS
    status, chats = _http(base, "GET", "/chats")
    summary = next(c for c in chats["chats"] if c["id"] == chat)
    comp = summary["companion"]
    assert "assist" not in summary, "a chat does not carry this; a sender does"
    assert comp, "the arrival turn had nowhere to run"
    assert TURNS[0]["session"] == f"conv:{comp}"
    # A companion turn starts a new session rather than resuming the thread's.
    assert TURNS[0]["resume"] is False, TURNS[0]

    prompt = TURNS[0]["prompt"]
    assert "A new message has arrived in this chat" in prompt
    assert "Nina" in prompt
    # The chat note rides along, so the turn reads the message in its
    # conversation, sees the draft, and is told where its answer goes and who
    # writes it — none of which the arrival head restates.
    assert "Passt Samstag 15 Uhr?" in prompt
    assert "chat-draft.py" in prompt and "secretary" in prompt
    # And when a dashboard conversation is warranted, and when it is not.
    assert "conversation-push.py" in prompt
    # Answering is only part of it: what the user gets from a VIP turn is that
    # the system knows what they were just told.
    assert "memory.py store" in prompt and "project" in prompt

    # Ara's note lands in the companion thread and notifies nobody a second
    # time: the arrival already pushed, and one message is one notification.
    status, conv = _http(base, "GET", f"/conversations/{comp}")
    assert conv["messages"][-1]["role"] == "assistant"
    assert "staged" in conv["messages"][-1]["text"]
    # Two arrivals so far, two pushes: the turn added none of its own.
    assert len(PUSHES) == 2, "the companion turn pushed on top of the arrivals"

    # And it never asks the user to rule on a correspondent. Who is worth a
    # turn was settled by the switch above; a thread asking it would be the
    # per-message dashboard conversation this replaced, in a hat.
    low = prompt.lower()
    assert "whitelist" not in low and "blacklist" not in low, prompt

    # The message itself is in the instruction, not only in the chat note the
    # store builds: a store outage must not leave a turn answering about a
    # message it never saw while its job still reports done.
    assert "<external_message>" in prompt and "Passt Samstag 15 Uhr?" in prompt

    # A held class is accepted like everything else — the mirror has it, so
    # there is nothing left for a drain to sweep — but it notifies nobody and
    # buys no turn.
    TURNS.clear()
    n = len(PUSHES)
    status, body = _http(base, "POST", rail,
                         dict(event, message_id="a3", text="spam",
                              gate={"forward": False, "vip": False,
                                    "reason": "blacklisted"}))
    assert status == 200 and body["accepted"] is True, body
    assert "job_url" not in body and body["pushed"] is False
    assert len(PUSHES) == n and TURNS == []

    # It follows the person, not the room: the same VIP writing in a group is
    # worked exactly as in a 1:1, and a non-VIP in that group is not.
    TURNS.clear()
    grp = {"direction": "in", "channel": "telegram", "chat": "-100777",
           "group": True, "handover": True, "sender": "777001",
           "sender_name": "Nina", "message_id": "g1", "text": "wer kommt mit?",
           "gate": {"forward": True, "vip": True, "reason": "vip"}}
    status, body = _http(base, "POST", rail, grp)
    assert status == 202 and body["accepted"] is True, body
    assert _await_job(base, body["job_url"])["status"] == "done"
    assert len(TURNS) == 1, TURNS
    TURNS.clear()
    status, body = _http(base, "POST", rail,
                         dict(grp, sender="777002", sender_name="Someone",
                              message_id="g2",
                              gate={"forward": True, "vip": False,
                                    "reason": "unknown"}))
    assert status == 200 and body["accepted"] is True and "job_url" not in body
    assert TURNS == [], "the room is not the VIP; the person is"

    # Nor does a forward from a caller that did not offer to hand the message
    # over. That caller forwards it to triage itself whatever its gate says —
    # every gateway built before this contract does, which is what a deployment
    # runs between rebuilding the web-gateway and rebuilding the gateways — so
    # a turn here would be the second handling of one message.
    TURNS.clear()
    no_handover = {k: v for k, v in event.items() if k != "handover"}
    status, body = _http(base, "POST", rail, dict(no_handover, message_id="a3c"))
    assert status == 200 and "job_url" not in body, body
    assert body["pushed"] is True, "notification is not what is being withheld"
    assert TURNS == []

    # Both opt-ins are read strictly, because this is a JSON boundary and both
    # are documented booleans. A truthy stand-in must not pass for either: the
    # first would reintroduce the double handling the handover prevents, the
    # second would spend a turn on somebody nobody named a VIP.
    TURNS.clear()
    status, body = _http(base, "POST", rail,
                         dict(event, message_id="s1", handover="true"))
    assert status == 200 and "accepted" not in body, body
    status, body = _http(base, "POST", rail,
                         dict(event, message_id="s2",
                              gate={"forward": True, "vip": 1,
                                    "reason": "unknown"}))
    assert status == 200 and body["accepted"] is True, body
    assert "job_url" not in body, body
    assert TURNS == []

    # An event with no verdict at all is still taken — notification fails open
    # — but nobody is a VIP by default, so no turn runs.
    TURNS.clear()
    no_gate = {k: v for k, v in event.items() if k != "gate"}
    status, body = _http(base, "POST", rail, dict(no_gate, message_id="a3b"))
    assert status == 200 and body["accepted"] is True, body
    assert "job_url" not in body and body["pushed"] is True
    assert TURNS == []

    # One message, one turn — however often the rail delivers it. A gateway
    # retries a POST whose answer was lost, and a ledger can redeliver a
    # stanza; both must get the handle the first call minted back.
    TURNS.clear()
    status, first_delivery = _http(base, "POST", rail,
                                   vip(message_id="dup1", text="hoi"))
    status, again = _http(base, "POST", rail,
                          vip(message_id="dup1", text="hoi"))
    assert again["job_url"] == first_delivery["job_url"], (first_delivery, again)
    assert _await_job(base, first_delivery["job_url"])["status"] == "done"
    assert len(TURNS) == 1, TURNS

    # What the message carried travels with it, so the turn can open it rather
    # than answer a photo it never saw.
    TURNS.clear()
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 32).decode()
    status, body = _http(base, "POST", rail,
                         vip(message_id="a4", text="schau mal",
                              files=[{"filename": "x.png",
                                      "content_type": "image/png",
                                      "data": png}]))
    assert _await_job(base, body["job_url"])["status"] == "done"
    assert "image/png" in TURNS[0]["prompt"], TURNS[0]["prompt"][-400:]

    # Messages arriving while a turn runs fold into one follow-up turn — a turn
    # reads the chat as it stands, so a second one would re-read the first's
    # messages and the two would overwrite each other's draft. Every arrival
    # still gets its own handle, and the turn that covers it answers all of
    # them: no message is ever reported delivered that no turn has seen.
    TURNS.clear()
    hold = threading.Event()
    TURN_GATE["hold"] = hold
    try:
        first = _http(base, "POST", rail,
                      vip(message_id="b1", text="eins"))[1]
        _wait_for(lambda: len(TURNS) == 1, "the first turn to start")
        rest = [_http(base, "POST", rail,
                      vip(message_id=f"b{i}", text=str(i)))[1]
                for i in (2, 3)]
        assert all(r.get("job_url") for r in rest), rest
        assert len(TURNS) == 1, "a second turn started while one was running"
    finally:
        hold.set()
        TURN_GATE["hold"] = None
    for handle in [first] + rest:
        assert _await_job(base, handle["job_url"])["status"] == "done", handle
    assert len(TURNS) == 2, TURNS

    # A turn that fails resolves its jobs as errors, which is what leaves the
    # message undelivered at the gateway for the daily drain to retry.
    TURNS.clear()
    wg.send_message = lambda *a, **k: {"error": "upstream is down"}
    try:
        status, body = _http(base, "POST", rail,
                             vip(message_id="d1", text="hallo?"))
        failed = _await_job(base, body["job_url"])
        assert failed["status"] == "error", failed
    finally:
        wg.send_message = _stub_send_message

    # So does a turn whose reply could not be stored. "Delivered" means a model
    # turn accounted for the message; a reply that reached no thread accounts
    # for nothing, however well the model answered.
    TURNS.clear()
    real_add = wg._conv_add_message
    wg._conv_add_message = lambda *a, **k: None
    try:
        status, body = _http(base, "POST", rail,
                             vip(message_id="d2", text="und jetzt?"))
        lost = _await_job(base, body["job_url"])
        assert lost["status"] == "error", lost
    finally:
        wg._conv_add_message = real_add

    # A VIP is owed work, so a VIP's message is accepted only once the turn is
    # actually running. With no companion thread to be had this rail cannot do
    # it, and saying "I have this" anyway would have the gateway record it
    # delivered and skip the forward that would still have got it done.
    TURNS.clear()
    real_companion = wg._chat_companion

    def _no_companion(_chat_id):
        raise RuntimeError("the store is down")

    wg._chat_companion = _no_companion
    try:
        status, body = _http(base, "POST", rail, vip(message_id="c0"))
        assert status == 200, body
        assert "accepted" not in body and "job_url" not in body, body
        assert TURNS == []
        # A non-VIP in the same state is still accepted: nothing was owed.
        status, body = _http(base, "POST", rail, dict(event, message_id="c0b"))
        assert status == 200 and body["accepted"] is True, body
    finally:
        wg._chat_companion = real_companion

    # And the switch is reversible without a revert: off, the rail takes
    # nothing at all — no acceptance and no handle — so every gateway falls
    # back to the held/forward path it had before the chat surface existed.
    wg.CHAT_ARRIVAL_TURNS = False
    try:
        TURNS.clear()
        status, body = _http(base, "POST", rail, vip(message_id="c1"))
        assert status == 200, body
        assert "accepted" not in body and "job_url" not in body, body
        assert TURNS == []
    finally:
        wg.CHAT_ARRIVAL_TURNS = True
    print("PASS test_arrival_starts_a_companion_turn")


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


def test_media_is_asked_for_not_guessed(base, wg, sib_port):
    """Two inbox accounts on a channel: unsendable chats stay readable.

    A legacy record names the blob, never the account that stored it. With
    two inbox accounts the send resolver rightly refuses — it cannot tell
    whose chat this is — but serving a picture needs no identity, only a
    gateway that has it. So the proxy asks the named gateway and, on its
    404, the channel's others in turn: the reader never reads another
    service's store and never guesses; each gateway states what it holds."""
    sib_slug = "signal-gateway-personal"
    wg._CHANNEL_GATEWAYS[sib_slug] = {"base_url": f"http://127.0.0.1:{sib_port}",
                                      "token": "sib-secret",
                                      "label": "Signal (personal)"}
    wg._gw_identity.clear()

    def media(slug, mid):
        req = urllib.request.Request(base + f"/chats/media/{slug}/{mid}")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.headers.get("Content-Type"), \
                    resp.read(), resp.headers.get("Cache-Control")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers.get("Content-Type"), exc.read(), \
                exc.headers.get("Cache-Control")

    def asked(log, mid):
        return [r for r in log if r[1] == f"/media/{mid}"]

    try:
        # The legacy chat is now unsendable ...
        STATE["sent"].clear()
        status, body = _http(base, "POST", "/chats/" + _quote(CHAT1) + "/send",
                             {"text": "whose account?"})
        assert status == 409 and "cannot tell which account" in body["error"], body
        assert STATE["sent"] == []
        # ... and still lists its messages, media routed to a gateway to ask.
        status, body = _http(base, "GET", "/chats/" + _quote(CHAT1) + "/messages")
        assert status == 200, body
        m_att = next(m for m in body["messages"] if m["id"] == "903")
        atts = {a["id"]: a for a in m_att["attachments"]}
        assert atts[MID_ATT]["url"] == f"/chats/media/127.0.0.1/{MID_ATT}"
        assert atts[MID_ATT3]["url"] == f"/chats/media/127.0.0.1/{MID_ATT3}"

        # Named gateway lacks it → the sibling is asked → served, with its
        # own token and its own content type. Order: the named one first.
        STATE["gw_requests"].clear(); STATE["sib_requests"].clear()
        status, ctype, data, cache = media("127.0.0.1", MID_SIB)
        assert (status, ctype, data) == (200, "audio/ogg", b"OGGBYTES"), (status, ctype)
        assert cache and "max-age" in cache
        assert asked(STATE["gw_requests"], MID_SIB) and asked(STATE["sib_requests"], MID_SIB)
        assert asked(STATE["sib_requests"], MID_SIB)[0][2] == "Bearer sib-secret"
        # And the other way round.
        STATE["gw_requests"].clear(); STATE["sib_requests"].clear()
        status, ctype, data, _ = media(sib_slug, MID_ATT)
        assert (status, ctype, data) == (200, "image/jpeg", b"JPEGBYTES"), (status, ctype)
        assert asked(STATE["sib_requests"], MID_ATT) and asked(STATE["gw_requests"], MID_ATT)
        # A blob nobody holds: both asked, an honest 404, and no day-long
        # cache on a miss.
        STATE["gw_requests"].clear(); STATE["sib_requests"].clear()
        status, _, _, cache = media("127.0.0.1", MID_NONE)
        assert status == 404 and not cache, (status, cache)
        assert asked(STATE["gw_requests"], MID_NONE) and asked(STATE["sib_requests"], MID_NONE)

        # A control-mode sibling is never asked: ledger media is inbox-only,
        # and a prompt channel is not a place to look for the user's pictures.
        STATE["sib_mode"] = "control"
        wg._gw_identity.clear()
        STATE["gw_requests"].clear(); STATE["sib_requests"].clear()
        status, _, _, _ = media("127.0.0.1", MID_SIB)
        assert status == 404
        assert asked(STATE["gw_requests"], MID_SIB) and not asked(STATE["sib_requests"], MID_SIB)
        assert media(sib_slug, MID_ATT)[0] == 404, "named control gateway refused outright"
        assert not asked(STATE["sib_requests"], MID_ATT)

        # The sibling being down is not a 404: the named gateway's answer
        # stands, and only when nobody could answer at all is it a 502.
        STATE["sib_mode"] = "inbox"
        wg._gw_identity.clear()
        wg._CHANNEL_GATEWAYS[sib_slug]["base_url"] = "http://127.0.0.1:1"
        assert media("127.0.0.1", MID_ATT)[0] == 200
        assert media("127.0.0.1", MID_SIB)[0] == 404
        wg._CHANNEL_GATEWAYS["127.0.0.1"] = dict(wg._CHANNEL_GATEWAYS["127.0.0.1"],
                                                base_url="http://127.0.0.1:1")
        assert media("127.0.0.1", MID_ATT)[0] == 502
    finally:
        wg._CHANNEL_GATEWAYS["127.0.0.1"] = dict(wg._CHANNEL_GATEWAYS["127.0.0.1"],
                                                base_url=f"http://127.0.0.1:{STATE['gw_port']}")
        wg._CHANNEL_GATEWAYS.pop(sib_slug, None)
        STATE["sib_mode"] = "inbox"
        wg._gw_identity.clear()
    print("PASS test_media_is_asked_for_not_guessed")


def test_archive_and_mute(base, wg):
    """The two shelf flags, and what an arrival does to each.

    A chat behaves like a dashboard conversation: archiving is "out of my way
    until it speaks again", so the next inbound message brings it back. Muting
    is the stronger wish — "out of my way, and do not let it speak" — so it
    archives in the same breath and the chat stays gone however busy it gets.
    Un-muting deliberately leaves it archived: quiet was the ask, not
    attention, and the next message is what returns it.

    On a chat of its own: this test posts arrivals, which reorder the list, and
    the rail test asserts what sorts first."""
    cid = "telegram:900900"
    rail = "/internal/chats/inbound"
    event = {"direction": "in", "channel": "telegram", "chat": "900900",
             "sender": "900900", "sender_name": "Neighbourhood association",
             "group": True, "message_id": "h1", "text": "Summer party on the 30th.",
             "gateway": "127.0.0.1", "gate": {"forward": True, "reason": "open"}}
    _http(base, "POST", rail, event)
    flags_path = "/chats/" + _quote(cid) + "/flags"

    status, body = _http(base, "GET", "/chats")
    c = next(c for c in body["chats"] if c["id"] == cid)
    assert not c.get("archived"), "a new chat starts visible"

    # Archive alone: the chat leaves the list, and stays a place that can speak.
    status, body = _http(base, "POST", flags_path, {"archived": True})
    assert status == 200 and body["archived"] is True and body["muted"] is False, body
    status, body = _http(base, "GET", "/chats")
    c = next(c for c in body["chats"] if c["id"] == cid)
    assert c["archived"] is True and c["muted"] is False, c

    # ... and an arrival undoes it. That is the whole point of archiving.
    _http(base, "POST", rail, dict(event, message_id="h2", text="one more thing"))
    status, body = _http(base, "GET", "/chats")
    c = next(c for c in body["chats"] if c["id"] == cid)
    assert c["archived"] is False, "archived alone is undone by an arrival"

    # Mute archives too — the user never has to ask for both.
    status, body = _http(base, "POST", flags_path, {"muted": True})
    assert status == 200 and body["muted"] is True and body["archived"] is True, body
    status, body = _http(base, "GET", "/chats")
    c = next(c for c in body["chats"] if c["id"] == cid)
    assert c["archived"] is True and c["muted"] is True, c

    # ... and it holds: a muted chat does not come back on a new message.
    _http(base, "POST", rail, dict(event, message_id="h3", text="and another"))
    status, body = _http(base, "GET", "/chats")
    c = next(c for c in body["chats"] if c["id"] == cid)
    assert c["archived"] is True, "a muted chat must not return on a new message"

    # Un-muting lets it speak again without pulling it back by itself: it is
    # still archived, and the next message is what returns it.
    status, body = _http(base, "POST", flags_path, {"muted": False})
    assert status == 200 and body["muted"] is False and body["archived"] is True, body
    _http(base, "POST", rail, dict(event, message_id="h4", text="still here"))
    status, body = _http(base, "GET", "/chats")
    c = next(c for c in body["chats"] if c["id"] == cid)
    assert c["archived"] is False, "un-muted, the next arrival restores the chat"

    # Restore clears both at once — the dashboard's Restore button, which a
    # muted chat needs because no arrival will ever return it.
    _http(base, "POST", flags_path, {"muted": True})
    status, body = _http(base, "POST", flags_path, {"archived": False, "muted": False})
    assert status == 200 and body["archived"] is False and body["muted"] is False, body
    status, body = _http(base, "GET", "/chats")
    c = next(c for c in body["chats"] if c["id"] == cid)
    assert c["archived"] is False and c["muted"] is False, c

    # Either flag alone is a legitimate body; neither of them is not.
    status, _ = _http(base, "POST", flags_path, {"muted": True})
    assert status == 200
    status, _ = _http(base, "POST", flags_path, {"muted": False})
    assert status == 200
    status, _ = _http(base, "POST", flags_path, {"archived": False})
    assert status == 200
    status, _ = _http(base, "POST", flags_path, {})
    assert status == 400, "a body with no flag is refused"
    status, _ = _http(base, "POST", flags_path, {"archived": "yes"})
    assert status == 400, "a non-boolean flag is refused"
    print("PASS test_archive_and_mute")


def test_delete_chat(base, wg):
    """Delete erases a chat from the system, and the peer can start over.

    The ledger is the gateways' to erase, so the web-gateway asks them (with
    their token) and drops only its own traces once one has: the chat state,
    the companion thread, the live overlay. A gateway that cannot erase
    leaves everything as it was — a half-deleted chat is worse than none.
    Afterwards the next message from the same peer is a new chat: no archive
    flag, no companion, nothing carried over."""
    key = "+41790007777"
    cid = "signal:" + key
    rail = "/internal/chats/inbound"
    event = {"direction": "in", "channel": "signal", "chat": key,
             "sender": key, "sender_name": "Old acquaintance",
             "message_id": "d1", "text": "Remember me?",
             "gateway": "127.0.0.1", "gate": {"forward": True, "reason": "open"}}
    _http(base, "POST", rail, event)
    status, body = _http(base, "POST", "/chats/" + _quote(cid) + "/companion")
    assert status in (200, 201), body
    conv_id = body["id"]
    assert (wg.CONVERSATIONS_DIR / f"{conv_id}.json").exists()
    _http(base, "POST", "/chats/" + _quote(cid) + "/flags", {"muted": True})
    delete_path = "/chats/" + _quote(cid) + "/delete"

    # Without the erase capability configured, nothing is attempted.
    wg.CHAT_ERASE_TOKEN = ""
    STATE["deleted"] = []
    status, body = _http(base, "POST", delete_path)
    assert status == 503 and "CHAT_ERASE_TOKEN" in body["error"], (status, body)
    assert STATE["deleted"] == [], "a gateway was asked without the capability"
    wg.CHAT_ERASE_TOKEN = "erase-cap"

    # A gateway that cannot erase the messages — failing outright, or
    # answering 200 but reporting a file it could not remove: nothing else is
    # touched, so the chat stays whole enough to retry.
    for mode in ("http", "partial"):
        STATE["gw_delete_fail"] = mode
        try:
            status, body = _http(base, "POST", delete_path)
        finally:
            STATE["gw_delete_fail"] = None
        assert status == 502, (mode, status, body)
        status, body = _http(base, "GET", "/chats")
        assert any(c["id"] == cid for c in body["chats"]), (mode, "the chat stays")
        assert (wg.CONVERSATIONS_DIR / f"{conv_id}.json").exists(), mode
        assert wg._CHAT_STATE.get(cid)["muted"] is True, mode

    STATE["deleted"] = []
    STATE["gw_requests"].clear()
    status, body = _http(base, "POST", delete_path)
    assert status == 200 and body.get("deleted") is True, body
    assert STATE["deleted"] == [{"chat": key, "account": ""}], STATE["deleted"]
    auth = [a for m, p, a in STATE["gw_requests"] if p == "/chats/delete"]
    assert auth and auth[0].startswith("Bearer "), "the gateway hop carries its token"
    assert STATE["erase_tokens"][-1] == "erase-cap", "and the erase capability"
    assert body.get("companion") is True, body

    status, body = _http(base, "GET", "/chats")
    assert not any(c["id"] == cid for c in body["chats"]), "a deleted chat is gone"
    assert cid not in wg._CHAT_STATE.all(), "its state document is gone"
    assert not (wg.CONVERSATIONS_DIR / f"{conv_id}.json").exists(), \
        "its companion thread is gone"
    status, body = _http(base, "GET", "/chats/" + _quote(cid) + "/messages")
    assert status == 200 and not [m for m in body["messages"]
                                  if m.get("text") == "Remember me?"], body

    # The store may still serve the erased rows for a moment; the tombstone
    # hides exactly those records, by the identities the gateway reported —
    # not by time, so nothing that arrives afterwards is caught by it.
    assert wg._chat_record_erased(cid, "urn:retinue:inbound:signal:erased1")
    assert wg._chat_record_erased(cid, message_id="d1")
    assert not wg._chat_record_erased(cid, "urn:retinue:inbound:signal:new", "d2")
    assert not wg._chat_record_erased("signal:+41790000000", message_id="d1")
    # A late rail event for an erased message does not bring it back, nor
    # recreate any state: it is accepted (the gateway must not forward it to
    # triage) and dropped.
    status, body = _http(base, "POST", rail, dict(event, handover=True))
    assert status == 200 and body.get("erased") and body.get("accepted"), body
    status, body = _http(base, "GET", "/chats")
    assert not any(c["id"] == cid for c in body["chats"]), "an erased message came back"
    assert cid not in wg._CHAT_STATE.all(), "a late event recreated the chat state"

    # A companion request left over from before the delete is refused, not
    # answered with a fresh thread for the erased chat.
    status, body = _http(base, "POST", "/chats/" + _quote(cid) + "/companion")
    assert status == 404, (status, body)
    assert cid not in wg._CHAT_STATE.all()

    # The same peer writing again — in the same second, even — is a new chat,
    # with nothing carried over.
    _http(base, "POST", rail, dict(event, message_id="d2", text="Hello again"))
    status, body = _http(base, "GET", "/chats")
    c = next(c for c in body["chats"] if c["id"] == cid)
    assert c["archived"] is False and c["muted"] is False, c
    assert c["companion"] is None, c
    assert c["last"]["text"] == "Hello again", c
    status, body = _http(base, "POST", "/chats/" + _quote(cid) + "/companion")
    assert status == 201, "the new chat may have a companion of its own"

    # A companion turn still running when the chat was deleted ends by
    # recording its session: that write is refused and its transcript erased,
    # so the deleted chat's Claude history cannot come back.
    sid = "0f0e0d0c-0b0a-4908-8706-050403020100"
    with tempfile.TemporaryDirectory() as cfg:
        transcript = Path(cfg) / "projects" / "-workspace" / f"{sid}.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text("{}")
        old_cfg = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = cfg
        try:
            wg._update_session_entry(wg.CONV_SESSION_KEY_PREFIX + conv_id,
                                     {"session_id": sid, "last_activity": 0})
        finally:
            if old_cfg is None:
                os.environ.pop("CLAUDE_CONFIG_DIR", None)
            else:
                os.environ["CLAUDE_CONFIG_DIR"] = old_cfg
        assert (wg.CONV_SESSION_KEY_PREFIX + conv_id) not in wg._load_state()
        assert not transcript.exists(), "the late turn's transcript is erased"

    status, _ = _http(base, "POST", "/chats/not-a-chat/delete")
    assert status == 404
    print("PASS test_delete_chat")


def test_mute_races_an_arrival(base, wg):
    """A Mute landing while a message arrives is never half-applied.

    The rail brings an archived chat back unless it is muted. Decided from a
    snapshot read a moment earlier, a Mute (which archives too) landing in
    between is read back stale: the arrival clears `archived` from what it saw
    while `muted` stays, and a chat the user just muted is in the active list
    again. The decision belongs under the state lock.

    The precondition is a chat archived but NOT muted, which is the ordinary
    soft state; the bad end state is archived=False with muted=True, which no
    ordering of the two requests can legitimately produce."""
    cid = "telegram:900901"
    rail = "/internal/chats/inbound"
    flags_path = "/chats/" + _quote(cid) + "/flags"
    event = {"direction": "in", "channel": "telegram", "chat": "900901",
             "sender": "900901", "sender_name": "Club", "group": True,
             "message_id": "r0", "text": "hello", "gateway": "127.0.0.1",
             "gate": {"forward": True, "reason": "open"}}
    _http(base, "POST", rail, event)

    for i in range(40):
        # Archived but unmuted: what the arrival would legitimately undo.
        _http(base, "POST", flags_path, {"archived": True, "muted": False})
        out = []
        def mute():
            out.append(_http(base, "POST", flags_path, {"muted": True}))
        def arrive():
            out.append(_http(base, "POST", rail, dict(event, message_id=f"r{i}")))
        threads = [threading.Thread(target=mute), threading.Thread(target=arrive)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        _, body = _http(base, "GET", "/chats")
        c = next(c for c in body["chats"] if c["id"] == cid)
        assert not (c["archived"] is False and c["muted"] is True), (
            f"round {i}: a mute was half-applied — archived cleared from a stale "
            f"read while muted stayed: {c}")
    print("PASS test_mute_races_an_arrival")


def test_store_down_serves_recent_list(base, wg):
    """A store that cannot answer a rebuild does not blank the phone: the last
    good skeleton is served while it is recent. Expiry (what every rail event,
    read and send does) keeps it as the fallback; the fallback then counts as
    fresh for one cache window, so the polls behind it do not each wait out a
    store timeout; a read made meanwhile still clears the badge; and only age
    beyond CHAT_LIST_STALE_SECONDS makes the failure a 502 again."""
    STATE["list_rows"] = [
        _lit_row(chat=STALE_PEER, channel="signal", ts=TS3, type=T_IN,
                 text="noch ungelesen", sender=STALE_PEER, atts=""),
    ]
    chat_id = "signal:" + STALE_PEER
    wg._chats_cache_clear()
    try:
        status, before = _http(base, "GET", "/chats")
        assert status == 200, before
        assert next(c for c in before["chats"] if c["id"] == chat_id)["unread"] == 3
        wg._chats_cache_invalidate()
        STATE["fail"] = True
        wg.CHAT_LIST_CACHE_SECONDS = 30.0
        status, body = _http(base, "GET", "/chats")
        assert status == 200, body
        assert [c["id"] for c in body["chats"]] == [c["id"] for c in before["chats"]]
        assert next(c for c in body["chats"] if c["id"] == chat_id)["unread"] == 3
        # The polls behind it reuse the fallback: nothing reaches the store.
        seen = len(STATE["queries"])
        status, body = _http(base, "GET", "/chats")
        assert status == 200 and len(STATE["queries"]) == seen
        # A read while the store is down still clears the badge.
        status, _ = _http(base, "POST", "/chats/" + _quote(chat_id) + "/read",
                          {"ts": TS3})
        assert status == 200
        status, body = _http(base, "GET", "/chats")
        assert status == 200, body
        assert next(c for c in body["chats"] if c["id"] == chat_id)["unread"] == 0
        # Too old to be honest: the same failure is a 502 again.
        wg._chats_cache_invalidate()
        wg._chats_cache["built"] -= wg.CHAT_LIST_STALE_SECONDS + 1
        status, body = _http(base, "GET", "/chats")
        assert status == 502 and "life store" in body["error"]
    finally:
        STATE["fail"] = False
        STATE["list_rows"] = None
        wg.CHAT_LIST_CACHE_SECONDS = 0.0
        wg._chats_cache_clear()
    print("PASS test_store_down_serves_recent_list")


def test_write_during_rebuild_is_not_lost(base, wg):
    """A write that expires the cache while a rebuild is querying the store is
    not in that rebuild's result — so the result is published already expired
    (kept as the fallback, rebuilt on the next poll) rather than served as
    fresh for a whole window."""
    fetch = wg._fetch_chats_skeleton

    def fetch_and_race(deadline=None):
        skeleton = fetch(deadline)
        wg._chats_cache_invalidate()  # a rail event landing mid-flight
        return skeleton

    wg.CHAT_LIST_CACHE_SECONDS = 30.0
    wg._fetch_chats_skeleton = fetch_and_race
    try:
        wg._chats_cache_clear()
        wg._chats_skeleton_and_unread()
        assert wg._chats_cache["skeleton"] is not None
        assert wg._chats_cache["at"] == 0.0, "raced result published as fresh"
        wg._fetch_chats_skeleton = fetch
        wg._chats_skeleton_and_unread()
        assert wg._chats_cache["at"] > 0.0
    finally:
        wg._fetch_chats_skeleton = fetch
        wg.CHAT_LIST_CACHE_SECONDS = 0.0
        wg._chats_cache_clear()
    print("PASS test_write_during_rebuild_is_not_lost")


def test_empty_store_is_an_empty_list(base, wg):
    """No messages at all is an empty list, not an error: the heads query
    returns nothing and no records query is sent (an empty VALUES block is
    not a query)."""
    STATE["list_rows"] = []
    wg._chats_cache_clear()
    try:
        seen = len(STATE["queries"])
        status, body = _http(base, "GET", "/chats")
        assert status == 200, body
        assert not any("VALUES ?m {" in q for q in STATE["queries"][seen:])
        # Every chat left comes from the overlay, none from the store.
        assert all(c["last"]["ts"] for c in body["chats"])
    finally:
        STATE["list_rows"] = None
        wg._chats_cache_clear()
    print("PASS test_empty_store_is_an_empty_list")


def test_rebuild_deadline_bounds_a_stalling_store(base, wg):
    """One rebuild works to a total deadline across its round trips: once it
    has passed, no further store call is made and the fallback is served.
    (The store here is fine — the budget is set to zero, so the very first
    call is already over the deadline.)"""
    status, before = _http(base, "GET", "/chats")
    assert status == 200, before
    wg._chats_cache_invalidate()
    wg.CHAT_LIST_REBUILD_TIMEOUT = 0.0
    try:
        seen = len(STATE["queries"])
        status, body = _http(base, "GET", "/chats")
        assert status == 200, body
        assert len(STATE["queries"]) == seen, "deadline passed but the store was queried"
        assert [c["id"] for c in body["chats"]] == [c["id"] for c in before["chats"]]
    finally:
        wg.CHAT_LIST_REBUILD_TIMEOUT = 20.0
        wg._chats_cache_clear()
    print("PASS test_rebuild_deadline_bounds_a_stalling_store")


def test_store_down_is_502(base, wg):
    """With nothing cached to fall back on, a store failure is an honest 502 —
    and the requests that follow within CHAT_LIST_FAILURE_BACKOFF share that
    verdict without asking the store again; past it, the store is retried."""
    wg._chats_cache_clear()
    STATE["fail"] = True
    try:
        status, body = _http(base, "GET", "/chats")
        assert status == 502 and "life store" in body["error"]
        seen = len(STATE["queries"])
        status, body = _http(base, "GET", "/chats")
        assert status == 502 and "not retried" in body["detail"], body
        assert len(STATE["queries"]) == seen, "store asked again inside the backoff"
        wg._chats_cache["failed_at"] -= wg.CHAT_LIST_FAILURE_BACKOFF + 1
        status, body = _http(base, "GET", "/chats")
        assert status == 502 and len(STATE["queries"]) > seen, body
        status, body = _http(base, "GET", "/chats/" + _quote(CHAT1) + "/messages")
        assert status == 502
    finally:
        STATE["fail"] = False
        wg._chats_cache_clear()
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
    sib = _serve(_MockSibling)
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp), sparql.server_address[1],
                           gw.server_address[1])
        # Capture pushes instead of talking to a push service.
        wg.push_notify.enabled = lambda: True
        wg.push_notify.notify_async = lambda *a, **k: PUSHES.append((a, k))
        # A forwarded arrival runs a companion turn (see
        # test_arrival_starts_a_companion_turn); no test may spawn `claude`.
        wg.send_message = _stub_send_message
        server = ThreadingHTTPServer(("127.0.0.1", 0), wg.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"

        test_chat_list_contract(base, wg)
        # What the gateway stated about the blob rides on the shaped
        # attachment — type, size, and the intrinsic size sniffed at ingest.
        status, body = _http(base, "GET", "/chats/" + _quote(CHAT1) + "/messages")
        att = next(a for a in body["messages"][-1]["attachments"]
                   if a["id"] == MID_ATT)
        assert att.get("type") == "image/jpeg" and att.get("size") == 717
        assert att.get("width") == 320 and att.get("height") == 420
        test_chat_list_shows_media_preview(base, wg)
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
        test_media_is_asked_for_not_guessed(base, wg, sib.server_address[1])
        test_arrival_starts_a_companion_turn(base, wg)
        test_archive_and_mute(base, wg)
        test_mute_races_an_arrival(base, wg)
        test_delete_chat(base, wg)
        test_store_down_serves_recent_list(base, wg)
        test_write_during_rebuild_is_not_lost(base, wg)
        test_empty_store_is_an_empty_list(base, wg)
        test_rebuild_deadline_bounds_a_stalling_store(base, wg)
        test_store_down_is_502(base, wg)
        server.shutdown()
    sparql.shutdown()
    gw.shutdown()
    sib.shutdown()
    print("all chat-api tests passed")


if __name__ == "__main__":
    main()
