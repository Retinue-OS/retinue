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
- a client marks what it showed (`seen`, the summary's `inbound_seq`): a
  message that has landed since refuses the mark, 409, and nothing is read or
  settled unseen;
- arrival order beside the timestamps: a message that lands after a read is
  unread whatever its stamp — a delayed delivery, the mark's own second — and
  that survives the store indexing it, the undo, and the store being away;
- an arrival is counted once: a rail event repeated for one message (a
  retried POST, a redelivery) is answered as before and is no second
  arrival, for the unread count, the attention model and a held ring alike;
- no message's stamp moves the mark: an arrival from ahead of this clock is
  taken as now, and the mark is this clock's own, never later;
- a message stamped a little ahead of this clock is read with the chat, and
  stays read once the store answers with its record — up to the skew a
  clock is allowed, and the undo takes exactly that back;
- the reply-marks query failing alone leaves the list standing, and it is
  given a whole query's timeout unless a rebuild's deadline says otherwise;
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

import contextlib
import io
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
# The run's zero, two hours back: every offset a test stamps a message with
# (up to +5000) then lies in the past, as a real message's stamp does. One
# from ahead of the gateway's clock is not kept — the rail takes it as now,
# and the read endpoint refuses it — so those cases stamp by _clock instead.
T0 = int(time.time()) - 7200

# The ledger the mock store answers from: one dict per message record, as the
# gateways write them. A test appends to it to put a record in the store
# without any rail event — a backfilled message.
LEDGER: list[dict] = []


def _iso(epoch: float) -> str:
    from datetime import datetime, timezone
    return (datetime.fromtimestamp(epoch, tz=timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"))


def _at(offset: float) -> str:
    """An instant relative to the run's zero, as the ledger stamps it."""
    return _iso(T0 + offset)


def _clock(offset: float = 0.0) -> str:
    """An instant relative to the clock as it reads right now."""
    return _iso(time.time() + offset)


def _epoch(iso: str) -> float:
    from datetime import datetime
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _store(key: str, offset: float, direction: str, text: str = "…", *,
           channel: str = "signal", account: str = "", author: str | None = None,
           mid: str | None = None, ts: str | None = None) -> dict:
    record = {"chat": key, "account": account, "channel": channel,
              "ts": ts or _at(offset),
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
             "ts": T0 + offset, "gate": {"forward": True, "reason": "open"}}
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
    # Both carry the arrival count a mark hands back as `seen`: none has
    # passed the rail for these chats.
    assert rows["signal:" + a]["inbound_seq"] == 0 and page["chat"]["inbound_seq"] == 0
    # A chat the list does not hold still gets the same fields.
    status, page = _http(base, "GET", "/chats/" + _quote("signal:+41791999999") + "/messages")
    assert status == 200, page
    assert (page["chat"]["unread"], page["chat"]["list_group"], page["chat"]["last_read"],
            page["chat"]["inbound_seq"]) == (0, "read", None, 0), page["chat"]
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
        "message_id": "echo-1", "text": "ok", "ts": T0 + 5})
    assert status == 200
    row = _chat(base, "signal:" + echo)
    assert (row["list_group"], row["unread"]) == ("read", 0), row

    # An agent sending on its own: the chat's last message is now outbound,
    # and the message the user never saw is still unread.
    status, _ = _http(base, "POST", RAIL, {
        "direction": "out", "channel": "signal", "chat": agent, "author": "agent",
        "message_id": "agent-1", "text": "Ara here", "ts": T0 + 5})
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
    status, body = _arrive(base, echo, 600, "noch was", mid="again-1")
    assert status == 200, body
    row = _chat(base, "signal:" + echo)
    assert (row["list_group"], row["unread"]) == ("unread", 1), row
    # … and once the store has indexed it, it is still one message, not two:
    # the count by timestamp and the count of arrivals are two views of it.
    _store(echo, 600, "in", "noch was", mid="again-1")
    assert _chat(base, "signal:" + echo)["unread"] == 1
    # The same for one stamped before the reply it follows — the reply went
    # out a moment ago, this message is stamped an hour back.
    status, body = _arrive(base, sent, 600, "noch was", mid="again-2")
    assert status == 200, body
    row = _chat(base, "signal:" + sent)
    assert (row["list_group"], row["unread"]) == ("unread", 1), row
    _store(sent, 600, "in", "noch was", mid="again-2")
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

    # With no ts the mark is "everything there is": this clock's now.
    before = _clock()
    status, body = _read(base, cid)
    assert status == 200 and body["previous"] is None, body
    assert before <= body["last_read"] <= _clock(), body
    assert (body["unread"], body["list_group"]) == (0, "read")
    marked = body["last_read"]
    # A reply in between is newer word about the chat: the undo is refused
    # and nothing changes. (Stamped by the phone, a moment after the mark.)
    replied = _iso(_epoch(marked) + 2)
    status, _ = _http(base, "POST", RAIL, {
        "direction": "out", "channel": "signal", "chat": key, "author": "device",
        "message_id": "mk-echo", "text": "ok", "ts": _epoch(replied)})
    assert status == 200
    status, body = _read(base, cid, {"restore": None, "marked": marked})
    assert status == 409 and body["last_read"] == replied, body
    assert (body["unread"], body["list_group"]) == (0, "read"), body
    assert _chat(base, cid)["list_group"] == "read"

    # What the undo is not: an advance, or a guess.
    for bad in ({"restore": _iso(_epoch(replied) + 90), "marked": replied},  # later than its mark
                {"marked": replied},                         # no restore named
                {"restore": None},                           # no mark named
                {"restore": None, "marked": "then"},
                {"restore": "then", "marked": replied},
                {"restore": 5, "marked": replied},
                {"restore": "2026-02-30T10:00:00Z", "marked": replied},  # no such day
                {"restore": None, "marked": "2026-02-30T10:00:00Z"}):
        status, body = _read(base, cid, bad)
        assert status == 400, (bad, status, body)
    assert wg._CHAT_STATE.get(cid)["last_read"] == replied

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
        "message_id": "att-echo", "text": "Antwort", "ts": T0 + 2010})
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
                                                          ts=T0 + offset))
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

    # Nor is a mark for what the page showed before the message landed: it
    # is refused, moves nothing, and the message rings.
    shown = wg._CHAT_STATE.get(cid)["inbound_seq"]

    def mark_what_was_shown():
        status, body = _read(base, cid, {"seen": shown})
        assert status == 409 and body["unread"] == 2, body

    held_arrival("unseen", 3050, 4, mark_what_was_shown)
    assert len(PUSHES) == 1, "a message marked read unseen was not rung"
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

    # The same for a message delivered late, stamped behind the mark that
    # stands (3100): there the mark's instant says nothing — it was past the
    # message before the message landed — and what tells a read that covers
    # it from one taken back is the order things happened in.
    def mark_seen_and_undo():
        seq = wg._CHAT_STATE.get(cid)["inbound_seq"]
        status, body = _read(base, cid, {"seen": seq})
        assert status == 200 and body["unread"] == 0, body
        status, body = _read(base, cid, {"restore": body["previous"], "marked": body["last_read"]})
        assert status == 200 and body["last_read"] == _at(3100), body
        # The late message, and the two before it whose marks were undone.
        assert (body["unread"], body["list_group"]) == (3, "unread"), body

    held_arrival("late-stamped", 3090, 4, mark_seen_and_undo)
    assert len(PUSHES) == 1, "a late message whose mark was taken back did not ring"
    assert instructed()["delivery"] == "rang", instructed()
    # Nor does a read naming an instant behind the mark count for it.
    held_arrival("late-stale-read", 3095, 4, lambda: _read(base, cid, {"ts": _at(3000)}))
    assert len(PUSHES) == 1, "a stale read kept a late message from ringing"
    # One that covers it does.
    held_arrival("late-read", 3096, 4,
                 lambda: _read(base, cid, {"seen": wg._CHAT_STATE.get(cid)["inbound_seq"]}))
    assert PUSHES == [] and instructed()["delivery"] == "seen", instructed()
    wg._CONTACTS.delete(person["key"])
    print("PASS test_instructed_turn_and_the_read_mark")


def test_mark_names_what_was_seen(base, wg):
    """A client marks what it showed: `seen` is the arrival count of the
    summary it rendered, and a message that has landed since refuses the
    mark instead of being read — and settled — unseen."""
    key = "+41791000061"
    cid = "signal:" + key
    _arrive(base, key, 4000, "eins")
    row = _chat(base, cid)
    assert (row["inbound_seq"], row["unread"]) == (1, 1), row
    status, page = _http(base, "GET", row["messages"])
    assert status == 200 and page["chat"]["inbound_seq"] == 1, page["chat"]

    # The page's count is the one read before its messages were collected:
    # an arrival that lands while the page is being built is not claimed as
    # shown by a page that may not hold it.
    real_summary = wg._chat_summary

    def summary_after_an_arrival(chat_id):
        wg._CHAT_STATE.mark_unread(chat_id, time.time())
        return real_summary(chat_id)

    wg._chat_summary = summary_after_an_arrival
    try:
        status, page = _http(base, "GET", row["messages"])
    finally:
        wg._chat_summary = real_summary
    assert status == 200 and page["chat"]["inbound_seq"] == 1, page["chat"]
    assert wg._CHAT_STATE.get(cid)["inbound_seq"] == 2

    # The list the user is looking at goes stale: another message lands.
    _arrive(base, key, 4010, "zwei")
    before = wg._CHAT_STATE.get(cid)
    status, body = _read(base, cid, {"seen": row["inbound_seq"]})
    assert status == 409 and "arrived" in body["error"], body
    assert {k: body.get(k) for k in ("id", "last_read", "unread", "list_group")} == \
        {"id": cid, "last_read": None, "unread": 2, "list_group": "unread"}, body
    assert _chat(base, cid)["inbound_seq"] == 3
    after = wg._CHAT_STATE.get(cid)
    assert [after[k] for k in ("last_read", "read_seq", "read_inbound", "read_undo")] == \
        [before[k] for k in ("last_read", "read_seq", "read_inbound", "read_undo")], \
        "a refused mark wrote something"
    assert _attention(wg, cid)["state"] == "open", "a refused mark settled the item"
    assert _chat(base, cid)["unread"] == 2

    # Shown, and marked again: read, at this clock's now, and the item settled.
    row = _chat(base, cid)
    assert row["inbound_seq"] == 3, row
    status, body = _read(base, cid, {"seen": row["inbound_seq"]})
    assert status == 200 and (body["unread"], body["list_group"]) == (0, "read"), body
    assert body["previous"] is None and body["last_read"] <= _clock(), body
    assert _attention(wg, cid)["done_how"] == "marked"
    # A count ahead of the server's is no reason to refuse.
    status, body = _read(base, cid, {"seen": 99})
    assert status == 200, body
    # With an instant as well: guarded the same way.
    _arrive(base, key, 4020, "drei")
    status, body = _read(base, cid, {"seen": 3, "ts": _at(4020)})
    assert status == 409 and body["unread"] == 1, body
    status, body = _read(base, cid, {"seen": 4, "ts": _clock()})
    assert status == 200 and body["unread"] == 0, body

    # `seen` is a count and nothing else.
    for bad in (True, "2", -1, 1.5, [2]):
        status, body = _read(base, cid, {"seen": bad})
        assert status == 400, (bad, status, body)
    print("PASS test_mark_names_what_was_seen")


def test_an_arrival_after_a_read_is_unread_whatever_its_stamp(base, wg):
    """Unread is decided by the order things landed in, beside the
    timestamps: a message that arrives after the mark is unread even when it
    is stamped at or before it."""
    # SMS: the stamp is when the phone received the message, and a delayed
    # webhook delivers it later — here, after the user marked the chat read.
    key = "+41791000071"
    cid = "sms:" + key
    _arrive(base, key, 4100, "eins", channel="sms", mid="sms-1")
    status, body = _read(base, cid, {"seen": 1})
    assert status == 200 and body["list_group"] == "read", body
    mark = body["last_read"]
    _arrive(base, key, 4050, "verspätet", channel="sms", mid="sms-late")
    assert _at(4050) < mark
    row = _chat(base, cid)
    assert (row["list_group"], row["unread"], row["last_read"]) == ("unread", 1, mark), row
    assert row["inbound_seq"] == 2
    assert _attention(wg, cid)["state"] == "open"
    # Once the store has indexed both records the answer is the same: one
    # unread message, counted once.
    _store(key, 4100, "in", "eins", channel="sms", mid="sms-1")
    _store(key, 4050, "in", "verspätet", channel="sms", mid="sms-late")
    row = _chat(base, cid)
    assert (row["list_group"], row["unread"]) == ("unread", 1), row

    # Marked read, it is read; the undo brings back exactly what the mark
    # found unread — the late message, which no timestamp would.
    status, body = _read(base, cid, {"seen": 2})
    assert status == 200 and (body["unread"], body["list_group"]) == (0, "read"), body
    status, body = _read(base, cid, {"restore": body["previous"], "marked": body["last_read"]})
    assert status == 200 and (body["unread"], body["list_group"]) == (1, "unread"), body
    # A late echo of a reply written before the mark says nothing about what
    # landed after it.
    status, _ = _http(base, "POST", RAIL, {
        "direction": "out", "channel": "sms", "chat": key, "author": "device",
        "message_id": "sms-old-echo", "text": "ok", "ts": T0 + 4060})
    assert status == 200
    assert _chat(base, cid)["unread"] == 1, "a stale echo read a message that came after it"
    # A read naming an older instant: the same.
    status, body = _read(base, cid, {"ts": _at(4060)})
    assert status == 200 and (body["unread"], body["list_group"]) == (1, "unread"), body
    # One naming the mark itself covers what is stamped up to it.
    status, body = _read(base, cid, {"ts": mark})
    assert status == 200 and (body["unread"], body["list_group"]) == (0, "read"), body

    # Any channel, in the mark's own second: the whole-second stamps are
    # equal, and the message came after.
    other = "+41791000072"
    oid = "signal:" + other
    _arrive(base, other, 4100, "eins")
    status, body = _read(base, oid, {"seen": 1})
    assert status == 200 and body["list_group"] == "read", body
    status, _ = _arrive(base, other, 0, "gleich danach", mid="same-second",
                        ts=_epoch(body["last_read"]))
    row = _chat(base, oid)
    assert (row["list_group"], row["unread"]) == ("unread", 1), row
    # The user's answer from the dashboard reads whatever has landed.
    status, msg = _http(base, "POST", "/chats/" + _quote(oid) + "/send", {"text": "ok"})
    assert status == 200, msg
    row = _chat(base, oid)
    assert (row["list_group"], row["unread"]) == ("read", 0), row
    # … and nothing reaches behind it: an arrival after the reply is unread
    # again, a mark and its undo leave it so.
    _arrive(base, other, 4110, "noch eins", mid="after-reply")
    status, body = _read(base, oid, {"seen": 3})
    assert status == 200 and body["unread"] == 0, body
    status, body = _read(base, oid, {"restore": body["previous"], "marked": body["last_read"]})
    assert status == 200 and (body["unread"], body["list_group"]) == (1, "unread"), body

    # The dashboard's answer is written now, to everything that has landed —
    # also where the mark already stands ahead of the send's own stamp (the
    # phone the user answered from before has a fast clock).
    fourth = "+41791000074"
    fid = "signal:" + fourth
    _arrive(base, fourth, 4100, "eins")
    status, _ = _http(base, "POST", RAIL, {
        "direction": "out", "channel": "signal", "chat": fourth, "author": "device",
        "message_id": "fast-echo", "text": "ok", "ts": time.time() + 90})
    assert status == 200 and _chat(base, fid)["list_group"] == "read"
    _arrive(base, fourth, 0, "und?", mid="after-fast-echo", ts=time.time())
    row = _chat(base, fid)
    assert (row["list_group"], row["unread"]) == ("unread", 1), row
    status, msg = _http(base, "POST", "/chats/" + _quote(fid) + "/send", {"text": "ja"})
    assert status == 200 and msg["ts"] < wg._CHAT_STATE.get(fid)["last_read"], msg
    row = _chat(base, fid)
    assert (row["list_group"], row["unread"]) == ("read", 0), row

    # A reply that reached the ledger without passing the rail, later than
    # the mark: no read was recorded for it, so its timestamp decides — and
    # it is later than the message.
    third = "+41791000073"
    tid = "signal:" + third
    _arrive(base, third, 4100, "eins")
    status, body = _read(base, tid, {"seen": 1})
    _arrive(base, third, 4050, "verspätet", mid="late-3")
    assert _chat(base, tid)["unread"] == 1
    _store(third, 0, "out", "vom Telefon", author="device", ts=_clock(5))
    row = _chat(base, tid)
    assert (row["list_group"], row["unread"]) == ("read", 0), row
    print("PASS test_an_arrival_after_a_read_is_unread_whatever_its_stamp")


def test_a_senders_clock_never_moves_the_mark(base, wg):
    """A message's stamp comes from whoever stamped it. One from ahead of
    this clock is taken as now when it arrives, and the mark is this clock's
    own — so a peer with a wrong clock cannot have later messages counted
    read."""
    key = "+41791000081"
    cid = "signal:" + key
    years = 3 * 365 * 86400
    status, body = _arrive(base, key, 0, "aus der Zukunft", mid="fut-1",
                           ts=time.time() + years)
    assert status == 200, body
    row = _chat(base, cid)
    assert row["last"]["ts"] <= _clock(), "an arrival kept a stamp from ahead of this clock"
    status, page = _http(base, "GET", row["messages"])
    assert [m["ts"] <= _clock() for m in page["messages"]] == [True], page["messages"]
    status, body = _read(base, cid, {"seen": row["inbound_seq"]})
    assert status == 200 and body["last_read"] <= _clock(), body
    assert (body["unread"], body["list_group"]) == (0, "read"), body
    # The next message, from a clock that is right, is unread.
    _arrive(base, key, 0, "und jetzt?", mid="fut-2", ts=time.time())
    row = _chat(base, cid)
    assert (row["list_group"], row["unread"]) == ("unread", 1), row
    assert row["last_read"] <= _clock()
    # And a later, earlier-stamped one too (the clock was set right again).
    status, body = _read(base, cid, {"seen": 2})
    _arrive(base, key, 4300, "nochmal", mid="fut-3")
    assert _chat(base, cid)["unread"] == 1

    # A ledger record stamped by a clock running ahead (a phone's, on SMS):
    # no mark is lifted to it. A mark made before this clock has passed the
    # stamp leaves it unread, and the answer says so.
    ahead = "+41791000082"
    _store(ahead, 0, "in", "voraus", channel="sms", ts=_clock(5000))
    status, body = _read(base, "sms:" + ahead)
    assert status == 200 and body["last_read"] <= _clock(), body
    assert (body["unread"], body["list_group"]) == (1, "unread"), body
    # Nor does a caller's own instant get there: refused from the future,
    # taken as now within a fast clock's reach.
    status, body = _read(base, "sms:" + ahead, {"ts": _clock(5000)})
    assert status == 400, body
    status, body = _read(base, "sms:" + ahead, {"ts": _clock(60)})
    assert status == 200 and body["last_read"] <= _clock(), body
    print("PASS test_a_senders_clock_never_moves_the_mark")


def test_a_repeated_event_is_one_arrival(base, wg):
    """The rail delivers at least once: a gateway retries a POST that timed
    out, and redelivers what it was never told was taken. The repeat is
    answered as the first event was, and is no second arrival."""
    key = "+41791000101"
    cid = "sms:" + key
    _arrive(base, key, 4500, "eins", channel="sms", mid="rep-1")
    status, body = _read(base, cid, {"seen": 1})
    assert status == 200 and body["list_group"] == "read", body
    # One message, and its event twice — the retry after a timeout.
    for _ in range(2):
        status, body = _arrive(base, key, 4510, "zwei", channel="sms", mid="rep-2")
        assert status == 200 and body["id"] == cid, body
    row = _chat(base, cid)
    assert (row["list_group"], row["unread"], row["inbound_seq"]) == ("unread", 1, 2), row
    # Once the store has indexed both records: still one.
    _store(key, 4500, "in", "eins", channel="sms", mid="rep-1")
    _store(key, 4510, "in", "zwei", channel="sms", mid="rep-2")
    row = _chat(base, cid)
    assert (row["list_group"], row["unread"], row["inbound_seq"]) == ("unread", 1, 2), row
    # The count is in the chat's state document, so a gateway that restarted
    # inside the retry window answers the same: nothing of the first event
    # is left in memory here.
    wg._CHAT_OVERLAY.forget(cid)
    with wg._chat_turns_lock:
        wg._CHAT_ARRIVAL_JOBS.clear()
    status, body = _arrive(base, key, 4510, "zwei", channel="sms", mid="rep-2")
    assert status == 200, body
    row = _chat(base, cid)
    assert (row["unread"], row["inbound_seq"]) == (1, 2), row
    # A mark for what the page showed before the repeat came is not refused
    # for it, and a redelivery after the read does not unread the chat.
    status, body = _read(base, cid, {"seen": 2})
    assert status == 200 and (body["unread"], body["list_group"]) == (0, "read"), body
    _arrive(base, key, 4510, "zwei", channel="sms", mid="rep-2")
    row = _chat(base, cid)
    assert (row["list_group"], row["unread"], row["inbound_seq"]) == ("read", 0, 2), row
    # An event that names no message cannot be told from a new one: counted.
    for _ in range(2):
        status, _ = _http(base, "POST", RAIL, {
            "direction": "in", "channel": "sms", "chat": key, "sender": key,
            "group": False, "text": "ohne Kennung", "ts": T0 + 4520,
            "gate": {"forward": True, "reason": "open"}})
        assert status == 200
    assert wg._CHAT_STATE.get(cid)["inbound_seq"] == 4

    # What a repeat is answered has not changed: the handle of the turn its
    # message bought, and the acceptance — for one turn and one arrival.
    vip = "+41791000102"
    event = {"direction": "in", "channel": "signal", "chat": vip, "sender": vip,
             "sender_name": "Vip", "group": False, "handover": True, "text": "hoi",
             "message_id": "rep-vip", "ts": T0 + 4530,
             "gate": {"forward": True, "vip": True, "reason": "open"}}
    TURNS.clear()
    status, first = _http(base, "POST", RAIL, event)
    status_again, again = _http(base, "POST", RAIL, event)
    assert status == status_again == 202, (first, again)
    assert again["job_url"] == first["job_url"] and again["accepted"] is True, (first, again)
    assert _await_job(base, first["job_url"])["status"] == "done"
    state = wg._CHAT_STATE.get("signal:" + vip)
    assert len(TURNS) == 1 and (state["inbound_seq"], state["attended_seq"]) == (1, 1), state

    # The attention model is told which arrival a block was decided from. A
    # repeat names its own message's place in the count, not the count as it
    # stands — which a newer message, not taken in yet, has moved on (here
    # one that landed while the chat was muted).
    older = "+41791000103"
    oid = "signal:" + older
    _arrive(base, older, 4540, "eins", mid="rep-old")
    flags = "/chats/" + _quote(oid) + "/flags"
    assert _http(base, "POST", flags, {"muted": True})[0] == 200
    _arrive(base, older, 4550, "zwei", mid="rep-new")
    assert _http(base, "POST", flags, {"muted": False, "archived": False})[0] == 200
    state = wg._CHAT_STATE.get(oid)
    assert (state["inbound_seq"], state["attended_seq"]) == (2, 1), state
    _arrive(base, older, 4540, "eins", mid="rep-old")
    state = wg._CHAT_STATE.get(oid)
    assert (state["inbound_seq"], state["attended_seq"]) == (2, 1), state

    # A held ring: the repeat of an instructed message whose turn is working
    # gets that turn's handle and registers no second ring, and the mark the
    # user makes for what they were shown still covers the message — it is
    # not rung afterwards.
    agent = {"X-Conversation-Backend-Token": "agent-token"}
    chambers = Path(wg._CONTACTS.chambers_dir)
    (chambers / "private").mkdir(parents=True, exist_ok=True)
    person = wg._CONTACTS.create("private", "Hauswart", [("telegram", "889101")],
                                 instruction="Ring me only if the water is off.")
    hid = "telegram:889101"
    held = {"direction": "in", "channel": "telegram", "chat": "889101", "sender": "889101",
            "sender_name": "Hauswart", "group": False, "handover": True, "text": "Info",
            "message_id": "rep-held", "ts": T0 + 4560,
            "gate": {"forward": True, "vip": False, "instructed": True, "reason": "open"}}

    def verdict(_prompt):
        status, body = _http(base, "POST", f"/internal/chats/{_quote(hid)}/verdict",
                             {"importance": 4}, headers=agent)
        assert status == 200, body

    hold = threading.Event()
    PUSHES.clear()
    TURN_GATE.update(hold=hold, during=verdict)
    try:
        status, first = _http(base, "POST", RAIL, held)
        assert status == 202 and first["pushed"] is False, first
        _wait_for(lambda: (_chat(base, hid).get("instructed") or {}).get("verdict"),
                  "the turn to set its verdict")
        shown = _chat(base, hid)["inbound_seq"]
        status, again = _http(base, "POST", RAIL, held)
        assert status == 202 and again["job_url"] == first["job_url"], (first, again)
        assert len(wg._INSTRUCTED_RINGS) == 1, wg._INSTRUCTED_RINGS
        status, body = _read(base, hid, {"seen": shown})
        assert shown == 1 and status == 200 and body["unread"] == 0, body
    finally:
        hold.set()
        TURN_GATE.update(hold=None, during=None)
    _await_job(base, first["job_url"])
    assert PUSHES == [], "a message marked read rang after a repeat of its event"
    assert _chat(base, hid)["instructed"]["delivery"] == "seen"
    wg._CONTACTS.delete(person["key"])
    print("PASS test_a_repeated_event_is_one_arrival")


def test_a_fast_clock_is_read_with_the_chat(base, wg):
    """A message stamped a little ahead of this clock — an SMS phone running
    fast, whose ledger record keeps the phone's time — is read by the mark
    that covers it, and stays read once the store answers with that record."""
    key = "+41791000111"
    cid = "sms:" + key
    stamp = time.time() + 30
    _arrive(base, key, 0, "voraus", channel="sms", mid="fast-1", ts=stamp)
    row = _chat(base, cid)
    assert row["last"]["ts"] <= _clock() and row["unread"] == 1, row
    status, mark = _read(base, cid, {"seen": 1})
    assert status == 200 and (mark["unread"], mark["list_group"]) == (0, "read"), mark
    assert mark["previous"] is None and mark["last_read"] <= _clock(), mark
    assert _attention(wg, cid)["state"] == "done"
    # The store indexes the record, under the phone's stamp: later than the
    # mark, and read all the same — nothing new has come.
    _store(key, 0, "in", "voraus", channel="sms", mid="fast-1", ts=_iso(stamp))
    assert _iso(stamp) > mark["last_read"]
    row = _chat(base, cid)
    assert (row["list_group"], row["unread"], row["last_read"]) == \
        ("read", 0, mark["last_read"]), row
    # What lands next is unread, once: by its place in the count on the
    # rail, and no differently when the store holds its record too.
    _arrive(base, key, 0, "und jetzt?", channel="sms", mid="fast-2", ts=time.time())
    row = _chat(base, cid)
    assert (row["list_group"], row["unread"]) == ("unread", 1), row
    _store(key, 0, "in", "und jetzt?", channel="sms", mid="fast-2", ts=_clock())
    assert _chat(base, cid)["unread"] == 1
    # A reply the rail never saw (answered on the phone: SMS echoes none),
    # later than the mark: no read was recorded for it, so its timestamp
    # decides — against the mark's own instant, not the stamp it reaches.
    _store(key, 0, "out", "vom Telefon", channel="sms", author="device", ts=_clock(5))
    row = _chat(base, cid)
    assert (row["list_group"], row["unread"]) == ("read", 0), row

    # The undo takes back exactly what the mark changed: the message is
    # unread again, and nothing of the mark's reach is left behind.
    other = "+41791000112"
    oid = "sms:" + other
    stamp = time.time() + 45
    _arrive(base, other, 0, "voraus", channel="sms", mid="fast-u", ts=stamp)
    _store(other, 0, "in", "voraus", channel="sms", mid="fast-u", ts=_iso(stamp))
    status, body = _read(base, oid, {"seen": 1})
    assert status == 200 and (body["unread"], body["list_group"]) == (0, "read"), body
    state = wg._CHAT_STATE.get(oid)
    assert state["read_ahead"] == _iso(stamp) and state["last_read"] <= _clock(), state
    status, body = _read(base, oid, {"restore": body["previous"], "marked": body["last_read"]})
    assert status == 200 and (body["unread"], body["list_group"]) == (1, "unread"), body
    state = wg._CHAT_STATE.get(oid)
    assert [state[k] for k in ("last_read", "read_inbound", "read_ahead", "read_undo")] == \
        [None] * 4, state
    # A mark up to a named instant reads up to that instant, as it says:
    # not a message stamped later, and one stamped there — though the mark
    # itself goes no further than this clock.
    status, body = _read(base, oid, {"ts": _clock()})
    assert status == 200 and (body["unread"], body["list_group"]) == (1, "unread"), body
    status, body = _read(base, oid, {"ts": _iso(stamp)})
    assert status == 200 and (body["unread"], body["list_group"]) == (0, "read"), body
    assert body["last_read"] <= _clock(), body
    # The dashboard's own answer is written to everything that has landed —
    # on any channel: here a Signal peer's clock is the one running ahead.
    third = "+41791000114"
    tid = "signal:" + third
    stamp = time.time() + 40
    _arrive(base, third, 0, "voraus", mid="fast-s", ts=stamp)
    status, msg = _http(base, "POST", "/chats/" + _quote(tid) + "/send", {"text": "ok"})
    assert status == 200 and msg["ts"] < _iso(stamp), msg
    _store(third, 0, "in", "voraus", mid="fast-s", ts=_iso(stamp))
    row = _chat(base, tid)
    assert (row["list_group"], row["unread"]) == ("read", 0), row

    # Further ahead than a clock is allowed to be wrong, a stamp is not
    # taken for a clock's error. The case left over: the mark reads such a
    # message while only the rail's overlay holds it, and it is unread again
    # once the store answers with its record.
    far = "+41791000113"
    fid = "sms:" + far
    stamp = time.time() + wg.CHAT_READ_SKEW_SECONDS + 600
    _arrive(base, far, 0, "weit voraus", channel="sms", mid="far-1", ts=stamp)
    status, body = _read(base, fid, {"seen": 1})
    assert status == 200 and (body["unread"], body["list_group"]) == (0, "read"), body
    assert wg._CHAT_STATE.get(fid)["read_ahead"] is None
    _store(far, 0, "in", "weit voraus", channel="sms", mid="far-1", ts=_iso(stamp))
    row = _chat(base, fid)
    assert (row["list_group"], row["unread"]) == ("unread", 1), row
    print("PASS test_a_fast_clock_is_read_with_the_chat")


def test_reply_marks_are_a_refinement(base, wg):
    """The query for the user's own latest messages refines the list; its
    failure alone must not take the list down."""
    answered, marked = "+41791000091", "+41791000092"
    _store(answered, -900, "in")
    _store(answered, -800, "out", author="device")   # read by the ledger's reply only
    _store(marked, -900, "in")
    status, body = _read(base, "signal:" + marked, {"ts": _at(-900)})
    assert status == 200, body
    assert _chat(base, "signal:" + answered)["list_group"] == "read"

    api.STATE["fail"] = "VALUES ?author"
    said = io.StringIO()
    try:
        # What the last skeleton knew of the replies is kept meanwhile.
        with contextlib.redirect_stdout(said):
            rows = {c["id"]: c for c in _chats(base)["chats"]}
        assert rows["signal:" + answered]["list_group"] == "read", rows["signal:" + answered]
        assert rows["signal:" + marked]["list_group"] == "read"
        assert "the last known replies" in said.getvalue(), said.getvalue()
        # With no skeleton to take them from: the read marks alone — still a
        # list, with the unread counts asked as ever — and the log says that
        # there was nothing to keep.
        wg._chats_cache_clear()
        asked = len(api.STATE["queries"])
        said = io.StringIO()
        with contextlib.redirect_stdout(said):
            rows = {c["id"]: c for c in _chats(base)["chats"]}
        assert "no earlier list" in said.getvalue(), said.getvalue()
        assert "the last known replies" not in said.getvalue(), said.getvalue()
        row = rows["signal:" + answered]
        assert (row["list_group"], row["unread"]) == ("unread", 1), row
        assert rows["signal:" + marked]["list_group"] == "read"
        assert any("VALUES (?chat ?account ?cut)" in q for q in api.STATE["queries"][asked:])
    finally:
        api.STATE["fail"] = False
        wg._chats_cache_clear()
    assert _chat(base, "signal:" + answered)["list_group"] == "read"

    # Its timeout. With no rebuild deadline in force, the plain one of any
    # query. Under a deadline, half of what that budget really has left, so
    # that running out of time leaves the unread query its share — which is
    # a whole query's timeout wherever the budget holds two of them, not
    # half of one.
    timeouts = []
    real = wg._sparql_bindings
    wg._sparql_bindings = lambda _query, timeout=None: timeouts.append(timeout) or []
    one = {"signal:x": {"key": "x", "account": None}}
    try:
        wg._fetch_reply_marks(one)
        wg._fetch_reply_marks(one, time.time() + 8)
        wg._fetch_reply_marks(one, time.time() + 2 * wg.QLEVER_TIMEOUT + 8)
        wg._fetch_reply_marks(one, time.time() + wg.QLEVER_TIMEOUT)
        try:
            wg._fetch_reply_marks(one, time.time() - 1)
        except TimeoutError:
            pass
        else:
            raise AssertionError("a deadline that has passed must not be asked past")
    finally:
        wg._sparql_bindings = real
    assert len(timeouts) == 4 and wg.QLEVER_TIMEOUT > 8, timeouts
    assert timeouts[0] == wg.QLEVER_TIMEOUT, timeouts
    assert 0 < timeouts[1] <= 4.0, timeouts
    assert timeouts[2] == wg.QLEVER_TIMEOUT, timeouts
    assert 0 < timeouts[3] <= wg.QLEVER_TIMEOUT / 2, timeouts
    print("PASS test_reply_marks_are_a_refinement")


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

    # The arrival count is in the chat's state and needs no store: a message
    # that lands behind a mark is unread on the last good skeleton too, the
    # guard holds, and where no list can be built at all the state answers.
    late = "+41791000052"
    lid = "sms:" + late
    _arrive(base, late, 4400, "eins", channel="sms", mid="away-1")
    _store(late, 4400, "in", "eins", channel="sms", mid="away-1")
    assert _chat(base, lid)["unread"] == 1
    wg.CHAT_LIST_CACHE_SECONDS = 30.0
    wg._chats_cache_invalidate()
    api.STATE["fail"] = True
    try:
        status, body = _read(base, lid, {"seen": 1})
        assert status == 200 and (body["unread"], body["list_group"]) == (0, "read"), body
        _arrive(base, late, 4350, "verspätet", channel="sms", mid="away-late")
        row = _chat(base, lid)
        assert (row["list_group"], row["unread"], row["inbound_seq"]) == ("unread", 1, 2), row
        status, body = _read(base, lid, {"seen": 1})
        assert status == 409 and (body["unread"], body["list_group"]) == (1, "unread"), body
        # Nothing to serve: the read endpoint still answers, from the state.
        wg._chats_cache_clear()
        status, _ = _http(base, "GET", "/chats")
        assert status == 502
        status, body = _read(base, lid, {"seen": 1})
        assert status == 409 and (body["unread"], body["list_group"]) == (1, "unread"), body
        status, body = _read(base, lid, {"seen": 2})
        assert status == 200 and (body["unread"], body["list_group"]) == (0, "read"), body
        status, body = _read(base, lid, {"restore": body["previous"],
                                         "marked": body["last_read"]})
        assert status == 200 and (body["unread"], body["list_group"]) == (1, "unread"), body
    finally:
        api.STATE["fail"] = False
        wg.CHAT_LIST_CACHE_SECONDS = 0.0
        wg._chats_cache_clear()
    assert _chat(base, lid)["unread"] == 1
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
        test_mark_names_what_was_seen(base, wg)
        test_an_arrival_after_a_read_is_unread_whatever_its_stamp(base, wg)
        test_a_senders_clock_never_moves_the_mark(base, wg)
        test_a_repeated_event_is_one_arrival(base, wg)
        test_a_fast_clock_is_read_with_the_chat(base, wg)
        test_reply_marks_are_a_refinement(base, wg)
        test_store_away(base, wg)
        server.shutdown()
    sparql.shutdown()
    gw.shutdown()
    print("all chat-read tests passed")


if __name__ == "__main__":
    main()
