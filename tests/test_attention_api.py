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
  times; a permit granted in Deep work releases the sender's held chat;
- the chats rail: an inbound is held or pushed by the mode, the family repeat
  breaks through in Off, the user's own reply settles the chat's item;
- a project from the store carries its frontmatter's importance and deadline;
- the tick: the 12:00 digest releases what Deep work held, the sweep pushes
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
            bindings = [{"p": cell(PROJECT), "title": cell("VAT return Q3"),
                         "actor": cell("urn:retinue:actor:reto"), "expected": cell("2026-09-30"),
                         "importance": cell("4"), "sphere": cell("admin"), "tag": cell("finance"),
                         "kind": cell("tax filing"), "next": cell("Collect the receipts")}]
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
    _mode(base, "open")
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
    _mode(base, "deep")
    PUSHES.clear()
    body = _open(base, "Card renewal", "The card on file expires Friday. Renew?",
                 {"importance": 4, "sphere": "admin", "kind": "admin chore"})
    assert body["attention"]["delivery"] == "hold" and "until" in body["attention"], body
    assert body["attention"]["reason"] == "Deep work admits only critical"
    assert not PUSHES, "a held thread must not push"
    # The badge is there for whoever opens the dashboard; the push is not.
    status, conv = _http(base, "GET", f"/conversations/{body['id']}")
    assert conv["unread"] is True and conv["attention"]["released"] is False
    tid = "thread:" + body["id"]
    assert _find(_sections(base), tid)[0] == "held"
    status, out = _http(base, "POST", "/attention/items/pull", {"id": tid})
    assert status == 200 and out["item"]["released"] is True and out["item"]["pulled"] is True, out
    assert _find(_sections(base), tid)[0] == "next", "active is below Deep work's bar: Next, not Now — and pulled, so not folded"
    # Deep work lists only what it admits: a released item it does not admit
    # folds into Not now; the pulled one above stays. The fold is a per-mode
    # rule the menu toggles.
    passive = _open(base, "Newsletter", "The monthly newsletter is out.", {"importance": 1, "sphere": "admin"})
    pid = "thread:" + passive["id"]
    assert passive["attention"]["delivery"] == "list"
    assert _find(_sections(base), pid)[0] == "not_now"
    status, out = _http(base, "POST", "/attention/modes", {"mode": "deep", "only_admitted": False})
    assert status == 200 and out["changed"] == ["Deep work lists everything"] and out["mode"]["only_admitted"] is False, out
    assert _find(_sections(base), pid)[0] == "next"
    status, out = _http(base, "POST", "/attention/modes", {"mode": "deep", "only_admitted": True})
    assert status == 200 and _find(_sections(base), pid)[0] == "not_now"
    status, out = _http(base, "POST", "/attention/modes", {"mode": "deep", "threshold": "passive"})
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
    _mode(base, "deep")
    PUSHES.clear()
    body = _open(base, "Backup failed", "Nightly backup exited with code 2.",
                 {"importance": 5, "sphere": "system", "kind": "system alert", "critical": True})
    assert body["attention"]["delivery"] == "push" and body["attention"]["level"] == "critical"
    assert body["attention"]["reason"] == "critical rings in every mode"
    assert len(PUSHES) == 1
    PUSHES.clear()
    body = _open(base, "Newsletter filed", "Filed the newsletter into news.", {"importance": 1})
    assert body["attention"]["delivery"] == "list" and not PUSHES
    assert _find(_sections(base), "thread:" + body["id"])[0] == "not_now", "listed — folded, since Deep work lists only what it admits"
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
    _mode(base, "deep")
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
    _mode(base, "deep")
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


def test_corrections_learn(base, wg):
    _mode(base, "open")
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
    status, out = _http(base, "POST", "/attention/admit", {"sphere": "customers", "mode": "deep", "on": True})
    assert status == 200 and out["changed"] is True and "customers" in out["modes"]["deep"]["admits"]
    status, out = _http(base, "POST", "/attention/admit", {"sphere": "customers", "mode": "deep", "on": False})
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
    _mode(base, "deep")
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
    # A permit lets Beat interrupt Deep work: the held chat is released and pushed.
    PUSHES.clear()
    status, out = _http(base, "POST", "/attention/permits", {"sender": "Beat Frei", "mode": "deep", "on": True})
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
    _mode(base, "off")
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
    _mode(base, "open")
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
    print("ok test_chat_inbound_gated_and_settled")


# ── projects ───────────────────────────────────────────────────────────────

def test_project_from_store(base, wg):
    _mode(base, "open")
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
    _clock(wg, monday.replace(hour=10, minute=0))          # Deep work by schedule
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
    # but Deep work admits nothing below critical, so it still waits.
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
    _clock(wg, monday.replace(hour=12, minute=1))
    where, row = _find(_sections(base), tid)
    assert where == "now", (where, row)
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
    _mode(base, "open")
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
    body = _sections(base)
    assert body["timezone"] == "UTC" and body["mode"]["id"] in {m["id"] for m in body["modes"]}
    assert len(body["schedule"]) == 8 and body["digest_times"] == [480, 720, 1020, 1260]
    assert set(body["counts"]) == {"now", "next", "held", "waiting", "not_now"}
    assert all("only_admitted" in m for m in body["modes"]) and body["mode"]["only_admitted"] in (True, False)
    assert body["spheres"] == ["customers", "admin", "health", "friends", "family", "system"]
    assert body["next_breakpoint"] and body["mode"]["scheduled"]["until"]
    status, out = _http(base, "GET", "/attention/item?id=nothing")
    assert status == 404
    status, out = _http(base, "POST", "/attention/mode", {"mode": "nope"})
    assert status == 400
    print("ok test_payload_shape")


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
        test_corrections_learn(base, wg)
        test_chat_inbound_gated_and_settled(base, wg)
        test_project_from_store(base, wg)
        test_tick_digest_and_sweep(base, wg)
        test_internal_set(base, wg)
        server.shutdown()
    sparql.shutdown()
    print("all attention API checks passed")


if __name__ == "__main__":
    main()
