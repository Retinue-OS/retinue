#!/usr/bin/env python3
"""The gate for mail across the seam: the REAL mail gateway posting to the
REAL web gateway (docs/mail-chats.md, "The gate"; docs/dashboard.md, "The
gate for mail").

tests/test_mail_gateway_gate.py runs the mail gateway against a fake rail,
and tests/test_chat_mail_gate.py runs the web gateway against hand-made rail
events. Each is right about its own side; a defect that lives between the
two — a field one side sends that the other does not read, a token one
presents that the other does not take, an answer one gives in a shape the
other misreads — passes both. Here there is no stand-in for either half:
scripts/mail-gateway.py's Mirror over the tests' fake mailbox, with its own
_rail_post/_rail_offer and its HTTP handler, posting over HTTP to
scripts/web-gateway.py's handler (the loader of tests/test_chat_mail.py,
the model turn stood in for), which calls the mail gateway back to archive.
The SPARQL mock answers from the ledger the mail gateway writes, and the
job a turn runs under is polled by the mail gateway's real job_delivery
(only faster). Both sides' state is asserted:

- an ordinary sender's mail: listed, nothing pushed, delivered in the ledger;
- a VIP's: its turn runs, it rings, delivered once the job ends;
- an instructed sender's: verdict ring, verdict quiet, a failing turn
  (retried up to MAIL_ARRIVAL_TURN_ATTEMPTS times, then accepted, rung
  once), and a turn past MAIL_INSTRUCTION_TIMEOUT (shortened here);
- a mail with two From addresses, and one with only a Sender header naming
  the VIP: nobody's — listed, no turn, no ring;
- a rail event without a token, or with a wrong one: 403 and nothing
  recorded, with CHATS_INGEST_TOKEN unset and set (and the mail gateway's
  own offers taken with the token it presents in each case);
- the web gateway away, then back: the mail is offered again and rings once;
- an offer repeated after its answer was lost: no second ring, no second turn;
- the instructed turn archiving its chat (scripts/chat-flags.py → the
  internal flags endpoint → the mail gateway's /chats/archive) with exactly
  the turn's mails, the fake mailbox showing the move — and the same while
  a newer mail arrived meanwhile: the chat stays, only the turn's mail moves;
- a news-flagged list's mail goes to the feed and makes no chat; a VIP's
  mail in that list gets its chat and its turn, is still filed, and stays in
  the INBOX;
- a mail the web gateway answers 5xx for, for ever — one that arrived, and
  the user's own sent mail — then a VIP's reply in the same thread: the
  failing mail steps aside after RAIL_HOLD_ATTEMPTS tries, the reply is
  offered, rings once and gets its turn, and /health counts the one that
  keeps failing until it is taken.

Standalone:

    python3 tests/test_mail_gate_e2e.py
"""
from __future__ import annotations

import email
import functools
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import types
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(SCRIPTS))

import test_chat_mail as mail  # noqa: E402  (the web gateway's loader, the SPARQL mock)

_spec = importlib.util.spec_from_file_location("mail_gateway_helpers_e2e",
                                               HERE / "test_mail_gateway.py")
t = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(t)

AGENT = {"X-Conversation-Backend-Token": "agent-token"}
PUSHES: list = []
TURNS: list = []
TURN_GATE: dict = {"during": None, "hold": None, "fail": False}


def _stub_send_message(prompt, display_question=None, session_key=None, **_kw):
    """The model turn: records its prompt, runs what the test has it do."""
    TURNS.append({"prompt": prompt, "session": session_key})
    during = TURN_GATE.get("during")
    if during is not None:
        during(prompt)
    hold = TURN_GATE.get("hold")
    if hold is not None:
        hold.wait(30)
    if TURN_GATE.get("fail"):
        return {"error": "upstream is down"}
    return {"response": "Read it; nothing to add.", "model_name": "stub-model", "cost_usd": 0.0}


def _wait(pred, what, timeout=20.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return
        time.sleep(0.02)
    raise AssertionError("timed out: " + what)


class World:
    """One mail gateway (module, Mirror, fake mailbox, HTTP handler) and one
    web gateway, each behind its real HTTP handler, wired as compose wires
    them: one MAIL_GATEWAY_TOKEN on both sides."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.sparql = mail._serve(mail._MockSparql)
        # The retinue side reads the ledger the mail gateway writes.
        self.ledger = tmp / "chambers" / "_generated" / "messenger" / "email"
        self.ledger.mkdir(parents=True)
        mail.STATE["ledger"] = self.ledger
        for name in ("ATTENTION_PUSH_GATE", "CHAT_ARRIVAL_TURNS", "TRIAGE_MESSENGER_DIR",
                     "TRIAGE_EMAIL_WHITELIST_PATH"):
            os.environ.pop(name, None)
        os.environ["MAILBOX_RETRY_SECONDS"] = "0"
        # Both halves read the policy files under CHAMBERS_DIR, which the
        # web gateway's loader sets: triage_policy and contacts cache it.
        for name in ("triage_policy", "contacts"):
            sys.modules.pop(name, None)
        wg = self.wg = mail._load_gateway(tmp, self.sparql.server_address[1])
        wg.MAIL_GATEWAY_TOKEN = t.TOKEN
        wg.push_notify.enabled = lambda: True
        wg.push_notify.notify_async = lambda *a, **k: PUSHES.append((a, k))
        wg.send_message = _stub_send_message
        wg.contacts_mod.commit = lambda *a, **k: None
        wg.ATTENTION_PUSH_GATE = True
        self.wg_port = 0
        self.start_wg()
        gw = self.gw = t.load(tmp / "gw", INBOUND_STORE_DIR=str(self.ledger))
        assert gw.RAIL_TOKEN == t.TOKEN, "the mail gateway presents its own token"
        assert gw.POLICY_PATH.parent == wg.triage_policy.email_whitelist_path().parent
        gw._chats.CHATS_INGEST_URL = self.base + "/internal/chats/inbound"
        # The feed stays tests/test_mail_gateway.py's stand-in (gw.filed).
        # The real job poller, polling often.
        gw._jobs = types.SimpleNamespace(confirm_delivery=functools.partial(
            gw._jobs.confirm_delivery, interval=0.05, backoff=1.0))
        self.box = t.FakeMailbox(gw)
        self.offset = 0.0
        self.mirror = gw.Mirror(self.box, clock=lambda: time.time() + self.offset)
        assert self.mirror.poll() is False
        gw.MIRROR = self.mirror
        # What the web gateway asked the mailbox to archive, as the mail
        # gateway's handler received it.
        self.archived: list = []
        real_archive = self.mirror.archive

        def archive(chat, only=None):
            self.archived.append((chat, None if only is None else set(only)))
            return real_archive(chat, only)
        self.mirror.archive = archive
        self.gw_server, self.gw_base = t.serve(gw)
        wg._CHANNEL_GATEWAYS = {mail.MAIL_SLUG: {
            "base_url": self.gw_base, "token": t.TOKEN, "label": "E-mail", "channel": "email"}}
        wg._gw_identity.clear()
        status, out = mail._http(self.base, "POST", "/attention/mode", {"mode": "focused"})
        assert status == 200, out

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.wg_port}"

    def start_wg(self) -> None:
        ThreadingHTTPServer.allow_reuse_address = True
        self.wg_server = ThreadingHTTPServer(("127.0.0.1", self.wg_port), self.wg.Handler)
        self.wg_port = self.wg_server.server_address[1]
        threading.Thread(target=self.wg_server.serve_forever, daemon=True).start()

    def stop_wg(self) -> None:
        self.wg_server.shutdown()
        self.wg_server.server_close()

    def close(self) -> None:
        self.wg._mailbox_wait()
        self.stop_wg()
        self.gw_server.shutdown()
        self.sparql.shutdown()

    # -- the mailbox -------------------------------------------------------

    def arrives(self, mid, raw=None, **kw) -> None:
        """A mail lands in the INBOX (dated now: an arrival, not old mail)
        and the mirror polls."""
        kw.setdefault("day", (time.time() - 5 - t.T0) / t.DAY)
        self.box.put("INBOX", raw if raw is not None else t.msg(mid, **kw))
        self.mirror.poll()

    def later(self, seconds=1000.0) -> None:
        """The mirror's clock moves on past every wait, and it polls."""
        self.offset += seconds
        self.mirror.poll()

    def rec(self, mid) -> dict:
        return t.by_id(self.gw).get(mid) or {}

    def delivered(self, mid) -> bool:
        return self.rec(mid).get("delivered") is True

    def owed(self, mid) -> bool:
        return self.mirror.index.one("SELECT 1 FROM offers WHERE message_id = ?",
                                     (mid,)) is not None

    def in_folder(self, folder, mid) -> bool:
        return self.box.uid_of(folder, mid) is not None

    # -- the web gateway ---------------------------------------------------

    def chat(self, key) -> str:
        return mail.cs.make_chat_id("email", key, t.ACCOUNT)

    def doc(self, key) -> dict:
        return self.wg._CHAT_STATE.get(self.chat(key))

    def exists(self, key) -> bool:
        return self.wg._CHAT_STATE.exists(self.chat(key))

    def listed(self, key) -> bool:
        return self.chat(key) in mail._chats(self.base)

    def person(self, name, *emails, **facts) -> dict:
        record = self.wg._CONTACTS.create("private", name, [("email", e) for e in emails],
                                          **facts)
        self.wg._contacts_sync_policy()
        return record

    def verdict(self, key, importance):
        def _set(_prompt):
            status, body = mail._http(
                self.base, "POST", f"/internal/chats/{mail._quote(self.chat(key))}/verdict",
                {"importance": importance}, headers=AGENT)
            assert status == 200, body
        return _set

    def script(self, name, *args) -> tuple[int, str]:
        """One of the scripts a turn is told to run, as it would run it."""
        env = dict(os.environ, CHAT_DRAFT_BACKEND_URL=self.base + "/internal/chats",
                   CONVERSATION_BACKEND_TOKEN="agent-token")
        proc = subprocess.run([sys.executable, str(SCRIPTS / name), *args], env=env,
                              capture_output=True, text=True, timeout=30)
        return proc.returncode, proc.stdout + proc.stderr


def _reset():
    PUSHES.clear()
    TURNS.clear()
    TURN_GATE.update(during=None, hold=None, fail=False)


# ── Who the gate takes it for ─────────────────────────────────────────────────

def test_an_ordinary_mail(w: World):
    _reset()
    w.arrives("plain1@x.org", sender="Ann <ann@x.org>", subject="Hello")
    rec = w.rec("plain1@x.org")
    assert rec["delivered"] is True and rec["gate_sender"] == "ann@x.org", rec
    assert not w.owed("plain1@x.org")
    assert w.listed("plain1@x.org") and w.exists("plain1@x.org")
    assert not PUSHES and not TURNS
    print("PASS test_an_ordinary_mail")


def test_a_vips_mail(w: World):
    _reset()
    TURN_GATE["hold"] = hold = threading.Event()
    # The address as the sender's client wrote it: the gate sender is its
    # ASCII lowercase, on both sides.
    w.arrives("vip1@corp.example", sender="Boss <Boss@Corp.Example>", subject="Budget",
              files=[("Plan.pdf", "application/pdf", b"%PDF-1.7 x")])
    assert w.rec("vip1@corp.example")["gate_sender"] == "boss@corp.example"
    # Rung on arrival; worked; delivered only when the turn's job is done.
    assert len(PUSHES) == 1 and PUSHES[0][0][0] == "The Boss", PUSHES
    _wait(lambda: len(TURNS) == 1, "the VIP's turn")
    assert not w.delivered("vip1@corp.example") and w.owed("vip1@corp.example")
    prompt = TURNS[0]["prompt"]
    assert "marked its sender a VIP" in prompt and "From: Boss <" in prompt
    assert re.search(r"message-files/[0-9a-f]{32}\.pdf", prompt), "the file was not handed over"
    hold.set()
    _wait(lambda: w.delivered("vip1@corp.example"), "the VIP's mail delivered")
    assert not w.owed("vip1@corp.example") and len(PUSHES) == 1 and len(TURNS) == 1
    print("PASS test_a_vips_mail")


def test_an_instructed_senders_verdicts(w: World):
    # Verdict 5: rings, once the turn is done.
    _reset()
    TURN_GATE["during"] = w.verdict("i1@firm.example", 5)
    w.arrives("i1@firm.example", sender="Clerk <clerk@firm.example>", subject="Urgent")
    assert not PUSHES, "the ring waits for the verdict"
    _wait(lambda: w.delivered("i1@firm.example"), "the instructed mail delivered")
    _wait(lambda: len(PUSHES) == 1, "the verdict's ring")
    assert w.doc("i1@firm.example")["instructed"]["delivery"] == "rang"
    # Verdict 0: listed, never rung.
    _reset()
    TURN_GATE["during"] = w.verdict("i2@firm.example", 0)
    w.arrives("i2@firm.example", sender="Clerk <clerk@firm.example>", subject="FYI")
    _wait(lambda: w.delivered("i2@firm.example"), "the quiet mail delivered")
    assert len(TURNS) == 1 and not PUSHES, PUSHES
    assert w.doc("i2@firm.example")["instructed"]["delivery"] != "rang"
    assert w.listed("i2@firm.example")
    print("PASS test_an_instructed_senders_verdicts")


def test_a_failing_turn_is_retried_then_accepted(w: World):
    _reset()
    TURN_GATE["fail"] = True
    retry, w.wg.CHAT_ARRIVAL_RETRY_SECONDS = w.wg.CHAT_ARRIVAL_RETRY_SECONDS, 0.0
    attempts = w.wg.MAIL_ARRIVAL_TURN_ATTEMPTS
    try:
        w.arrives("i3@firm.example", sender="Clerk <clerk@firm.example>", subject="Fail")
        for n in range(1, attempts):
            _wait(lambda: len(TURNS) == n, f"turn {n}")
            # The failed job leaves the mail owed at the mail gateway.
            _wait(lambda: not any(j.is_alive() for j in w.mirror._jobs.values()), "job ended")
            assert not w.delivered("i3@firm.example") and w.owed("i3@firm.example")
            assert len(PUSHES) == 1, f"rung {len(PUSHES)} times by attempt {n}"
            w.later()
        # The last attempt's job reports the turns given up: the mail is
        # accepted as it stands, and offered no more.
        _wait(lambda: w.delivered("i3@firm.example"), "accepted after the last attempt")
        assert not w.owed("i3@firm.example")
        w.later()
    finally:
        TURN_GATE["fail"] = False
        w.wg.CHAT_ARRIVAL_RETRY_SECONDS = retry
    assert len(TURNS) == attempts and len(PUSHES) == 1, (len(TURNS), PUSHES)
    assert "An earlier turn for this mail did not finish" in TURNS[-1]["prompt"]
    conv = w.wg._load_conv(w.doc("i3@firm.example")["companion"])
    notes = [m["text"] for m in conv["messages"] if m.get("role") == "assistant"]
    assert len(notes) == 1 and notes[0].startswith(f"I tried {attempts} times"), notes
    print("PASS test_a_failing_turn_is_retried_then_accepted")


def test_a_turn_past_the_mail_timeout(w: World):
    _reset()
    timeout, w.wg.MAIL_INSTRUCTION_TIMEOUT = w.wg.MAIL_INSTRUCTION_TIMEOUT, 1.5
    assert w.wg.CHAT_INSTRUCTION_TIMEOUT >= 30, "the messengers' bound would ring first"
    TURN_GATE["hold"] = hold = threading.Event()
    try:
        w.arrives("i4@firm.example", sender="Clerk <clerk@firm.example>", subject="Slow")
        _wait(lambda: len(TURNS) == 1, "the slow turn")
        assert "runs past 1.5 seconds" in TURNS[0]["prompt"], "the turn is told its bound"
        time.sleep(0.5)
        assert not PUSHES, "rung before the bound"
        _wait(lambda: len(PUSHES) == 1, "the timeout's ring", 10)
        assert not w.delivered("i4@firm.example"), "delivered before the turn ended"
    finally:
        hold.set()
        w.wg.MAIL_INSTRUCTION_TIMEOUT = timeout
    _wait(lambda: w.delivered("i4@firm.example"), "delivered when the turn ended")
    assert len(PUSHES) == 1 and len(TURNS) == 1
    print("PASS test_a_turn_past_the_mail_timeout")


def _without_from(raw: bytes, sender: str) -> bytes:
    parsed = email.message_from_bytes(raw)
    del parsed["From"]
    parsed["Sender"] = sender
    return parsed.as_bytes()


def test_a_mail_from_nobody(w: World):
    _reset()
    # Two addresses in its From: which of them is it from?
    w.arrives("two@corp.example", sender="Boss <boss@corp.example>, Ann <ann@x.org>",
              subject="Both of us")
    # Only a Sender naming the VIP: the Sender is for display.
    w.arrives("sndr@corp.example",
              raw=_without_from(t.msg("sndr@corp.example", sender="x@x.org", subject="On behalf",
                                      day=(time.time() - 5 - t.T0) / t.DAY),
                                "Boss <boss@corp.example>"))
    # Two From headers, the first naming the VIP.
    w.arrives("twice@corp.example",
              raw=b"From: Boss <boss@corp.example>\n" + t.msg(
                  "twice@corp.example", sender="Ann <ann@x.org>", subject="Twice",
                  day=(time.time() - 5 - t.T0) / t.DAY))
    for mid in ("two@corp.example", "sndr@corp.example", "twice@corp.example"):
        rec = w.rec(mid)
        assert rec and not rec.get("gate_sender"), (mid, rec)
        assert rec["delivered"] is True, (mid, rec)
        assert w.listed(mid), mid
        block = w.doc(mid).get("attention") or {}
        assert not block.get("vip"), (mid, block)
        # Nor is the thread filed under the VIP's card by the address it is
        # shown as from — on its arrival, or by the sweep.
        assert not w.doc(mid).get("contact"), (mid, w.doc(mid))
    assert w.wg._chats_autolink() == 0
    assert not TURNS and not PUSHES, (TURNS, PUSHES)
    # A mail the gate takes for the VIP, in one of those threads: now the
    # card is the thread's.
    w.arrives("sndr-re@corp.example", sender="Boss <boss@corp.example>", subject="Re: On behalf",
              irt="sndr@corp.example", refs=["sndr@corp.example"])
    assert (w.doc("sndr@corp.example").get("contact") or {}).get("name") == "The Boss"
    _wait(lambda: len(PUSHES) == 1, "the VIP's ring")
    _wait(lambda: len(TURNS) == 1, "the VIP's turn")
    print("PASS test_a_mail_from_nobody")


# ── The rail is a way in ──────────────────────────────────────────────────────

def _event(key, sender="boss@corp.example"):
    return {"channel": "email", "account": t.ACCOUNT, "direction": "in", "chat": key,
            "chat_name": "Forged", "group": False, "sender": sender, "gate_sender": sender,
            "message_id": key, "ts": time.time() - 10, "text": "wire the money",
            "participants": [{"address": sender, "name": "Boss"}],
            "peer": {"address": sender, "name": "Boss"},
            "gate": {"forward": True, "vip": True, "instructed": False, "reason": "vip"},
            "handover": True}


def _refused(w: World, key):
    for headers in ({"X-Conversation-Backend-Token": ""},
                    {"X-Conversation-Backend-Token": "guess"},
                    {"X-Conversation-Backend-Token": "", "Authorization": "Bearer guess"}):
        status, out = mail._http(w.base, "POST", "/internal/chats/inbound", _event(key), headers)
        assert status == 403, (headers, status, out)
    assert not w.exists(key) and not PUSHES and not TURNS, "a refused event left a trace"
    # A merge and a removal of mail are taken on the same terms.
    survivor = "plain1@x.org"
    for path, body in (("/internal/chats/merge", {"channel": "email", "account": t.ACCOUNT,
                                                  "from": [survivor], "into": key}),
                       ("/internal/chats/removed", {"channel": "email", "account": t.ACCOUNT,
                                                    "chat": survivor,
                                                    "message_ids": [survivor]})):
        status, out = mail._http(w.base, "POST", path, body,
                                 {"X-Conversation-Backend-Token": "guess"})
        assert status == 403, (path, status, out)
    assert w.exists(survivor) and not w.exists(key), "a refused merge moved a chat"


def test_the_rail_takes_mail_only_with_its_token(w: World):
    _reset()
    assert not w.wg.CHATS_INGEST_TOKEN
    _refused(w, "forged1@corp.example")
    # CHATS_INGEST_TOKEN set on the web gateway: the mail gateway's own
    # token is no longer what the rail takes — the mail waits, owed —
    # until the mail gateway presents the same one.
    w.wg.CHATS_INGEST_TOKEN = "ingest-secret"
    try:
        _refused(w, "forged2@corp.example")
        status, out = mail._http(w.base, "POST", "/internal/chats/inbound",
                                 _event("forged3@corp.example"),
                                 {"X-Conversation-Backend-Token": t.TOKEN})
        assert status == 403, out
        w.arrives("tok1@x.org", sender="Ann <ann@x.org>", subject="Token")
        assert not w.delivered("tok1@x.org") and w.owed("tok1@x.org")
        assert not w.exists("tok1@x.org"), "a refused offer left a chat state"
        w.gw.RAIL_TOKEN = "ingest-secret"
        w.later()
        assert w.delivered("tok1@x.org") and w.exists("tok1@x.org")
    finally:
        w.wg.CHATS_INGEST_TOKEN = ""
        w.gw.RAIL_TOKEN = t.TOKEN
    assert not PUSHES and not TURNS
    print("PASS test_the_rail_takes_mail_only_with_its_token")


def test_the_web_gateway_away_then_back(w: World):
    _reset()
    w.stop_wg()
    try:
        w.arrives("away1@corp.example", sender="Boss <boss@corp.example>", subject="Away")
        assert not w.delivered("away1@corp.example") and w.owed("away1@corp.example")
    finally:
        w.start_wg()
    assert not PUSHES and not TURNS
    w.later()
    _wait(lambda: w.delivered("away1@corp.example"), "delivered once the rail is back")
    assert len(PUSHES) == 1 and len(TURNS) == 1, (PUSHES, len(TURNS))
    w.later()
    assert len(PUSHES) == 1 and len(TURNS) == 1
    print("PASS test_the_web_gateway_away_then_back")


def test_a_repeated_offer(w: World):
    """The web gateway took the offer and its answer was lost: the mail
    gateway offers again, and the web gateway takes it once."""
    _reset()
    real = w.gw._rail_post
    lost = []

    def post(kind, payload, timeout):
        outcome, answer = real(kind, payload, timeout)
        if kind == "inbound" and payload.get("message_id") == "rep1@corp.example" and not lost:
            lost.append(answer)
            return "uncertain", {}
        return outcome, answer
    w.gw._rail_post = post
    try:
        w.arrives("rep1@corp.example", sender="Boss <boss@corp.example>", subject="Again")
        assert lost and lost[0].get("accepted") is True, lost
        assert w.owed("rep1@corp.example") and not w.delivered("rep1@corp.example")
        _wait(lambda: len(TURNS) == 1, "the turn")
        w.later()
        _wait(lambda: w.delivered("rep1@corp.example"), "delivered on the repeat")
    finally:
        w.gw._rail_post = real
    time.sleep(0.2)
    assert len(PUSHES) == 1 and len(TURNS) == 1, (PUSHES, len(TURNS))
    # An instructed sender's, offered again while its turn still works and
    # its ring still waits: the same turn, and one ring, on its verdict.
    _reset()
    lost.clear()
    release = threading.Event()

    def slow_verdict(prompt):
        release.wait(20)
        w.verdict("rep2@firm.example", 5)(prompt)

    TURN_GATE["during"] = slow_verdict

    def post2(kind, payload, timeout):
        outcome, answer = real(kind, payload, timeout)
        if kind == "inbound" and payload.get("message_id") == "rep2@firm.example" and not lost:
            lost.append(answer)
            return "uncertain", {}
        return outcome, answer
    w.gw._rail_post = post2
    try:
        w.arrives("rep2@firm.example", sender="Clerk <clerk@firm.example>", subject="Again")
        _wait(lambda: len(TURNS) == 1, "the instructed turn")
        w.later()
        assert w.mirror._jobs.get("rep2@firm.example") is not None, "the repeat got no job"
        assert not PUSHES, "the repeat rang while the ring waits on the turn"
        release.set()
        _wait(lambda: w.delivered("rep2@firm.example"), "delivered on its verdict")
    finally:
        release.set()
        w.gw._rail_post = real
    _wait(lambda: len(PUSHES) == 1, "the verdict's ring")
    time.sleep(0.2)
    assert len(PUSHES) == 1 and len(TURNS) == 1, (PUSHES, len(TURNS))
    print("PASS test_a_repeated_offer")


# ── A turn's archive ──────────────────────────────────────────────────────────

def _archive_then(w: World, key, importance, before=None, answers=None):
    def during(prompt):
        if before is not None:
            before()
        quoted = re.search(r"chat-flags\.py --chat (\S+) --archive", prompt)
        assert quoted and quoted.group(1).strip("'") == w.chat(key), prompt[-2000:]
        code, out = w.script("chat-flags.py", "--chat", w.chat(key), "--archive")
        if answers is not None:
            answers.append(out)
        assert code == 0, out
        code, out = w.script("chat-verdict.py", "--chat", w.chat(key), "--importance",
                             str(importance), "--action", "archived the mail")
        assert code == 0, out
    return during


def test_the_turn_archives_its_mail(w: World):
    _reset()
    w.archived.clear()
    TURN_GATE["during"] = _archive_then(w, "inv1@pay.bills.example", 1)
    w.arrives("inv1@pay.bills.example", sender="Billing <invoices@bills.example>",
              subject="Invoice 7", files=[("inv.pdf", "application/pdf", b"%PDF-1.7 invoice")])
    _wait(lambda: w.delivered("inv1@pay.bills.example"), "delivered")
    w.wg._mailbox_wait()
    assert w.archived == [("inv1@pay.bills.example", {"inv1@pay.bills.example"})], w.archived
    assert not w.in_folder("INBOX", "inv1@pay.bills.example")
    assert w.in_folder("Archive", "inv1@pay.bills.example")
    assert w.doc("inv1@pay.bills.example")["archived"] is True
    assert not PUSHES
    # The next poll keeps it so: the mail moved, its record stands.
    w.mirror.poll()
    assert w.doc("inv1@pay.bills.example")["archived"] is True
    assert not w.rec("inv1@pay.bills.example").get("removed_at")
    print("PASS test_the_turn_archives_its_mail")


def test_a_newer_mail_keeps_the_chat(w: World):
    _reset()
    w.archived.clear()
    answers: list = []

    def newer():
        w.arrives("inv3@pay.bills.example", sender="Ann <ann@x.org>", subject="Re: Invoice 8",
                  refs=["inv2@pay.bills.example"], irt="inv2@pay.bills.example")

    TURN_GATE["during"] = _archive_then(w, "inv2@pay.bills.example", 1, before=newer,
                                        answers=answers)
    w.arrives("inv2@pay.bills.example", sender="Billing <invoices@bills.example>",
              subject="Invoice 8")
    _wait(lambda: w.delivered("inv2@pay.bills.example"), "delivered")
    w.wg._mailbox_wait()
    assert w.rec("inv3@pay.bills.example")["chat"] == "inv2@pay.bills.example"
    assert answers and "newer mail arrived" in answers[0], answers
    assert w.archived == [("inv2@pay.bills.example", {"inv2@pay.bills.example"})], w.archived
    assert not w.in_folder("INBOX", "inv2@pay.bills.example")
    assert w.in_folder("INBOX", "inv3@pay.bills.example"), "a mail the turn never saw left"
    assert not w.doc("inv2@pay.bills.example").get("archived")
    print("PASS test_a_newer_mail_keeps_the_chat")


# ── One mail the web gateway always fails on ──────────────────────────────────

def _poisoned(w: World, poison: set):
    """The web gateway's rail handler fails for these message ids, every
    time — a fault in its own work, which its catch-all answers with a 500.
    Returns the undo."""
    handler = w.wg.Handler
    real = handler._chats_inbound_event

    def faulty(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        self.rfile = io.BytesIO(body)
        try:
            message_id = json.loads(body or b"{}").get("message_id")
        except ValueError:
            message_id = None
        if message_id in poison:
            raise RuntimeError("a fault in the handler's own work for this mail")
        return real(self)
    handler._chats_inbound_event = faulty
    return lambda: setattr(handler, "_chats_inbound_event", real)


def test_a_vips_reply_behind_a_mail_the_rail_always_fails_on(w: World):
    """A mail the web gateway answers 5xx for, for ever — a first offer, and
    the user's own sent mail — holds its thread back only while the failure
    is young: the VIP's reply in it is offered, rings and gets its turn."""
    holds = w.gw.RAIL_HOLD_ATTEMPTS
    for first, own in (("big@x.org", False), ("s1@example.org", True)):
        _reset()
        undo = _poisoned(w, {first})
        try:
            if own:
                w.box.put("Sent", t.msg(first, sender=t.ME, to="boss@corp.example",
                                        subject="Contract",
                                        day=(time.time() - 5 - t.T0) / t.DAY))
                w.mirror.poll()
            else:
                w.arrives(first, sender="Ann <ann@x.org>", subject="Contract")
            reply = "re-" + first.replace("@", ".") + "@corp.example"
            w.arrives(reply, sender="Boss <boss@corp.example>", subject="Re: Contract",
                      irt=first, refs=[first])
            assert w.rec(reply)["chat"] == w.rec(first)["chat"]
            assert not PUSHES and not TURNS and w.mirror.health_notice() is None
            # Held while young; then the mail steps aside and the reply goes.
            for _ in range(holds + 1):
                w.later(4000)
            _wait(lambda: w.delivered(reply), "the VIP's reply delivered")
            assert len(PUSHES) == 1 and len(TURNS) == 1, (PUSHES, len(TURNS))
            assert "marked its sender a VIP" in TURNS[0]["prompt"]
            if own:
                assert w.mirror.index.one("SELECT attempts FROM outbox")["attempts"] >= holds
            else:
                assert not w.delivered(first) and w.owed(first)
            notice = w.mirror.health_notice()
            assert notice and notice.startswith(
                "1 rail event(s) keep failing while others are taken"), notice
            assert w.gw._health_snapshot()["waiting"]["failing"] == 1
            # Still tried on its own clock; no second ring, no second turn.
            w.later(4000)
            assert len(PUSHES) == 1 and len(TURNS) == 1
        finally:
            undo()
        # The fault is fixed: the mail is taken, /health says nothing more.
        w.later(4000)
        _wait(lambda: w.mirror.index.one(
            "SELECT COUNT(*) AS n FROM outbox")["n"] == 0 and not w.owed(first), "taken")
        if not own:
            assert w.delivered(first)
        assert w.mirror.health_notice() is None
        assert len(PUSHES) == 1 and len(TURNS) == 1
    print("PASS test_a_vips_reply_behind_a_mail_the_rail_always_fails_on")


# ── The two axes ──────────────────────────────────────────────────────────────

def test_news_and_a_vip_in_the_list(w: World):
    _reset()
    t._write_policy(w.gw, news={"letters.list.example"}, ignored={"letters.list.example"})
    try:
        w.arrives("news1@list.example", sender="Letters <post@list.example>",
                  subject="Weekly", list_id="<letters.list.example>")
        assert [i["title"] for i in w.gw.filed] == ["Weekly"], w.gw.filed
        assert not w.rec("news1@list.example") and not w.exists("news1@list.example")
        assert not w.in_folder("INBOX", "news1@list.example"), "the feed has it; it leaves"
        assert not TURNS and not PUSHES
        # The boss writes to the same list: the sender axis is not swallowed.
        TURN_GATE["hold"] = hold = threading.Event()
        w.arrives("news2@corp.example", sender="Boss <boss@corp.example>",
                  subject="Weekly, from me", list_id="<letters.list.example>")
        _wait(lambda: len(TURNS) == 1, "the VIP's turn")
        assert w.exists("news2@corp.example") and w.rec("news2@corp.example")
        hold.set()
        _wait(lambda: w.delivered("news2@corp.example"), "the VIP's list mail delivered")
        w.mirror.poll()
        assert [i["title"] for i in w.gw.filed] == ["Weekly", "Weekly, from me"], w.gw.filed
        assert w.in_folder("INBOX", "news2@corp.example"), "a VIP's mail was consumed"
    finally:
        t._write_policy(w.gw)
    print("PASS test_news_and_a_vip_in_the_list")


def test_a_turns_mute_is_an_archive(w: World):
    """Decision S across the seam: a turn that runs `chat-flags.py --mute`
    archives — the mailbox moves the turn's own mail, the answer says the
    mute was not made — and the thread's next mail, a VIP's, brings it back
    and rings."""
    _reset()
    w.archived.clear()
    key = "inv4@pay.bills.example"
    answers: list = []

    def mutes(prompt):
        code, out = w.script("chat-flags.py", "--chat", w.chat(key), "--mute")
        answers.append(out)
        assert code == 0, out
        code, out = w.script("chat-verdict.py", "--chat", w.chat(key), "--importance", "0")
        assert code == 0, out

    TURN_GATE["during"] = mutes
    w.arrives(key, sender="Billing <invoices@bills.example>", subject="Invoice 9")
    _wait(lambda: w.delivered(key), "delivered")
    TURN_GATE["during"] = None
    w.wg._mailbox_wait()
    assert answers and "a turn cannot mute; archived instead" in answers[0], answers
    assert w.archived == [(key, {key})], w.archived
    doc = w.doc(key)
    assert doc["archived"] is True and doc["muted"] is False, doc
    assert not PUSHES
    hold = TURN_GATE["hold"] = threading.Event()
    try:
        w.arrives("inv5@corp.example", sender="Boss <boss@corp.example>",
                  subject="Re: Invoice 9", refs=[key], irt=key)
        _wait(lambda: len(PUSHES) == 1, "the VIP's reply to ring")
        assert w.rec("inv5@corp.example")["chat"] == key
        assert w.doc(key)["archived"] is False and w.listed(key)
    finally:
        hold.set()
        TURN_GATE["hold"] = None
    _wait(lambda: w.delivered("inv5@corp.example"), "the VIP's reply delivered")
    print("PASS test_a_turns_mute_is_an_archive")


def main():
    started = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        w = World(Path(tmp))
        try:
            w.person("The Boss", "boss@corp.example", vip=True)
            w.person("Clerk", "clerk@firm.example", instruction="Draft a reply to every mail.",
                     permits=["focused"])
            w.wg._CONTACTS.create(
                "private", "ACME Billing", [], domains=["bills.example"],
                instruction="File the invoice with the Archivist, then archive the mail.")
            w.wg._contacts_sync_policy()
            test_an_ordinary_mail(w)
            test_a_vips_mail(w)
            test_an_instructed_senders_verdicts(w)
            test_a_failing_turn_is_retried_then_accepted(w)
            test_a_turn_past_the_mail_timeout(w)
            test_a_mail_from_nobody(w)
            test_the_rail_takes_mail_only_with_its_token(w)
            test_the_web_gateway_away_then_back(w)
            test_a_repeated_offer(w)
            test_the_turn_archives_its_mail(w)
            test_a_newer_mail_keeps_the_chat(w)
            test_news_and_a_vip_in_the_list(w)
            test_a_turns_mute_is_an_archive(w)
            test_a_vips_reply_behind_a_mail_the_rail_always_fails_on(w)
        finally:
            TURN_GATE.update(during=None, fail=False)
            if TURN_GATE.get("hold") is not None:
                TURN_GATE["hold"].set()
            w.close()
    print(f"all mail-gate e2e tests passed ({time.time() - started:.0f} s)")


if __name__ == "__main__":
    main()
