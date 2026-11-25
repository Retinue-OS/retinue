#!/usr/bin/env python3
"""Chat triage as the web gateway serves it (docs/chat-triage.md): GET and
POST /chats/triage over the real handler, the ledger and SPARQL mock of
tests/test_chat_mail.py, a fake `claude` for the sorting pass and a stub
`send_message` for the follow-up turns. Pins:

- a press sorts exactly the chats on the Unread tab, in one tool-less call
  of the triage model (no tools, no MCP server, no session, away from
  /workspace), with every chat's text inside its own <chat> element;
- what the answer said lands per chat with the chat's `inbound_seq`;
- an action chat gets a follow-up turn in its companion thread, on the
  follow-up model, which pushes nothing, and its reply lands on the entry;
- a second press sorts nothing that is still fresh; `again` sorts all;
- a cross-site press is refused, and so is any press with triage off.

Standalone (stdlib + the gateway module's own deps):

    python3 tests/test_chat_triage_gateway.py
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

import test_chat_mail as mail  # noqa: E402  (the web gateway's loader, the SPARQL mock)

CALLS: list = []
TURNS: list = []


def fake_claude(cmd, **kwargs):
    """The sorting pass: every chat whose name sorts first is action, the
    rest archive — enough to tell the buckets apart."""
    CALLS.append({"cmd": cmd, **kwargs})
    prompt = cmd[-1]
    found = re.findall(r'<chat n="(\d+)"', prompt)
    chats = [{"n": int(n), "bucket": "action" if i == 0 else "archive",
              "note": f"note {n}", "reply": i == 0} for i, n in enumerate(found)]
    return subprocess.CompletedProcess(cmd, 0, json.dumps(
        {"type": "result", "subtype": "success", "is_error": False,
         "result": json.dumps({"chats": chats})}), "")


def wait_for(fn, what, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def main() -> int:
    sparql = mail._serve(mail._MockSparql)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        mail.STATE["ledger"] = tmp / "ledger"
        mail._build_ledger(mail.STATE["ledger"])
        wg = mail._load_gateway(tmp, sparql.server_address[1])
        wg.push_notify.enabled = lambda: False
        wg._run_claude = fake_claude
        wg.send_message = lambda *a, **k: TURNS.append((a, k)) or {
            "response": "Pay the invoice by Friday; I staged a short reply.",
            "model_name": "stub", "cost_usd": 0.0}
        # Another turn's reply lands in the companion right after the
        # follow-up's own: the triage entry must keep the follow-up's.
        real_add = wg._conv_add_message

        def add_then_another(cid, role, text, **kw):
            conv = real_add(cid, role, text, **kw)
            if role == "assistant" and text.startswith("Pay the invoice"):
                real_add(cid, "assistant", "Some other turn's reply.", unread=True)
            return conv

        wg._conv_add_message = add_then_another
        wg.CHAT_TRIAGE_MODEL = "fast-model"
        wg.CHAT_TRIAGE_FOLLOWUP_MODEL = "frontier-model"
        # Follow-ups are off by default; this test opts in to exercise them.
        assert wg.CHAT_TRIAGE_FOLLOWUPS == 0 and wg._TRIAGE.followup_max == 0
        wg._TRIAGE.followup_max = 20
        wg.contacts_mod.commit = lambda *a, **k: None
        server = ThreadingHTTPServer(("127.0.0.1", 0), wg.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            chats = list(mail._chats(base).values())
            unread = [c for c in chats if c["list_group"] == "unread"]
            assert unread, "the fixture ledger has unread chats"
            # Make one unread chat read, so the scope is visibly the tab's.
            status, _ = mail._http(base, "POST", "/chats/" + mail._quote(unread[-1]["id"]) + "/read",
                                   {"seen": unread[-1]["inbound_seq"]})
            assert status == 200, status
            chats = list(mail._chats(base).values())
            unread = {c["id"]: c for c in chats if c["list_group"] == "unread"}

            status, doc = mail._http(base, "GET", "/chats/triage")
            assert status == 200 and doc["enabled"] and doc["run"] is None and doc["entries"] == {}, doc

            status, doc = mail._http(base, "POST", "/chats/triage", {},
                                     {"Sec-Fetch-Site": "cross-site"})
            assert status == 403, (status, doc)
            assert not CALLS, "a refused press spends nothing"

            status, doc = mail._http(base, "POST", "/chats/triage", {})
            assert status == 202 and doc["started"] and doc["total"] == len(unread), doc
            wg._TRIAGE.join(15)
            doc = mail._http(base, "GET", "/chats/triage")[1]
            assert doc["run"]["state"] == "done", doc["run"]
            entries = doc["entries"]
            assert set(entries) == set(unread), (set(entries), set(unread))
            for cid, e in entries.items():
                assert e["seq"] == unread[cid]["inbound_seq"], e
                assert e["bucket"] in ("action", "archive"), e

            # The sorting pass: tool-less, no session, the triage model.
            assert len(CALLS) == 1, "every unread chat in one batch"
            call = CALLS[0]
            cmd = call["cmd"]
            assert cmd[cmd.index("--model") + 1] == "fast-model"
            assert cmd[cmd.index("--allowed-tools") + 1] == ""
            assert "--no-session-persistence" in cmd and "--strict-mcp-config" in cmd
            assert not str(call["cwd"]).startswith("/workspace")
            assert cmd[-1].count("<chat n=") == len(unread)

            # The action chat's follow-up: one companion turn on the follow-up
            # model, its reply on the entry.
            action = [cid for cid, e in entries.items() if e["bucket"] == "action"]
            assert len(action) == 1, entries
            done = wait_for(lambda: (mail._http(base, "GET", "/chats/triage")[1]["entries"]
                                     [action[0]].get("followup", {}).get("state") == "done"),
                            "the follow-up")
            assert done
            fu = mail._http(base, "GET", "/chats/triage")[1]["entries"][action[0]]["followup"]
            assert fu["text"].startswith("Pay the invoice"), fu
            assert len(TURNS) == 1, TURNS
            _args, kwargs = TURNS[0]
            assert kwargs.get("model") == "frontier-model", kwargs
            companion = wg._CHAT_STATE.get(action[0]).get("companion")
            assert companion, "the follow-up runs in the chat's companion"
            conv = wg._load_conv(companion)
            assert conv["messages"][-1]["role"] == "assistant"
            assert "archive" not in [e["bucket"] for e in [entries[action[0]]]]

            wait_for(lambda: not mail._http(base, "GET", "/chats/triage")[1]["busy"],
                     "the follow-ups to be over")
            # A second press: everything is fresh, nothing is sorted again.
            status, doc = mail._http(base, "POST", "/chats/triage", {})
            assert status == 202 and doc["total"] == 0 and doc["skipped"] == len(unread), doc
            assert len(CALLS) == 1
            # `again` sorts them all.
            status, doc = mail._http(base, "POST", "/chats/triage", {"again": True})
            assert status == 202 and doc["total"] == len(unread), doc
            wg._TRIAGE.join(15)
            assert len(CALLS) == 2

            # Feedback: saved, quoted into the next run, and that run sorts
            # every unread chat again.
            wait_for(lambda: not mail._http(base, "GET", "/chats/triage")[1]["busy"],
                     "the run to be over")
            status, doc = mail._http(base, "POST", "/chats/triage", {"feedback": ""})
            assert status == 400, (status, doc)
            status, doc = mail._http(base, "POST", "/chats/triage",
                                     {"feedback": "x"}, {"Sec-Fetch-Site": "cross-site"})
            assert status == 403
            calls_before = len(CALLS)
            status, doc = mail._http(base, "POST", "/chats/triage",
                                     {"feedback": "Mails from Zoe are never to be deleted."})
            assert status == 202 and doc["started"] and doc["total"] == len(unread), doc
            assert [n["text"] for n in doc["notes"]] == ["Mails from Zoe are never to be deleted."]
            wg._TRIAGE.join(15)
            assert len(CALLS) == calls_before + 1
            assert "- Mails from Zoe are never to be deleted." in CALLS[-1]["cmd"][-1]
            note_id = doc["notes"][0]["id"]
            status, _ = mail._http(base, "POST", "/chats/triage/notes", {"remove": "nope"})
            assert status == 404
            status, doc = mail._http(base, "POST", "/chats/triage/notes", {"remove": note_id})
            assert status == 200 and doc["notes"] == [], doc
            wait_for(lambda: not mail._http(base, "GET", "/chats/triage")[1]["busy"],
                     "the follow-ups to be over")

            # Off is off.
            wg.CHAT_TRIAGE = False
            status, doc = mail._http(base, "POST", "/chats/triage", {})
            assert status == 409, (status, doc)
            assert mail._http(base, "GET", "/chats/triage")[1]["enabled"] is False
        finally:
            # Let the last follow-ups finish before the directory goes.
            wg._TRIAGE.join(15)
            time.sleep(0.2)
            server.shutdown()
            sparql.shutdown()
    print("all chat-triage gateway tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
