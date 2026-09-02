#!/usr/bin/env python3
"""Checks that one inbound message can only ever open one dashboard thread.

Opening a thread is a side effect, and the same inbound is legitimately handled
more than once: the escalation re-run replays a junior turn's prompt on the
frontier model (its reply is discarded, but a thread it already opened is not),
a channel can redeliver a stanza after a reconnect, and a live turn that dies
before finishing leaves its ledger record undelivered for the daily drain to
pick up again. Each of those used to raise its own thread, so the user got the
same message twice.

Covers the whole chain:

  * inbound_store.thread_key: the canonical identity — account and chat carried
    alongside the native message id, because Telegram numbers messages per chat,
    Signal identifies one by (source, timestamp), and a deployment may run two
    gateways on one channel. The id-less fallback never merges distinct
    arrivals.
  * web-gateway: POST /internal/conversations with `key` creates once and
    thereafter returns the same thread (200, deduplicated, nothing appended and
    nothing pushed); concurrent duplicates collapse; a deleted thread frees its
    key; an oversized key is rejected; keyless creates behave as before.
  * conversation-push.py: --key reaches the payload when opening a thread, and
    is left out when appending to one (--thread already addresses it).
  * all three gateways: the live forward prompt and the drained ledger row
    carry the *same* key for the same message.

    python3 tests/test_thread_idempotency.py
"""
import importlib.util
import json
import os
import sys
import tempfile
import threading
import types
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

FAILURES: list[str] = []


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


def _stub_langdetect():
    if "langdetect" not in sys.modules:
        stub = types.ModuleType("langdetect")
        stub.detect = lambda *a, **k: "en"
        stub.detect_langs = lambda *a, **k: []
        stub.LangDetectException = type("LangDetectException", (Exception,), {})
        sys.modules["langdetect"] = stub


# ── 1. The canonical key ──────────────────────────────────────────────────────

def test_thread_key():
    print("inbound_store.thread_key")
    ibs = _load("ibstore_under_test", "inbound_store.py")

    same = ibs.thread_key("telegram", "+41790000000", "chat-1", "77")
    check("same message yields the same key",
          same == ibs.thread_key("telegram", "+41790000000", "chat-1", "77"))

    # Telegram numbers messages per chat: the id alone would collide.
    check("same id in a different chat is a different key",
          same != ibs.thread_key("telegram", "+41790000000", "chat-2", "77"))
    # A deployment may run two gateways on one channel.
    check("same id on a different receiving account is a different key",
          same != ibs.thread_key("telegram", "+41791111111", "chat-1", "77"))
    check("same id on a different channel is a different key",
          same != ibs.thread_key("signal", "+41790000000", "chat-1", "77"))

    # Signal identifies an inbound by (source, sent timestamp): two senders can
    # share a millisecond, so the chat must separate them.
    ts = "1764500000123"
    check("same Signal timestamp from two senders does not merge",
          ibs.thread_key("signal", "+41790000000", "+41791234567", ts)
          != ibs.thread_key("signal", "+41790000000", "+41799999999", ts))

    # No native id: never merge distinct arrivals.
    a = ibs.thread_key("whatsapp", "+41790000000", "chat-1", None)
    b = ibs.thread_key("whatsapp", "+41790000000", "chat-1", None)
    check("id-less arrivals never collide", a != b, f"{a} vs {b}")
    check("id-less key is still channel-scoped", a.startswith("whatsapp:"))

    # The record's own URN keeps a drained id-less record's key stable.
    subj = "urn:retinue:inbound:whatsapp:deadbeef"
    check("a subject pins the id-less key",
          ibs.thread_key("whatsapp", "+41790000000", "chat-1", None, subject=subj)
          == subj)
    check("a subject never overrides a real message id",
          ibs.thread_key("whatsapp", "+41790000000", "chat-1", "ABC", subject=subj)
          != subj)


# ── 2. The gateway endpoint ───────────────────────────────────────────────────

def test_gateway_dedupe():
    print("web-gateway: POST /internal/conversations with a key")
    conv_dir = tempfile.mkdtemp(prefix="convs-")
    os.environ.update({
        "CONVERSATIONS_DIR": conv_dir,
        "CONVERSATION_BACKEND_TOKEN": "test-token",
        "PRESENTATION_LINT": "0",          # no model calls in a unit test
        "CONVERSATION_BASE_URL": "https://retinue.example.org",
    })
    wg = _load("web_gateway_under_test", "web-gateway.py")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), wg.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def create(message, key=None, title=None):
        payload = {"message": message}
        if key:
            payload["key"] = key
        if title:
            payload["title"] = title
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/internal/conversations",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "X-Conversation-Backend-Token": "test-token"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")

    def get_conv(cid):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/conversations/{cid}", timeout=10) as r:
            return json.loads(r.read().decode())

    try:
        KEY = "whatsapp:+41790000000:41791234567@s.whatsapp.net:3EB0C767D2"
        s1, b1 = create("Voice note from Stefanie", key=KEY, title="WhatsApp")
        s2, b2 = create("Voice note from Stefanie", key=KEY, title="WhatsApp")
        check("first create returns 201", s1 == 201, str(s1))
        check("the repeat returns 200", s2 == 200, str(s2))
        check("the repeat is flagged deduplicated", b2.get("deduplicated") is True)
        check("the repeat reuses the same thread", b1["id"] == b2.get("id"))
        check("the repeat carries the thread url",
              str(b2.get("url", "")).endswith(b1["id"]))
        check("nothing was appended to the thread",
              len(get_conv(b1["id"])["messages"]) == 1)
        check("the repeat pushed no notification", "push_subscribers" not in b2)

        s3, b3 = create("Another message", key=KEY + "-other")
        check("a different key opens its own thread",
              s3 == 201 and b3["id"] != b1["id"])

        _, b4 = create("Keyless", title="Keyless")
        _, b5 = create("Keyless", title="Keyless")
        check("keyless creates stay independent", b4["id"] != b5["id"])

        os.remove(os.path.join(conv_dir, b3["id"] + ".json"))
        s6, b6 = create("Another message", key=KEY + "-other")
        check("a deleted thread frees its key",
              s6 == 201 and b6["id"] != b3["id"], str(s6))

        s7, _ = create("x", key="k" * 500)
        check("an oversized key is rejected", s7 == 400, str(s7))

        # The escalation re-run and a redelivery are both "the same key twice,
        # close together" — including genuinely concurrent.
        results = []
        def worker():
            results.append(create("Race", key=KEY + "-race"))
        threads = [threading.Thread(target=worker) for _ in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        # Every worker must have come back: five of them silently erroring
        # would leave one 201 in `results` and satisfy the checks below while
        # proving nothing.
        check("all six concurrent requests returned", len(results) == 6, str(len(results)))
        ids = {b["id"] for _, b in results}
        check("concurrent duplicates collapse to one thread", len(ids) == 1, str(ids))
        check("exactly one of them created it",
              sum(1 for s, _ in results if s == 201) == 1)
        check("the other five were served the existing thread",
              sum(1 for s, _ in results if s == 200) == 5,
              str(sorted(s for s, _ in results)))
    finally:
        srv.shutdown()


# ── 3. The CLI ────────────────────────────────────────────────────────────────

def test_conversation_push_key():
    print("conversation-push.py: --key")
    push = _load("conversation_push_under_test", "conversation-push.py")
    src = (SCRIPTS_DIR / "conversation-push.py").read_text(encoding="utf-8")
    check("--key is a documented option", '"--key"' in src)
    check("the key reaches the payload when opening a thread",
          'payload["key"] = args.key' in src)
    check("the key is withheld when appending to a named thread",
          "if args.key and not args.thread:" in src)
    check("a deduplicated response is reported to the caller",
          'body.get("deduplicated")' in src)


# ── 4. Live forward and drain agree ───────────────────────────────────────────

def test_gateways_agree():
    print("gateways: the live prompt and the drained row carry the same key")
    _stub_langdetect()
    ibs = _load("ibstore_for_gateways", "inbound_store.py")

    for channel, account, chat, mid in (
            ("signal", "+41790000000", "+41791234567", "1764500000123"),
            ("whatsapp", "+41790000000", "41791234567@s.whatsapp.net", "3EB0C767D2"),
            ("telegram", "+41790000000", "12345678", "77")):
        live = ibs.thread_key(channel, account, chat, mid)
        drained = ibs.thread_key(channel, account, chat, mid,
                                 subject=f"urn:retinue:inbound:{channel}:cafe")
        check(f"{channel}: drain reproduces the live key", live == drained)

    # Every gateway builds its key through the shared helper and decorates its
    # drained rows with one — grepped, since importing them needs signal-cli et al.
    for script, chan in (("signal-gateway.py", "signal"),
                         ("whatsapp-gateway.py", "whatsapp"),
                         ("telegram-gateway.py", "telegram")):
        src = (SCRIPTS_DIR / script).read_text(encoding="utf-8")
        check(f"{chan}: live forward uses the shared helper",
              "thread_key = _ibstore.thread_key(" in src)
        check(f"{chan}: the prompt hands the key to triage",
              "Thread key:" in src)
        check(f"{chan}: drained rows carry the key",
              'msg["thread_key"] = _ibstore.thread_key(' in src)
        check(f"{chan}: the key is not built from the message id alone",
              f'"{chan}:" + (message_id' not in src)


if __name__ == "__main__":
    test_thread_key()
    test_gateway_dedupe()
    test_conversation_push_key()
    test_gateways_agree()
    print()
    if FAILURES:
        print("FAILED: " + ", ".join(FAILURES))
        sys.exit(1)
    print("ALL PASS")
