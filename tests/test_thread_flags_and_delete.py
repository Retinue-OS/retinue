#!/usr/bin/env python3
"""The Threads page's swipe, star and Delete, on the gateway side.

Runs the REAL web-gateway handler on a local port. Covers:

  * POST /conversations/<id>/flags — the swipe and its tray: booleans only,
    at least one known flag; `starred` lands on the summary and survives
    archive, mute, an agent's wake and restore; archiving settles the
    attention item and restoring reopens what archiving settled.
  * POST /conversations/<id>/unarchive clears `muted` too (it is the list's
    Restore), and the agent flags endpoint takes `starred`.
  * POST /conversations/<id>/delete — the user's own act: refused from inside
    the container and cross-site, refused for a chat's companion, and
    otherwise erasing the thread, its attachments and its Claude session —
    a turn that ends afterwards cannot record the session back.
  * A deleted thread stays deleted: reads, user and agent writes and a keyed
    open under the key that created it all answer 410 and create nothing.
  * conversation-push.py: --star/--unstar go out as flags, and a 410 is
    reported as the user's decision (exit 0), not as a failure.

    python3 tests/test_thread_flags_and_delete.py
"""
import base64
import importlib.util
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from contextlib import redirect_stderr
from http.server import ThreadingHTTPServer
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
TOKEN = "test-token"

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


class Gateway:
    def __init__(self, wg, port: int):
        self.wg = wg
        self.base = f"http://127.0.0.1:{port}"

    def call(self, method: str, path: str, payload=None, agent=False, headers=None):
        data = json.dumps(payload).encode() if payload is not None else (b"" if method == "POST" else None)
        hdrs = {"Content-Type": "application/json"}
        if agent:
            hdrs["X-Conversation-Backend-Token"] = TOKEN
        hdrs.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")

    def open_thread(self, message, key=None, attachments=None):
        payload = {"message": message, "title": message[:30]}
        if key:
            payload["key"] = key
        if attachments:
            payload["attachments"] = attachments
        status, body = self.call("POST", "/internal/conversations", payload, agent=True)
        assert status == 201, (status, body)
        return body["id"]

    def summary(self, cid, scope="all=1"):
        _, body = self.call("GET", f"/conversations?{scope}")
        return next((t for t in body.get("conversations", []) if t["id"] == cid), None)

    def edge(self, on: bool):
        """Whether loopback counts as the reverse proxy (EDGE_PROXY_PEERS)."""
        self.wg._EDGE_PEER_NETS = self.wg._parse_edge_peers("127.0.0.1") if on else []


def test_flags(gw: Gateway):
    print("POST /conversations/<id>/flags")
    cid = gw.open_thread("Party RSVP: confirm or decline?")
    status, _ = gw.call("POST", f"/conversations/{cid}/flags", {"starred": "yes"})
    check("a non-boolean is refused", status == 400, str(status))
    status, _ = gw.call("POST", f"/conversations/{cid}/flags", {"pinned": True})
    check("no known flag is refused", status == 400, str(status))

    status, body = gw.call("POST", f"/conversations/{cid}/flags", {"starred": True})
    check("starring answers the summary", status == 200 and body.get("starred") is True, str(body))
    check("the list shows the star", (gw.summary(cid, "") or {}).get("starred") is True)
    attention = (gw.wg._load_conv(cid) or {}).get("attention") or {}
    check("a star settles nothing", attention.get("state", "open") == "open", str(attention))

    status, body = gw.call("POST", f"/conversations/{cid}/flags", {"archived": True, "muted": True})
    check("the tray's Mute archives and mutes", status == 200 and body["archived"] and body["muted"])
    check("…and keeps the star", body.get("starred") is True)
    check("it is in the Archived scope", gw.summary(cid, "archived=1") is not None)
    attention = (gw.wg._load_conv(cid) or {}).get("attention") or {}
    check("archiving settles the attention item",
          attention.get("state") == "done" and attention.get("done_how") == "archived", str(attention))

    gw.call("POST", f"/internal/conversations/{cid}/messages", {"message": "News"}, agent=True)
    conv = gw.wg._load_conv(cid)
    check("a muted thread stays archived on news", conv["archived"] is True)

    status, body = gw.call("POST", f"/conversations/{cid}/unarchive")
    check("Unarchive restores", status == 200 and body["archived"] is False, str(body))
    check("…clears mute, as Restore does", body["muted"] is False)
    check("…and keeps the star", body["starred"] is True)
    attention = (gw.wg._load_conv(cid) or {}).get("attention") or {}
    check("restoring reopens what archiving settled", attention.get("state") == "open", str(attention))

    gw.call("POST", f"/conversations/{cid}/flags", {"archived": True})
    gw.call("POST", f"/internal/conversations/{cid}/messages", {"message": "More news"}, agent=True)
    conv = gw.wg._load_conv(cid)
    check("an unmuted archived thread wakes on news", conv["archived"] is False)
    check("…keeping its star", conv.get("starred") is True)

    status, body = gw.call("POST", f"/internal/conversations/{cid}/flags", {"starred": False}, agent=True)
    check("the agent endpoint takes starred", status == 200 and body["starred"] is False, str(body))
    status, body = gw.call("POST", f"/conversations/{cid}/archive")
    check("the bar's Archive leaves muted alone", status == 200 and body["muted"] is False)


def test_delete_gates(gw: Gateway):
    print("POST /conversations/<id>/delete — who may")
    cid = gw.open_thread("Keep me unless the user says so")
    gw.edge(False)
    status, body = gw.call("POST", f"/conversations/{cid}/delete")
    check("refused from inside the container", status == 403, str(status))
    check("…naming the reverse proxy", "reverse proxy" in body.get("error", "")
          and "loopback" in body.get("detail", ""), str(body))
    check("…and the thread is untouched", gw.wg._load_conv(cid) is not None)
    status, _ = gw.call("POST", f"/conversations/{cid}/delete", agent=True)
    check("the agent token is no way around it", status == 403, str(status))

    gw.edge(True)
    status, _ = gw.call("POST", f"/conversations/{cid}/delete",
                        headers={"Sec-Fetch-Site": "cross-site"})
    check("a cross-site request is refused", status == 403, str(status))
    check("…and the thread is untouched", gw.wg._load_conv(cid) is not None)

    gw.wg._conv_set_flags(cid, kind="companion")
    status, _ = gw.call("POST", f"/conversations/{cid}/delete")
    check("a chat's companion is not deleted on its own", status == 409, str(status))
    check("…and stays", gw.wg._load_conv(cid) is not None)

    status, _ = gw.call("POST", f"/conversations/{'0' * 32}/delete")
    check("an unknown thread is 404", status == 404, str(status))


def test_delete_erases(gw: Gateway):
    print("POST /conversations/<id>/delete — what goes, and that it stays gone")
    gw.edge(True)
    key = "whatsapp:+41790000000:41791234567@s.whatsapp.net:DELETEME"
    att = [{"filename": "note.txt", "content_type": "text/plain",
            "data": base64.b64encode(b"private").decode()}]
    cid = gw.open_thread("Something said in the wrong place", key=key, attachments=att)
    att_dir = gw.wg.CONVERSATION_ATTACHMENTS_DIR / cid
    check("the thread has its attachment on disk", att_dir.is_dir() and any(att_dir.iterdir()))

    session_key = gw.wg.CONV_SESSION_KEY_PREFIX + cid
    state = gw.wg._load_state()
    state[session_key] = {"session_id": "11111111-2222-3333-4444-555555555555"}
    gw.wg._save_state(state)

    before = len(list(gw.wg.CONVERSATIONS_DIR.glob("*.json")))
    status, body = gw.call("POST", f"/conversations/{cid}/delete")
    check("deleting answers 200", status == 200 and body.get("deleted") is True, str(body))
    check("the thread file is gone", gw.wg._load_conv(cid) is None)
    check("its attachments are gone", not att_dir.exists())
    check("its session entry is gone", session_key not in gw.wg._load_state())
    recorded = gw.wg._record_turn_session(session_key, {"session_id": "66666666-7777-8888-9999-000000000000"})
    check("a turn ending afterwards cannot record it back",
          recorded is False and session_key not in gw.wg._load_state())
    check("it leaves every list", gw.summary(cid) is None)

    status, body = gw.call("GET", f"/conversations/{cid}")
    check("a read answers 410", status == 410 and body.get("deleted") is True, f"{status} {body}")
    check("…with when", bool(body.get("deleted_at")))
    status, _ = gw.call("POST", f"/conversations/{cid}/messages", {"message": "hello?"})
    check("a user reply answers 410", status == 410, str(status))
    status, _ = gw.call("POST", f"/conversations/{cid}/flags", {"starred": True})
    check("a user flag write answers 410", status == 410, str(status))
    status, _ = gw.call("POST", f"/internal/conversations/{cid}/messages", {"message": "x"}, agent=True)
    check("an agent append answers 410", status == 410, str(status))
    status, _ = gw.call("POST", f"/internal/conversations/{cid}/flags", {"archived": True}, agent=True)
    check("an agent flag write answers 410", status == 410, str(status))
    status, body = gw.call("POST", "/internal/conversations",
                           {"message": "Something said in the wrong place", "key": key}, agent=True)
    check("a redelivered keyed open answers 410", status == 410 and body.get("id") == cid, f"{status} {body}")
    status, _ = gw.call("POST", "/internal/conversations",
                        {"message": "A corrected proposal", "key": key}, agent=True)
    check("…whatever it says", status == 410, str(status))
    after = len(list(gw.wg.CONVERSATIONS_DIR.glob("*.json")))
    check("and nothing was created in its place", after == before - 1, f"{before} -> {after}")

    status, body = gw.call("POST", f"/conversations/{cid}/delete")
    check("deleting again is a no-op success", status == 200 and body.get("deleted") is True)


def _run_push(mod, argv, error=None, response=None):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        if error is not None:
            raise urllib.error.HTTPError(req.full_url, error, "Gone", {},
                                         BytesIO(b'{"error": "the user deleted this conversation"}'))

        class R:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(response or {"id": "c" * 32}).encode()
        return R()

    err, out = StringIO(), StringIO()
    with patch.object(mod.urllib.request, "urlopen", fake_urlopen), redirect_stderr(err), \
            patch("sys.stdout", out):
        old = sys.argv
        sys.argv = ["conversation-push.py"] + argv
        try:
            code = mod.main()
        finally:
            sys.argv = old
    return code, captured, err.getvalue()


def test_conversation_push():
    print("conversation-push.py: --star, and a deleted thread")
    os.environ["CONVERSATION_BACKEND_TOKEN"] = TOKEN
    os.environ["CONVERSATION_BACKEND_URL"] = "http://127.0.0.1:9/internal/conversations"
    mod = _load("conversation_push_under_test", "conversation-push.py")
    thread = "a" * 32
    code, cap, _ = _run_push(mod, ["--thread", thread, "--star"])
    check("--star is a flags-only call", code == 0 and cap["payload"] == {"starred": True}, str(cap))
    check("…to the thread's flags", cap["url"].endswith(f"/{thread}/flags"), cap.get("url", ""))
    code, cap, _ = _run_push(mod, ["--thread", thread, "--unstar"])
    check("--unstar clears it", cap["payload"] == {"starred": False}, str(cap))
    code, _, err = _run_push(mod, ["--star", "x"])
    check("--star with a message is refused", code == 2, str(code))
    code, _, err = _run_push(mod, ["--thread", thread, "News for you"], error=410)
    check("a deleted thread is not a failure", code == 0, str(code))
    check("…and says so", "deleted this thread" in err and "do not reopen" in err, err)
    code, _, err = _run_push(mod, ["--thread", thread, "News"], error=500)
    check("a real error still fails", code == 1, str(code))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="thread-flags-"))
    os.environ.update({
        "CONVERSATIONS_DIR": str(tmp / "convs"),
        "CONVERSATION_BACKEND_TOKEN": TOKEN,
        "PRESENTATION_LINT": "0",
        "WEB_GATEWAY_STATE": str(tmp / "state.json"),
        "CONVERSATION_DIR": str(tmp / "convlog"),
        "PUSH_DIR": str(tmp / "push"),
        "CLAUDE_CONFIG_DIR": str(tmp / "claude"),
    })
    os.environ.pop("EDGE_PROXY_PEERS", None)
    wg = _load("web_gateway_under_test", "web-gateway.py")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), wg.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    gw = Gateway(wg, srv.server_address[1])
    try:
        test_flags(gw)
        test_delete_gates(gw)
        test_delete_erases(gw)
    finally:
        srv.shutdown()
    test_conversation_push()
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed:", *FAILURES, sep="\n  ")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
