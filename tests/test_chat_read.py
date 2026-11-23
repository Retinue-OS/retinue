#!/usr/bin/env python3
"""Unread · Read · Archived: what makes a chat read, on every channel.

A chat is read only when its latest inbound message was **replied to** or
explicitly **marked read** — opening it reads nothing. These checks run the
real web-gateway handler (the harness of tests/test_chat_api.py) against a
mock store that answers the chat-list queries from a small ledger of records,
so the derivation is exercised end to end rather than canned:

- GET /chats: every chat states its `list_group` (archived wins, else unread
  > 0), `group` stays the boolean "a group chat", and `counts` are chats per
  group;
- the unread count is the inbound later than max(read mark, the user's own
  latest message): a reply from the dashboard, a device echo on the rail, an
  outbound record that is in the store only, and the live overlay all read
  the chat — an agent-authored message, wherever it comes from, does not;
- a new inbound message on a read chat makes it unread again, and still
  un-archives the chat unless it is muted;
- POST /chats/<id>/read is the explicit *Mark as read*, and its guarded undo:
  the mark goes back only while it is still the one being undone;
- marking read settles the chat's attention item as *Mark handled* does, the
  undo reopens it only if that mark is what settled it, and a mark that a
  newer message outran settles nothing;
- an instructed sender's held ring: opening the chat while the turn works is
  no read, marking it read is, a mark taken back before the ring is due is
  not — and one taken back afterwards puts the message on the list;
- the store away: a mark still reads the chat, and its undo still unreads it.

Standalone (stdlib + the gateway module's own deps):

    python3 tests/test_chat_read.py
"""
from __future__ import annotations

import re
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_chat_api as api  # noqa: E402  (reuses its harness and mock gateway)

_http, _quote, _wait_for, _await_job = api._http, api._quote, api._wait_for, api._await_job
PUSHES, TURNS, TURN_GATE = api.PUSHES, api.TURNS, api.TURN_GATE

RAIL = "/internal/chats/inbound"
NOW = int(time.time())

# The ledger the mock store answers from: one dict per message record, as the
# gateways write them. A test appends to it to put a record in the store
# without any rail event — a backfilled message.
LEDGER: list[dict] = []


def _at(offset: float) -> str:
    """An instant relative to the start of the run, as the ledger stamps it."""
    from datetime import datetime, timezone
    return (datetime.fromtimestamp(NOW + offset, tz=timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"))


def _store(key: str, offset: float, direction: str, text: str = "…", *,
           channel: str = "signal", account: str = "", author: str | None = None,
           mid: str | None = None) -> dict:
    record = {"chat": key, "account": account, "channel": channel, "ts": _at(offset),
              "dir": direction, "text": text, "author": author,
              "mid": mid or f"s{len(LEDGER) + 1}"}
    LEDGER.append(record)
    return record


class _LedgerSparql(api._MockSparql):
    """The life store, answering the chat queries from LEDGER.

    The reply-marks and unread queries are *evaluated* here rather than
    canned — which authors the query asks for, which cut-off it injects per
    chat — so a test fails if the gateway asks the wrong question, not only
    if it misreads the answer."""

    def _chat_list(self):
        heads: dict[tuple, dict] = {}
        for record in LEDGER:
            pair = (record["chat"], record["account"])
            if pair not in heads or record["ts"] >= heads[pair]["ts"]:
                heads[pair] = record
        return [api._lit_row(
            chat=r["chat"], account=r["account"] or None, channel=r["channel"],
            ts=r["ts"], type=api.T_OUT if r["dir"] == "out" else api.T_IN,
            text=r["text"], sender=r["chat"] if r["dir"] == "in" else None,
            author=r["author"], mid=r["mid"], atts="")
            for _pair, r in sorted(heads.items())]

    def _replied(self, query):
        asked = set(re.findall(r'"(\w+)"', query.split("VALUES ?author {", 1)[1].split("}", 1)[0]))
        latest: dict[tuple, str] = {}
        for record in LEDGER:
            if record["dir"] == "out" and record["author"] in asked:
                pair = (record["chat"], record["account"])
                latest[pair] = max(latest.get(pair, ""), record["ts"])
        return [api._lit_row(chat=key, account=account, replied=ts)
                for (key, account), ts in latest.items()]

    def _unread(self, query):
        rows = re.findall(
            r'\("((?:[^"\\]|\\.)*)"\s+"((?:[^"\\]|\\.)*)"\s+"([^"]+)"\^\^', query)
        out = []
        for key, account, cut in rows:
            n = sum(1 for r in LEDGER if r["dir"] == "in" and r["chat"] == key
                    and r["account"] == account and r["ts"] > cut)
            if n:  # a COUNT under GROUP BY has no row for an empty group
                out.append(api._lit_row(chat=key, account=account, n=str(n)))
        return out

    def _messages(self, query):
        key = re.search(r'k:chat "((?:[^"\\]|\\.)*)"', query)
        account = re.search(r'COALESCE\(\?acc0, ""\) = "([^"]*)"', query)
        if not key:
            return []
        rows = [r for r in LEDGER if r["chat"] == key.group(1)
                and r["account"] == (account.group(1) if account else "")]
        rows.sort(key=lambda r: r["ts"], reverse=True)
        return [api._lit_row(
            m=f"urn:mock:record:{r['mid']}", ts=r["ts"], text=r["text"], mid=r["mid"],
            type=api.T_OUT if r["dir"] == "out" else api.T_IN,
            sender=r["chat"] if r["dir"] == "in" else None, author=r["author"])
            for r in rows]


def _chats(base):
    status, body = _http(base, "GET", "/chats")
    assert status == 200, body
    return body


def _chat(base, chat_id):
    return next(c for c in _chats(base)["chats"] if c["id"] == chat_id)


def _read(base, chat_id, body=None):
    return _http(base, "POST", "/chats/" + _quote(chat_id) + "/read", body or {})


def _arrive(base, key, offset, text="hoi", *, channel="signal", mid=None, **extra):
    event = {"direction": "in", "channel": channel, "chat": key, "sender": key,
             "sender_name": extra.pop("name", None) or f"Peer {key[-2:]}", "group": False,
             "message_id": mid or f"r{key[-3:]}-{offset}", "text": text,
             "ts": NOW + offset, "gate": {"forward": True, "reason": "open"}}
    event.update(extra)
    return _http(base, "POST", RAIL, event)


def test_three_groups(base, wg):
    """GET /chats sorts every chat into one of three groups, from the store
    alone: no rail event has passed for any of these chats."""
    a, b, c, d, e = ("+41791000001", "+41791000002", "+41791000003",
                     "+41791000004", "+41791000005")
    grp = "120363000000000001@g.us"
    _store(a, -3600, "in")
    _store(a, -3500, "in")                       # two unanswered: unread
    _store(b, -3600, "in")
    _store(b, -3500, "out", author="device")     # answered from the phone: read
    _store(c, -3600, "in")                       # unanswered, but archived below
    _store(d, -3600, "in")
    _store(d, -3500, "out", author="agent")      # an agent's answer reads nothing
    _store(e, -3600, "in")
    _store(e, -3500, "out", author="user")
    _store(e, -3400, "in")                       # written again after the reply
    _store(grp, -3600, "in", channel="whatsapp")
    status, _ = _http(base, "POST", "/chats/" + _quote("signal:" + c) + "/flags",
                      {"archived": True})
    assert status == 200

    body = _chats(base)
    rows = {ch["id"]: ch for ch in body["chats"]}
    got = {key: (rows["signal:" + key]["list_group"], rows["signal:" + key]["unread"])
           for key in (a, b, c, d, e)}
    assert got == {a: ("unread", 2), b: ("read", 0), c: ("archived", 1),
                   d: ("unread", 1), e: ("unread", 1)}, got
    # `group` is still the boolean "a group chat", beside the new field.
    g = rows["whatsapp:" + grp]
    assert g["group"] is True and g["list_group"] == "unread", g
    assert rows["signal:" + a]["group"] is False
    # Chats per group, and every chat is in exactly one.
    assert body["counts"] == {"unread": 4, "read": 1, "archived": 1}, body["counts"]
    assert sum(body["counts"].values()) == len(body["chats"])
    # None of them carries a read mark: the store's replies read B and E.
    assert all(rows["signal:" + key]["last_read"] is None for key in (a, b, c, d, e))
    # The reply marks were asked for the user's own authorship only, and went
    # into the unread query as the cut-off of the chats that have one.
    replied_q = [q for q in api.STATE["queries"] if "VALUES ?author" in q][-1]
    assert 'VALUES ?author { "user" "device" }' in replied_q, replied_q
    unread_q = [q for q in api.STATE["queries"] if "VALUES (?chat ?account ?cut)" in q][-1]
    assert f'("{b}" "" "{_at(-3500)}"' in unread_q, unread_q
    assert f'("{d}" "" "1970-01-01T00:00:00Z"' in unread_q, "an agent's message set a cut-off"

    # Opening a chat reads nothing: the page's summary states the same three
    # fields as the list, and the list is unchanged afterwards.
    status, page = _http(base, "GET", rows["signal:" + a]["messages"])
    assert status == 200 and len(page["messages"]) == 2, page
    assert page["chat"]["unread"] == 2 and page["chat"]["list_group"] == "unread"
    assert "last_read" in page["chat"] and page["chat"]["last_read"] is None
    assert _chat(base, "signal:" + a)["unread"] == 2
    # A chat the list does not hold still gets the three fields.
    status, page = _http(base, "GET", "/chats/" + _quote("signal:+41791999999") + "/messages")
    assert status == 200, page
    assert (page["chat"]["unread"], page["chat"]["list_group"], page["chat"]["last_read"]) \
        == (0, "read", None), page["chat"]
    print("PASS test_three_groups")


def test_a_reply_reads_the_chat(base, wg):
    """Every way the user answers reads the chat; an agent's message never
    does; and the next inbound message makes it unread again."""
    sent, echo, agent, live, live_agent = ("+41791000011", "+41791000012", "+41791000013",
                                           "+41791000014", "+41791000015")
    for key in (sent, echo, agent, live, live_agent):
        _store(key, -3000, "in")
        assert _chat(base, "signal:" + key)["list_group"] == "unread"

    # From the dashboard: the send itself, before the store knows the record.
    status, msg = _http(base, "POST", "/chats/" + _quote("signal:" + sent) + "/send",
                        {"text": "bis Samstag!"})
    assert status == 200, msg
    row = _chat(base, "signal:" + sent)
    assert (row["list_group"], row["unread"]) == ("read", 0), row
    assert row["last_read"] == msg["ts"]

    # From the user's own device, echoed on the rail.
    status, _ = _http(base, "POST", RAIL, {
        "direction": "out", "channel": "signal", "chat": echo, "author": "device",
        "message_id": "echo-1", "text": "ok", "ts": NOW + 5})
    assert status == 200
    row = _chat(base, "signal:" + echo)
    assert (row["list_group"], row["unread"]) == ("read", 0), row

    # An agent sending on its own: the chat's last message is now outbound,
    # and the message the user never saw is still unread.
    status, _ = _http(base, "POST", RAIL, {
        "direction": "out", "channel": "signal", "chat": agent, "author": "agent",
        "message_id": "agent-1", "text": "Ara here", "ts": NOW + 5})
    assert status == 200
    row = _chat(base, "signal:" + agent)
    assert row["last"]["direction"] == "out" and row["last"]["author"] == "agent", row
    assert (row["list_group"], row["unread"]) == ("unread", 1), row
    assert wg._CHAT_STATE.get("signal:" + agent)["last_read"] is None

    # The live overlay is a source of the reply too, on its own: an entry the
    # store has not indexed and no read mark covers still reads the chat —
    # for the user's authorship, and not for an agent's.
    for key, author in ((live, "user"), (live_agent, "agent")):
        wg._CHAT_OVERLAY.insert({"chat_id": "signal:" + key, "channel": "signal",
                                 "direction": "out", "author": author, "text": "…",
                                 "ts": _at(5), "message_id": f"live-{author}"})
    assert wg._CHAT_STATE.get("signal:" + live)["last_read"] is None
    assert _chat(base, "signal:" + live)["list_group"] == "read"
    assert _chat(base, "signal:" + live_agent)["list_group"] == "unread"

    # A new inbound message on a read chat: unread again, counting only what
    # came after the reply.
    status, body = _arrive(base, sent, 600, "noch was", mid="again-1")
    assert status == 200, body
    row = _chat(base, "signal:" + sent)
    assert (row["list_group"], row["unread"]) == ("unread", 1), row
    # … and once the store has indexed it, it is still one message, not two.
    _store(sent, 600, "in", "noch was", mid="again-1")
    assert _chat(base, "signal:" + sent)["unread"] == 1
    print("PASS test_a_reply_reads_the_chat")


def test_mark_as_read_and_undo(base, wg):
    key = "+41791000021"
    cid = "signal:" + key
    _store(key, -2000, "in", "eins")
    _store(key, -1900, "in", "zwei")
    t1, t2 = _at(-2000), _at(-1900)

    # A mark the newest message outran: read up to what the user saw, the
    # rest stays unread, and the answer says so.
    status, body = _read(base, cid, {"ts": t1})
    assert status == 200, body
    assert body == {"id": cid, "last_read": t1, "previous": None,
                    "unread": 1, "list_group": "unread"}, body
    # Marked by the newest message's own time: read.
    status, body = _read(base, cid, {"ts": t2})
    assert body == {"id": cid, "last_read": t2, "previous": t1,
                    "unread": 0, "list_group": "read"}, body
    assert _chat(base, cid)["list_group"] == "read"
    # Forward only: an older mark moves nothing.
    status, body = _read(base, cid, {"ts": t1})
    assert status == 200 and body["last_read"] == t2 and body["previous"] == t2, body

    # The undo puts the mark back where the call it undoes found it.
    status, body = _read(base, cid, {"restore": t1, "marked": t2})
    assert status == 200, body
    assert body == {"id": cid, "last_read": t1, "previous": t2,
                    "unread": 1, "list_group": "unread"}, body
    row = _chat(base, cid)
    assert (row["list_group"], row["unread"], row["last_read"]) == ("unread", 1, t1), row
    # Spent: the mark is no longer the one being undone.
    status, body = _read(base, cid, {"restore": None, "marked": t2})
    assert status == 409 and body["last_read"] == t1 and body["list_group"] == "unread", body
    assert wg._CHAT_STATE.get(cid)["last_read"] == t1, "a refused undo changed the mark"
    # Back to never read at all.
    status, body = _read(base, cid, {"restore": None, "marked": t1})
    assert status == 200 and body["last_read"] is None and body["unread"] == 2, body

    # With no ts the mark is "everything there is": now.
    before = wg.chat_state_mod.iso_z()
    status, body = _read(base, cid)
    assert status == 200 and body["previous"] is None, body
    assert body["last_read"] >= before and (body["unread"], body["list_group"]) == (0, "read")
    marked = body["last_read"]
    # A reply in between is newer word about the chat: the undo is refused
    # and nothing changes.
    status, _ = _http(base, "POST", RAIL, {
        "direction": "out", "channel": "signal", "chat": key, "author": "device",
        "message_id": "mk-echo", "text": "ok", "ts": NOW + 900})
    assert status == 200
    status, body = _read(base, cid, {"restore": None, "marked": marked})
    assert status == 409 and body["last_read"] == _at(900), body
    assert (body["unread"], body["list_group"]) == (0, "read"), body
    assert _chat(base, cid)["list_group"] == "read"

    # What the undo is not: an advance, or a guess.
    for bad in ({"restore": _at(990), "marked": _at(900)},   # later than the mark it undoes
                {"marked": _at(900)},                        # no restore named
                {"restore": None},                           # no mark named
                {"restore": None, "marked": "then"},
                {"restore": "then", "marked": _at(900)},
                {"restore": 5, "marked": _at(900)}):
        status, body = _read(base, cid, bad)
        assert status == 400, (bad, status, body)
    assert wg._CHAT_STATE.get(cid)["last_read"] == _at(900)

    # A message stamped ahead of this clock — the channel's clock, not ours —
    # is still covered by a mark that names no time.
    ahead = "+41791000022"
    _store(ahead, 5000, "in", "aus der Zukunft")
    status, body = _read(base, "signal:" + ahead)
    assert status == 200 and body["last_read"] == _at(5000), body
    assert (body["unread"], body["list_group"]) == (0, "read"), body

    # No state is invented for an undo of a chat nobody marked.
    status, _ = _read(base, "signal:+41791000029", {"restore": None, "marked": t1})
    assert status == 409 and not wg._CHAT_STATE.exists("signal:+41791000029")
    print("PASS test_mark_as_read_and_undo")


def test_an_arrival_unreads_and_unarchives(base, wg):
    """A new inbound message on a read chat makes it unread again, and brings
    an archived chat back — unless it is muted."""
    key = "+41791000031"
    cid = "signal:" + key
    flags = "/chats/" + _quote(cid) + "/flags"
    _store(key, -1500, "in")
    status, body = _read(base, cid, {"ts": _at(-1500)})
    assert body["list_group"] == "read", body

    status, _ = _arrive(base, key, 1000, "da bin ich wieder")
    row = _chat(base, cid)
    assert (row["list_group"], row["unread"]) == ("unread", 1), row
    # The read mark itself did not move: it is the message that is later.
    assert row["last_read"] == _at(-1500)

    # Read and archived: the next message returns it, to Unread.
    _read(base, cid, {"ts": _at(1000)})
    _http(base, "POST", flags, {"archived": True})
    assert _chat(base, cid)["list_group"] == "archived"
    _arrive(base, key, 1100, "und nochmal")
    row = _chat(base, cid)
    assert row["archived"] is False and (row["list_group"], row["unread"]) == ("unread", 1), row

    # Muted: it stays archived however much arrives, and marking it read
    # leaves it there.
    _http(base, "POST", flags, {"muted": True})
    _arrive(base, key, 1200, "hallo?")
    row = _chat(base, cid)
    assert row["archived"] is True and row["list_group"] == "archived" and row["unread"] == 2, row
    status, body = _read(base, cid)
    assert status == 200 and (body["unread"], body["list_group"]) == (0, "archived"), body
    counts = _chats(base)["counts"]
    assert counts["archived"] >= 1 and sum(counts.values()) == len(_chats(base)["chats"])
    print("PASS test_an_arrival_unreads_and_unarchives")


def _attention(wg, cid):
    return wg._CHAT_STATE.get(cid).get("attention") or {}


def test_mark_as_read_settles_attention(base, wg):
    """Marking read is the user saying "dealt with": the chat's attention item
    is settled as *Mark handled* settles it, and the undo reopens it — only
    if that mark is what settled it."""
    key = "+41791000041"
    cid = "signal:" + key
    _arrive(base, key, 2000, "Kannst du morgen?")
    assert _attention(wg, cid).get("state") == "open"

    status, body = _read(base, cid)
    assert status == 200 and body["list_group"] == "read", body
    block = _attention(wg, cid)
    assert block["state"] == "done" and block["done_how"] == "marked", block
    marked, previous = body["last_read"], body["previous"]
    status, body = _read(base, cid, {"restore": previous, "marked": marked})
    assert status == 200 and body["list_group"] == "unread", body
    block = _attention(wg, cid)
    assert block["state"] == "open" and block.get("done_how") is None, block
    assert wg._CHAT_STATE.get(cid)["read_settled"] is None

    # A mark the newest message outran settles nothing: that message's item
    # is not one the user has dismissed.
    _arrive(base, key, 2100, "Oder übermorgen?")
    status, body = _read(base, cid, {"ts": _at(2000)})
    assert status == 200 and body["unread"] == 1, body
    assert _attention(wg, cid)["state"] == "open", "a stale mark settled an unseen message"

    # Not this mark's item: settled by the mark, reopened by a new message,
    # settled again by *Mark handled* on the home screen. Undoing the old
    # mark moves the mark back and leaves that item alone.
    status, body = _read(base, cid, {"ts": _at(2100)})
    assert _attention(wg, cid)["done_how"] == "marked"
    marked, previous = body["last_read"], body["previous"]
    _arrive(base, key, 2200, "Hallo?")
    assert _attention(wg, cid)["state"] == "open"
    status, _ = _http(base, "POST", "/attention/items/done", {"id": "chat:" + cid})
    assert status == 200
    status, body = _read(base, cid, {"restore": previous, "marked": marked})
    assert status == 200 and body["last_read"] == previous, body
    block = _attention(wg, cid)
    assert block["state"] == "done" and block["done_how"] == "marked", \
        "the undo reopened an item Mark handled settled"

    # Nor a reply's: an item the answer settled keeps its own marker through
    # a mark and its undo.
    other = "+41791000042"
    oid = "signal:" + other
    _arrive(base, other, 2000, "Frage")
    status, _ = _http(base, "POST", RAIL, {
        "direction": "out", "channel": "signal", "chat": other, "author": "device",
        "message_id": "att-echo", "text": "Antwort", "ts": NOW + 2010})
    assert _attention(wg, oid)["done_how"] == "replied"
    status, body = _read(base, oid, {"ts": _at(2020)})
    assert status == 200 and _attention(wg, oid)["done_how"] == "replied"
    assert wg._CHAT_STATE.get(oid)["read_settled"] is None
    status, body = _read(base, oid, {"restore": body["previous"], "marked": body["last_read"]})
    assert status == 200 and _attention(wg, oid)["state"] == "done", _attention(wg, oid)
    # The undo cannot make an answered chat unread: the reply stands.
    assert (body["unread"], body["list_group"]) == (0, "read"), body

    # The same for a message that lands while the mark is being written: the
    # mark carries the arrival count it found, and an item that has taken in
    # (or is about to take in) a later arrival is left open.
    raced = "+41791000044"
    rid = "signal:" + raced
    _arrive(base, raced, 2000, "eins")
    seq = wg._CHAT_STATE.get(rid)["inbound_seq"]
    assert wg._attention_settle_read(rid, seq - 1) is None
    assert _attention(wg, rid)["state"] == "open", "a mark settled an arrival it never saw"
    assert wg._attention_settle_read(rid, seq) == _attention(wg, rid)["done_at"]
    assert _attention(wg, rid)["done_how"] == "marked"

    # A chat nothing was pending in gains no item by being marked.
    quiet = "+41791000043"
    _store(quiet, -1000, "in")
    status, body = _read(base, "signal:" + quiet)
    assert status == 200 and body["list_group"] == "read"
    assert wg._CHAT_STATE.get("signal:" + quiet)["attention"] is None
    print("PASS test_mark_as_read_settles_attention")


def test_instructed_turn_and_the_read_mark(base, wg):
    """A held instructed arrival is not rung once the user has dealt with the
    chat while its turn worked — which now means a reply or an explicit mark,
    since opening the chat posts no read."""
    agent = {"X-Conversation-Backend-Token": "agent-token"}
    chambers = Path(wg._CONTACTS.chambers_dir)
    (chambers / "private").mkdir(parents=True, exist_ok=True)
    person = wg._CONTACTS.create("private", "Spitex", [("telegram", "889001")],
                                 instruction="Ring me only if a visit moves to today.")
    key, cid = "889001", "telegram:889001"
    event = {"direction": "in", "channel": "telegram", "chat": key, "sender": key,
             "sender_name": "Spitex", "group": False, "handover": True,
             "gate": {"forward": True, "vip": False, "instructed": True, "reason": "open"}}

    def verdict(importance):
        def _set(_prompt):
            status, body = _http(base, "POST", f"/internal/chats/{_quote(cid)}/verdict",
                                 {"importance": importance}, headers=agent)
            assert status == 200, body
        return _set

    def instructed():
        return _chat(base, cid).get("instructed") or {}

    def held_arrival(mid, offset, importance, while_working):
        """One instructed message whose turn is held open while
        ``while_working`` runs; returns once its job has resolved."""
        hold = threading.Event()
        PUSHES.clear()
        TURN_GATE.update(hold=hold, during=verdict(importance))
        try:
            status, body = _http(base, "POST", RAIL, dict(event, message_id=mid, text=mid,
                                                          ts=NOW + offset))
            assert status == 202 and body["pushed"] is False, body
            _wait_for(lambda: instructed().get("message_id") == mid
                      and instructed().get("verdict"), "the turn to set its verdict")
            while_working()
        finally:
            hold.set()
            TURN_GATE.update(hold=None, during=None)
        _await_job(base, body["job_url"])

    # Opening the chat while the turn works reads nothing: the message rings.
    def open_the_chat():
        status, page = _http(base, "GET", "/chats/" + _quote(cid) + "/messages")
        assert status == 200 and page["chat"]["unread"] == 1, page["chat"]

    held_arrival("opened", 3000, 4, open_the_chat)
    assert len(PUSHES) == 1, "a message the user only looked at was not rung"
    assert instructed()["delivery"] == "rang", instructed()

    # Marking it read while the turn works: nothing rings afterwards.
    held_arrival("marked", 3100, 4, lambda: _read(base, cid, {"ts": _at(3100)}))
    assert PUSHES == [], "a message marked read rang after its turn"
    assert instructed()["delivery"] == "seen" and "marked" in instructed()["reason"], instructed()

    # Marked and taken back while the turn still works: the mark is behind
    # the message again, so it rings by the verdict as if never marked.
    def mark_and_undo():
        status, body = _read(base, cid, {"ts": _at(3200)})
        assert status == 200 and body["unread"] == 0, body
        status, body = _read(base, cid, {"restore": body["previous"], "marked": body["last_read"]})
        assert status == 200 and body["unread"] == 1, body

    held_arrival("undone", 3200, 4, mark_and_undo)
    assert len(PUSHES) == 1, "a mark the user took back kept the message from ringing"
    assert instructed()["delivery"] == "rang", instructed()
    assert _attention(wg, cid)["state"] == "open"

    # Marked while the turn works, undone after it ended: the message was
    # delivered as seen and never listed, so the undo lists it — with the
    # verdict's judgement, and without ringing. The item is settled by hand
    # first, so that it is the instructed message, not the mark's own item,
    # that brings it back.
    status, _ = _http(base, "POST", "/attention/items/done", {"id": "chat:" + cid})
    assert status == 200 and _attention(wg, cid)["state"] == "done"
    marks = {}

    def mark():
        status, body = _read(base, cid, {"ts": _at(3300)})
        assert status == 200 and body["unread"] == 0, body
        marks.update(body)

    held_arrival("late-undo", 3300, 5, mark)
    assert PUSHES == [] and instructed()["delivery"] == "seen", instructed()
    assert wg._CHAT_STATE.get(cid)["read_settled"] is None, "the mark settled nothing"
    assert _attention(wg, cid)["state"] == "done"
    status, body = _read(base, cid, {"restore": marks["previous"], "marked": marks["last_read"]})
    # (Two unread: this message and the one before it, whose mark was undone.)
    assert status == 200 and (body["unread"], body["list_group"]) == (2, "unread"), body
    block = _attention(wg, cid)
    assert block["state"] == "open", "an unread instructed message is on no list"
    assert block["importance"] == 5 and block["importance_from"] == "instruction", block
    assert PUSHES == [], "the undo rang"
    ins = instructed()
    assert ins["delivery"] == "list" and "undone" in ins["reason"], ins
    wg._CONTACTS.delete(person["key"])
    print("PASS test_instructed_turn_and_the_read_mark")


def test_store_away(base, wg):
    """With the store unable to answer, the list is served from the last good
    skeleton — and a mark made meanwhile still reads the chat, its undo still
    unreads it."""
    key = "+41791000051"
    cid = "signal:" + key
    _store(key, -800, "in")
    _store(key, -700, "in")
    assert _chat(base, cid)["unread"] == 2
    wg.CHAT_LIST_CACHE_SECONDS = 30.0
    wg._chats_cache_invalidate()
    api.STATE["fail"] = True
    try:
        assert _chat(base, cid)["unread"] == 2
        status, body = _read(base, cid, {"ts": _at(-700)})
        assert status == 200 and (body["unread"], body["list_group"]) == (0, "read"), body
        assert _chat(base, cid)["list_group"] == "read"
        status, body = _read(base, cid, {"restore": None, "marked": _at(-700)})
        assert status == 200, body
        # How many is the store's to say; that there is one, the head tells.
        assert body["unread"] >= 1 and body["list_group"] == "unread", body
        assert _chat(base, cid)["list_group"] == "unread"
    finally:
        api.STATE["fail"] = False
        wg.CHAT_LIST_CACHE_SECONDS = 0.0
        wg._chats_cache_clear()
    assert _chat(base, cid)["unread"] == 2
    print("PASS test_store_away")


def main():
    sparql = api._serve(_LedgerSparql)
    gw = api._serve(api._MockGateway)
    api.STATE["gw_port"] = gw.server_address[1]
    with tempfile.TemporaryDirectory() as tmp:
        wg = api._load_gateway(Path(tmp), sparql.server_address[1], gw.server_address[1])
        # Capture pushes and model turns instead of making them.
        wg.push_notify.enabled = lambda: True
        wg.push_notify.notify_async = lambda *a, **k: PUSHES.append((a, k))
        wg.send_message = api._stub_send_message
        server = ThreadingHTTPServer(("127.0.0.1", 0), wg.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"

        test_three_groups(base, wg)
        test_a_reply_reads_the_chat(base, wg)
        test_mark_as_read_and_undo(base, wg)
        test_an_arrival_unreads_and_unarchives(base, wg)
        test_mark_as_read_settles_attention(base, wg)
        test_instructed_turn_and_the_read_mark(base, wg)
        test_store_away(base, wg)
        server.shutdown()
    sparql.shutdown()
    gw.shutdown()
    print("all chat-read tests passed")


if __name__ == "__main__":
    main()
