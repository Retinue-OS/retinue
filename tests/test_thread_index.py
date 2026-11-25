#!/usr/bin/env python3
"""Thread summaries in the life store, and the Archived list paged over them.

Covers scripts/thread_index.py and how the REAL web-gateway handler uses it:

  * The summary: the list fields as N-Triples, valid (parsed with rdflib when
    present), escaped, deterministic — and never a message text, a preview,
    a context note or the model choice.
  * The gateway keeps it current: opening, archiving and starring a thread
    rewrite its summary; deleting the thread removes the file, so nothing
    about it is left for the store. The startup reconcile writes missing
    summaries and removes those whose thread is gone.
  * GET /conversations?archived=1&limit=N: the store's order (starred first,
    then newest), a page at a time with has_more; rows built from the files,
    so a thread deleted or restored since the store last looked is dropped;
    the files answer when the store is down or knows no archived thread.
    Without `limit` the endpoint is the full list it always was.

With rdflib installed (CI installs it) the store is played by rdflib running
the gateway's own page query over the emitted files — real SPARQL ordering,
not a stand-in.

    python3 tests/test_thread_index.py
"""
import importlib.util
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
TOKEN = "test-token"
FAILURES: list[str] = []

try:
    import rdflib  # noqa: F401 — optional, see the docstring
    HAVE_RDFLIB = True
except ImportError:
    HAVE_RDFLIB = False


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  ok   " if ok else "  FAIL ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def _load(module_name: str, script: str):
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS_DIR / script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── The summary itself ───────────────────────────────────────────────────────

def test_summary(ti):
    print("thread_index.to_ntriples")
    cid = "a" * 32
    conv = {
        "id": cid, "title": 'Party "RSVP"\\ line\nbreak', "kind": "chat", "initiator": "agent",
        "created": "2026-11-01T10:00:00+01:00", "updated": "2026-11-02T08:30:00+00:00",
        "archived": True, "muted": False, "starred": True, "unread": True, "pending": True,
        "model": "secret-model-choice",
        "project": "urn:notes:project-garden",
        "chat": "signal:+41790000000",
        "messages": [
            {"role": "user", "text": "MESSAGE-BODY-ONE", "context": "CONTEXT-NOTE"},
            {"role": "assistant", "text": "MESSAGE-BODY-TWO",
             "shell_actions": [{"id": "x", "text": "SHELL-TEXT"}]},
        ],
    }
    nt = ti.to_ntriples(conv)
    for secret in ("MESSAGE-BODY", "CONTEXT-NOTE", "SHELL-TEXT", "secret-model-choice", "pending",
                   "RSVP", "/title>"):
        check(f"no {secret} in the summary", secret not in nt, nt)
    s = f"<urn:retinue:thread:{cid}>"
    check("typed kb:Thread", f"{s} <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://w3id.org/retinue/kb#Thread> ." in nt)
    check("modified is normalised to UTC",
          '<http://purl.org/dc/terms/modified> "2026-11-02T08:30:00Z"^^<http://www.w3.org/2001/XMLSchema#dateTime>' in nt)
    check("created too", '"2026-11-01T09:00:00Z"^^' in nt)
    check("flags are booleans", '<https://w3id.org/retinue/kb#archived> "true"^^<http://www.w3.org/2001/XMLSchema#boolean>' in nt
          and '<https://w3id.org/retinue/kb#muted> "false"^^' in nt)
    check("the message count", '<https://w3id.org/retinue/kb#messageCount> "2"^^' in nt)
    check("the project link", "<https://w3id.org/retinue/kb#project> <urn:notes:project-garden> ." in nt)
    check("the companion's chat, as the attention emit names it",
          "<https://w3id.org/retinue/kb#companionOf> <urn:retinue:chat:signal:+41790000000> ." in nt, nt)
    check("deterministic", nt == ti.to_ntriples(dict(conv)))
    odd = dict(conv, project="not an iri <x>", updated="garbage", created="also garbage")
    nt_odd = ti.to_ntriples(odd)
    check("an unusable project link is left out", "#project>" not in nt_odd)
    check("unreadable times give no modified", "/modified>" not in nt_odd)
    check("no thread, no summary", ti.to_ntriples({"id": "../etc"}) == "" and ti.to_ntriples({}) == "")
    if HAVE_RDFLIB:
        g = rdflib.Graph()
        g.parse(data=nt, format="nt")
        check("rdflib parses it", len(g) == len(nt.strip().splitlines()), str(len(g)))
        chat = g.value(rdflib.URIRef(f"urn:retinue:thread:{cid}"),
                       rdflib.URIRef("https://w3id.org/retinue/kb#companionOf"))
        check("…and reads the chat link back intact",
              str(chat) == "urn:retinue:chat:signal:+41790000000", repr(chat))

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "threads"
        check("a first write writes", ti.write(out, conv) is True)
        check("an unchanged one does not", ti.write(out, conv) is False)
        check("a change does", ti.write(out, dict(conv, starred=False)) is True)
        (out / ("b" * 32 + ".nt")).write_text("stale\n")
        (out / "README").write_text("not a summary\n")
        check("stale summaries are found", ti.stale_ids(out, {cid}) == ["b" * 32])
        check("remove drops the file", ti.remove(out, cid) is True and not (out / f"{cid}.nt").exists())
        check("removing twice is harmless", ti.remove(out, cid) is False)
        check("an id outside the format is never a path", ti.remove(out, "../x") is False)

    q = ti.page_query("chat", 11, 20)
    check("the page query pages", "LIMIT 11 OFFSET 20" in q, q)
    check("…starred first, then newest", "ORDER BY DESC(?starred) DESC(?modified)" in q, q)
    check("kind all drops the kind pattern", "kb:kind" not in ti.page_query("all"))
    rows = [{"t": {"value": f"urn:retinue:thread:{c}"}} for c in ("c" * 32, "c" * 32, "d" * 32)] + \
        [{"t": {"value": "urn:other:x"}}, {}]
    check("page_ids keeps order, drops repeats and strangers", ti.page_ids(rows) == ["c" * 32, "d" * 32])


# ── The gateway ──────────────────────────────────────────────────────────────

class Gateway:
    def __init__(self, wg, port: int):
        self.wg = wg
        self.base = f"http://127.0.0.1:{port}"

    def call(self, method, path, payload=None, agent=False):
        data = json.dumps(payload).encode() if payload is not None else (b"" if method == "POST" else None)
        hdrs = {"Content-Type": "application/json"}
        if agent:
            hdrs["X-Conversation-Backend-Token"] = TOKEN
        req = urllib.request.Request(self.base + path, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")

    def open_thread(self, message, title=True):
        payload = {"message": message}
        if title:
            payload["title"] = message[:40]
        status, body = self.call("POST", "/internal/conversations", payload, agent=True)
        assert status == 201, (status, body)
        return body["id"]

    def flags(self, cid, **flags):
        status, body = self.call("POST", f"/conversations/{cid}/flags", flags)
        assert status == 200, (status, body)
        return body

    def page(self, query):
        status, body = self.call("GET", f"/conversations?{query}")
        assert status == 200, (status, body)
        return body


class Store:
    """The life store, as the page query sees it. With rdflib, the gateway's
    own query runs over the emitted summary files; without, the same order is
    computed by hand. `down` makes it fail; `frozen` answers from a snapshot
    (the store not caught up yet)."""

    def __init__(self, out_dir: Path):
        self.out_dir = out_dir
        self.down = False
        self.frozen = None
        self.queries = []

    def snapshot(self):
        return {p.name: p.read_text() for p in self.out_dir.glob("*.nt")}

    def __call__(self, query, timeout=None):
        self.queries.append(query)
        if self.down:
            raise OSError("store unreachable")
        files = self.frozen if self.frozen is not None else self.snapshot()
        if HAVE_RDFLIB:
            g = rdflib.Graph()
            for text in files.values():
                g.parse(data=text, format="nt")
            return [{"t": {"value": str(row.t)}} for row in g.query(query)]
        return self._by_hand(files, query)

    @staticmethod
    def _by_hand(files, query):
        import re
        limit, offset = map(int, re.search(r"LIMIT (\d+) OFFSET (\d+)", query).groups())
        kind = re.search(r'kb:kind "([^"]+)"', query)
        rows = []
        for text in files.values():
            if '#archived> "true"' not in text:
                continue
            if kind and f'#kind> "{kind.group(1)}"' not in text:
                continue
            subj = text.split(" ", 1)[0].strip("<>")
            mod = re.search(r'/modified> "([^"]+)"', text)
            if not mod:
                continue
            rows.append(('#starred> "true"' in text, mod.group(1), subj))
        rows.sort(key=lambda r: r[2])
        rows.sort(key=lambda r: (r[0], r[1]), reverse=True)
        return [{"t": {"value": r[2]}} for r in rows[offset:offset + limit]]


def test_gateway(gw: Gateway, out: Path, store: Store):
    wg = gw.wg
    print("the gateway keeps the summaries")
    # No title given: the gateway titles the thread with the start of its
    # first message, so the title is the message's words too.
    a = gw.open_thread("SECRET-WORDS: confirm the plumber on Thursday?", title=False)
    check("the gateway derived the title from the message",
          "SECRET-WORDS" in (wg._load_conv(a) or {}).get("title", ""))
    summary = out / f"{a}.nt"
    check("opening a thread writes its summary", summary.exists())
    text = summary.read_text()
    check("…without the message, nor a title made of it", "SECRET-WORDS" not in text, text)
    titled = gw.open_thread("An agent's thread", title=True)
    check("…nor an explicit title", "An agent" not in (out / f"{titled}.nt").read_text())
    check("…not archived yet", '#archived> "false"' in text)
    gw.flags(a, archived=True)
    check("archiving rewrites it", '#archived> "true"' in summary.read_text())

    # Threads to page through, archived in a known order (newest last).
    ids = [a]
    for n in range(4):
        cid = gw.open_thread(f"Thread {n}")
        gw.flags(cid, archived=True)
        ids.append(cid)
    active = gw.open_thread("Still active")
    star = ids[1]
    gw.flags(star, starred=True)

    print("GET /conversations?archived=1&limit=N")
    newest_first = list(reversed(ids))
    expected = [star] + [c for c in newest_first if c != star]
    # The order the store gives is "updated", which the starring write bumped
    # for `star` only; the rest keep their archive order.
    body = gw.page("archived=1&limit=2")
    check("a page comes from the store", body.get("source") == "store", str(body.get("source")))
    got = [t["id"] for t in body["conversations"]]
    check("starred first, then newest", got == expected[:2], f"{got} vs {expected[:2]}")
    check("has_more while there is more", body["has_more"] is True and body["next_offset"] == 2)
    check("the query asks one past the page", "LIMIT 3 OFFSET 0" in store.queries[-1], store.queries[-1])
    body = gw.page("archived=1&limit=2&offset=4")
    check("the last page", [t["id"] for t in body["conversations"]] == expected[4:]
          and body["has_more"] is False, str(body))
    body = gw.page("archived=1&limit=50")
    check("one page holds them all", [t["id"] for t in body["conversations"]] == expected)
    check("rows are full summaries", "last_preview" in body["conversations"][0]
          and body["conversations"][0]["archived"] is True)
    check("no active thread on it", active not in [t["id"] for t in body["conversations"]])
    body = gw.page("archived=1&limit=0")
    check("a limit is at least one", len(body["conversations"]) == 1)

    full = gw.page("archived=1")
    check("without limit: the full list, as before",
          "has_more" not in full and {t["id"] for t in full["conversations"]} == set(ids))

    print("the store a few seconds behind")
    store.frozen = store.snapshot()
    gone = ids[2]
    # The delete is the dashboard's own act: loopback stands in for the proxy.
    wg._EDGE_PEER_NETS = wg._parse_edge_peers("127.0.0.1")
    status, _ = gw.call("POST", f"/conversations/{gone}/delete")
    check("the thread is deleted", status == 200, str(status))
    check("its summary file goes with it", not (out / f"{gone}.nt").exists())
    restored = ids[3]
    gw.flags(restored, archived=False, muted=False)
    body = gw.page("archived=1&limit=50")
    listed = [t["id"] for t in body["conversations"]]
    check("a deleted thread the store still lists is dropped", gone not in listed, str(listed))
    check("so is a restored one", restored not in listed, str(listed))
    check("the rest stay", set(listed) == set(ids) - {gone, restored}, str(listed))
    # Store order: star, ids4, ids3 (restored), ids2 (deleted), ids0.
    body = gw.page("archived=1&limit=2")
    check("page one is full", [t["id"] for t in body["conversations"]] == [star, ids[4]]
          and body["has_more"] is True and body["next_offset"] == 2, str(body))
    body = gw.page("archived=1&limit=1&offset=2")
    check("stale rows cost no slot: the page reads on to a live one",
          [t["id"] for t in body["conversations"]] == [ids[0]], str(body))
    check("…and the next page starts after what it read",
          body["next_offset"] == 5 and body["has_more"] is False, str(body))
    store.frozen = None

    print("when the store cannot help")
    store.down = True
    body = gw.page("archived=1&limit=2")
    check("store down: the files answer", body.get("source") == "files"
          and [t["id"] for t in body["conversations"]] == [c for c in expected if c not in (gone, restored)][:2],
          str(body))
    check("…paged the same way", body["has_more"] is True)
    store.down = False
    store.frozen = {}
    body = gw.page("archived=1&limit=50")
    check("a store with no archived thread yet: the files answer", body.get("source") == "files"
          and len(body["conversations"]) == len(ids) - 2, str(body.get("source")))
    store.frozen = None

    print("the startup reconcile")
    (out / f"{ids[0]}.nt").unlink()
    stray = "f" * 32
    (out / f"{stray}.nt").write_text("<urn:retinue:thread:%s> <urn:x> <urn:y> .\n" % stray)
    written, removed = wg._thread_index_reconcile()
    check("a missing summary is written", (out / f"{ids[0]}.nt").exists() and written == 1, str(written))
    check("a summary without its thread is removed", not (out / f"{stray}.nt").exists() and removed == 1)
    check("a second run finds nothing to do", wg._thread_index_reconcile() == (0, 0))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="thread-index-"))
    out = tmp / "chambers" / "_generated" / "threads"
    os.environ.update({
        "CONVERSATIONS_DIR": str(tmp / "convs"),
        "CHAMBERS_DIR": str(tmp / "chambers"),
        "THREADS_EMIT_DIR": str(out),
        "CONVERSATION_BACKEND_TOKEN": TOKEN,
        "PRESENTATION_LINT": "0",
        "WEB_GATEWAY_STATE": str(tmp / "state.json"),
        "CONVERSATION_DIR": str(tmp / "convlog"),
        "PUSH_DIR": str(tmp / "push"),
        "ATTENTION_DIR": str(tmp / "attention"),
        "CHAMBERS_MANIFEST": str(tmp / "no-manifest.json"),
        "CLAUDE_CONFIG_DIR": str(tmp / "claude"),
        "QLEVER_LIFE_URL": "http://127.0.0.1:9",
    })
    os.environ.pop("EDGE_PROXY_PEERS", None)
    ti = _load("thread_index", "thread_index.py")
    test_summary(ti)
    wg = _load("web_gateway_under_test", "web-gateway.py")
    store = Store(out)
    wg._sparql_bindings = store
    srv = ThreadingHTTPServer(("127.0.0.1", 0), wg.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        test_gateway(Gateway(wg, srv.server_address[1]), out, store)
    finally:
        srv.shutdown()
    print(f"\n(store played by {'rdflib' if HAVE_RDFLIB else 'a hand-written stand-in'})")
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed:", *FAILURES, sep="\n  ")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
