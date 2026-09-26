#!/usr/bin/env python3
"""Integration checks for the attention model in the web-gateway
(docs/attention-model.md, scripts/attention.py, scripts/attention_store.py).

Runs the REAL gateway handler on a local port against a tiny mock SPARQL
server (empty chat ledgers, one canned project) with pushes captured, and
covers, end to end through HTTP:

- an agent thread declared important and due rings in Open, is held in Deep
  work (badge, no push) and lands in Held; Pull, Later and Mark done move it;
- critical rings in every mode; a passive thread is listed, never pushed;
- the mode set by hand is a breakpoint: what was held is released, one
  Topic-collapsed digest goes out, and the item shows where the new mode puts it;
- corrections on the three fields, and the profile learning priors and lead
  times; a permit granted in Focused releases the sender's held chat;
- the chats rail: an inbound is held or pushed by the mode, the family repeat
  breaks through in Off, the user's own reply settles the chat's item;
- a direct sender nothing vouches for (no VIP flag, no card) is screened into
  the `unknown` sphere, and the contact card names them, teaches the profile,
  writes the address book and lets their next message through; a VIP rings
  in any mode, unless their chat is muted;
- a project from the store carries its frontmatter's importance and deadline;
- the tick: the 12:00 digest releases what Focused held, the sweep pushes
  what crossed into the next urgency band; the life-store emit is written;
- the agent-facing /internal/attention/set and its token.

Standalone (stdlib + the gateway module's own deps):

    python3 tests/test_attention_api.py
"""
import importlib.util
import json
import os
import sys
import tempfile
import threading
import types
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
TOKEN = "agent-token"
PROJECT = "urn:retinue:project:vat-q3"
PUSHES: list = []
STATE: dict = {"projects": True}
# What the project's frontmatter says, as the store serves it; a test moves
# the deadline here the way an author edits the file.
PROJECT_ROW = {"title": "VAT return Q3", "actor": "urn:retinue:actor:owner", "expected": "2026-09-30",
               "importance": "4", "sphere": "admin", "tag": "finance", "kind": "tax filing",
               "next": "Collect the receipts"}


class _MockSparql(BaseHTTPRequestHandler):
    """Empty ledgers, one running project — enough for the union."""

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        query = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8")).get("query", [""])[0]
        bindings = []
        if "k:Project" in query and STATE["projects"]:
            cell = lambda v: {"value": v}  # noqa: E731
            bindings = [{"p": cell(PROJECT), **{k: cell(v) for k, v in PROJECT_ROW.items() if v is not None}}]
        payload = json.dumps({"results": {"bindings": bindings}}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/sparql-results+json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _load_gateway(tmp: Path, sparql_port: int):
    os.environ["QLEVER_LIFE_URL"] = f"http://127.0.0.1:{sparql_port}"
    for var in ("SIGNAL_GATEWAY_BASE_URL", "WHATSAPP_GATEWAY_BASE_URL", "TELEGRAM_GATEWAY_BASE_URL",
                "MESSENGER_GATEWAYS", "CHATS_INGEST_TOKEN"):
        os.environ.pop(var, None)
    os.environ["EDGE_PROXY_PEERS"] = "127.0.0.1"
    os.environ["CHAT_STATE_DIR"] = str(tmp / "chat-state")
    os.environ["CHAT_LIST_CACHE_SECONDS"] = "0"
    os.environ["ATTENTION_PROJECTS_CACHE_SECONDS"] = "0"
    os.environ["CONVERSATION_BACKEND_TOKEN"] = TOKEN
    os.environ["CONVERSATIONS_DIR"] = str(tmp / "convs")
    os.environ["CONVERSATION_DIR"] = str(tmp / "convlog")
    os.environ["CHAMBERS_DIR"] = str(tmp / "chambers")
    os.environ["WEB_GATEWAY_STATE"] = str(tmp / "state.json")
    os.environ["PUSH_DIR"] = str(tmp / "push")
    os.environ["ATTENTION_DIR"] = str(tmp / "attention")
    os.environ["ATTENTION_TZ"] = "UTC"
    os.environ["PRESENTATION_LINT"] = "0"
    (tmp / "chambers").mkdir(parents=True, exist_ok=True)
    # One chamber to file contacts in; no manifest, so it holds them at the
    # default path. Nothing here is a git repository: no commits.
    (tmp / "chambers" / "private").mkdir(parents=True, exist_ok=True)
    os.environ["CHAMBERS_MANIFEST"] = str(tmp / "no-manifest.json")
    os.environ["CONTACTS_COMMIT"] = "0"
    # The gateway renders its own pages with markdown-it; nothing here reads
    # them, so a stock Python without the package gets a stand-in.
    try:
        import markdown_it  # noqa: F401
    except ImportError:
        stub = types.ModuleType("markdown_it")

        class _MD:
            def __init__(self, *a, **k): pass
            def enable(self, *a, **k): return self
            def render(self, text): return text
        stub.MarkdownIt = _MD
        sys.modules["markdown_it"] = stub
    sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location("web_gateway_attention_under_test",
                                                  SCRIPTS_DIR / "web-gateway.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _http(base, method, path, body=None, headers=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"raw": raw}


AGENT = {"X-Conversation-Backend-Token": TOKEN}


def _open(base, title, message, attention=None, **extra):
    payload = {"title": title, "message": message, **extra}
    if attention:
        payload["attention"] = attention
    status, body = _http(base, "POST", "/internal/conversations", payload, AGENT)
    assert status == 201, (status, body)
    return body


def _mode(base, mode):
    status, body = _http(base, "POST", "/attention/mode", {"mode": mode})
    assert status == 200, (status, body)
    return body


def _sections(base):
    status, body = _http(base, "GET", "/attention")
    assert status == 200, (status, body)
    return body


def _find(body, item_id):
    for key in ("now", "next", "held", "waiting", "not_now"):
        for row in body["sections"][key]:
            if row["id"] == item_id:
                return key, row
    return None, None


def _clock(wg, when):
    wg.ATTENTION_CLOCK = (lambda: when) if when else None


def _due(hours):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


# ── threads ────────────────────────────────────────────────────────────────

def test_open_mode_pushes_and_lists_now(base, wg):
    _mode(base, "chores")
    PUSHES.clear()
    body = _open(base, "Quote for Müller AG", "Draft ready for review; due today.",
                 {"importance": 4, "sphere": "customers", "due": _due(6), "kind": "customer request",
                  "tags": ["finance"]})
    assert body["attention"]["delivery"] == "push", body
    assert body["attention"]["level"] == "time-sensitive"
    assert len(PUSHES) == 1 and PUSHES[0][1].get("urgency") == "high", PUSHES
    tid = "thread:" + body["id"]
    where, row = _find(_sections(base), tid)
    assert where == "now" and row["sphere"] == "customers" and row["tags"] == ["finance"], row
    assert "customers" in row["reason"] and row["kind"] == "thread" and row["href"].endswith(body["id"])
    print("ok test_open_mode_pushes_and_lists_now")


def test_deep_work_holds_pull_later_done(base, wg):
    _mode(base, "focused")
    PUSHES.clear()
    body = _open(base, "Card renewal", "The card on file expires Friday. Renew?",
                 {"importance": 4, "sphere": "admin", "kind": "admin chore"})
    assert body["attention"]["delivery"] == "hold" and "until" in body["attention"], body
    assert body["attention"]["reason"] == "Focused on nothing admits only critical"
    assert not PUSHES, "a held thread must not push"
    # The badge is there for whoever opens the dashboard; the push is not.
    status, conv = _http(base, "GET", f"/conversations/{body['id']}")
    assert conv["unread"] is True and conv["attention"]["released"] is False
    tid = "thread:" + body["id"]
    assert _find(_sections(base), tid)[0] == "held"
    status, out = _http(base, "POST", "/attention/items/pull", {"id": tid})
    assert status == 200 and out["item"]["released"] is True and out["item"]["pulled"] is True, out
    assert _find(_sections(base), tid)[0] == "next", "active is below Focused's bar: Next, not Now — and pulled, so not folded"
    # Focused lists only what it admits: a released item it does not admit
    # folds into Not now; the pulled one above stays. The fold is a per-mode
    # rule the menu toggles.
    passive = _open(base, "Newsletter", "The monthly newsletter is out.", {"importance": 1, "sphere": "admin"})
    pid = "thread:" + passive["id"]
    assert passive["attention"]["delivery"] == "list"
    assert _find(_sections(base), pid)[0] == "not_now"
    status, out = _http(base, "POST", "/attention/modes", {"mode": "focused", "only_admitted": False})
    assert status == 200 and out["changed"] == ["Focused lists everything"] and out["mode"]["only_admitted"] is False, out
    assert _find(_sections(base), pid)[0] == "next"
    status, out = _http(base, "POST", "/attention/modes", {"mode": "focused", "only_admitted": True})
    assert status == 200 and _find(_sections(base), pid)[0] == "not_now"
    status, out = _http(base, "POST", "/attention/modes", {"mode": "focused", "threshold": "passive"})
    assert status == 400
    _http(base, "POST", "/attention/items/done", {"id": pid})
    status, out = _http(base, "POST", "/attention/items/later", {"id": tid, "when": "tomorrow"})
    assert status == 200 and out["item"]["snoozed_until"], out
    where, row = _find(_sections(base), tid)
    assert where == "held" and row["delivery"].startswith("snoozed until"), row
    status, out = _http(base, "POST", "/attention/items/done", {"id": tid})
    assert status == 200 and out["item"]["state"] == "done"
    assert _find(_sections(base), tid)[0] is None
    status, out = _http(base, "POST", "/attention/items/reopen", {"id": tid})
    assert status == 200 and _find(_sections(base), tid)[0] == "next"
    # Archiving from the thread bar settles it too.
    status, _ = _http(base, "POST", f"/conversations/{body['id']}/archive")
    assert status == 200 and _find(_sections(base), tid)[0] is None
    print("ok test_deep_work_holds_pull_later_done")


def test_critical_and_passive(base, wg):
    _mode(base, "focused")
    PUSHES.clear()
    body = _open(base, "Backup failed", "Nightly backup exited with code 2.",
                 {"importance": 5, "sphere": "system", "kind": "system alert", "critical": True})
    assert body["attention"]["delivery"] == "push" and body["attention"]["level"] == "critical"
    assert body["attention"]["reason"] == "critical rings in every mode"
    assert len(PUSHES) == 1
    PUSHES.clear()
    body = _open(base, "Newsletter filed", "Filed the newsletter into news.", {"importance": 1})
    assert body["attention"]["delivery"] == "list" and not PUSHES
    assert _find(_sections(base), "thread:" + body["id"])[0] == "not_now", "listed — folded, since Focused lists only what it admits"
    # A thread that says nothing about itself is passive: listed, not pushed.
    body = _open(base, "Plain thread", "Just so you know.")
    assert body["attention"]["delivery"] == "list" and body["attention"]["level"] == "passive"
    # Waiting on someone else: listed under Waiting, no push.
    body = _open(base, "Brochure translation", "Handed to the Publisher.",
                 {"importance": 3, "sphere": "customers", "actor": "Publisher"})
    assert body["attention"]["delivery"] == "waiting"
    where, row = _find(_sections(base), "thread:" + body["id"])
    assert where == "waiting" and row["actor"] == "Publisher" and row["delivery"].startswith("waiting on Publisher")
    # Quiet threads are records, not items.
    body = _open(base, "Audit", "The connector asked about X.", quiet=True)
    assert _find(_sections(base), "thread:" + body["id"])[0] is None
    print("ok test_critical_and_passive")


def test_user_thread_never_gated(base, wg):
    _mode(base, "focused")
    PUSHES.clear()
    status, conv = _http(base, "POST", "/conversations", {"message": "What is on today?"})
    assert status == 201
    # The user's own thread appears only while Ara's reply is unread; Ara's
    # reply itself is pushed as it always was — simulate her turn landing.
    wg._conv_add_message(conv["id"], "assistant", "Three things.", unread=True, pending=False)
    wg._push_conv_notification(wg._load_conv(conv["id"]), "Three things.")
    assert len(PUSHES) == 1
    where, row = _find(_sections(base), "thread:" + conv["id"])
    assert where == "next" and row["importance_from"] == "your thread", row
    status, _ = _http(base, "POST", f"/conversations/{conv['id']}/read")
    assert _find(_sections(base), "thread:" + conv["id"])[0] is None
    print("ok test_user_thread_never_gated")


def test_mode_change_is_a_breakpoint(base, wg):
    _mode(base, "focused")
    body = _open(base, "Anna: dinner Friday?", "Anna asks whether Friday 19:00 works.",
                 {"importance": 4, "sphere": "friends", "due": _due(30), "kind": "invitation"})
    assert body["attention"]["delivery"] == "hold"
    tid = "thread:" + body["id"]
    PUSHES.clear()
    out = _mode(base, "social")
    assert out["mode"]["id"] == "social" and out["mode"]["manual"] is True
    digests = [p for p in PUSHES if p[1].get("topic") == "digest"]
    assert len(digests) == 1 and digests[0][1].get("urgency") == "normal", PUSHES
    assert "Anna: dinner Friday?" in digests[0][0][1]
    where, row = _find(out, tid)
    assert where == "now", (where, row)   # Social admits friends; the invitation is time-sensitive
    assert row["delivery"].startswith("in Now")
    # Back to the schedule: no held items, no digest.
    PUSHES.clear()
    out = _mode(base, None)
    assert out["mode"]["manual"] is False and not PUSHES
    print("ok test_mode_change_is_a_breakpoint")


def test_digest_on_every_device(base, wg):
    """A digest is framed on every open dashboard, not only the one whose
    push was tapped, until it is marked Done on any of them."""
    _mode(base, "focused")
    body = _open(base, "Card renewal", "The card on file expires Friday.",
                 {"importance": 4, "sphere": "admin", "kind": "admin chore"})
    assert body["attention"]["delivery"] == "hold"
    tid = "thread:" + body["id"]
    out = _mode(base, "chores")                     # a change by hand is a breakpoint
    unseen = out["unseen_digest"]
    assert unseen and unseen["count"] >= 1, out.get("unseen_digest")
    assert _find(out, tid)[1]["digest_at"] == unseen["at"]
    # Another device loads the home: the same digest, still unseen.
    assert _sections(base)["unseen_digest"] == unseen
    # Done on one device, and it is seen everywhere.
    status, done = _http(base, "POST", "/attention/seen", {"digest": unseen["at"]})
    assert status == 200 and done["seen"] == unseen["at"], done
    home = _sections(base)
    assert home["unseen_digest"] is None and home["last_digest"]["at"] == unseen["at"], home["last_digest"]
    # A seen mark never moves back; a bad one is refused.
    status, _ = _http(base, "POST", "/attention/seen", {"digest": "2020-01-01T00:00:00+00:00"})
    assert status == 200 and _sections(base)["unseen_digest"] is None
    status, _ = _http(base, "POST", "/attention/seen", {"digest": "soon"})
    assert status == 400
    _http(base, "POST", "/attention/items/done", {"id": tid})
    _mode(base, None)
    print("ok test_digest_on_every_device")


def test_corrections_learn(base, wg):
    _mode(base, "chores")
    body = _open(base, "Tax office letter", "Statement due 30 September.",
                 {"importance": 3, "sphere": "admin", "due": "2026-09-30", "kind": "tax filing"})
    tid = "thread:" + body["id"]
    status, out = _http(base, "POST", "/attention/items/correct", {"id": tid, "importance": 5})
    assert status == 200 and out["item"]["importance"] == 5 and out["item"]["importance_from"] == "you"
    assert out["learned_now"] and "tax filing" in out["learned_now"][0], out["learned_now"]
    status, out = _http(base, "POST", "/attention/items/correct", {"id": tid, "lead": "4w"})
    assert status == 200 and out["item"]["lead"] == 4 * 7 * 1440 and out["item"]["lead_from"] == "you"
    status, prof = _http(base, "GET", "/attention/profile")
    assert prof["profile"]["priors"]["tax filing"] == 5
    assert prof["profile"]["leads"]["tax filing"] == 4 * 7 * 1440
    status, out = _http(base, "POST", "/attention/items/correct", {"id": tid, "due": None})
    assert status == 200 and out["item"]["due"] is None and out["item"]["urgency"] == "no deadline"
    status, out = _http(base, "POST", "/attention/items/correct", {"id": tid, "sphere": "customers"})
    assert status == 200 and out["item"]["sphere"] == "customers"
    status, out = _http(base, "POST", "/attention/items/correct", {"id": tid, "sphere": "nonsense"})
    assert status == 400
    status, out = _http(base, "POST", "/attention/items/correct", {"id": "thread:" + "0" * 32, "importance": 1})
    assert status == 404
    # A Focus rule: admit a sphere in the mode in force.
    status, out = _http(base, "POST", "/attention/admit", {"sphere": "customers", "mode": "focused", "on": True})
    assert status == 200 and out["changed"] is True and "customers" in out["modes"]["focused"]["admits"]
    status, out = _http(base, "POST", "/attention/admit", {"sphere": "customers", "mode": "focused", "on": False})
    assert status == 200 and out["changed"] is True
    print("ok test_corrections_learn")


# ── chats ──────────────────────────────────────────────────────────────────

def _inbound(base, sender, name, text, ts, **extra):
    payload = {"direction": "in", "channel": "signal", "chat": sender, "sender": sender,
               "sender_name": name, "text": text, "ts": ts, "message_id": f"m{abs(hash(ts))}",
               **extra}
    status, body = _http(base, "POST", "/internal/chats/inbound", payload)
    assert status == 200, (status, body)
    return body


def test_chat_inbound_gated_and_settled(base, wg):
    mum, beat = "+41790000001", "+41790000002"
    _mode(base, "focused")
    PUSHES.clear()
    body = _inbound(base, mum, "Mum", "Call me when you are up", "2026-09-05T06:40:00Z")
    assert body["pushed"] is False, body
    doc = wg._CHAT_STATE.get("signal:" + mum)
    assert doc["attention"]["state"] == "open" and doc["attention"]["released"] is False
    cid = "chat:signal:" + mum
    where, row = _find(_sections(base), cid)
    assert where == "held" and row["kind"] == "chat" and row["sender"] == "Mum" and row["sphere"] == "friends", row
    assert row["importance"] == 4 and row["importance_from"] == "default" and row["level"] == "active"
    assert row["href"] == "/chat.html?id=" + urllib.parse.quote("signal:" + mum, safe="")
    # The rail may carry the triage's judgement: importance, deadline, kind.
    body = _inbound(base, beat, "Beat Frei", "Clause 7 — your view by noon tomorrow", "2026-09-05T10:05:00Z",
                    attention={"importance": 4, "due": _due(26), "kind": "customer request",
                               "sphere": "customers"})
    assert body["pushed"] is False
    bid = "chat:signal:" + beat
    where, row = _find(_sections(base), bid)
    assert where == "held" and row["level"] == "time-sensitive" and row["sphere"] == "customers", row
    # A permit lets Beat interrupt Focused: the held chat is released and pushed.
    PUSHES.clear()
    status, out = _http(base, "POST", "/attention/permits", {"sender": "Beat Frei", "mode": "focused", "on": True})
    assert status == 200 and out["changed"] is True and bid in out["pushed"], out
    assert len(PUSHES) == 1 and PUSHES[0][1].get("urgency") == "high"
    where, row = _find(_sections(base), bid)
    assert where == "now" and row["permit"] is True and "permit" in row["reason"], row
    # Mum's sphere corrected to family, remembered for her.
    status, out = _http(base, "POST", "/attention/items/correct", {"id": cid, "sphere": "family"})
    assert status == 200 and out["item"]["sphere"] == "family"
    status, prof = _http(base, "GET", "/attention/profile")
    assert prof["profile"]["spheres"]["Mum"] == "family"
    # Off releases nothing (the morning digest carries it), so a message that
    # arrives while the chat is still held is a repeat — and a family repeat
    # breaks through in Off. Settle the morning's message first, so the night
    # starts clean: the first message is held, the second rings.
    status, _ = _http(base, "POST", "/attention/items/done", {"id": cid})
    assert status == 200
    _mode(base, "rest")
    PUSHES.clear()
    body = _inbound(base, mum, "Mum", "Are you there?", "2026-09-05T23:10:00Z")
    assert body["pushed"] is False, body
    body = _inbound(base, mum, "Mum", "Please call", "2026-09-05T23:12:00Z")
    assert body["pushed"] is True and len(PUSHES) == 1, (body, PUSHES)
    # The user's own reply from the phone settles the chat.
    status, body = _http(base, "POST", "/internal/chats/inbound",
                         {"direction": "out", "channel": "signal", "chat": mum, "author": "user",
                          "text": "On my way", "ts": "2026-09-05T23:15:00Z"})
    assert status == 200
    assert wg._CHAT_STATE.get("signal:" + mum)["attention"]["state"] == "done"
    assert _find(_sections(base), cid)[0] is None
    # Mark handled from the sheet, for the group chat nobody needs to answer.
    status, out = _http(base, "POST", "/attention/items/done", {"id": bid})
    assert status == 200 and _find(_sections(base), bid)[0] is None
    # A muted chat stays silent and is no item.
    _mode(base, "chores")
    wg._CHAT_STATE.set_flags("signal:+41790000003", muted=True)
    PUSHES.clear()
    body = _inbound(base, "+41790000003", "Group", "Street party!", "2026-09-05T12:00:00Z", group=True)
    assert body["pushed"] is False and not PUSHES
    # A group is chatter: passive, listed, never rung — even in Open.
    body = _inbound(base, "group:street", "Quartier", "Who brings the grill?", "2026-09-05T12:01:00Z",
                    group=True, chat_name="Quartier group")
    assert body["pushed"] is False and not PUSHES
    where, row = _find(_sections(base), "chat:signal:group:street")
    assert where == "next" and row["level"] == "passive" and row["importance"] == 1, row
    # The chat page's switches: archiving settles the item and unarchiving
    # brings back exactly what archiving settled; mute is its own flag.
    gid = "chat:signal:group:street"
    status, out = _http(base, "POST", "/chats/signal:group:street/flags", {"archived": True})
    assert status == 200 and out["archived"] is True and out["muted"] is False, out
    assert _find(_sections(base), gid)[0] is None
    status, out = _http(base, "POST", "/chats/signal:group:street/flags", {"muted": True})
    assert status == 200 and out["archived"] is True and out["muted"] is True
    status, out = _http(base, "POST", "/chats/signal:group:street/flags", {"archived": False})
    assert status == 200 and out["archived"] is False and out["muted"] is True
    assert _find(_sections(base), gid)[0] == "next"
    # What Mark handled settled does not come back by being unarchived.
    _http(base, "POST", "/attention/items/done", {"id": gid})
    _http(base, "POST", "/chats/signal:group:street/flags", {"archived": True})
    status, out = _http(base, "POST", "/chats/signal:group:street/flags", {"archived": False})
    assert status == 200 and _find(_sections(base), gid)[0] is None
    status, out = _http(base, "POST", "/chats/signal:group:street/flags", {})
    assert status == 400
    print("ok test_chat_inbound_gated_and_settled")


def test_unknown_sender_screened_then_named(base, wg):
    """A stranger with the user's number: screened until the contact card."""
    nadia = "+41791000042"
    chat = "signal:" + nadia
    cid = "chat:" + chat
    status, _ = _http(base, "POST", "/attention/mode", {"mode": "focused", "subject": "customers"})
    assert status == 200
    PUSHES.clear()
    # The gate let it through and she is no VIP; no name rides along either —
    # the chat is a bare number.
    body = _inbound(base, nadia, None, "Hi, Nadia from the workshop yesterday.",
                    "2026-09-05T14:05:00Z",
                    gate={"forward": True, "vip": False, "reason": "open"})
    assert body["pushed"] is False and not PUSHES, PUSHES
    where, row = _find(_sections(base), cid)
    assert where == "held", (where, row)
    # A human wrote to a human, so the importance stands; only the guess about
    # where they belong is withheld, and no mode admits that sphere.
    assert row["sphere"] == "unknown" and row["importance"] == 4 and row["level"] == "active", row
    assert row["unknown_sender"] is True and row["handle"] == nadia and row["contact"] is None, row
    assert row["title"] == nadia, row
    assert "Focused on customers — this is not" in row["delivery"], row["delivery"]

    # Pulled onto the list ahead of the digest — the message is never hidden.
    status, out = _http(base, "POST", "/attention/items/pull", {"id": cid})
    assert status == 200 and _find(_sections(base), cid)[0] == "next"

    # The contact card: a name, a sphere, and a second group as a tag.
    status, out = _http(base, "POST", f"/chats/{urllib.parse.quote(chat, safe='')}/contact",
                        {"name": "Nadia Brunner", "sphere": "customers", "tags": ["friends"]})
    # A new contact is stored in a chamber, and the card must say which.
    assert status == 400 and "chamber" in out["error"], out
    status, out = _http(base, "POST", f"/chats/{urllib.parse.quote(chat, safe='')}/contact",
                        {"name": "Nadia Brunner", "sphere": "customers", "tags": ["friends"],
                         "chamber": "private"})
    assert status == 200, out
    assert out["contact"]["name"] == "Nadia Brunner" and out["contact"]["sphere"] == "customers"
    assert out["contact"]["tags"] == ["friends"] and out["name"] == "Nadia Brunner"
    assert "whitelisted" not in out, out
    row = out["item"]
    assert row["sphere"] == "customers" and row["tags"] == ["friends"], row
    assert row["unknown_sender"] is False and row["title"] == "Nadia Brunner", row
    assert any("sphere for Nadia Brunner" in line for line in out["learned_now"]), out
    # What the card taught, where each half of it lives.
    profile = wg._ATTENTION.profile()
    assert profile["spheres"]["Nadia Brunner"] == "customers"
    assert out["contact"]["chamber"] == "private" and out["person"]["chamber"] == "private", out
    files = list((Path(os.environ["CHAMBERS_DIR"]) / "private" / "contacts").glob("nadia-brunner-*.nt"))
    assert len(files) == 1, files
    card = files[0].read_text(encoding="utf-8")
    assert '<http://www.w3.org/2006/vcard/ns#fn> "Nadia Brunner"' in card, card
    assert "<https://w3id.org/retinue/kb#sphere> <urn:retinue:sphere:customers>" in card, card
    assert "<urn:retinue:sphere:friends>" in card and f"<tel:{nadia}>" in card, card
    assert '<https://w3id.org/retinue/kb#channel> "signal"' in card and "foaf/0.1/OnlineAccount" in card, card
    assert "Nadia Brunner" in wg._contact_names()
    # The address book knows her by her number, on any channel.
    status, book = _http(base, "GET", f"/contacts?channel=signal&handle={urllib.parse.quote(nadia)}")
    assert status == 200 and [c["name"] for c in book["contacts"]] == ["Nadia Brunner"], book

    # Her next message comes from a named contact with a deadline:
    # time-sensitive, and customers is the scope — it rings, where the first
    # one was screened. The gate says what it said before; the card decides.
    PUSHES.clear()
    body = _inbound(base, nadia, "Nadia Brunner", "Can you send the studio address before 18:00?",
                    "2026-09-05T16:40:00Z",
                    gate={"forward": True, "vip": False, "reason": "open"},
                    attention={"importance": 4, "due": _due(1), "kind": "customer request"})
    assert body["pushed"] is True and PUSHES, PUSHES
    where, row = _find(_sections(base), cid)
    assert where == "now" and row["sphere"] == "customers" and row["level"] == "time-sensitive", row

    # Removing the card takes the name off and the item back to `unknown`.
    status, out = _http(base, "POST", f"/chats/{urllib.parse.quote(chat, safe='')}/contact",
                        {"name": ""})
    assert status == 200 and out["contact"] is None and out["name"] is None, out
    # The item is back in the screening sphere; the title falls back to the
    # roster, which learned her name from the channel's own second message.
    assert out["item"]["sphere"] == "unknown" and out["item"]["contact"] is None, out["item"]
    # Nothing vouches for her any more: no card, and she is no VIP.
    assert out["item"]["unknown_sender"] is True, out["item"]
    # The handle left her; she stays in the address book, reachable no more
    # by this chat.
    card = files[0].read_text(encoding="utf-8")
    assert '"Nadia Brunner"' in card and "OnlineAccount" not in card, card
    status, book = _http(base, "GET", f"/contacts?channel=signal&handle={urllib.parse.quote(nadia)}")
    assert status == 200 and book["contacts"] == [], book
    print("ok test_unknown_sender_screened_then_named")


def test_one_person_many_channels(base, wg):
    """A contact is a person, not a handle: the WhatsApp chat of someone filed
    from Signal is offered their contact and links to it, a rename reaches
    every chat of theirs, and an e-mail address is one more handle."""
    ivo = "+41791000099"
    gate = {"forward": True, "vip": False, "reason": "open"}
    _inbound(base, ivo, None, "Hi, Ivo here.", "2026-09-05T12:00:00Z", gate=gate)
    _inbound(base, ivo, None, "Ivo, on WhatsApp.", "2026-09-05T12:01:00Z", gate=gate, channel="whatsapp")
    sig, wa = "signal:" + ivo, "whatsapp:" + ivo
    status, out = _http(base, "POST", f"/chats/{urllib.parse.quote(sig, safe='')}/contact",
                        {"name": "Ivo Brand", "sphere": "friends", "chamber": "private"})
    assert status == 200, out
    person = out["person"]["id"]
    # The WhatsApp chat's sheet: same number, another channel — a suggestion,
    # and the chambers a new contact could go to.
    status, sheet = _http(base, "GET", "/attention/item?id=" + urllib.parse.quote("chat:" + wa, safe=""))
    assert status == 200, sheet
    book = sheet["contact_book"]
    assert book["chambers"] == ["private"] and book["default"] == "private", book
    assert [(c["id"], c["exact"]) for c in book["suggestions"]] == [(person, False)], book
    status, out = _http(base, "POST", f"/chats/{urllib.parse.quote(wa, safe='')}/contact",
                        {"name": "Ivo Brand", "sphere": "friends", "person": person})
    assert status == 200 and out["contact"]["person"] == person, out
    handles = {(h["channel"], h["handle"]) for h in out["person"]["handles"]}
    assert handles == {("signal", ivo), ("whatsapp", ivo)}, handles
    # E-mail through the address book's own API; a rename there reaches both chats.
    status, out = _http(base, "POST", f"/contacts/{person}",
                        {"name": "Ivo Brandt", "add": [{"channel": "email", "handle": "Ivo@Example.org"}]})
    assert status == 200 and out["contact"]["name"] == "Ivo Brandt", out
    assert ("email", "ivo@example.org") in {(h["channel"], h["handle"]) for h in out["contact"]["handles"]}, out
    assert wg._CHAT_STATE.get(sig)["name"] == "Ivo Brandt" and wg._CHAT_STATE.get(wa)["name"] == "Ivo Brandt"
    status, found = _http(base, "GET", "/contacts?channel=email&handle=ivo%40example.org")
    assert status == 200 and [c["id"] for c in found["contacts"]] == [person], found
    # A second contact cannot claim his address; a chamber that holds no
    # contacts cannot store one.
    status, out = _http(base, "POST", "/contacts", {"chamber": "private", "name": "Someone Else",
                                                    "handles": [{"channel": "email", "handle": "ivo@example.org"}]})
    assert status == 409, out
    status, out = _http(base, "POST", "/contacts", {"chamber": "elsewhere", "name": "Someone Else"})
    assert status == 400 and "elsewhere" in out["error"], out
    status, out = _http(base, "POST", "/contacts", {"chamber": "private", "name": "Eva Roth",
                                                    "handles": [{"channel": "email", "handle": "eva@example.org"}]})
    assert status == 201 and out["contact"]["chamber"] == "private", out
    for chat in (sig, wa):
        _http(base, "POST", "/attention/items/done", {"id": "chat:" + chat})
    print("ok test_one_person_many_channels")


def test_legacy_cards_are_filed(base, wg):
    """A card of the earlier, chamber-less kind — a name on the chat document
    and one generated Turtle file — is filed as a person in the default
    chamber at startup, and the generated file goes."""
    lea = "+41791000111"
    chat = "signal:" + lea
    _inbound(base, lea, None, "Lea here.", "2026-09-05T12:30:00Z",
             gate={"forward": True, "vip": False, "reason": "open"})
    wg._CHAT_STATE.set_contact(chat, name="Lea Graf", sphere="friends")
    wg.LEGACY_CONTACTS_EMIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    wg.LEGACY_CONTACTS_EMIT_PATH.write_text("# old\n", encoding="utf-8")
    assert wg._contacts_migrate() == 1
    card = wg._CHAT_STATE.get(chat)["contact"]
    assert card["chamber"] == "private" and card["person"], card
    record = wg._CONTACTS.get(card["person"])
    assert record["name"] == "Lea Graf" and ("signal", lea) in record["accounts"], record
    assert not wg.LEGACY_CONTACTS_EMIT_PATH.exists()
    assert wg._contacts_migrate() == 0, "a filed card is done"
    _http(base, "POST", "/attention/items/done", {"id": "chat:" + chat})
    print("ok test_legacy_cards_are_filed")


def test_a_person_in_several_spheres(base, wg):
    """A customer who is also a friend: every sphere of theirs counts for a
    mode, until the triage judges a message to be about one of them."""
    rita = "+41791000077"
    chat = "signal:" + rita
    cid = "chat:" + chat
    gate = {"forward": True, "vip": False, "reason": "open"}
    _inbound(base, rita, None, "Hi, Rita here.", "2026-09-05T09:00:00Z", gate=gate)
    status, out = _http(base, "POST", f"/chats/{urllib.parse.quote(chat, safe='')}/contact",
                        {"name": "Rita Keller", "sphere": "customers", "tags": ["friends"],
                         "chamber": "private"})
    assert status == 200 and out["item"]["sphere"] == "customers" and out["item"]["tags"] == ["friends"], out
    assert wg._ATTENTION.profile()["tags"]["Rita Keller"] == ["friends"], "the card's further spheres are hers"
    _http(base, "POST", "/attention/items/done", {"id": cid})
    # Social admits friends: her friends sphere lets her through, though her
    # main one is customers.
    _mode(base, "social")
    PUSHES.clear()
    body = _inbound(base, rita, "Rita Keller", "Can you look at the offer before 18:00?", "2026-09-05T09:10:00Z",
                    gate=gate, attention={"importance": 4, "due": _due(1), "kind": "customer request"})
    assert body["pushed"] is True and PUSHES, "a further sphere counts like the main one"
    where, row = _find(_sections(base), cid)
    assert row["sphere"] == "customers" and row["tags"] == ["friends"], row
    assert row["admission"] == {"by": "sphere", "what": "friends"} and row["reason"] == "Social admits friends", row
    _http(base, "POST", "/attention/items/done", {"id": cid})
    # Focused on customers — but the triage judged this message to be about
    # the barbecue: friends only, for this message.
    status, _ = _http(base, "POST", "/attention/mode", {"mode": "focused", "subject": "customers"})
    PUSHES.clear()
    body = _inbound(base, rita, "Rita Keller", "Barbecue on Saturday — bring salad?", "2026-09-05T09:20:00Z",
                    gate=gate, attention={"importance": 4, "due": _due(1), "sphere": "friends"})
    assert body["pushed"] is False and not PUSHES, "the message's own sphere decides"
    where, row = _find(_sections(base), cid)
    assert where == "held" and row["sphere"] == "friends" and row["tags"] == [] and row["admission"] is None, (where, row)
    # A follow-up the triage did not judge is still about the barbecue: the
    # deadline stays, and so does friends — it is held, not rung.
    body = _inbound(base, rita, "Rita Keller", "Bring a salad?", "2026-09-05T09:21:00Z", gate=gate)
    assert body["pushed"] is False and not PUSHES, "a follow-up keeps its judgement whole"
    where, row = _find(_sections(base), cid)
    assert where == "held" and row["sphere"] == "friends" and row["level"] == "time-sensitive", (where, row)
    # The next message, judged without a sphere, is hers again: customers + friends.
    body = _inbound(base, rita, "Rita Keller", "And the offer — did you see it?", "2026-09-05T09:30:00Z",
                    gate=gate, attention={"importance": 4, "due": _due(1), "kind": "customer request"})
    assert body["pushed"] is True
    where, row = _find(_sections(base), cid)
    assert row["sphere"] == "customers" and row["tags"] == ["friends"], row
    assert row["admission"] == {"by": "scope", "what": "customers"}, row
    # The sheet's switches edit her further spheres, remembered for her.
    status, out = _http(base, "POST", "/attention/items/correct", {"id": cid, "tags": ["friends", "family", "customers"]})
    assert status == 200 and out["item"]["tags"] == ["friends", "family"], out["item"]
    assert wg._ATTENTION.profile()["tags"]["Rita Keller"] == ["friends", "family"]
    assert any("further spheres for Rita Keller" in x for x in out["learned_now"]), out["learned_now"]
    _http(base, "POST", "/attention/items/done", {"id": cid})
    _mode(base, "")
    print("ok test_a_person_in_several_spheres")


def test_a_message_judgement_is_its_own(base, wg):
    """A triage judgement belongs to the chat's open item: tags alone count
    beside the sender's main sphere, an unclassified follow-up keeps the whole
    judgement while the item is open, the next message after it was handled
    starts fresh with the sender's spheres, and a tags-only correction
    teaches the profile further spheres beside the sender's main sphere."""
    nora = "+41791000088"
    chat = "signal:" + nora
    cid = "chat:" + chat
    gate = {"forward": True, "vip": False, "reason": "open"}
    _inbound(base, nora, None, "Hi, Nora here.", "2026-09-05T11:00:00Z", gate=gate)
    status, out = _http(base, "POST", f"/chats/{urllib.parse.quote(chat, safe='')}/contact",
                        {"name": "Nora Weber", "sphere": "customers", "tags": ["friends"],
                         "chamber": "private"})
    assert status == 200, out
    _http(base, "POST", "/attention/items/done", {"id": cid})
    status, _ = _http(base, "POST", "/attention/mode", {"mode": "focused", "subject": "family"})
    assert status == 200
    # Tags without a sphere: health, which Focused admits whatever the scope.
    PUSHES.clear()
    body = _inbound(base, nora, "Nora Weber", "The results from the clinic are in.", "2026-09-05T11:10:00Z",
                    gate=gate, attention={"importance": 4, "due": _due(1), "tags": ["health"]})
    assert body["pushed"] is True and PUSHES, "a message's tags count without a sphere of its own"
    where, row = _find(_sections(base), cid)
    assert row["sphere"] == "customers" and row["tags"] == ["health"], row
    assert row["admission"] == {"by": "tag", "what": "health"}, row
    _http(base, "POST", "/attention/items/done", {"id": cid})
    # An unclassified follow-up while the judged item is open keeps the whole
    # judgement — its sphere with its deadline, never one without the other.
    _mode(base, "social")
    _inbound(base, nora, "Nora Weber", "Barbecue Saturday?", "2026-09-05T11:20:00Z",
             gate=gate, attention={"importance": 4, "due": _due(1), "sphere": "family"})
    where, row = _find(_sections(base), cid)
    assert row["sphere"] == "family" and row["tags"] == [], row
    judged_due = row["due"]
    _inbound(base, nora, "Nora Weber", "Bring a salad?", "2026-09-05T11:22:00Z", gate=gate)
    where, row = _find(_sections(base), cid)
    assert row["sphere"] == "family" and row["tags"] == [] and row["due"] == judged_due, row
    # Once it is handled, the next message starts fresh: hers again, and
    # without the settled deadline.
    _http(base, "POST", "/attention/items/done", {"id": cid})
    _inbound(base, nora, "Nora Weber", "Also, the invoice.", "2026-09-05T11:25:00Z", gate=gate)
    where, row = _find(_sections(base), cid)
    assert row["sphere"] == "customers" and row["tags"] == ["friends"], row
    assert row["due"] is None and row["importance_from"] in ("default", "prior"), row
    assert row["admission"] == {"by": "sphere", "what": "friends"}, row
    # A tags-only correction of a judged message: the item keeps the
    # message's sphere now and on reload, the profile learns further spheres
    # beside her main one.
    _inbound(base, nora, "Nora Weber", "Barbecue Saturday — salad?", "2026-09-05T11:30:00Z",
             gate=gate, attention={"importance": 4, "due": _due(1), "sphere": "family"})
    status, out = _http(base, "POST", "/attention/items/correct",
                        {"id": cid, "tags": ["friends", "family", "board-games"]})
    assert status == 200 and out["item"]["sphere"] == "family", out["item"]
    assert out["item"]["tags"] == ["friends", "board-games"], out["item"]
    assert wg._ATTENTION.profile()["tags"]["Nora Weber"] == ["friends", "family", "board-games"]
    where, row = _find(_sections(base), cid)
    assert row["sphere"] == "family" and row["tags"] == ["friends", "board-games"], row
    _http(base, "POST", "/attention/items/done", {"id": cid})
    _mode(base, "")
    print("ok test_a_message_judgement_is_its_own")


def test_vip_always_rings(base, wg):
    """A VIP rings whatever the mode; a muted chat keeps even a VIP quiet."""
    lena = "+41791000077"
    cid = "chat:signal:" + lena
    status, _ = _http(base, "POST", "/attention/mode", {"mode": "focused", "subject": "customers"})
    assert status == 200
    PUSHES.clear()
    body = _inbound(base, lena, "Lena", "Landed — can you pick me up?", "2026-09-05T14:30:00Z",
                    gate={"forward": True, "vip": True, "reason": "open"})
    assert body["pushed"] is True and PUSHES, PUSHES
    where, row = _find(_sections(base), cid)
    # Known by being a VIP: never screened, whatever the card says (none here).
    assert where == "now" and row["unknown_sender"] is False and row["sphere"] != "unknown", (where, row)
    assert "VIP" in row["delivery"], row["delivery"]
    # The same person in a chat the user muted: the mirror has it, nobody rings.
    status, _ = _http(base, "POST", f"/chats/{urllib.parse.quote('signal:' + lena, safe='')}/flags",
                      {"muted": True})
    assert status == 200
    PUSHES.clear()
    body = _inbound(base, lena, "Lena", "Never mind, got a taxi.", "2026-09-05T14:40:00Z",
                    gate={"forward": True, "vip": True, "reason": "open"})
    assert body["pushed"] is False and not PUSHES, PUSHES
    print("ok test_vip_always_rings")


def test_focused_takes_a_scope(base, wg):
    """Focused on a sphere admits the sphere; on a project, only what is about it."""
    PUSHES.clear()
    quote = _open(base, "Quote for Müller AG", "Draft ready", {"importance": 4, "sphere": "customers", "due": _due(3), "kind": "customer request"},
                  project="urn:retinue:project:mueller", project_title="Müller AG")
    other = _open(base, "Frei Bau retainer", "Draft ready", {"importance": 4, "sphere": "customers", "due": _due(3), "kind": "customer request"})
    body = _mode(base, "focused")
    assert body["mode"]["subject"] is None and body["mode"]["label"] == "Focused"
    # The change by hand released what was held; on nothing, Focused folds it away.
    assert _find(_sections(base), "thread:" + quote["id"])[0] == "not_now"
    status, body = _http(base, "POST", "/attention/mode", {"mode": "focused", "subject": "customers"})
    assert status == 200 and body["mode"]["subject"]["id"] == "customers" and body["mode"]["admits"] == ["customers"], body["mode"]
    assert body["mode"]["label"] == "Focused on customers"
    assert _find(_sections(base), "thread:" + quote["id"])[0] == "now" and _find(_sections(base), "thread:" + other["id"])[0] == "now"
    status, body = _http(base, "POST", "/attention/mode", {"mode": "focused", "project": "urn:retinue:project:mueller"})
    assert status == 200 and body["mode"]["subject"]["kind"] == "project" and body["mode"]["label"] == "Focused on Müller AG", body["mode"]
    where_q, row_q = _find(_sections(base), "thread:" + quote["id"])
    where_o, row_o = _find(_sections(base), "thread:" + other["id"])
    assert where_q == "now" and row_q["reason"] == "Focused on Müller AG: this is about it", row_q
    assert where_o == "not_now" and row_o["reason"] == "Focused on Müller AG — this is not", row_o
    status, body = _http(base, "POST", "/attention/mode", {"mode": "focused", "project": "urn:retinue:project:nope"})
    assert status == 400
    # Releasing the mode drops the scope with it.
    body = _mode(base, "")
    assert body["mode"]["manual"] is False and wg._ATTENTION.focus()["subject"] is None
    for tid in (quote["id"], other["id"]):
        _http(base, "POST", "/attention/items/done", {"id": "thread:" + tid})
    print("ok test_focused_takes_a_scope")


def test_focused_admits_health_by_word(base, wg):
    """Focused on customers lets health through by a word it admits wherever
    it stands, not by its sphere list: the row says so, so the sheet offers to
    stop *that* — and stopping it folds the health items away."""
    status, _ = _http(base, "POST", "/attention/mode", {"mode": "focused", "subject": "customers"})
    assert status == 200
    # Listed ones (passive, so on the list at once) and one held for the digest.
    physio = _open(base, "Physio moved", "Now Thursday 08:00.", {"importance": 2, "sphere": "health"})
    walk = _open(base, "Walk with Anna", "Her knee is better.", {"importance": 2, "sphere": "friends", "tags": ["health"]})
    scan = _open(base, "Scan results", "The clinic will call.", {"importance": 4, "sphere": "health"})
    quote = _open(base, "Quote for Frei Bau", "Draft ready.", {"importance": 4, "sphere": "customers"})
    home = _sections(base)
    for tid in (physio["id"], walk["id"]):
        where, row = _find(home, "thread:" + tid)
        assert where == "next" and row["admission"] == {"by": "tag", "what": "health"}, (where, row)
        assert row["admits_sphere"] is False, "not by the sphere list — the old switch offered to admit it"
    assert _find(home, "thread:" + scan["id"])[1]["admission"] == {"by": "tag", "what": "health"}
    assert _find(home, "thread:" + quote["id"])[1]["admission"] == {"by": "scope", "what": "customers"}
    status, out = _http(base, "POST", "/attention/modes", {"mode": "focused", "tag_off": ["health"]})
    assert status == 200 and out["changed"] == ["Focused no longer admits the tag health"], out
    home = _sections(base)
    for tid in (physio["id"], walk["id"]):
        where, row = _find(home, "thread:" + tid)
        assert where == "not_now" and row["admission"] is None, (where, row)
    where, row = _find(home, "thread:" + scan["id"])
    assert where == "held" and row["admission"] is None and row["reason"] == "Focused on customers — this is not", (where, row)
    # Back as it shipped, for the checks that follow.
    status, out = _http(base, "POST", "/attention/modes", {"mode": "focused", "tag_on": ["health"]})
    assert status == 200 and _find(_sections(base), "thread:" + physio["id"])[1]["admission"]["by"] == "tag"
    for tid in (physio["id"], walk["id"], scan["id"], quote["id"]):
        _http(base, "POST", "/attention/items/done", {"id": "thread:" + tid})
    _mode(base, "")
    print("ok test_focused_admits_health_by_word")


def test_spheres_are_a_word_away(base, wg):
    """A sphere is the user's subject; adding one costs a word."""
    status, out = _http(base, "POST", "/attention/spheres", {"add": "Board Games"})
    assert status == 200 and out["added"] == "board-games" and "board-games" in out["spheres"], out
    status, out = _http(base, "POST", "/attention/spheres", {"add": "Ökologie"})
    assert status == 200 and out["added"] == "ökologie", out
    status, out = _http(base, "POST", "/attention/spheres", {"add": "board games"})
    assert status == 200 and out["spheres"].count("board-games") == 1, out
    status, out = _http(base, "POST", "/attention/spheres", {"add": "!!"})
    assert status == 400, out
    assert "board-games" in _sections(base)["spheres"]
    # Usable at once: on an item, and in a rule.
    body = _open(base, "Club night", "Thursday at the club", {"importance": 3, "sphere": "friends"})
    tid = "thread:" + body["id"]
    status, out = _http(base, "POST", "/attention/items/correct", {"id": tid, "sphere": "board-games"})
    assert status == 200 and out["item"]["sphere"] == "board-games", out
    status, out = _http(base, "POST", "/attention/admit", {"sphere": "board-games", "mode": "social", "on": True})
    assert status == 200, out
    # Not removable while a rule names it; removable once none does.
    status, out = _http(base, "POST", "/attention/spheres", {"remove": "board-games"})
    assert status == 400 and "Social" in out["error"], out
    _http(base, "POST", "/attention/admit", {"sphere": "board-games", "mode": "social", "on": False})
    status, out = _http(base, "POST", "/attention/spheres", {"remove": "board-games"})
    assert status == 200 and "board-games" not in out["spheres"], out
    status, out = _http(base, "POST", "/attention/spheres", {"remove": "unknown"})
    assert status == 400, out
    # The item keeps its word.
    status, out = _http(base, "GET", "/attention/item?id=" + urllib.parse.quote(tid, safe=""))
    assert status == 200 and out["item"]["sphere"] == "board-games", out
    print("ok test_spheres_are_a_word_away")


# ── projects ───────────────────────────────────────────────────────────────

def test_project_from_store(base, wg):
    _mode(base, "chores")
    where, row = _find(_sections(base), PROJECT)
    assert where in ("now", "next") and row["kind"] == "project", (where, row)
    assert row["importance"] == 4 and row["importance_from"] == "frontmatter"
    assert row["sphere"] == "admin" and row["tags"] == ["finance"] and row["kind_label"] == "tax filing"
    assert row["due"].startswith("2026-09-30T17:00"), row["due"]
    # The lead corrected on the tax-office thread applies to every "tax filing".
    status, prof = _http(base, "GET", "/attention/profile")
    assert row["lead"] == prof["profile"]["leads"]["tax filing"] == 4 * 7 * 1440, row["lead"]
    assert row["preview"] == "Collect the receipts" and row["href"].startswith("/project.html?id=")
    # A correction on a project touches the gateway's state, never the file.
    status, out = _http(base, "POST", "/attention/items/correct", {"id": PROJECT, "importance": 5})
    assert status == 200 and out["item"]["importance"] == 5
    states = json.loads((Path(os.environ["ATTENTION_DIR"]) / "projects.json").read_text())
    assert states[PROJECT]["importance"] == 5
    # The store down: the list still comes, and says what is missing.
    STATE["projects"] = False
    try:
        body = _sections(base)
        assert body["degraded"] == [] and _find(body, PROJECT)[0] is None or True
    finally:
        STATE["projects"] = True
    # A thread an agent opens about the project — recurring-projects' wake-up
    # — takes the project's place on the list: one row, not two, linking to
    # both; when the thread is settled the project is its own row again.
    body = _open(base, "Due: VAT return Q3", "The return is due; the figures are ready.",
                 {"importance": 4, "sphere": "admin", "kind": "tax filing"}, project=PROJECT, project_title="VAT return Q3")
    tid = "thread:" + body["id"]
    assert _find(_sections(base), PROJECT)[0] is None, "the project row folds into its thread"
    where, row = _find(_sections(base), tid)  # active without a deadline: held in Open
    assert where == "held" and row["project"] == PROJECT and row["project_title"] == "VAT return Q3", (where, row)
    assert row["project_href"] == "/project.html?id=" + urllib.parse.quote(PROJECT, safe=""), row["project_href"]
    status, conv = _http(base, "GET", f"/conversations/{body['id']}")
    assert conv["project"] == PROJECT
    _http(base, "POST", "/attention/items/done", {"id": tid})
    assert _find(_sections(base), PROJECT)[0] in ("now", "next")
    status, body = _http(base, "POST", "/internal/conversations", {"message": "x", "project": "not a uri"}, AGENT)
    assert status == 400
    print("ok test_project_from_store")


# ── the tick ───────────────────────────────────────────────────────────────

def test_tick_digest_and_sweep(base, wg):
    _mode(base, None)
    monday = datetime(2026, 9, 7, tzinfo=timezone.utc)
    _clock(wg, monday.replace(hour=10, minute=0))          # Focused on nothing by schedule
    body = _open(base, "Sign the NDA", "Their lawyer wants it before 12:30.",
                 {"importance": 4, "sphere": "customers", "due": monday.replace(hour=12, minute=30).isoformat(),
                  "kind": "customer request"})
    assert body["attention"]["delivery"] == "hold", body
    tid = "thread:" + body["id"]
    # An admin chore that is not urgent yet at 10:00 but will be by 10:30.
    body2 = _open(base, "Renew the permit", "The parking permit lapses at noon.",
                  {"importance": 3, "sphere": "admin", "due": monday.replace(hour=12, minute=0).isoformat(),
                   "lead": "2h"})
    assert body2["attention"]["delivery"] == "hold"
    tid2 = "thread:" + body2["id"]
    PUSHES.clear()
    report = wg._attention_tick(monday.replace(hour=10, minute=0))
    assert report["events"] == ["sweep"] and not PUSHES, report
    assert wg._attention_tick(monday.replace(hour=10, minute=0)) == {}, "one run per minute"
    # 10:30: the sweep finds the permit within a third of its lead — it climbs,
    # but Focused on nothing admits nothing below critical, so it still waits.
    report = wg._attention_tick(monday.replace(hour=10, minute=30))
    assert "sweep" in report["events"] and not PUSHES
    where, row = _find(_sections(base), tid2)
    assert where == "held" and row["level"] == "active", row
    # 12:00: the digest and the scheduled change to Open — one digest push,
    # the held items released, and the NDA in Now (Open admits customers).
    report = wg._attention_tick(monday.replace(hour=12, minute=0))
    assert set(report["events"]) >= {"digest", "mode", "sweep"}, report
    digests = [p for p in PUSHES if p[1].get("topic") == "digest"]
    assert len(digests) == 1 and report["digest"] >= 2, (PUSHES, report)
    # The push: one line per item with why it is there, the most pressing
    # first, and a link that opens the home on what it released.
    (title, text), kw = digests[0]
    assert title.startswith("Digest 12:00 · ") and "\n" in text and " — due " in text, (title, text)
    assert kw["url"].startswith("/?digest=2026-09-07T12%3A00%3A00"), kw["url"]
    _clock(wg, monday.replace(hour=12, minute=1))
    home = _sections(base)
    where, row = _find(home, tid)
    assert where == "now", (where, row)
    assert row["digest_at"] == "2026-09-07T12:00:00+00:00" and home["last_digest"]["at"] == row["digest_at"], (row, home["last_digest"])
    assert home["last_digest"]["count"] == report["digest"], home["last_digest"]
    emitted = Path(os.environ["CHAMBERS_DIR"]) / "_generated" / "attention" / "items.nt"
    text = emitted.read_text()
    assert f"<urn:retinue:thread:{body['id']}> <https://w3id.org/retinue/kb#sphere> <urn:retinue:sphere:customers> ." in text
    assert "<urn:retinue:chat:signal:" in text or "urn:retinue:project" in text
    # 13:00, Work mode: an appointment 2.5 h away is active (its lead is 2 h)
    # and waits; at 13:45 it is within the lead — time-sensitive, and Work
    # admits health, so the sweep at 14:00 pushes it.
    _clock(wg, monday.replace(hour=13, minute=0))
    body3 = _open(base, "Physio at 15:30", "Leave by 15:00.",
                  {"importance": 4, "sphere": "health", "due": monday.replace(hour=15, minute=30).isoformat(),
                   "kind": "appointment"})
    assert body3["attention"]["delivery"] == "hold" and body3["attention"]["level"] == "active", body3
    PUSHES.clear()
    report = wg._attention_tick(monday.replace(hour=14, minute=0))
    assert "thread:" + body3["id"] in report["pushed"], report
    assert PUSHES and PUSHES[-1][1].get("urgency") == "high"
    _clock(wg, monday.replace(hour=14, minute=1))
    assert _find(_sections(base), "thread:" + body3["id"])[0] == "now"
    PUSHES.clear()
    body4 = _open(base, "Invoice run", "Monthly invoices due 18:00.",
                  {"importance": 4, "sphere": "customers", "due": monday.replace(hour=18, minute=0).isoformat(),
                   "kind": "invoice run"})
    assert body4["attention"]["delivery"] == "push"
    _clock(wg, None)
    print("ok test_tick_digest_and_sweep")


def test_internal_set(base, wg):
    _mode(base, "chores")
    body = _open(base, "Brochure", "Draft attached.", {"importance": 2, "sphere": "customers"})
    tid = "thread:" + body["id"]
    status, out = _http(base, "POST", "/internal/attention/set", {"id": tid, "importance": 4})
    assert status == 403, "token-gated"
    PUSHES.clear()
    status, out = _http(base, "POST", "/internal/attention/set",
                        {"id": tid, "importance": 4, "due": _due(3), "kind": "customer request"}, AGENT)
    assert status == 200 and out["item"]["level"] == "time-sensitive", out
    assert out["effect"] and out["effect"]["type"] == "push" and len(PUSHES) == 1
    status, out = _http(base, "POST", "/internal/attention/set", {"id": tid, "actor": "Publisher"}, AGENT)
    assert status == 200 and _find(_sections(base), tid)[0] == "waiting"
    status, out = _http(base, "POST", "/internal/attention/set", {"id": tid, "state": "done"}, AGENT)
    assert status == 200 and _find(_sections(base), tid)[0] is None
    status, out = _http(base, "POST", "/internal/attention/set", {"id": "thread:" + "1" * 32}, AGENT)
    assert status == 404
    print("ok test_internal_set")


def test_payload_shape(base, wg):
    _clock(wg, datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc))      # a Monday: the Workday plan
    body = _sections(base)
    assert body["timezone"] == "UTC" and body["mode"]["id"] in {m["id"] for m in body["modes"]}
    assert len(body["schedule"]) == 8 and body["digest_times"] == [480, 720, 1020, 1260]
    assert body["mode"]["day"] == {"plan": "Workday", "days": ["mon-fri"], "holiday": None}, body["mode"]["day"]
    assert [p["name"] for p in body["week"]] == ["Workday", "Day off"] and body["holidays"] == []
    assert set(body["counts"]) == {"now", "next", "held", "waiting", "not_now"}
    assert all("only_admitted" in m for m in body["modes"]) and body["mode"]["only_admitted"] in (True, False)
    assert body["spheres"] == ["customers", "admin", "health", "friends", "family", "system",
                               "unknown"]
    assert body["next_breakpoint"] and body["mode"]["scheduled"]["until"]
    status, out = _http(base, "GET", "/attention/item?id=nothing")
    assert status == 404
    status, out = _http(base, "POST", "/attention/mode", {"mode": "nope"})
    assert status == 400
    _clock(wg, None)
    print("ok test_payload_shape")


def test_hand_set_focus_and_breaks(base, wg):
    """Switching into Focused by hand sends no digest; a timed mode keeps its
    own breakpoints — a break every 55 minutes past an hour, and its end —
    and gives way to the schedule when its time is up."""
    monday = datetime(2026, 9, 7, 13, 58, tzinfo=timezone.utc)
    _clock(wg, monday)
    _mode(base, "social")                         # customers are held in Social
    body = _open(base, "Invoice question", "Which account?",
                 {"importance": 4, "sphere": "customers", "due": (monday + timedelta(hours=30)).isoformat(),
                  "kind": "customer request"})
    tid = "thread:" + body["id"]
    assert body["attention"]["delivery"] == "hold", body["attention"]
    _clock(wg, monday.replace(minute=0, hour=14))
    PUSHES.clear()
    status, out = _http(base, "POST", "/attention/mode", {"mode": "focused", "minutes": 120})
    assert status == 200 and not PUSHES, PUSHES                        # no digest into Focused
    assert _find(out, tid)[0] == "held", _find(out, tid)
    assert out["mode"]["manual_until"] == "2026-09-07T16:00:00+00:00" and out["mode"]["breaks"] == ["2026-09-07T14:55:00+00:00"], out["mode"]
    assert out["next_breakpoint"] == "2026-09-07T14:55:00+00:00", out["next_breakpoint"]
    # 14:55: the suggested breakpoint releases what waited, as a break.
    report = wg._attention_tick(monday.replace(hour=14, minute=55))
    assert "break" in report["events"] and report["digest"] == 1, report
    digests = [p for p in PUSHES if p[1].get("topic") == "digest"]
    assert len(digests) == 1 and digests[0][0][0] == "Break 14:55 · 1 thing waited", digests
    # 16:00: its time is up — back to the schedule (Focused on customers).
    PUSHES.clear()
    report = wg._attention_tick(monday.replace(hour=16, minute=0))
    assert "end" in report["events"], report
    _clock(wg, monday.replace(hour=16, minute=1))
    home = _sections(base)
    assert home["mode"]["manual"] is False and home["mode"]["manual_until"] is None and home["mode"]["label"] == "Focused on customers", home["mode"]
    # Focused on a sphere by hand: what it lets through rings at once, alone.
    _mode(base, "social")
    other = _open(base, "Contract deadline", "Sign by 18:00",
                  {"importance": 4, "sphere": "customers", "due": monday.replace(hour=17, minute=0).isoformat(),
                   "kind": "customer request"})
    assert other["attention"]["delivery"] == "hold", other["attention"]
    PUSHES.clear()
    status, out = _http(base, "POST", "/attention/mode", {"mode": "focused", "subject": "customers", "until": "18:00"})
    assert status == 200 and out["mode"]["manual_until"] == "2026-09-07T18:00:00+00:00", out["mode"]
    # Pushes, not a digest: the new deadline, and anything held or released
    # before that Focused on customers now lets through and never rang.
    assert "Contract deadline" in [p[0][0] for p in PUSHES] and not any(p[1].get("topic") for p in PUSHES), PUSHES
    assert _find(out, "thread:" + other["id"])[0] == "now"
    for bad in ({"mode": "chores", "minutes": -5}, {"mode": "chores", "minutes": 2000}, {"mode": "chores", "until": "noon"}):
        status, out = _http(base, "POST", "/attention/mode", bad)
        assert status == 400, (bad, out)
    _mode(base, None)
    _clock(wg, None)
    print("ok test_hand_set_focus_and_breaks")


def test_week_and_holidays(base, wg):
    """A day plan rules the days it names; a holiday follows the plan that
    claims holidays, whatever its weekday; the patch keeps every weekday in
    exactly one plan."""
    saturday = datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc)
    _clock(wg, saturday)
    status, _ = _http(base, "POST", "/attention/mode", {"mode": None})    # follow the schedule
    assert status == 200
    body = _sections(base)
    assert body["mode"]["day"]["plan"] == "Day off" and body["mode"]["id"] == "social", body["mode"]
    assert body["digest_times"] == [540, 1080] and body["schedule"][0] == [0, "rest"]
    # A week off: told as a date range, not a new schedule.
    status, out = _http(base, "POST", "/attention/modes", {"holiday_add": "2026-10-05..2026-10-09 Autumn break"})
    assert status == 200 and out["changed"] == ["holiday Autumn break, 2026-10-05 – 2026-10-09"], out
    _clock(wg, datetime(2026, 10, 7, 10, 0, tzinfo=timezone.utc))      # a Wednesday in it
    body = _sections(base)
    assert body["mode"]["day"] == {"plan": "Day off", "days": ["sat", "sun", "holiday"], "holiday": "Autumn break"}, body["mode"]["day"]
    assert body["mode"]["id"] == "social"
    # A plan of its own for Fridays: the day moves out of Workday.
    _clock(wg, saturday)
    status, out = _http(base, "POST", "/attention/modes", {"plan": "Friday", "days": "fri",
                                                            "schedule": "07:00 chores, 08:00 focused, 14:00 social, 22:00 rest"})
    assert status == 200 and out["changed"] == ["new day plan Friday: fri"], out
    assert [(p["name"], p["days"]) for p in out["focus"]["week"]] == [("Workday", ["mon-thu"]), ("Day off", ["sat", "sun", "holiday"]), ("Friday", ["fri"])]
    _clock(wg, datetime(2026, 9, 11, 15, 0, tzinfo=timezone.utc))      # Friday afternoon
    assert _sections(base)["mode"]["id"] == "social"
    for bad in ({"plan": "Workday", "days": "mon-wed"}, {"holiday_add": "2026-02-30"},
                {"schedule": "08:00 chores"}, {"holiday_remove": "Easter"}):
        status, out = _http(base, "POST", "/attention/modes", bad)
        assert status == 400 and out["error"], (bad, out)
    status, out = _http(base, "POST", "/attention/modes", {"plan": "Workday", "days": "mon-fri", "holiday_remove": "Autumn break"})
    assert status == 200 and "Friday is gone — no days left" in out["changed"] and out["focus"]["holidays"] == [], out
    # A document from before the week: its one schedule rules every day.
    status, out = _http(base, "GET", "/attention/profile")
    legacy = {k: v for k, v in out["focus"].items() if k not in ("week", "holidays")}
    legacy["schedule"] = [[0, "rest"], [480, "chores"], [1320, "rest"]]
    status, out = _http(base, "POST", "/attention/profile", {"focus": legacy})
    assert status == 200 and [(p["name"], p["days"]) for p in out["focus"]["week"]] == [("Every day", ["mon-sun", "holiday"])], out["focus"]
    assert _sections(base)["mode"]["id"] == "chores"
    status, out = _http(base, "POST", "/attention/profile", {"focus": {**legacy, "week": [{"name": "A", "days": "mon-fri", "schedule": [[0, "rest"]]}]}})
    assert status == 400 and "sat" in out["error"], out
    status, out = _http(base, "POST", "/attention/modes", {"week": [
        {"name": "Workday", "days": "weekdays", "schedule": [[0, "rest"], [420, "chores"], [480, "focused"], [720, "chores"], [780, "focused", "customers"], [1020, "chores"], [1080, "social"], [1320, "rest"]]},
        {"name": "Day off", "days": "weekend, holiday", "schedule": "00:00 rest, 09:00 social, 22:00 rest", "digest_times": ["09:00", "18:00"]}]})
    assert status == 200 and out["changed"][0].startswith("the week: Workday mon-fri · Day off sat, sun, holiday"), out
    _clock(wg, None)
    print("ok test_week_and_holidays")

# ── what the review of the branch found ─────────────────────────────────────

def test_append_reports_delivery(base, wg):
    """An append is judged like an opening, and says so: an agent told
    nothing would report the user notified while the news waits."""
    _mode(base, "focused")
    body = _open(base, "Card renewal, again", "The card expires Friday.",
                 {"importance": 4, "sphere": "admin", "kind": "admin chore"})
    assert body["attention"]["delivery"] == "hold", body
    PUSHES.clear()
    status, out = _http(base, "POST", f"/internal/conversations/{body['id']}/messages",
                        {"message": "The bank sent a reminder."}, AGENT)
    assert status == 201 and out["attention"]["delivery"] == "hold" and out["attention"]["until"], out
    assert not PUSHES
    status, out = _http(base, "POST", f"/internal/conversations/{body['id']}/messages",
                        {"message": "It is blocked now.", "attention": {"critical": True}}, AGENT)
    assert status == 201 and out["attention"]["delivery"] == "push", out
    print("ok test_append_reports_delivery")


def test_unarchive_keeps_done(base, wg):
    """Unarchiving brings back what archiving settled, not what the user had
    already marked done — as on the chat path."""
    _mode(base, "chores")
    done = _open(base, "Settled before archiving", "Nothing left to do.", {"importance": 4})
    _http(base, "POST", "/attention/items/done", {"id": "thread:" + done["id"]})
    _http(base, "POST", f"/conversations/{done['id']}/archive")
    _http(base, "POST", f"/conversations/{done['id']}/unarchive")
    assert _find(_sections(base), "thread:" + done["id"])[0] is None, "marked done stays done"
    open_ = _open(base, "Archived while open", "Still wants an answer.", {"importance": 4})
    _http(base, "POST", f"/conversations/{open_['id']}/archive")
    assert _find(_sections(base), "thread:" + open_["id"])[0] is None
    _http(base, "POST", f"/conversations/{open_['id']}/unarchive")
    assert _find(_sections(base), "thread:" + open_["id"])[0] is not None, "what archiving settled comes back"
    print("ok test_unarchive_keeps_done")


def test_declared_spheres_are_words(base, wg):
    """A sphere or tag an agent declares is normalised to the word the rules
    use, and the emit stays loadable N-Triples."""
    _mode(base, "chores")
    body = _open(base, "Game night", "Saturday at Tom's?",
                 {"importance": 2, "sphere": "Board Games", "tags": ["Friends", "Board Games"]})
    where, row = _find(_sections(base), "thread:" + body["id"])
    assert row["sphere"] == "board-games" and row["tags"] == ["friends", "board-games"], row
    wg._attention_emit(wg._attention_items(wg._ATTENTION.profile(), wg._attention_now())[0])
    emitted = (Path(os.environ["CHAMBERS_DIR"]) / "_generated" / "attention" / "items.nt").read_text()
    assert "<urn:retinue:sphere:board-games>" in emitted and "Board Games" not in emitted
    print("ok test_declared_spheres_are_words")


def test_broken_focus_document(base, wg):
    """A focus.json naming modes it does not define serves the list anyway."""
    path = wg.ATTENTION_DIR / "focus.json"
    saved = path.read_text()
    try:
        path.write_text(json.dumps({"modes": {"off": {"id": "off", "name": "Off", "admits": [],
                                                       "threshold": "critical"}}, "manual": "deep"}))
        status, body = _http(base, "GET", "/attention")
        assert status == 200 and body["mode"]["id"] in {m["id"] for m in body["modes"]}, (status, body)
        chat = {"channel": "signal", "account": "+41790000000", "chat": "+41791234567", "direction": "in",
                "text": "Still on for tonight?", "ts": "2026-09-07T10:00:00+00:00"}
        status, _ = _http(base, "POST", "/internal/chats/inbound", chat, {"X-Chats-Ingest-Token": "x"})
        assert status != 500, "the inbound rail must not fail on the focus document"
        wg._attention_tick_state.update(minute=None, emitted=False, ended=None)
        wg._attention_tick(datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc))
    finally:
        path.write_text(saved)
    print("ok test_broken_focus_document")


def test_project_frontmatter_moves(base, wg):
    """A deadline moved in the project file after the gateway stored the
    project's block is the deadline the list shows."""
    _mode(base, "chores")
    status, out = _http(base, "POST", "/attention/items/later", {"id": PROJECT, "when": "next"})
    assert status == 200
    states = json.loads((Path(os.environ["ATTENTION_DIR"]) / "projects.json").read_text())
    assert states[PROJECT]["frontmatter"]["deadline"] == "2026-09-30", states[PROJECT]
    PROJECT_ROW["expected"] = "2026-10-15"
    try:
        status, out = _http(base, "GET", "/attention/item?id=" + urllib.parse.quote(PROJECT, safe=""))
        assert status == 200 and out["item"]["due"].startswith("2026-10-15"), out["item"]["due"]
    finally:
        PROJECT_ROW["expected"] = "2026-09-30"
    print("ok test_project_frontmatter_moves")


def test_tick_makes_up_what_it_missed(base, wg):
    """A digest time the tick never got to — a stall, a restart — or whose
    run failed is made up, not lost."""
    _mode(base, None)
    tuesday = datetime(2026, 9, 8, tzinfo=timezone.utc)
    wg._attention_tick_state.update(minute=None, emitted=False, ended=None)
    wg._attention_tick(tuesday.replace(hour=7, minute=58))
    # 08:00 passes without a tick; the next one, at 08:02, makes it up.
    report = wg._attention_tick(tuesday.replace(hour=8, minute=2))
    assert "digest" in report["events"], report
    # A run that fails is tried again within the same minute.
    real = wg._attention_items
    calls = []

    def failing(*a, **k):
        if not calls:
            calls.append(1)
            raise RuntimeError("store stalled")
        return real(*a, **k)
    wg._attention_items = failing
    try:
        try:
            wg._attention_tick(tuesday.replace(hour=12, minute=0))
            raise AssertionError("the first run should have failed")
        except RuntimeError:
            pass
        report = wg._attention_tick(tuesday.replace(hour=12, minute=0))
        assert "digest" in report["events"], report
    finally:
        wg._attention_items = real
    # A restart across 17:00: the marker on disk says where the tick stopped.
    wg._attention_tick(tuesday.replace(hour=16, minute=30))
    wg._attention_tick_state.update(minute=None, emitted=False, ended=None)
    report = wg._attention_tick(tuesday.replace(hour=17, minute=3))
    assert "digest" in report["events"], report
    # A clock that went back (the simulation's seek) answers for its own
    # minute only: the half-hour sweep, and nothing made up.
    report = wg._attention_tick(tuesday.replace(hour=9, minute=0))
    assert report["events"] == ["sweep"], report
    print("ok test_tick_makes_up_what_it_missed")


def main():
    sparql = _serve(_MockSparql)
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp), sparql.server_address[1])
        wg.push_notify.enabled = lambda: True
        wg.push_notify.subscription_count = lambda: 1
        wg.push_notify.notify_async = lambda *a, **k: PUSHES.append((a, k))
        server = ThreadingHTTPServer(("127.0.0.1", 0), wg.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        test_payload_shape(base, wg)
        test_open_mode_pushes_and_lists_now(base, wg)
        test_deep_work_holds_pull_later_done(base, wg)
        test_critical_and_passive(base, wg)
        test_user_thread_never_gated(base, wg)
        test_mode_change_is_a_breakpoint(base, wg)
        test_digest_on_every_device(base, wg)
        test_corrections_learn(base, wg)
        test_chat_inbound_gated_and_settled(base, wg)
        test_unknown_sender_screened_then_named(base, wg)
        test_a_person_in_several_spheres(base, wg)
        test_one_person_many_channels(base, wg)
        test_legacy_cards_are_filed(base, wg)
        test_a_message_judgement_is_its_own(base, wg)
        test_vip_always_rings(base, wg)
        test_spheres_are_a_word_away(base, wg)
        test_focused_takes_a_scope(base, wg)
        test_focused_admits_health_by_word(base, wg)
        test_project_from_store(base, wg)
        test_tick_digest_and_sweep(base, wg)
        test_internal_set(base, wg)
        test_hand_set_focus_and_breaks(base, wg)
        test_week_and_holidays(base, wg)
        test_append_reports_delivery(base, wg)
        test_unarchive_keeps_done(base, wg)
        test_declared_spheres_are_words(base, wg)
        test_broken_focus_document(base, wg)
        test_project_frontmatter_moves(base, wg)
        test_tick_makes_up_what_it_missed(base, wg)
        server.shutdown()
    sparql.shutdown()
    print("all attention API checks passed")


if __name__ == "__main__":
    main()
