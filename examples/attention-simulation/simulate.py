#!/usr/bin/env python3
"""The attention model's day, on the real stack.

Boots the real web-gateway in this process — the attention model, the home
screen, the chat page, the threads — against a mock life store that serves the
example day's message ledger and projects, three mock messenger gateways that
accept the user's sends, and a canned Ara that answers in the threads and the
chat companions the way the dialogues script it. The clock is the story's:
the runner moves it through the day, feeds each arrival through the real rail
and the real /internal/conversations, plays the user's scripted actions
through the real attention API, and lets the gateway's own tick run the
breakpoints and the sweep at the story's minutes. The dashboard in the phone
frame is the real one; the viewer can take over at any time.

    python3 examples/attention-simulation/simulate.py [--port 8766] [--open]

then open http://localhost:8766/simulation.html — the deck (clock, timeline,
narration, system state) beside the phone. `story.py` is the day; the record
of a headless run is `record.py`.

Nothing here reaches a real store, gateway or model; nothing is written
outside a temporary directory.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import types
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(HERE))

import story  # noqa: E402

DAY = story.DAY
TOKEN = "simulation-token"
KB = "https://w3id.org/retinue/kb#"
T_IN, T_OUT = KB + "InboundMessage", KB + "OutboundMessage"
BEAT_HOLD_SECONDS = 3.2
SPEEDS = [(2.0, "×1 — the day in 12 min"), (4.8, "×2 — 5 min"), (12.0, "×5 — 2 min"), (24.0, "×10 — 1 min")]


# ── the clock ───────────────────────────────────────────────────────────────

class Clock:
    """The story's minutes as aware datetimes on today's date, local zone."""

    def __init__(self):
        import attention_store
        self.tz = attention_store.zone()
        today = datetime.now(self.tz).date()
        self.base = datetime(today.year, today.month, today.day, tzinfo=self.tz)
        self.minute = 0.0

    def at(self, minute: float) -> datetime:
        return self.base + timedelta(minutes=minute)

    def now(self) -> datetime:
        return self.at(self.minute)

    def iso(self, minute: float) -> str:
        return self.at(minute).astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    def date(self, minute: float) -> str:
        return self.at(minute).date().isoformat()

    @staticmethod
    def hhmm(minute: float) -> str:
        m = int(round(minute)) % DAY
        return f"{m // 60:02d}:{m % 60:02d}"


# ── the mock life store ─────────────────────────────────────────────────────

class Ledger:
    """What the messenger gateways' ledgers hold at the story's minute: the
    example messages that have arrived, plus the user's sends."""

    def __init__(self, clock: Clock):
        self.clock = clock
        self.sent: list[dict] = []
        self.lock = threading.Lock()

    def records(self) -> list[dict]:
        out = []
        for i, m in enumerate(story.MESSAGES):
            if m["at"] > self.clock.minute:
                continue
            c = story.CONTACTS[m["chat"]]
            out.append({"m": f"urn:retinue:inbound:{c['channel']}:{i}", "chat": c["chat"], "channel": c["channel"],
                        "ts": self.clock.iso(m["at"]), "type": T_IN, "text": m["text"],
                        "sender": _sender_key(m), "mid": f"story-{i}"})
        with self.lock:
            out.extend(self.sent)
        return out

    def add_sent(self, channel: str, key: str, text: str, mid: str, ts: str) -> None:
        with self.lock:
            self.sent.append({"m": f"urn:retinue:outbound:{channel}:{mid}", "chat": key, "channel": channel,
                              "ts": ts, "type": T_OUT, "text": text, "author": "user", "mid": mid})

    def reset(self) -> None:
        with self.lock:
            self.sent = []


def _sender_key(m: dict) -> str:
    c = story.CONTACTS[m["chat"]]
    if c.get("group"):
        return "+4179" + str(abs(hash(m.get("from", ""))) % 10_000_000).zfill(7)
    return c["chat"]


def sender_name(m: dict) -> str:
    return m.get("from") or m["chat"]


class MockStore(BaseHTTPRequestHandler):
    sim: "Simulation"

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        query = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8")).get("query", [""])[0]
        cell = lambda v: {"value": v}  # noqa: E731
        rows = []
        records = self.sim.ledger.records()
        if "VALUES (?chat ?account ?cut)" in query:
            for key, acct, cut in re.findall(r'\("((?:[^"\\]|\\.)*)"\s+"((?:[^"\\]|\\.)*)"\s+"([^"]+)"\^\^', query):
                n = sum(1 for r in records if r["chat"] == key and r["type"] == T_IN and r["ts"] > cut)
                rows.append({"chat": cell(key), "account": cell(acct), "n": cell(str(n))})
        elif "MAX(?ts0)" in query:
            latest: dict[str, dict] = {}
            for r in records:
                if r["chat"] not in latest or r["ts"] > latest[r["chat"]]["ts"]:
                    latest[r["chat"]] = r
            for r in latest.values():
                rows.append(self._row(r, with_chat=True))
        elif "k:Project" in query:
            rows = self._projects()
        elif "k:chat " in query:
            m = re.search(r'k:chat "((?:[^"\\]|\\.)*)"', query)
            key = m.group(1) if m else ""
            before = re.search(r'FILTER\(\?ts < "([^"]+)"', query)
            for r in sorted((r for r in records if r["chat"] == key), key=lambda r: r["ts"], reverse=True):
                if before and r["ts"] >= before.group(1):
                    continue
                rows.append(self._row(r, with_chat=False))
        payload = json.dumps({"results": {"bindings": rows}}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/sparql-results+json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    @staticmethod
    def _row(r: dict, with_chat: bool) -> dict:
        cell = lambda v: {"value": v}  # noqa: E731
        row = {"m": cell(r["m"]), "type": cell(r["type"]), "text": cell(r["text"]), "ts": cell(r["ts"]),
               "mid": cell(r["mid"]), "atts": cell("")}
        if r.get("sender"):
            row["sender"] = cell(r["sender"])
        if r.get("author"):
            row["author"] = cell(r["author"])
        if with_chat:
            row.update({"chat": cell(r["chat"]), "channel": cell(r["channel"]), "account": cell("")})
        return row

    def _projects(self) -> list[dict]:
        cell = lambda v: {"value": v}  # noqa: E731
        clock = self.sim.clock
        rows = []
        for p in story.PROJECTS:
            if p.get("paused") and p["story"] not in self.sim.woken:
                continue
            base = {"p": cell(p["id"]), "title": cell(p["title"]), "actor": cell(self.sim.wg._RETO if p["actor"] == "you" else p["actor"]),
                    "importance": cell(str(p["importance"])), "sphere": cell(p["sphere"]), "kind": cell(p["kind"]),
                    "next": cell(p.get("next", ""))}
            if p.get("expected") is not None:
                base["expected"] = cell(clock.date(p["expected"]))
            if p.get("remind_before"):
                base["remindBefore"] = cell(p["remind_before"])
            if p.get("since") is not None:
                base["since"] = cell(clock.date(p["since"]))
            for tag in p.get("tags") or [None]:
                row = dict(base)
                if tag:
                    row["tag"] = cell(tag)
                rows.append(row)
        return rows


class MockGateway(BaseHTTPRequestHandler):
    """One channel's gateway: reports an inbox account, accepts sends."""
    sim: "Simulation"
    channel = ""
    account = ""

    def log_message(self, fmt, *args):
        pass

    def _json(self, status, body):
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.rstrip("/") in ("", "/health"):
            self._json(200, {"status": "ok", "configured": True, "connected": True,
                             "mode": "inbox", "account": self.account})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        if self.path.rstrip("/") == "/send":
            sim = self.sim
            mid = f"sent-{int(time.time() * 1000)}-{len(sim.ledger.sent)}"
            now = sim.clock.now()
            sim.ledger.add_sent(self.channel, payload.get("recipient", ""), payload.get("message", ""),
                                mid, sim.clock.iso(sim.clock.minute))
            self._json(200, {"status": "sent", "recipient": payload.get("recipient"), "message_id": mid,
                             "ts": now.timestamp(), "attachments": []})
            return
        self._json(404, {"error": "not found"})


def _serve(handler, port: int = 0):
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ── the gateway, booted in-process ─────────────────────────────────────────

def load_gateway(tmp: Path, store_port: int, gateways: dict[str, int], sim_port: int):
    os.environ.update({
        "QLEVER_LIFE_URL": f"http://127.0.0.1:{store_port}",
        "EDGE_PROXY_PEERS": "127.0.0.1",
        "CHAT_STATE_DIR": str(tmp / "chat-state"),
        "CHAT_LIST_CACHE_SECONDS": "0",
        "CONVERSATION_BACKEND_TOKEN": TOKEN,
        "CONVERSATIONS_DIR": str(tmp / "convs"),
        "CONVERSATION_DIR": str(tmp / "convlog"),
        "CHAMBERS_DIR": str(tmp / "chambers"),
        "WEB_GATEWAY_STATE": str(tmp / "state.json"),
        "PUSH_DIR": str(tmp / "push"),
        "ATTENTION_DIR": str(tmp / "attention"),
        "PRESENTATION_LINT": "0",
        "TRANSCRIPT_CLEANUP": "0",
        "WEB_GATEWAY_PORT": str(sim_port),
        "MESSENGER_GATEWAYS": "",
        "CHATS_INGEST_TOKEN": "",
    })
    for channel, port in gateways.items():
        os.environ[f"{channel.upper()}_GATEWAY_BASE_URL"] = f"http://127.0.0.1:{port}"
        os.environ[f"{channel.upper()}_GATEWAY_TOKEN"] = "mock"
    (tmp / "chambers").mkdir(parents=True, exist_ok=True)
    # The gateway renders its own pages with markdown-it; the simulation shows
    # none of them, so a stock Python without the package gets a stand-in.
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
    spec = importlib.util.spec_from_file_location("web_gateway_simulated", SCRIPTS / "web-gateway.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── the runner ──────────────────────────────────────────────────────────────

class Simulation:
    def __init__(self, tmp: Path, port: int):
        self.tmp = tmp
        self.port = port
        self.clock = Clock()
        self.ledger = Ledger(self.clock)
        self.lock = threading.RLock()
        self.speed = SPEEDS[1][0]
        self.playing = False
        self.hold = 0.0
        self.driving = False
        self.ended = False
        self.feed: list[dict] = []
        self.index = 0
        self.event_index = 0
        self.woken: set[str] = set()
        self.threads: dict[str, str] = {}      # story id → conversation id
        self.by_cid: dict[str, str] = {}       # conversation id → story id
        self.stats = {"pushes": 0, "digests": 0, "handled": 0, "corrections": 0, "replies": 0}
        self.beats_done: list[dict] = []
        self.last_view: str | None = None
        self.events = sorted(
            [{"kind": "message", "at": m["at"], "n": i, **m} for i, m in enumerate(story.MESSAGES)]
            + [{"kind": "thread", **x} for x in story.THREADS]
            + [{**p, "kind_label": p["kind"], "kind": "wake", "at": p["wake_at"]} for p in story.PROJECTS if p.get("wake_at") is not None],
            key=lambda e: e["at"])
        self.wg = None
        self.base = f"http://127.0.0.1:{port}"

    # -- boot ------------------------------------------------------------------

    def boot(self):
        MockStore.sim = self
        MockGateway.sim = self
        store = _serve(MockStore)
        gateways = {}
        for channel, account in (("signal", "+41790000001"), ("whatsapp", "+41790000002"), ("telegram", "1000000001")):
            handler = type(f"Mock{channel.title()}", (MockGateway,), {"channel": channel, "account": account, "sim": self})
            gateways[channel] = _serve(handler).server_address[1]
        self.wg = load_gateway(self.tmp, store.server_address[1], gateways, self.port)
        wg = self.wg
        wg.WEBAPP_DIR = REPO / "webapp"
        # The registry keys gateways by service hostname, and the three mocks
        # share one — name them the way the deployment's services are named.
        wg._CHANNEL_GATEWAYS = {
            f"{channel}-gateway": {"base_url": f"http://127.0.0.1:{port}", "token": "mock", "label": channel.title()}
            for channel, port in gateways.items()}
        wg.ATTENTION_CLOCK = self.clock.now
        wg.push_notify.enabled = lambda: True
        wg.push_notify.subscription_count = lambda: 1
        wg.push_notify.notify_async = self._on_push
        wg._start_conv_turn = self._ara_turn
        original_companion = wg._chat_companion

        def companion(chat_id: str):
            cid, created = original_companion(chat_id)
            if created:
                self._companion_opening(chat_id, cid)
            return cid, created
        wg._chat_companion = companion

    def start(self):
        """Serve the gateway with the simulation routes, then set the day up
        (the set-up itself goes through the gateway's API)."""
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), make_handler(self))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.reset()
        threading.Thread(target=self.loop, daemon=True).start()

    # -- state ---------------------------------------------------------------------

    def reset(self):
        """Back to midnight: wipe what the gateway keeps, replay yesterday."""
        with self.lock:
            self.playing = False
            self.hold = 0.0
            self.driving = False
            self.ended = False
            self.feed = []
            self.index = 0
            self.event_index = 0
            self.woken = set()
            self.threads = {}
            self.by_cid = {}
            self.beats_done = []
            self.last_view = None
            self.stats = {"pushes": 0, "digests": 0, "handled": 0, "corrections": 0, "replies": 0}
            self.ledger.reset()
            wg = self.wg
            for d in (wg.CONVERSATIONS_DIR, wg.CHAT_STATE_DIR, wg.ATTENTION_DIR):
                shutil.rmtree(d, ignore_errors=True)
                Path(d).mkdir(parents=True, exist_ok=True)
            wg.CONVERSATION_ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
            wg._CHAT_OVERLAY = wg.chat_state_mod.ChatOverlay(ttl=wg.CHAT_OVERLAY_TTL_SECONDS)
            wg._chats_cache_invalidate()
            wg._attention_tick_state = {"minute": None, "emitted": False}
            profile = wg.attention_policy.default_profile()
            profile["priors"].update(story.PRIORS)
            profile["spheres"].update(story.SPHERES)
            wg._ATTENTION.save_profile(profile)
            wg._ATTENTION.save_focus(wg.attention_policy.default_focus())
            # Yesterday: what is open when the day begins, arriving at its
            # own hour — the model judged it then, and released it since.
            self.quiet = True
            for ev in self.events:
                if ev["at"] >= 0:
                    break
                self.clock.minute = float(ev["at"])
                self._run_event(ev)
                self.event_index += 1
            self.clock.minute = 0.0
            self.quiet = False
            self.feed = []
            self.stats = {"pushes": 0, "digests": 0, "handled": 0, "corrections": 0, "replies": 0}
            self.wg._attention_tick(self.clock.now())

    # -- the API, as the dashboard and the agents use it ------------------------------

    def api(self, method: str, path: str, body=None, agent: bool = False):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"}
        if agent:
            headers["X-Conversation-Backend-Token"] = TOKEN
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                return exc.code, json.loads(raw)
            except ValueError:
                return exc.code, {"raw": raw}

    # -- feed --------------------------------------------------------------------------

    def say(self, who: str, text: str, **extra):
        if self.quiet:
            return
        entry = {"t": self.clock.minute, "who": who, "text": text, **extra}
        self.feed.append(entry)

    def _on_push(self, title, body, url="/", tag=None, mode=None, archived=False, urgency=None, topic=None):
        if topic == "digest":
            self.stats["digests"] += 1
            self.say("push", f"{title}: {body}", urgency=urgency or "normal", digest=True)
        elif mode == "reply" and urgency is None:
            self.stats["replies"] += 1
            self.say("reply", f"Ara replied in “{title}” (pushed as a reply).")
        else:
            self.stats["pushes"] += 1
            self.say("push", f"{title} — {body}", urgency=urgency or "high")

    # -- ids -------------------------------------------------------------------------------

    def item_id(self, story_id: str) -> str | None:
        if story_id.startswith("chat:"):
            return "chat:" + story.chat_id(story_id[5:])
        if story_id.startswith("prj-"):
            p = next((p for p in story.PROJECTS if p["story"] == story_id), None)
            return p["id"] if p else None
        cid = self.threads.get(story_id)
        return f"thread:{cid}" if cid else None

    def _item(self, item_id: str) -> dict | None:
        status, body = self.api("GET", "/attention/item?" + urllib.parse.urlencode({"id": item_id}))
        return body["item"] if status == 200 else None

    def _href(self, story_id: str) -> str | None:
        if story_id.startswith("chat:"):
            return "/chat.html?" + urllib.parse.urlencode({"id": story.chat_id(story_id[5:])})
        cid = self.threads.get(story_id)
        return f"/#conversation-{cid}" if cid else None

    # -- arrivals --------------------------------------------------------------------------

    def _run_event(self, ev: dict):
        wg = self.wg
        if ev["kind"] == "message":
            c = story.CONTACTS[ev["chat"]]
            attention = None
            if ev.get("triage"):
                tr = ev["triage"]
                attention = {k: v for k, v in tr.items() if k != "due"}
                if tr.get("due") is not None:
                    attention["due"] = self.clock.at(tr["due"]).isoformat()
                attention["sphere"] = c["sphere"]
            payload = {"direction": "in", "channel": c["channel"], "chat": c["chat"], "sender": _sender_key(ev),
                       "sender_name": sender_name(ev), "text": ev["text"], "ts": self.clock.iso(ev["at"]),
                       "message_id": f"story-{ev['n']}", "group": bool(c.get("group")),
                       "gate": {"forward": True, "class": "whitelisted"}}
            if c.get("group"):
                payload["chat_name"] = ev["chat"]
            if attention:
                payload["attention"] = attention
            status, body = self.api("POST", "/internal/chats/inbound", payload)
            item = self._item("chat:" + story.chat_id(ev["chat"]))
            if item:
                self.say("system", f"{ev['chat']} ({c['channel']}): “{ev['text'][:60]}” → {item['level']}; {item['delivery']}")
        elif ev["kind"] == "thread":
            dlg = story.THREAD_DIALOGUES[ev["id"]]
            self._open_agent_thread(ev["id"], ev["title"], dlg, ev["agent"], ev["attention"])
        elif ev["kind"] == "wake":
            self.woken.add(ev["story"])
            dlg = story.THREAD_DIALOGUES[ev["story"]]
            attention = {"importance": ev["importance"], "sphere": ev["sphere"], "tags": ev.get("tags") or [],
                         "kind": ev["kind_label"], "lead": ev.get("remind_before"), "due": ev["expected"]}
            self._open_agent_thread(ev["story"], f"Due: {ev['title']}", dlg, None, attention)
            wg._chats_cache_invalidate()

    def _open_agent_thread(self, story_id: str, title: str, dlg: dict, agent: str | None, attention: dict):
        spec = {k: v for k, v in attention.items() if k not in ("due",) and v is not None}
        if attention.get("due") is not None:
            spec["due"] = self.clock.at(attention["due"]).isoformat()
        message = dlg["opening"] + "\n\n" + story.chips_markup(dlg["chips"])
        payload = {"title": title, "message": message, "key": f"story:{story_id}", "attention": spec}
        if agent:
            payload["agent"] = agent
        status, body = self.api("POST", "/internal/conversations", payload, agent=True)
        if status != 201:
            self.say("system", f"could not open {title}: {status} {body}")
            return
        self.threads[story_id] = body["id"]
        self.by_cid[body["id"]] = story_id
        d = body.get("attention") or {}
        until = f" until {Clock.hhmm(self._minute_of(d['until']))}" if d.get("until") else ""
        self.say("system", f"{title} → {d.get('level', '?')}; {d.get('delivery', '?')}{until} — {d.get('reason', '')}")

    def _minute_of(self, iso: str) -> float:
        dt = datetime.fromisoformat(iso)
        return (dt - self.clock.base).total_seconds() / 60

    # -- Ara, canned ---------------------------------------------------------------------------

    def _dialogue_for(self, conv: dict) -> tuple[dict | None, dict | None]:
        """(dialogue, chat message) for a thread — by its story id, or, for a
        companion, by the chat's sender and the stage of their last message."""
        story_id = self.by_cid.get(conv["id"])
        if story_id:
            return story.THREAD_DIALOGUES.get(story_id), None
        chat = conv.get("chat")
        if conv.get("kind") == "companion" and chat:
            name = next((n for n in story.CONTACTS if story.chat_id(n) == chat), None)
            if not name:
                return None, None
            arrived = [m for m in story.MESSAGES if m["chat"] == name and m["at"] <= self.clock.minute]
            last = arrived[-1] if arrived else None
            stage = (last or {}).get("stage") or "base"
            stages = story.COMPANION_DIALOGUES.get(name) or {}
            return stages.get(stage) or stages.get("base"), last
        return None, None

    def _fill(self, text: str, last: dict | None) -> str:
        return (text.replace("{time}", Clock.hhmm(self.clock.minute))
                .replace("{last}", (last or {}).get("text", ""))
                .replace("{arrived}", Clock.hhmm((last or {}).get("at", self.clock.minute))))

    def _companion_opening(self, chat_id: str, cid: str):
        conv = self.wg._load_conv(cid) or {"id": cid, "chat": chat_id, "kind": "companion"}
        dlg, last = self._dialogue_for(conv)
        if not dlg:
            return
        text = self._fill(dlg["opening"], last) + "\n\n" + story.chips_markup(dlg.get("chips") or [])
        self.wg._conv_add_message(cid, "assistant", text, unread=False, pending=False)

    def _ara_turn(self, cid: str):
        """Ara's canned turn: the dialogue's reply to the user's last words,
        its effects applied the way her real turn would apply them."""
        wg = self.wg
        conv = wg._load_conv(cid)
        if conv is None:
            return
        user_text = next((m["text"] for m in reversed(conv.get("messages") or []) if m.get("role") == "user"), "")
        dlg, last = self._dialogue_for(conv)
        if dlg is None and conv.get("project"):
            sid = next((p["story"] for p in story.PROJECTS if p["id"] == conv["project"]), None)
            dlg = story.THREAD_DIALOGUES.get(sid) if sid else None
            if sid and sid not in self.threads:
                self.threads[sid] = cid
                self.by_cid[cid] = sid
            if dlg and len([m for m in conv["messages"] if m.get("role") == "user"]) == 1:
                reply = {"text": dlg["opening"], "chips": dlg["chips"]}
            else:
                reply = (dlg or {}).get("replies", {}).get(user_text) or (dlg or {}).get("free") or {"text": "Noted."}
        elif dlg is None:
            reply = {"text": "Noted — I will take care of it.", "chips": []}
        else:
            reply = dlg.get("replies", {}).get(user_text) or dlg.get("free") or {"text": "Noted."}
        text = self._fill(reply["text"], last)
        if reply.get("chips"):
            text += "\n\n" + story.chips_markup(reply["chips"])
        conv = wg._conv_add_message(cid, "assistant", text, unread=True, pending=False)
        self.say("ara", self._fill(reply["text"], last)[:140])
        story_id = self.by_cid.get(cid)
        item_id = self.item_id(story_id) if story_id else None
        chat = conv.get("chat")
        if reply.get("done"):
            if item_id:
                wg._attention_mark(item_id, "done", "ara")
                # A project's own thread is the user's; the project is the item.
                if not item_id.startswith("thread:"):
                    wg._attention_mark(f"thread:{cid}", "done", "ara")
            elif chat:
                wg._attention_mark(f"chat:{chat}", "done", "ara")
            self.stats["handled"] += 1
        if reply.get("later"):
            target = item_id or (f"chat:{chat}" if chat else None)
            if target:
                self.api("POST", "/attention/items/later", {"id": target, "when": reply["later"]})
        if reply.get("wait_on") and item_id:
            self.api("POST", "/internal/attention/set", {"id": item_id, "actor": reply["wait_on"]}, agent=True)
        if reply.get("draft") and chat:
            wg._CHAT_STATE.set_draft(chat, reply["draft"], author="agent", agent="Ara")
            wg._chats_cache_invalidate()
        if conv is not None:
            wg._push_conv_notification(conv, text)

    # -- the user's scripted actions ------------------------------------------------------------

    def apply(self, a: dict) -> tuple[bool, str | None]:
        """Run one scripted action through the real API; (ok, view)."""
        kind = a["type"]
        if kind == "mode":
            status, body = self.api("POST", "/attention/mode", {"mode": a["id"]})
            mode = (body.get("mode") or {}) if status == 200 else {}
            self.say("system", f"Mode: {mode.get('name', '?')}{' by hand' if mode.get('manual') else ' — following the schedule'}.")
            return status == 200, "/"
        if kind == "permit":
            status, body = self.api("POST", "/attention/permits", {"sender": a["sender"], "mode": a["mode"], "on": a["on"]})
            if status == 200:
                self.stats["corrections"] += 1
                for entry in body.get("learned") or []:
                    self.say("learn", entry.get("text", ""))
                cid_chat = "chat:" + story.chat_id(a["sender"])
                return True, "/?" + urllib.parse.urlencode({"item": cid_chat})
            return False, None
        item_id = self.item_id(a["id"])
        item = self._item(item_id) if item_id else None
        if item is None or item["state"] != "open":
            return False, None
        if kind == "pull":
            if item["released"]:
                return False, None
            status, _ = self.api("POST", "/attention/items/pull", {"id": item_id})
            return status == 200, "/?" + urllib.parse.urlencode({"item": item_id})
        if kind == "later":
            status, _ = self.api("POST", "/attention/items/later", {"id": item_id, "when": a.get("when", "next")})
            return status == 200, "/?" + urllib.parse.urlencode({"item": item_id})
        if kind == "doIt":
            status, _ = self.api("POST", "/attention/items/done", {"id": item_id})
            if status == 200:
                self.stats["handled"] += 1
            return status == 200, "/?" + urllib.parse.urlencode({"item": item_id})
        if kind == "lead":
            status, body = self.api("POST", "/attention/items/correct", {"id": item_id, "lead": a["lead"]})
            if status == 200:
                self.stats["corrections"] += 1
                for line in body.get("learned_now") or []:
                    self.say("learn", line)
                eff = body.get("effect")
                if eff:
                    self.say("system", f"{item['title']}: {eff.get('reason', eff.get('type'))}")
            return status == 200, "/?" + urllib.parse.urlencode({"item": item_id})
        if kind == "reply":
            chat = story.chat_id(a["id"][5:])
            status, body = self.api("POST", f"/chats/{urllib.parse.quote(chat, safe='')}/send", {"text": a["text"]})
            if status == 200:
                self.stats["handled"] += 1
                self.say("you", f"You reply to {a['id'][5:]}: “{a['text']}”")
            else:
                self.say("system", f"send failed: {status} {body}")
            return status == 200, self._href(a["id"])
        if kind in ("chip", "say"):
            text = a.get("label") or a.get("text")
            cid = self.threads.get(a["id"])
            if a["id"].startswith("prj-") and cid is None:
                p = next(p for p in story.PROJECTS if p["story"] == a["id"])
                status, body = self.api("POST", "/conversations", {"message": text, "project": p["id"], "project_title": p["title"]})
                if status != 201:
                    return False, None
                cid = body["id"]
                self.threads[a["id"]] = cid
                self.by_cid[cid] = a["id"]
            elif cid is None:
                return False, None
            else:
                status, body = self.api("POST", f"/conversations/{cid}/messages", {"message": text})
                if status != 200:
                    return False, None
            self.say("you", f"You: “{text}”")
            self.api("POST", f"/conversations/{cid}/read", {})
            return True, f"/#conversation-{cid}"
        return False, None

    # -- time ------------------------------------------------------------------------------------

    def advance_to(self, target: float, holds: bool) -> None:
        """Move the clock to `target`, never skipping a whole minute (the
        gateway's tick runs on each), running what arrives and what you do at
        its minute — the tick first, then arrivals, then your beats. With
        `holds`, stop after each beat so the viewer can read it."""
        target = min(float(target), float(DAY))
        eps = 1e-6
        with self.lock:
            while True:
                ev = self.events[self.event_index] if self.event_index < len(self.events) else None
                bt = story.SCRIPT[self.index] if self.index < len(story.SCRIPT) else None
                minute = self.clock.minute
                if ev is not None and ev["at"] <= minute + eps:
                    self._run_event(ev)
                    self.event_index += 1
                    continue
                if bt is not None and bt["at"] <= minute + eps:
                    self._run_beat(bt)
                    self.index += 1
                    if holds:
                        self.hold = BEAT_HOLD_SECONDS
                        return
                    continue
                if minute >= target - eps:
                    break
                candidates = [target, float(int(minute) + 1)]
                if ev is not None:
                    candidates.append(float(ev["at"]))
                if bt is not None:
                    candidates.append(float(bt["at"]))
                nxt = min(c for c in candidates if c > minute + eps)
                self.clock.minute = nxt
                if abs(nxt - round(nxt)) < eps:
                    self.wg._attention_tick(self.clock.now())
            if self.clock.minute >= DAY - eps:
                self.ended = True
                self.playing = False

    def _run_beat(self, b: dict) -> None:
        view = None
        if b.get("action"):
            ok, view = self.apply(b["action"])
            if not ok:
                self.say("narrator", f"({b['text'].split('.')[0]} — skipped: you already handled this yourself.)", skipped=True)
                self.beats_done.append({"at": b["at"], "skipped": True})
                return
        self.say(b["who"], b["text"], summary=bool(b.get("summary")), beat=True, view=view)
        self.beats_done.append({"at": b["at"], "view": view})
        self.last_view = view

    # -- controls ---------------------------------------------------------------------------------

    def play(self):
        with self.lock:
            if self.ended:
                return
            self.playing = True
            self.driving = False

    def pause(self, driving: bool = False):
        with self.lock:
            self.playing = False
            if driving:
                self.driving = True

    def step(self):
        with self.lock:
            self.driving = False
            nxt = story.SCRIPT[self.index] if self.index < len(story.SCRIPT) else None
            self.advance_to(nxt["at"] if nxt else DAY, holds=True)

    def seek(self, minute: float):
        was_playing = self.playing
        self.reset()
        self.advance_to(max(0.0, min(float(minute), float(DAY))), holds=False)
        if was_playing and not self.ended:
            self.play()

    def loop(self):
        last = time.monotonic()
        while True:
            time.sleep(0.1)
            now = time.monotonic()
            dt, last = now - last, now
            if not self.playing:
                continue
            if self.hold > 0:
                self.hold -= dt
                continue
            self.advance_to(self.clock.minute + dt * self.speed, holds=True)

    # -- what the deck shows ----------------------------------------------------------------------

    def snapshot(self) -> dict:
        status, att = self.api("GET", "/attention")
        att = att if status == 200 else {}
        with self.lock:
            focus = self.wg._ATTENTION.focus()
            profile = self.wg._ATTENTION.profile()
            held = len((att.get("sections") or {}).get("held") or [])
            return {
                "minute": self.clock.minute, "time": Clock.hhmm(self.clock.minute), "date": self.clock.base.date().isoformat(),
                "playing": self.playing, "speed": self.speed, "speeds": SPEEDS, "hold": max(0.0, self.hold),
                "driving": self.driving, "ended": self.ended, "index": self.index,
                "beats": [{"at": b["at"], "who": b["who"], "action": bool(b.get("action"))} for b in story.SCRIPT],
                "feed": self.feed[-400:], "stats": {**self.stats, "held": held},
                "attention": {"mode": att.get("mode"), "next_breakpoint": att.get("next_breakpoint"),
                              "counts": att.get("counts"), "learned": att.get("learned"),
                              "schedule": focus["schedule"], "digest_times": focus["digest_times"],
                              "modes": {m["id"]: {"name": m["name"], "admits": m["admits"], "admit_tags": m.get("admit_tags", []), "threshold": m["threshold"]} for m in focus["modes"].values()},
                              "permits": profile.get("permits"), "priors": profile.get("priors"), "leads": profile.get("leads")},
                "last_view": self.last_view,
            }


# ── the server: the gateway plus the simulation routes ─────────────────────

def make_handler(sim: Simulation):
    Base = sim.wg.Handler

    class SimHandler(Base):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/simulation":
                self._send_json(200, sim.snapshot())
                return
            if path in ("/simulation.html", "/deck.js", "/deck.css"):
                if not self._serve_static_file(HERE / path.lstrip("/"), HERE):
                    self._send_json(404, {"error": "not found"})
                return
            super().do_GET()

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if path.startswith("/simulation/"):
                cmd = path[len("/simulation/"):].strip("/")
                payload = self._read_json_body() or {}
                if cmd == "play":
                    sim.play()
                elif cmd == "pause":
                    sim.pause(driving=bool(payload.get("driving")))
                elif cmd == "acted":
                    sim.pause(driving=True)
                elif cmd == "resume":
                    sim.play()
                elif cmd == "step":
                    sim.step()
                elif cmd == "restart":
                    sim.seek(0)
                elif cmd == "seek":
                    sim.seek(float(payload.get("minute", 0)))
                elif cmd == "speed":
                    sim.speed = float(payload.get("speed", sim.speed))
                else:
                    self._send_json(404, {"error": "unknown command"})
                    return
                self._send_json(200, sim.snapshot())
                return
            super().do_POST()

    return SimHandler


def main() -> int:
    ap = argparse.ArgumentParser(description="The attention model's day on the real gateway.")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--open", action="store_true", help="open the deck in the browser")
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="attention-simulation-"))
    sim = Simulation(tmp, args.port)
    sim.boot()
    sim.start()
    url = f"http://localhost:{args.port}/simulation.html"
    print(f"[simulation] the day is at {url} (dashboard: http://localhost:{args.port}/)", flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
