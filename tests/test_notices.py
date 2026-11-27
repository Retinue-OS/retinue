#!/usr/bin/env python3
"""Checks for the notice board (scripts/notices.py) and the ring guard
(scripts/ring_guard.py) in the CLIs that can make something ring.

    python3 tests/test_notices.py
"""
import http.server
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
import notices  # noqa: E402
import ring_guard  # noqa: E402

TIERED_LOW = {"CLAUDECODE": "1", "RETINUE_FRONTIER_MODEL": "big",
              "RETINUE_ROUTER_MODEL": "small", "RETINUE_SESSION_MODEL": "small"}
TIERED_HIGH = dict(TIERED_LOW, RETINUE_SESSION_MODEL="big")


def _at(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


# ── ring guard ───────────────────────────────────────────────────────────────

def test_guard_passes_what_cannot_ring():
    assert ring_guard.check("t", 3.0, None, TIERED_LOW) == (None, 3.0, None)
    assert ring_guard.check("t", None, None, TIERED_LOW) == (None, None, None)


def test_guard_passes_frontier_untiered_and_non_model():
    assert ring_guard.check("t", 5, True, TIERED_HIGH) == (None, 5, True)
    assert ring_guard.check("t", 5, True, {"CLAUDECODE": "1"}) == (None, 5, True)
    no_model = {k: v for k, v in TIERED_LOW.items() if k != "CLAUDECODE"}
    assert ring_guard.check("t", 5, True, no_model) == (None, 5, True)


def test_guard_refuses_junior_with_escalation():
    env = dict(TIERED_LOW, RETINUE_ESCALATE_FILE="/tmp/x")
    assert ring_guard.check("t", 4, None, env)[0] == ring_guard.EXIT_ESCALATE
    assert ring_guard.check("t", None, True, env)[0] == ring_guard.EXIT_ESCALATE


def test_guard_lowers_without_escalation():
    assert ring_guard.check("t", 5, True, TIERED_LOW) == (None, 3.0, False)
    assert ring_guard.check("t", 4, None, TIERED_LOW) == (None, 3.0, None)


class _Sink(http.server.BaseHTTPRequestHandler):
    bodies = []

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        _Sink.bodies.append(json.loads(self.rfile.read(n) or b"{}"))
        out = json.dumps({"id": "f" * 32, "attention": {"delivery": "push"}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


def _server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Sink)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _env(extra, url):
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDECODE", "RETINUE_FRONTIER_MODEL", "RETINUE_ROUTER_MODEL",
                        "RETINUE_SESSION_MODEL", "RETINUE_ESCALATE_FILE", "RETINUE_CLAUDE_MODEL")}
    env.update(extra)
    env["CONVERSATION_BACKEND_TOKEN"] = "t"
    env["CONVERSATION_BACKEND_URL"] = url
    env["ATTENTION_URL"] = "http://127.0.0.1:9"
    return env


def test_conversation_push_is_guarded():
    srv = _server()
    url = f"http://127.0.0.1:{srv.server_port}/internal/conversations"
    try:
        cmd = [sys.executable, str(SCRIPTS / "conversation-push.py"), "--title", "x",
               "--importance", "5", "--critical", "hello"]
        _Sink.bodies.clear()
        r = subprocess.run(cmd, env=_env(dict(TIERED_LOW, RETINUE_ESCALATE_FILE="/tmp/e"), url),
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 3, r.stderr
        assert not _Sink.bodies, "a refused declaration must not reach the gateway"
        r = subprocess.run(cmd, env=_env(TIERED_LOW, url), capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        att = _Sink.bodies[-1]["attention"]
        assert att["importance"] == 3.0 and "critical" not in att, att
        r = subprocess.run(cmd, env=_env(TIERED_HIGH, url), capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        att = _Sink.bodies[-1]["attention"]
        assert att["importance"] == 5 and att["critical"] is True, att
    finally:
        srv.shutdown()


# ── the board ────────────────────────────────────────────────────────────────

def _fresh_dir():
    d = tempfile.mkdtemp(prefix="notices-")
    os.environ["RETINUE_NOTICES_DIR"] = d
    return Path(d)


def test_group_and_handle_up_to_now():
    _fresh_dir()
    notices.add("Example Insurance", "1 document", "12345", "email:a", at=_at("2026-11-20T08:00:00"))
    notices.add("Example Insurance", "1 document", "12345", "email:b", at=_at("2026-11-24T08:00:00"))
    notices.add("Time Portal", "Alex: 3 h on Mon", None, "email:c", at=_at("2026-11-24T09:00:00"))
    gs = notices.board()
    by = {g["group"]: g for g in gs}
    assert by["example-insurance/12345"]["count"] == 2
    assert by["time-portal"]["count"] == 1
    r = notices.handle("example-insurance/12345", at=_at("2026-11-25T10:00:00"))
    assert r["handled"] == 2
    assert [g["group"] for g in notices.board()] == ["time-portal"]
    # a later notice opens the group again, with only itself in it
    notices.add("Example Insurance", "another", "12345", "email:d", at=_at("2026-11-26T08:00:00"))
    g = notices.board("example-insurance/12345")[0]
    assert g["count"] == 1 and g["notices"][0]["content"] == "another"


def test_add_is_idempotent_per_ref():
    _fresh_dir()
    _, created = notices.add("P", "x", ref="email:a")
    _, again = notices.add("P", "x", ref="email:a")
    assert created and not again
    assert notices.board()[0]["count"] == 1


def test_handle_by_id_and_unknown_group():
    d = _fresh_dir()
    rec, _ = notices.add("P", "x", ref="1", at=_at("2026-11-20T08:00:00"))
    notices.add("P", "y", ref="2", at=_at("2026-11-21T08:00:00"))
    notices.handle(notice_id=rec["id"])
    assert notices.board()[0]["count"] == 1
    try:
        notices.handle("nope")
    except LookupError:
        pass
    else:
        raise AssertionError("an unknown group must be refused")
    # The files are N-Triples the life store can load.
    for f in d.glob("*.nt"):
        for line in f.read_text().splitlines():
            assert line.startswith("<urn:retinue:") and line.endswith(" ."), line


def test_literal_round_trip():
    _fresh_dir()
    text = 'Quote "x" \\ back\nslash — ü'
    notices.add("P", text, ref="r")
    assert notices.board()[0]["notices"][0]["content"] == text


def test_urgency_needs_deadline_and_consequence():
    _fresh_dir()
    plain, _ = notices.add("P", "a", ref="1", due=_at("2026-12-01T17:00:00"))
    urgent, _ = notices.add("P", "b", ref="2", due=_at("2026-12-01T17:00:00"), consequence="locked")
    unsure, _ = notices.add("P", "c", ref="3", due=_at("2026-12-01T17:00:00"),
                            consequence="locked", unsure=True)
    assert plain["urgency"] is None and urgent["urgency"] == "urgent" and unsure["urgency"] == "unsure"


def test_cli_urgent_opens_guarded_thread():
    d = _fresh_dir()
    srv = _server()
    url = f"http://127.0.0.1:{srv.server_port}/internal/conversations"
    try:
        base = [sys.executable, str(SCRIPTS / "notices.py"), "add", "--source", "Bank",
                "--text", "confirm login", "--ref", "email:z", "--due", "2026-11-28T12:00:00Z",
                "--consequence", "account locked", "--json"]
        env = _env(dict(TIERED_LOW, RETINUE_ESCALATE_FILE="/tmp/e",
                        RETINUE_NOTICES_DIR=str(d), RETINUE_NOTICES_NOW="2026-11-27T20:00:00Z"), url)
        _Sink.bodies.clear()
        r = subprocess.run(base, env=env, capture_output=True, text=True, timeout=60)
        assert r.returncode == 3, (r.returncode, r.stderr)
        assert not _Sink.bodies
        # The frontier re-run finds the notice filed and opens the thread.
        env = _env(dict(TIERED_HIGH, RETINUE_NOTICES_DIR=str(d),
                        RETINUE_NOTICES_NOW="2026-11-27T20:00:00Z"), url)
        r = subprocess.run(base, env=env, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        out = json.loads(r.stdout)
        assert out["created"] is False and out["thread"] == "f" * 32
        att = _Sink.bodies[-1]["attention"]
        assert att["importance"] == 4.5 and att["critical"] is True, att
        assert len(list(d.glob("*.nt"))) == 1
        # And a third run opens nothing more.
        _Sink.bodies.clear()
        subprocess.run(base, env=env, capture_output=True, text=True, timeout=60)
        assert not _Sink.bodies
    finally:
        srv.shutdown()


def test_mcp_tools():
    _fresh_dir()
    os.environ.setdefault("ARA_MCP_STATE_DIR", tempfile.mkdtemp())
    spec = importlib.util.spec_from_file_location("ara_mcp", SCRIPTS / "ara-mcp-server.py")
    mcp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mcp)
    names = [t["name"] for t in mcp._tools("Ara")]
    assert "list_notices" in names and "handle_notices" in names
    notices.add("P", "x", "acc", ref="1", at=_at("2026-11-20T08:00:00"))
    listed = mcp._tool_list_notices({})
    assert listed["groups"][0]["group"] == "p/acc"
    assert mcp._tool_handle_notices({"group": "p/acc"})["handled"] == 1
    assert mcp._tool_list_notices({})["groups"] == []
    assert "error" in mcp._tool_handle_notices({"group": "p/acc"})


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"{len(tests)} passed")
