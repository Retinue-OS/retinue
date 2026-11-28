#!/usr/bin/env python3
"""Writing mail from the chat surface, across the seam: the REAL mail gateway
behind the REAL web gateway (docs/mail-chats.md, "Writing").

tests/test_chat_mail_send.py runs the web gateway against a mock mail
gateway, and tests/test_mail_gateway_send.py runs the mail gateway by itself.
Each is right about its own side, and a defect that lives between the two —
an answer one side gives in a shape the other does not read — passes both.
This file has no mock of either: scripts/mail-gateway.py's HTTP handler over
the tests' fake mailbox and scripted SMTP server (the World of
tests/test_mail_gateway_send.py), the web gateway's handler in front of it
(the loader of tests/test_chat_mail.py), the gateway's rail pointed at the
web gateway, and the SPARQL mock answering from the ledger the gateway
writes. What a browser would be told is what is asserted.

Written to the phase's decisions, each of which is a contract between the
two processes:

- a reply is sent: one mail, to the gateway's own recipients; the Message,
  the draft, the read mark, and the mailbox following (\\Seen);
- **a send that certainly failed ends its press** (E): the SMTP server's
  refusal is answered 424 {"sent": false} — and the same press, once the
  server is back, goes through under the same key;
- **a mail that may have gone out is never reported as not sent**, and **a
  repeat is not a reply** (F): an answer that was lost is 202 {unconfirmed};
  the same press asked again is answered from the mail that went out, and
  marks nothing read that arrived since — the gateway says "repeat";
- **a dismissed entry is over** (G): a slow send a page polled in flight,
  then refused, leaves no failed bubble — and a new mail no chat at all;
  and an outcome nobody knows is never worded as "failed" — in the chat,
  on the approval page, in what it polls, in the note to the thread that
  queued the send;
- **a kept key is dropped only on the mail gateway's own word about it**
  (H): every "not sent" says who said it ("by"), and an answer cut short
  on the wire is "not confirmed", never "not sent";
- **a repeat clears the draft it repeats** (I), and only that draft — a
  new attempt under the press's kept key too;
- a delete on a mailbox without a Trash says why, in the gateway's words;
- spam: the thread's inbound mails go to Junk and the chat is muted and
  archived; without a Junk folder the gateway's sentence is the answer and
  the chat is untouched; Restore brings the chat back and the mails stay
  in Junk.

Standalone:

    python3 tests/test_chat_mail_e2e.py
"""
from __future__ import annotations

import html
import http.client
import importlib.util
import json
import os
import re
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))


def _import(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


S = _import("mail_gateway_send_harness", HERE / "test_mail_gateway_send.py")  # World, MiniSmtp
t = S.t                                     # tests/test_mail_gateway.py: msg, serve, TOKEN
import test_chat_mail as mail  # noqa: E402  (the web gateway's loader, the SPARQL mock)

SLUG = "mail-gateway"
ANN = "ann@x.org"
# As crypto.randomUUID() mints a press's key.
KEY = "3f0e7a52-1111-4222-8333-4444555566{:02d}"
UNKNOWN = "it is not known whether the mail went out"
# A thread root as an ordinary correspondent's mail client mints one (the
# length of an Outlook Message-ID; the gateway keeps up to 240 characters).
# With it and the gateway's own Message-ID in them, the gateway's answers
# that name both are longer than 500 bytes.
LONG_ROOT = ("AM6PR04MB5463F1E2A9C7B8D0E1F2A3B4C5D6E7F8091A2B3C@"
             "AM6PR04MB5463.eurprd04.prod.outlook.com")


class Rig:
    """One mail gateway (its module, mirror, fake mailbox, SMTP server) and
    one web gateway in front of it, each behind its real HTTP handler."""

    def __init__(self, tmp: Path, policy: str = "trust"):
        self.w = S.World(str(tmp / "gw"), policy=policy, CHAT_ERASE_TOKEN="erase-token")
        gw = self.gw = self.w.gw
        self.box, self.smtp = self.w.box, self.w.smtp
        # Sends are stamped by the wall clock, as in a deployment: the web
        # gateway compares the gateway's instants with its own.
        gw._now = time.time
        gw._set_state(connected=True)
        self.gw_server, self.gw_base = t.serve(gw)
        # The web gateway reads the ledger the mail gateway writes.
        mail.STATE["ledger"] = Path(gw.INBOUND_STORE_DIR)
        mail.STATE["ledger"].mkdir(parents=True, exist_ok=True)
        self.sparql = mail._serve(mail._MockSparql)
        os.environ["CHAT_PENDING_RETRY_SECONDS"] = "0"
        wg = self.wg = mail._load_gateway(tmp / "wg", self.sparql.server_address[1])
        wg._CHANNEL_GATEWAYS = {SLUG: {"base_url": self.gw_base, "token": t.TOKEN,
                                       "label": "E-mail", "channel": "email"}}
        wg._gw_identity.clear()
        wg.push_notify.enabled = lambda: False
        wg.send_message = lambda *a, **k: {"response": "ok", "model_name": "stub",
                                           "cost_usd": 0.0}
        wg.contacts_mod.commit = lambda *a, **k: None
        wg.ec.policy_accounts = lambda *a, **k: []
        # A decision's note waits for the send's outcome; not a second each.
        wg.SEND_DECISION_POLL_INTERVAL = 0.05
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), wg.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        # The rail: the gateway's own events, posted to the web gateway.
        gw._chats.CHATS_INGEST_URL = self.base + "/internal/chats/inbound"
        gw._chats.CHATS_INGEST_TOKEN = "agent-token"
        # The rail takes a mail event only with a token (docs/mail-chats.md,
        # "The gate"): the web gateway is given the one the gateway sends.
        wg.CHATS_INGEST_TOKEN = "agent-token"
        self.rail: list = []

        def rail_send(kind, payload):
            status, answer = mail._http("", "POST", gw._rail_url(kind), payload,
                                        {"X-Conversation-Backend-Token": "agent-token"})
            self.rail.append((kind, payload.get("direction"), payload.get("message_id"), status))
            return "ok" if status < 300 else "drop"
        gw._rail_send = rail_send
        # A mail that arrived is offered, not told (the gate): through the
        # gateway's own post, with the token it presents — never the World's
        # stand-in, which takes every offer without the web gateway seeing it.
        gw.RAIL_TOKEN = "agent-token"

        def rail_offer(payload):
            outcome, answer = gw._rail_post("inbound", payload, gw.MAIL_RAIL_TIMEOUT)
            self.rail.append(("offer", payload.get("direction"), payload.get("message_id"),
                              outcome))
            return outcome, answer
        gw._rail_offer = rail_offer
        self.account = gw.ACCOUNT

    def close(self) -> None:
        self.server.shutdown()
        self.gw_server.shutdown()
        self.sparql.shutdown()
        S.done(self.w)

    # -- the mailbox and the gateway -------------------------------------------

    def policy(self, category: str) -> None:
        """The mailbox's send policy, as EMAIL_SEND_POLICY states it (read by
        the gateway at every send)."""
        os.environ["EMAIL_SEND_POLICY"] = json.dumps(
            [{"address": "ME@example.org", "category": category}])
        assert self.gw._send_category() == category

    def arrives(self, mid: str, **kwargs) -> None:
        """A mail arrives in INBOX and the mirror sees it (the rail says so)."""
        self.box.put("INBOX", t.msg(mid, **kwargs))
        self.w.mirror.poll()

    def loop(self) -> None:
        """One turn of the gateway's mirror loop between polls: what a send
        left to do — its record, the rail's word of it, the Sent copy."""
        self.gw._between_polls(self.w.mirror)

    def gateway(self, method: str, path: str, body=None, headers=None):
        return mail._http(self.gw_base, method, path, body,
                          {"Authorization": "Bearer " + t.TOKEN, **(headers or {})})

    def entries(self, key: str) -> list:
        return [e for e in self.gw.SENDS.entries() if e.get("request_key") == key]

    def seen(self, mid: str) -> bool:
        return "\\Seen" in S.flags(self.box, "INBOX", mid)

    # -- the dashboard -----------------------------------------------------------

    def chat(self, key: str) -> str:
        return mail.cs.make_chat_id("email", key, self.account)

    def http(self, method: str, path: str, body=None, headers=None):
        return mail._http(self.base, method, path, body, headers)

    def send(self, key: str, body: dict):
        return self.http("POST", "/chats/" + mail._quote(self.chat(key)) + "/send", body)

    def messages(self, key: str) -> dict:
        return mail._messages(self.base, self.chat(key))

    def listed(self, key: str) -> dict:
        return mail._chats(self.base)[self.chat(key)]

    def draft(self, key: str, text: str | None = None):
        if text is not None:
            self.wg._CHAT_STATE.set_draft(self.chat(key), text, author="user")
        return (self.wg._CHAT_STATE.get(self.chat(key)).get("draft") or {}).get("text")

    def settle(self) -> None:
        """Let the mailbox hooks under way finish."""
        self.wg._mailbox_wait()

    def page(self, path: str) -> str:
        status, _headers, data = mail._raw(self.base, path)
        assert status == 200, (path, status)
        return data.decode("utf-8")

    def press(self, path: str) -> int:
        """A form POST as the /sends pages make it (not following the redirect)."""
        conn = http.client.HTTPConnection(urllib.parse.urlsplit(self.base).netloc, timeout=30)
        try:
            conn.request("POST", path, body=b"", headers={"Content-Length": "0"})
            response = conn.getresponse()
            response.read()
            return response.status
        finally:
            conn.close()


def _wait_for(cond, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return bool(cond())


class _SlowSmtp:
    """The SMTP session of the next send waits until it is let go: `entered`
    is set when the gateway is about to speak to the server."""

    def __init__(self, r: Rig):
        self.r, self.entered, self.release = r, threading.Event(), threading.Event()
        self.real = r.gw._ec._smtp_send

    def __enter__(self):
        def slow(*args, **kwargs):
            self.entered.set()
            self.release.wait(30)
            return self.real(*args, **kwargs)
        self.r.gw._ec._smtp_send = slow
        return self

    def __exit__(self, *exc):
        self.release.set()
        self.r.gw._ec._smtp_send = self.real


# ── A reply ───────────────────────────────────────────────────────────────────

def test_a_reply_is_sent(r: Rig):
    r.arrives("a@x.org", day=1)
    chat = r.listed("a@x.org")
    assert chat["can_send"] is True and chat["unread"] == 1, chat
    r.draft("a@x.org", "Hello back")
    key = KEY.format(1)
    status, msg = r.send("a@x.org", {"text": "Hello back", "reply": "all", "request_key": key})
    assert status == 200, msg
    # One mail, to the recipients the gateway built from its own records.
    assert len(r.smtp.mails) == 1 and r.smtp.mails[-1]["rcpt"] == [ANN]
    (entry,) = r.entries(key)
    assert msg["id"] == entry["message_id"] and len(msg["id"]) > 40, "a real Message-ID"
    assert (msg["direction"], msg["author"], msg["text"]) == ("out", "user", "Hello back")
    assert "unconfirmed" not in msg and r.draft("a@x.org") is None
    assert r.listed("a@x.org")["unread"] == 0
    # The mailbox follows the chat: the thread's mail is seen there.
    r.settle()
    assert r.seen("a@x.org")
    # The gateway announces its own send; the chat holds the mail once.
    r.loop()
    assert ("inbound", "out", entry["message_id"], 200) in r.rail, r.rail
    body = r.messages("a@x.org")
    assert [(m["id"], m["direction"]) for m in body["messages"]] == [
        ("a@x.org", "in"), (entry["message_id"], "out")], body["messages"]
    assert body["pending"] == []


def test_the_press_sends_under_verify(r: Rig):
    """The press is the approval on e-mail as on the messengers: under
    `verify` the real gateway queues the user's reply, the press releases it
    at once, and the mail goes out — no approval link, nothing on /sends."""
    r.arrives("pv@x.org", day=1)
    r.policy("verify")
    try:
        key = KEY.format(60)
        mails = len(r.smtp.mails)
        status, msg = r.send("pv@x.org", {"text": "Yes, on my way.", "request_key": key})
        assert status == 200 and msg["text"] == "Yes, on my way.", msg
        assert "unconfirmed" not in msg and len(r.smtp.mails) == mails + 1
        (entry,) = r.entries(key)
        assert entry["status"] == "approved" and msg["id"] == entry["message_id"], entry
        assert r.draft("pv@x.org") is None and r.listed("pv@x.org")["unread"] == 0
        assert r.messages("pv@x.org")["pending"] == []
        # An agent's mail to the same thread still waits for the user.
        status, queued = r.gateway("POST", "/send", {
            "recipient": "pv@x.org", "message": "From Ara", "author": "agent",
            "user_approved": True})
        assert status == 202 and queued["status"] == "pending_approval", queued
        assert len(r.smtp.mails) == mails + 1
        r.gateway("POST", f"/pending-sends/{queued['request_id']}/retract")
    finally:
        r.policy("trust")
    r.settle()


# ── A send that certainly failed ends its press (decision E) ─────────────────

def test_a_refused_send_ends_its_press(r: Rig):
    r.arrives("b@x.org", day=1)
    key = KEY.format(2)
    body = {"text": "Thanks, see you Tuesday.", "request_key": key}
    r.draft("b@x.org", body["text"])
    r.smtp.refuse_login = True
    try:
        status, out = r.send("b@x.org", body)
    finally:
        r.smtp.refuse_login = False
    # Not sent, for certain — said so that a page ends the press: a 4xx,
    # "sent": false, the gateway's own sentence, the key it was made under.
    assert status == 424 and out["sent"] is False and out["request_key"] == key, out
    assert out["error"].startswith("send failed: ") and "535" in out["error"], out
    # ... by the mail gateway's own word, which ends what the key names too.
    assert set(out) == {"error", "sent", "by", "request_key"} and out["by"] == "gateway", out
    assert r.smtp.mails == [] or all(b"see you Tuesday" not in m["data"] for m in r.smtp.mails)
    # The words stay, and nothing stands in the chat as sent or as failed.
    assert r.draft("b@x.org") == body["text"]
    assert r.messages("b@x.org")["pending"] == [] and r.listed("b@x.org")["pending"] == 0
    assert not r.seen("b@x.org"), "no reply was made"
    # The next press is a new one — a new key — and goes through.
    before = len(r.smtp.mails)
    status, msg = r.send("b@x.org", {**body, "request_key": KEY.format(3)})
    assert status == 200 and len(r.smtp.mails) == before + 1, msg
    assert r.draft("b@x.org") is None


def test_the_same_press_goes_through_once_the_server_is_back(r: Rig):
    """Decision E, the mail gateway's half: a key whose send failed for
    certain is no earlier send. A page that kept the key — an older page, a
    5xx from a proxy in between — is not answered the old failure for a
    day."""
    r.arrives("b2@x.org", day=1)
    key = KEY.format(4)
    body = {"text": "The very same words.", "request_key": key}
    r.smtp.refuse_login = True
    try:
        status, out = r.send("b2@x.org", body)
    finally:
        r.smtp.refuse_login = False
    assert status == 424 and out["sent"] is False, out
    before, sessions = len(r.smtp.mails), r.smtp.sessions
    status, msg = r.send("b2@x.org", body)
    assert status == 200, ("the same press, the server back: tried afresh", status, msg)
    assert len(r.smtp.mails) == before + 1 and r.smtp.sessions == sessions + 1
    assert msg["id"] == r.entries(key)[-1]["message_id"] and len(r.entries(key)) == 1
    # A new mail, the same.
    new = {"channel": "email", "to": ["dora@w.org"], "subject": "Hi", "text": "New mail",
           "request_key": KEY.format(5)}
    r.smtp.reject_data = "451 4.3.0 try again later"
    try:
        status, out = r.http("POST", "/chats/compose", new)
    finally:
        r.smtp.reject_data = None
    assert status == 424 and out["sent"] is False and "451" in out["error"], out
    before = len(r.smtp.mails)
    status, out = r.http("POST", "/chats/compose", new)
    assert status == 200 and out["message"]["id"], out
    assert len(r.smtp.mails) == before + 1


# ── A mail that may have gone out; a repeat is not a reply (decision F) ──────

def _lost_answer_then_ann_replies(r: Rig, root: str, key: str) -> dict:
    """The user replies; the web gateway stops waiting before the gateway
    has sent; the mail goes out and the rail says so; the correspondent
    answers; the user starts a new draft. Returns the send's entry."""
    r.arrives(root, day=1)
    r.draft(root, "My reply")
    saved, r.wg.MAIL_SEND_TIMEOUT = r.wg.MAIL_SEND_TIMEOUT, 0.4
    try:
        with _SlowSmtp(r) as smtp:
            status, out = r.send(root, {"text": "My reply", "request_key": key})
            assert smtp.entered.is_set()
            # Not confirmed — never an error: the mail may be on its way.
            assert status == 202 and out["unconfirmed"] is True and "error" not in out, out
            assert out["request_key"] == key and out["id"] == r.chat(root)
            (entry,) = r.entries(key)
            assert out["message_id"] == entry["message_id"], "asked of the queue, by the key"
            assert r.draft(root) is None, "not handed back for a second send"
            assert [(q["status"], q["request_key"]) for q in r.messages(root)["pending"]] == [
                ("sending", key)]
            assert r.listed(root)["unread"] == 0, "writing the reply read the chat"
    finally:
        r.wg.MAIL_SEND_TIMEOUT = saved
    # It goes out, and the rail announces it: the bubble gives way to the
    # mail, and the chat stays read — here and in the mailbox.
    assert _wait_for(lambda: (r.gw.SENDS.get(entry["id"]) or {}).get("status") == "approved")
    # "approved" is set during the SMTP session, before the send's record —
    # and the rail's event — are written: wait for the mail itself, letting
    # the mirror loop turn until the rail has announced it, never on time.

    def listed_once() -> bool:
        r.loop()
        body = r.messages(root)
        return (body["pending"] == []
                and [m["id"] for m in body["messages"]].count(entry["message_id"]) == 1)

    assert _wait_for(listed_once), r.messages(root)
    body = r.messages(root)
    assert body["pending"] == []
    assert [m["id"] for m in body["messages"]].count(entry["message_id"]) == 1
    assert r.listed(root)["unread"] == 0
    # The read reaches the mailbox through a hook off the rail's request:
    # waited for as the flag it sets, not for a fixed while.
    assert _wait_for(lambda: r.settle() or r.seen(root))
    # Later: the correspondent answers, and the user starts something new.
    late = "late-" + root
    r.arrives(late, irt=root, refs=[root], day=2, subject="Re: Quarterly numbers")
    assert r.listed(root)["unread"] == 1
    r.draft(root, "typed since")
    return entry


def test_a_lost_answer_is_not_a_failure_and_its_repeat_is_not_a_reply(r: Rig):
    key = KEY.format(6)
    entry = _lost_answer_then_ann_replies(r, "c@x.org", key)
    doc = r.wg._CHAT_STATE.get(r.chat("c@x.org"))
    mails = len(r.smtp.mails)
    # "Check again", or the kept words pressed again: the same press.
    status, msg = r.send("c@x.org", {"text": "My reply", "request_key": key})
    # Reconciled: the mail that went out, and no second one.
    assert status == 200 and msg["id"] == entry["message_id"], msg
    assert len(r.smtp.mails) == mails and len(r.entries(key)) == 1
    body = r.messages("c@x.org")
    assert [m["id"] for m in body["messages"]].count(entry["message_id"]) == 1
    # And nothing else: the mail that arrived since is unread here and
    # unseen in the mailbox, and the new draft is in the composer.
    r.settle()
    after = r.wg._CHAT_STATE.get(r.chat("c@x.org"))
    assert r.listed("c@x.org")["unread"] == 1 and after["last_read"] == doc["last_read"]
    assert (after.get("attention") or {}).get("state") == (doc.get("attention") or {}).get("state")
    assert not r.seen("late-c@x.org"), "the mailbox was told nothing"
    assert r.draft("c@x.org") == "typed since"


def test_the_gateway_says_when_it_answers_a_repeat(r: Rig):
    """Decision F, the mail gateway's half: an answer given from an earlier
    entry says so — "repeat": true — where it says the mail was sent, where
    it says it waits, and where it says nobody knows."""
    key = KEY.format(7)
    _lost_answer_then_ann_replies(r, "c2@x.org", key)
    press = {"recipient": "c2@x.org", "message": "My reply", "author": "user",
             "user_approved": True, "request_key": key}
    status, answer = r.gateway("POST", "/send", press)
    assert status == 200 and answer["status"] == "sent", answer
    assert answer.get("repeat") is True, ("a sent mail, answered again", answer)
    # ... a queued send, asked again,
    r.policy("verify")
    try:
        queued = {"recipient": "c2@x.org", "message": "Queued", "author": "user",
                  "user_approved": True, "request_key": KEY.format(8)}
        status, first = r.gateway("POST", "/send", queued)
        assert status == 202 and first.get("repeat") is not True, first
        status, again = r.gateway("POST", "/send", queued)
        assert status == 202 and again["request_id"] == first["request_id"], again
        assert again.get("repeat") is True, ("a queued send, answered again", again)
        r.gateway("POST", f"/pending-sends/{first['request_id']}/retract")
    finally:
        r.policy("trust")
    # ... and one whose outcome nobody knows.
    unknown = {"recipient": "c2@x.org", "message": "Lost", "author": "user",
               "user_approved": True, "request_key": KEY.format(9)}
    r.smtp.drop, r.smtp.takes = "dot", False
    try:
        status, first = r.gateway("POST", "/send", unknown)
    finally:
        r.smtp.drop, r.smtp.takes = None, True
    assert status == 502 and first["unconfirmed"] is True and first.get("repeat") is not True
    status, again = r.gateway("POST", "/send", unknown)
    assert status == 502 and again["unconfirmed"] is True, again
    assert again.get("repeat") is True, ("an unknown outcome, answered again", again)
    r.gateway("POST", f"/pending-sends/{first['request_id']}/dismiss")


def test_an_outcome_the_gateway_cannot_know_is_not_a_failure(r: Rig):
    root = LONG_ROOT
    r.arrives(root, day=1)
    key = KEY.format(10)
    r.draft(root, "Did it go?")
    # The SMTP server has the whole mail and hangs up without an answer.
    r.smtp.drop, r.smtp.takes = "dot", True
    try:
        mails = len(r.smtp.mails)
        status, out = r.send(root, {"text": "Did it go?", "request_key": key})
    finally:
        r.smtp.drop = None
    (entry,) = r.entries(key)
    # The gateway's 502 {unconfirmed, outcome_unknown, request_id, message_id,
    # chat} — longer than 500 bytes with two real ids in it — read whole:
    status_gw, raw = r.gateway("POST", "/send", {
        "recipient": root, "message": "Did it go?", "author": "user",
        "user_approved": True, "request_key": key})
    assert status_gw == 502 and raw["outcome_unknown"] is True, raw
    assert (raw["chat"], raw["message_id"]) == (root, entry["message_id"]), raw
    assert len(json.dumps(raw)) > 500, len(json.dumps(raw))
    # ... and the browser is told "not confirmed", with the mail's id.
    assert status == 202 and out["unconfirmed"] is True and "error" not in out, out
    assert out["message_id"] == entry["message_id"] and UNKNOWN in out["note"]
    assert len(r.smtp.mails) == mails + 1, "the server holds it: it may well be delivered"
    assert r.draft(root) is None, "not handed back for a second send"
    pending = r.messages(root)["pending"]
    assert [(q["status"], q["outcome_unknown"], q["text"]) for q in pending] == [
        ("failed", True, "Did it go?")], pending
    assert UNKNOWN in pending[0]["error"]
    # Writing the reply read the chat, and the mailbox follows — though
    # nobody knows of the mail.
    assert r.listed(root)["unread"] == 0
    assert _wait_for(lambda: r.settle() or r.seen(root))
    # The list says the same of it, and the companion is not told "failed".
    assert r.listed(root)["pending"] == 1
    note = "\n".join(r.wg._conv_mail_send_note(r.chat(root), r.wg._CHAT_STATE.get(r.chat(root))))
    assert "never confirmed: it is not known whether it went out" in note, note
    assert "of this thread failed" not in note
    # The same press again: the same answer, no second mail — and a draft
    # written since is not taken for that press's words.
    r.draft(root, "typed since")
    status, again = r.send(root, {"text": "Did it go?", "request_key": key})
    assert status == 202 and again["message_id"] == out["message_id"], again
    assert len(r.smtp.mails) == mails + 1 and r.draft(root) == "typed since"
    # The user has read it and dismisses the bubble: gone, and it stays gone
    # — the gateway keeps the entry of a keyed send, marked, and still
    # answers for it by id.
    rid = pending[0]["request_id"]
    status, out = r.http("POST", f"/chats/{mail._quote(r.chat(root))}/pending/{rid}/dismiss",
                         {})
    assert status == 200 and out["pending"] == [], out
    status, kept = r.gateway("GET", f"/pending-sends/{rid}")
    assert (status, kept["status"], kept["dismissed"]) == (200, "error", True), kept
    assert r.messages(root)["pending"] == [] and r.listed(root)["pending"] == 0


# ── A dismissed entry is over (decision G) ───────────────────────────────────

def test_a_send_polled_in_flight_then_refused_leaves_no_failed_bubble(r: Rig):
    r.arrives("d@x.org", day=1)
    key = KEY.format(11)
    r.draft("d@x.org", "Slow one")
    answered: dict = {}
    r.smtp.refuse_login = True
    try:
        with _SlowSmtp(r) as smtp:
            press = threading.Thread(target=lambda: answered.update(
                out=r.send("d@x.org", {"text": "Slow one", "request_key": key})))
            press.start()
            assert smtp.entered.wait(10)
            # The chat page's poll while the SMTP session is under way.
            during = r.messages("d@x.org")["pending"]
            assert [(q["status"], q["text"], q["request_key"]) for q in during] == [
                ("sending", "Slow one", key)], during
            smtp.release.set()
            press.join(30)
    finally:
        r.smtp.refuse_login = False
    status, out = answered["out"]
    assert status == 424 and out["sent"] is False and "535" in out["error"], out
    # The gateway put the entry away: a send that failed for certain is
    # removed when it is dismissed — not listed, and by id not found.
    rid = during[0]["request_id"]
    assert r.gateway("GET", "/pending-sends?chat=d%40x.org")[1] == {"pending": []}
    status, kept = r.gateway("GET", f"/pending-sends/{rid}")
    assert status == 404, (status, kept)
    # The words are in the composer, and nowhere else: no failed bubble on
    # this poll or a later one, none counted in the list.
    assert r.draft("d@x.org") == "Slow one"
    for _ in range(3):
        assert r.messages("d@x.org")["pending"] == []
    assert r.listed("d@x.org")["pending"] == 0
    assert r.wg._chat_pending(r.wg._CHAT_STATE.get(r.chat("d@x.org"))) == []


def _chat_of(r: Rig, subject: str) -> list:
    """The ids of the chats whose state is named ``subject``."""
    return [chat_id for chat_id, doc in r.wg._CHAT_STATE.all().items()
            if doc.get("name") == subject]


def test_a_new_mail_polled_in_flight_then_refused_leaves_no_chat(r: Rig):
    """W1/W4: the Chats page's list poll runs while a new mail's SMTP session
    lasts and makes its chat (unsent, the send *sending*); the server then
    refuses the mail. The gateway removes the entry (404 by id) — and the
    chat, made of nothing but that send, goes with it: no list shows it, so
    nothing could ever delete it, and its state would keep the subject and
    every recipient for good."""
    key = KEY.format(41)
    answered: dict = {}
    r.smtp.refuse_login = True
    try:
        with _SlowSmtp(r) as smtp:
            press = threading.Thread(target=lambda: answered.update(out=r.http(
                "POST", "/chats/compose",
                {"channel": "email", "to": ["zed@x.org"], "subject": "Secret plan",
                 "text": "Hello Zed", "request_key": key})))
            press.start()
            assert smtp.entered.wait(10)
            # (GET /chats schedules the sync; run here directly.)
            r.wg._sync_pending_sends()
            (chat_id,) = _chat_of(r, "Secret plan")
            doc = r.wg._CHAT_STATE.get(chat_id)
            assert doc["unsent"] is True and [p["status"] for p in r.wg._chat_pending(doc)] == [
                "sending"], doc
            smtp.release.set()
            press.join(30)
    finally:
        r.smtp.refuse_login = False
    status, out = answered["out"]
    assert status == 424 and out["sent"] is False and out["by"] == "gateway", out
    assert r.entries(key) == [], "removed: a repeat is a new attempt"
    # Forgotten at once, and nothing brings it back.
    assert not r.wg._CHAT_STATE.exists(chat_id) and _chat_of(r, "Secret plan") == []
    for _ in range(3):
        r.wg._sync_pending_sends()
    assert chat_id not in mail._chats(r.base) and not r.wg._CHAT_STATE.exists(chat_id)


def test_a_failed_new_mail_tried_afresh_leaves_no_first_chat(r: Rig):
    """W1, its second path: a new mail whose answer the web gateway stopped
    waiting for, then refused — listed as failed in its chat — and sent by a
    new attempt under the same key. The new mail has a new Message-ID, so a
    chat of its own; the first chat's entry is gone (404), and the chat with
    it."""
    key = KEY.format(42)
    new = {"channel": "email", "to": ["yan@x.org"], "subject": "Second go",
           "text": "Hello Yan", "request_key": key}
    saved, r.wg.MAIL_SEND_TIMEOUT = r.wg.MAIL_SEND_TIMEOUT, 0.4
    r.smtp.refuse_login = True
    try:
        with _SlowSmtp(r) as smtp:
            status, out = r.http("POST", "/chats/compose", new)
            assert status == 202 and out["unconfirmed"] is True, out
            first = out["id"]
            smtp.release.set()
            assert _wait_for(lambda: [e["status"] for e in r.entries(key)] == ["error"])
    finally:
        r.wg.MAIL_SEND_TIMEOUT = saved
        r.smtp.refuse_login = False
    pending = mail._messages(r.base, first)["pending"]
    assert [(q["status"], q["outcome_unknown"]) for q in pending] == [("failed", False)], pending
    status, out = r.http("POST", "/chats/compose", new)
    assert status == 200 and out["id"] != first, out
    for _ in range(2):
        r.wg._sync_pending_sends()
    assert not r.wg._CHAT_STATE.exists(first) and first not in mail._chats(r.base)
    assert out["id"] in mail._chats(r.base)


# ── Who said "not sent" (decision H) ─────────────────────────────────────────

def test_a_press_again_the_gateway_never_heard_says_so(r: Rig):
    """A5's server half. The first press under a key went out, its answer was
    lost; pressed again while the mail gateway cannot be reached, the press
    is answered "not sent" — of this request, by this side ("by":
    "dashboard"), which says nothing of the key; asked once the gateway is
    back, the same key is answered from the mail that went out."""
    r.arrives("h@x.org", day=1)
    key = KEY.format(43)
    body = {"text": "Only once, please.", "request_key": key}
    status, msg = r.send("h@x.org", body)
    assert status == 200, msg
    mails = len(r.smtp.mails)
    real = r.wg._CHANNEL_GATEWAYS[SLUG]["base_url"]
    r.wg._CHANNEL_GATEWAYS[SLUG]["base_url"] = "http://127.0.0.1:1"
    try:
        status, out = r.send("h@x.org", body)
        assert status == 502 and out["sent"] is False and out["by"] == "dashboard", out
        assert "could not be reached" in out["error"] and out["request_key"] == key, out
        # A new mail asked of a gateway that never said which mailbox it
        # serves: the same.
        r.wg._gw_identity.clear()
        status, out = r.http("POST", "/chats/compose", {
            "channel": "email", "to": ["x@x.org"], "subject": "s", "text": "t",
            "request_key": KEY.format(44)})
        assert status in (409, 502) and out["by"] == "dashboard", out
    finally:
        r.wg._CHANNEL_GATEWAYS[SLUG]["base_url"] = real
        r.wg._gw_identity.clear()
        r.wg._pending_scan_failed.clear()
    status, again = r.send("h@x.org", body)
    assert status == 200 and again["id"] == msg["id"], again
    assert len(r.smtp.mails) == mails, "the key kept: no second mail"


def test_an_answer_cut_short_is_not_confirmed(r: Rig):
    """W2. The mail gateway's 502 {unconfirmed} — the mail was handed to the
    SMTP server and its answer never seen — with its last bytes lost on the
    wire (the process stopped while it wrote its answer). A body that ended
    before its Content-Length is no answer: "not confirmed", never "not
    sent" with the cut text as its sentence."""
    r.arrives("g@x.org", day=1)
    key = KEY.format(45)
    handler = r.gw._Handler
    real = handler._reply

    def cut(self, status, body):
        if status != 502 or not self.path.startswith("/send"):
            return real(self, status, body)
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload[:-40])
        self.close_connection = True
    handler._reply = cut
    r.smtp.drop, r.smtp.takes = "dot", True
    try:
        mails = len(r.smtp.mails)
        status, out = r.send("g@x.org", {"text": "Cut short", "request_key": key})
    finally:
        handler._reply = real
        r.smtp.drop = None
    assert status == 202 and out["unconfirmed"] is True and "error" not in out, out
    assert out["request_key"] == key and len(r.smtp.mails) == mails + 1
    (entry,) = r.entries(key)
    assert out["message_id"] == entry["message_id"], "asked of the queue, by the key"
    pending = r.messages("g@x.org")["pending"]
    assert [(q["status"], q["outcome_unknown"]) for q in pending] == [("failed", True)], pending
    assert r.http("POST", f"/chats/{mail._quote(r.chat('g@x.org'))}/pending/"
                          f"{pending[0]['request_id']}/dismiss", {})[0] == 200


# ── A repeat clears the draft it repeats (decision I) ────────────────────────

def test_a_repeat_clears_the_draft_it_repeats(r: Rig):
    """W3. The answer to a press is lost on its way to the browser; the page
    puts the words back into the box and autosaves them as the shared draft;
    the user presses Send again — the same words, the same key — and is
    answered as a repeat. The draft that holds this press's words goes, in
    each of the three repeat answers; one written since stays."""
    r.arrives("i@x.org", day=1)
    words = "Thanks, see you Tuesday."
    cid = r.chat("i@x.org")

    def autosave(text):
        # What the page posts a second after the words came back.
        doc = r.wg._CHAT_STATE.get(cid)
        status, out = r.http("POST", "/chats/" + mail._quote(cid) + "/draft",
                             {"text": text, "version": int(doc.get("draft_version") or 0)})
        assert status == 200, out
        assert r.draft("i@x.org") == text

    # Sent: answered 200 from the mail that went out.
    key = KEY.format(46)
    status, msg = r.send("i@x.org", {"text": words, "request_key": key})
    assert status == 200 and r.draft("i@x.org") is None, msg
    autosave(words)
    mails = len(r.smtp.mails)
    status, again = r.send("i@x.org", {"text": words, "request_key": key})
    assert status == 200 and again["id"] == msg["id"] and len(r.smtp.mails) == mails, again
    assert r.draft("i@x.org") is None, "the sent mail is not left as the draft"
    # ... and a draft of other words, written since, stays.
    autosave("Something else")
    assert r.send("i@x.org", {"text": words, "request_key": key})[0] == 200
    assert r.draft("i@x.org") == "Something else"
    r.wg._CHAT_STATE.clear_draft(cid)
    # Queued: answered 202 {pending} from the send that waits — what a
    # press leaves where its release cannot be given (_mail_send_pressed,
    # left out here to make one).
    r.policy("verify")
    pressed, r.wg._mail_send_pressed = r.wg._mail_send_pressed, r.wg._mail_send
    try:
        key = KEY.format(47)
        status, out = r.send("i@x.org", {"text": "Queued words", "request_key": key})
        assert status == 202 and out["pending"]["status"] == "pending", out
        autosave("Queued words")
        status, again = r.send("i@x.org", {"text": "Queued words", "request_key": key})
        assert status == 202 and again["pending"]["request_id"] == out["pending"]["request_id"]
        assert r.draft("i@x.org") is None, "the queued mail is not left as the draft"
        r.gateway("POST", f"/pending-sends/{out['pending']['request_id']}/retract")
    finally:
        r.wg._mail_send_pressed = pressed
        r.policy("trust")
    # Not confirmed: answered 202 {unconfirmed} from the send nobody knows of.
    key = KEY.format(48)
    r.smtp.drop, r.smtp.takes = "dot", True
    try:
        status, out = r.send("i@x.org", {"text": "Lost words", "request_key": key})
    finally:
        r.smtp.drop = None
    assert status == 202 and out["unconfirmed"] is True, out
    autosave("Lost words")
    status, again = r.send("i@x.org", {"text": "Lost words", "request_key": key})
    assert status == 202 and again["unconfirmed"] is True, again
    assert r.draft("i@x.org") is None, "the mail that may be out is not left as the draft"
    (entry,) = r.entries(key)
    assert r.http("POST", f"/chats/{mail._quote(cid)}/pending/{entry['id']}/dismiss",
                  {})[0] == 200


def test_a_new_attempt_under_a_kept_key_leaves_a_draft_typed_since(r: Rig):
    """Decision I is about the words, not about the gateway's "repeat". A
    press is not confirmed; its send then fails for certain; the user types
    something new; "Check again" on the old bubble is a new attempt at the
    gateway — the mail goes out now, answered as no repeat — and the draft
    typed meanwhile stays."""
    r.arrives("j@x.org", day=1)
    key = KEY.format(49)
    body = {"text": "First words", "request_key": key}
    r.draft("j@x.org", body["text"])
    saved, r.wg.MAIL_SEND_TIMEOUT = r.wg.MAIL_SEND_TIMEOUT, 0.4
    r.smtp.refuse_login = True
    try:
        with _SlowSmtp(r) as smtp:
            status, out = r.send("j@x.org", body)
            assert smtp.entered.is_set()
            assert status == 202 and out["unconfirmed"] is True, out
            assert r.draft("j@x.org") is None, "the press's own words go"
            (entry,) = r.entries(key)
        assert _wait_for(lambda: (r.gw.SENDS.get(entry["id"]) or {}).get("status") == "error")
        assert r.gw._failed_for_certain(r.gw.SENDS.get(entry["id"]))
    finally:
        r.smtp.refuse_login = False
        r.wg.MAIL_SEND_TIMEOUT = saved
    r.draft("j@x.org", "typed since")
    mails = len(r.smtp.mails)
    status, msg = r.send("j@x.org", body)
    assert status == 200 and len(r.smtp.mails) == mails + 1, msg
    assert r.entries(key)[-1]["message_id"] == msg["id"]
    assert r.draft("j@x.org") == "typed since", "a draft written since the press stays"
    r.wg._CHAT_STATE.clear_draft(r.chat("j@x.org"))


def test_an_allowed_mail_whose_answer_was_lost_is_not_worded_as_failed(r: Rig):
    r.arrives("f@x.org", day=1)
    r.policy("verify")
    # (A press whose release could not be given: _mail_send_pressed left out.)
    pressed, r.wg._mail_send_pressed = r.wg._mail_send_pressed, r.wg._mail_send
    try:
        # The user's own reply, queued under the policy ...
        status, out = r.send("f@x.org", {"text": "Queued reply", "request_key": KEY.format(12)})
        assert status == 202 and out["pending"]["status"] == "pending", out
        rid = out["pending"]["request_id"]
        # ... and an agent's, queued from a dashboard thread.
        conv = r.wg._new_conv("user", r.wg.DEFAULT_SESSION_KEY, "Offer", "agent", "hello")
        status, queued = r.gateway("POST", "/send", {
            "recipient": "f@x.org", "message": "From Ara", "author": "agent",
            "thread": conv["id"]})
        assert status == 202, queued
        mails = len(r.smtp.mails)
        # Allowed on /sends; the SMTP server takes the mail and its answer
        # is lost.
        r.smtp.drop, r.smtp.takes = "dot", False
        try:
            assert r.press(f"/sends/{SLUG}/{rid}/approve") in (302, 303)
            assert r.press(f"/sends/{SLUG}/{queued['request_id']}/approve") in (302, 303)
        finally:
            r.smtp.drop, r.smtp.takes = None, True
        assert len(r.smtp.mails) == mails
    finally:
        r.wg._mail_send_pressed = pressed
        r.policy("trust")
    status, entry = r.gateway("GET", f"/pending-sends/{rid}")
    assert (entry["status"], entry["outcome_unknown"]) == ("error", True), entry
    # The chat: failed to be confirmed — and it may have gone out.
    pending = {q["request_id"]: q for q in r.messages("f@x.org")["pending"]}
    assert (pending[rid]["status"], pending[rid]["outcome_unknown"]) == ("failed", True)
    # The approval page and what it polls: "not confirmed", never "could
    # not deliver".
    page = r.page(f"/sends/{SLUG}/{rid}")
    shown = html.unescape(re.search(r'id="st-note"[^>]*>([^<]*)<', page).group(1))
    assert shown.startswith("Not confirmed — it is not known whether this went out"), shown
    assert "could not deliver" not in shown and UNKNOWN in shown
    status, polled = r.http("GET", f"/sends/{SLUG}/{rid}/status")
    assert status == 200 and polled["status"] == "error", polled
    assert polled["outcome_unknown"] is True and polled["note"] == shown, polled
    # The thread that queued the agent's mail: told what is known and what
    # not to do — not that sending failed, which is what gets a mail sent twice.
    def noted():
        return next((m for m in r.wg._load_conv(conv["id"])["messages"]
                     if m["text"].startswith("⚠️ You allowed the e-mail")), None)
    assert _wait_for(lambda: noted() is not None), r.wg._load_conv(conv["id"])["messages"]
    note = noted()
    assert "it is not known whether it went out" in note["text"], note
    assert "Do not send it again" in note["text"] and "failed" not in note["text"], note
    assert "outcome unknown" in note["context"] and "NOT a failed send" in note["context"]
    for request_id in (rid, queued["request_id"]):
        status, out = r.http(
            "POST", f"/chats/{mail._quote(r.chat('f@x.org'))}/pending/{request_id}/dismiss", {})
        assert status == 200, out
    assert r.messages("f@x.org")["pending"] == []


# ── Writing across the gate ──────────────────────────────────────────────────

def test_a_reply_settles_an_arrival_the_user_was_never_told_of(r: Rig):
    """A mail counted and owed (its first offer died before it was told,
    docs/mail-chats.md, "The gate"), then answered from the composer: the
    reply reads it, here and in the mailbox, and the mail offered again is
    the repeat it now is — no arrival for the model, nothing reopened."""
    root = "u@x.org"
    real, failed = r.wg._attention_arrive_chat, []

    def dies_once(*args, **kwargs):
        if not failed:
            failed.append(True)
            raise OSError("No space left on device")
        return real(*args, **kwargs)
    r.wg._attention_arrive_chat = dies_once
    try:
        r.arrives(root, day=1)
    finally:
        r.wg._attention_arrive_chat = real
    doc = r.wg._CHAT_STATE.get(r.chat(root))
    assert failed and mail.cs.owes(doc, root), doc
    assert ("offer", "in", root, "unanswered") in r.rail, r.rail
    r.draft(root, "On it")
    status, msg = r.send(root, {"text": "On it", "request_key": KEY.format(20)})
    assert status == 200, msg
    r.settle()
    assert r.seen(root) and r.listed(root)["unread"] == 0
    before = r.wg._CHAT_STATE.get(r.chat(root)).get("attention")
    arrived = []
    r.wg._attention_arrive_chat = lambda *a, **k: arrived.append(a) or real(*a, **k)
    try:
        assert r.w.mirror._offer(root)
    finally:
        r.wg._attention_arrive_chat = real
    assert r.rail[-1] == ("offer", "in", root, "ok"), r.rail
    assert arrived == [], "a mail the user answered is no arrival"
    after = r.wg._CHAT_STATE.get(r.chat(root))
    assert after.get("attention") == before and r.listed(root)["unread"] == 0, after


def test_an_agents_mail_reads_nothing(r: Rig):
    """An instructed turn answers with scripts/mail-push.py --chat: a keyed
    /send without an author. It goes out once — its repeat is answered as
    one — and, the agent's and not the user's, it reads nothing: the mails
    stay unread here and unseen in the mailbox."""
    root = "v@x.org"
    r.arrives(root, day=1)
    r.arrives("late-" + root, irt=root, refs=[root], day=2, subject="Re: Quarterly numbers")
    press = {"recipient": root, "account": r.account, "message": "Filed, thanks.",
             "user_approved": True, "request_key": KEY.format(21)}
    status, answer = r.gateway("POST", "/send", press)
    assert status == 200 and answer["status"] == "sent", answer
    status, again = r.gateway("POST", "/send", press)
    assert status == 200 and again.get("repeat") is True, again
    r.loop()
    r.settle()
    assert ("inbound", "out", answer["message_id"], 200) in r.rail, r.rail
    assert r.listed(root)["unread"] == 2 and r.wg._CHAT_STATE.get(r.chat(root))["last_read"] is None
    assert not r.seen(root) and not r.seen("late-" + root)


# ── Delete ───────────────────────────────────────────────────────────────────

def test_a_delete_on_a_mailbox_without_a_trash_says_why(r: Rig):
    r.arrives("e@x.org", day=1)
    path = "/chats/" + mail._quote(r.chat("e@x.org")) + "/delete"
    trash = r.box.boxes.pop("Trash")
    try:
        status, out = r.http("POST", path, {})
    finally:
        r.box.boxes["Trash"] = trash
    # Refused — nothing is erased while the gateway has not said all of it
    # is done — and the reason is the gateway's own: what is missing, and
    # which variable names it.
    assert status == 502 and out["error"].startswith(
        "the chat's messages could not be completely erased (mail-gateway: the mailbox "
        "has no Trash folder"), out
    assert "MAIL_TRASH_FOLDER" in out["error"] and "file(s)" not in out["error"], out
    assert "\n" not in out["error"]
    assert r.chat("e@x.org") in mail._chats(r.base)
    assert "e@x.org" in S.held_in(r.box, "INBOX") and S.held_in(r.box, "Trash") == []
    # With a Trash the same delete goes through: the mail is moved there.
    status, out = r.http("POST", path, {})
    assert status == 200 and out["deleted"] is True, out
    assert out["gateways"][SLUG]["trashed"] == 1 and out["gateways"][SLUG]["errors"] == 0
    assert "e@x.org" in S.held_in(r.box, "Trash") and "e@x.org" not in S.held_in(r.box, "INBOX")
    assert r.chat("e@x.org") not in mail._chats(r.base)


# ── Spam ─────────────────────────────────────────────────────────────────────

def test_spam_sends_the_mails_to_junk_and_puts_the_chat_away(r: Rig):
    root = "spam@x.org"
    r.arrives(root, day=1, subject="Cheap watches")
    r.arrives("more-" + root, irt=root, refs=[root], day=2, subject="Re: Cheap watches")
    chat = r.chat(root)
    path = "/chats/" + mail._quote(chat) + "/spam"
    assert r.listed(root)["list_group"] == "unread"
    # No Junk folder: the gateway's sentence, and nothing changed anywhere.
    status, out = r.http("POST", path, None, {"Sec-Fetch-Site": "same-origin"})
    assert status == 502 and "MAIL_JUNK_FOLDER" in out["error"], out
    assert "\n" not in out["error"]
    assert r.listed(root)["list_group"] == "unread"
    doc = r.wg._CHAT_STATE.get(chat)
    assert not doc.get("muted") and not doc.get("archived")
    assert sorted(S.held_in(r.box, "INBOX")).count(root) == 1
    # With one: the mails are in Junk, the chat is muted and archived.
    r.box.boxes["Junk"] = {"validity": 700, "next": 1, "mails": {}}
    try:
        status, out = r.http("POST", path, None, {"Sec-Fetch-Site": "same-origin"})
        assert status == 200 and out["muted"] is True and out["archived"] is True, out
        assert out["moved"] == 2, out
        assert sorted(S.held_in(r.box, "Junk")) == sorted([root, "more-" + root])
        assert root not in S.held_in(r.box, "INBOX")
        r.settle()
        listed = r.listed(root)
        assert listed["list_group"] == "archived" and listed["muted"] is True, listed
        # The mirror's next pass sees them in no mirrored folder; nothing it
        # does brings the chat back.
        r.w.mirror.poll()
        r.settle()
        assert r.listed(root)["list_group"] == "archived"
        # Restore: the chat is back; the mails stay in Junk (the mailbox
        # follows the chat, never back).
        status, out = r.http("POST", "/chats/" + mail._quote(chat) + "/flags",
                             {"archived": False, "muted": False})
        assert status == 200 and out["archived"] is False and out["muted"] is False, out
        r.settle()
        assert r.listed(root)["list_group"] != "archived"
        assert sorted(S.held_in(r.box, "Junk")) == sorted([root, "more-" + root])
        assert root not in S.held_in(r.box, "INBOX")
    finally:
        r.box.boxes.pop("Junk", None)


# What a check needs from the mail gateway beyond what the web gateway does
# by itself: said with a failure, so a red line names the side it is on.
NEEDS = {
    "test_the_same_press_goes_through_once_the_server_is_back":
        "the mail gateway's half of decision E (a key whose send failed for certain is "
        "tried afresh)",
    "test_the_gateway_says_when_it_answers_a_repeat":
        "the mail gateway's half of decision F (\"repeat\": true in an answer given from "
        "an earlier entry)",
}


def main() -> int:
    tests = [(name, check) for name, check in globals().items()
             if name.startswith("test_") and callable(check)]
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        rig = Rig(Path(tmp))
        try:
            for name, check in tests:
                try:
                    check(rig)
                    print(f"PASS {name}")
                except Exception as exc:  # noqa: BLE001
                    failures += 1
                    print(f"FAIL {name}: {exc}"
                          + (f"\n     needs {NEEDS[name]}" if name in NEEDS else ""))
                    traceback.print_exc()
                    # What a failed check left set must not fail the next.
                    rig.smtp.refuse_login, rig.smtp.reject_data = False, None
                    rig.smtp.drop, rig.smtp.takes = None, True
                    rig.policy("trust")
        finally:
            rig.close()
    if failures:
        print(f"\n{failures} end-to-end mail check(s) failed.")
        return 1
    print("\nall end-to-end mail checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
