#!/usr/bin/env python3
"""Checks for the Ask-Ara MCP server's protocol handling and tool boundary.

The answering session (a `claude -p` subprocess) is stubbed throughout — these
checks are about the JSON-RPC surface an outside client sees, the read-only
boundary, and the rate limit, none of which should need a model to verify.

    python3 tests/test_ara_mcp_server.py
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

# Import-time env: a tiny sync window keeps the tests fast, and a state dir in
# /tmp keeps them off the real one.
_TMP = tempfile.mkdtemp(prefix="ara-mcp-test-")
os.environ["ARA_MCP_SYNC_WAIT"] = "5"
os.environ["ARA_MCP_STATE_DIR"] = _TMP
os.environ["ARA_MCP_AUDIT"] = "0"
os.environ.setdefault("ARA_MCP_RATE_LIMIT", "30")

spec = importlib.util.spec_from_file_location(
    "ara_mcp_server", SCRIPTS_DIR / "ara-mcp-server.py"
)
mcp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mcp)


def call(method, params=None, rid=1):
    msg = {"jsonrpc": "2.0", "method": method}
    if rid is not None:
        msg["id"] = rid
    if params is not None:
        msg["params"] = params
    return mcp.handle_rpc(msg)


def tool(name, args=None):
    """Call a tool and return its decoded payload."""
    reply = call("tools/call", {"name": name, "arguments": args or {}})
    content = reply["result"]["content"]
    assert content[0]["type"] == "text"
    return json.loads(content[0]["text"]), reply["result"].get("isError", False)


def test_initialize_carries_instructions():
    """The instructions field is the whole point: it retrains the client to ask
    Ara before interrupting the user."""
    res = call("initialize", {"protocolVersion": "2025-06-18",
                              "clientInfo": {"name": "test"}})["result"]
    assert res["protocolVersion"] == "2025-06-18"
    assert res["serverInfo"]["name"] == "ara"
    assert "tools" in res["capabilities"]
    instructions = res["instructions"]
    assert "ask_ara" in instructions
    # It must say to fall back to the user, not to stop.
    assert "user" in instructions.lower()
    print("ok: initialize returns capabilities and client instructions")


def test_initialize_negotiates_protocol_version():
    """An older client gets its own version back; an unknown one gets ours."""
    assert call("initialize", {"protocolVersion": "2024-11-05"})["result"][
        "protocolVersion"] == "2024-11-05"
    assert call("initialize", {"protocolVersion": "1999-01-01"})["result"][
        "protocolVersion"] == mcp.DEFAULT_PROTOCOL
    assert call("initialize", {})["result"][
        "protocolVersion"] == mcp.DEFAULT_PROTOCOL
    print("ok: protocol version negotiation")


def test_notifications_get_no_reply():
    """A JSON-RPC notification has no id and must not be answered."""
    assert mcp.handle_rpc({"jsonrpc": "2.0",
                           "method": "notifications/initialized"}) is None
    assert mcp.handle_rpc({"jsonrpc": "2.0", "method": "ping"}) is None
    print("ok: notifications produce no response")


def test_ping_and_unknown_method():
    assert call("ping")["result"] == {}
    err = call("does/not/exist")["error"]
    assert err["code"] == -32601
    print("ok: ping answers, unknown method is -32601")


def test_tools_list_is_read_only():
    """No tool may send, commit, or edit. tell_ara writes a note, and is the
    only one that leaves a mark — it must not claim to be read-only."""
    names = [t["name"] for t in call("tools/list")["result"]["tools"]]
    assert names == ["ask_ara", "get_answer", "list_projects",
                     "get_project", "tell_ara"], names
    for t in call("tools/list")["result"]["tools"]:
        assert t["description"] and t["inputSchema"]["type"] == "object"
        expected = t["name"] != "tell_ara"
        assert t["annotations"]["readOnlyHint"] is expected, t["name"]
    print("ok: the tool list is the read-only set, tell_ara flagged correctly")


def _capture_session(prompt):
    """Run ``_run_claude`` up to the exec, returning its (argv, kwargs)."""
    captured = {}

    def fake_run(cmd, **kw):
        captured["call"] = (cmd, kw)
        raise RuntimeError("stop before exec")

    real = mcp.subprocess.run
    mcp.subprocess.run = fake_run
    try:
        mcp._run_claude(prompt)
    finally:
        mcp.subprocess.run = real
    return captured["call"]


def test_forbidden_tools_are_stripped_from_the_session():
    """File mutation is removed from the answering session outright."""
    for name in ("Write", "Edit", "NotebookEdit"):
        assert name in mcp.FORBIDDEN_TOOLS
    cmd, _ = _capture_session("hello")
    for name in mcp.FORBIDDEN_TOOLS:
        assert "--disallowed-tools" in cmd and name in cmd, name
    # No permission-mode override: `claude -p` then auto-denies anything the
    # settings allowlist does not already permit.
    assert "--permission-mode" not in cmd
    assert "--dangerously-skip-permissions" not in cmd
    print("ok: the answering session cannot write, edit, or skip permissions")


def test_the_prompt_is_fed_on_stdin_not_as_an_argument():
    """--disallowed-tools is variadic, so a trailing prompt is read as a tool.

    A positional prompt after it never reaches the session; the CLI aborts with
    "Input must be provided either through stdin or as a prompt argument".
    """
    cmd, kw = _capture_session("what is the GV date?")
    assert "what is the GV date?" not in cmd
    assert kw.get("input") == "what is the GV date?"
    # Nothing positional at all: every argument is a flag or a flag's value.
    assert cmd[-1] in mcp.FORBIDDEN_TOOLS or cmd[-1].startswith("-")
    print("ok: the prompt reaches the session on stdin")


def test_ask_ara_returns_the_answer_synchronously():
    real = mcp._run_claude
    mcp._run_claude = lambda prompt: ("done", "The GV is on 29 August.")
    try:
        payload, is_error = tool("ask_ara", {"question": "When is the GV?"})
    finally:
        mcp._run_claude = real
    assert is_error is False
    assert payload["status"] == "done"
    assert "29 August" in payload["answer"]
    print("ok: a fast answer comes back in one round trip")


def test_ask_ara_hands_back_a_job_when_slow():
    """A slow answer must not hang the call; the client polls get_answer."""
    import threading
    release = threading.Event()

    def slow(prompt):
        release.wait(10)
        return "done", "eventually"

    real, real_wait = mcp._run_claude, mcp.SYNC_WAIT
    mcp._run_claude, mcp.SYNC_WAIT = slow, 0.2
    try:
        payload, _ = tool("ask_ara", {"question": "something slow"})
        assert payload["status"] == "pending", payload
        job_id = payload["job_id"]
        assert tool("get_answer", {"job_id": job_id})[0]["status"] == "pending"
        release.set()
        job = mcp._jobs[job_id]
        assert job["event"].wait(10)
        assert tool("get_answer", {"job_id": job_id})[0]["answer"] == "eventually"
    finally:
        release.set()
        mcp._run_claude, mcp.SYNC_WAIT = real, real_wait
    print("ok: a slow answer yields a job handle that get_answer resolves")


def test_ask_ara_reports_a_failed_session_as_an_error():
    real = mcp._run_claude
    mcp._run_claude = lambda prompt: ("error", "boom")
    try:
        payload, _ = tool("ask_ara", {"question": "x"})
    finally:
        mcp._run_claude = real
    assert payload["status"] == "error" and payload["answer"] == "boom"
    print("ok: a failed answering session is reported, not swallowed")


def test_prompt_states_the_read_only_contract():
    prompt = mcp._build_prompt("When is the GV?", "drafting the agenda")
    low = prompt.lower()
    assert "read-only" in low
    assert "do not send" in low and "do not commit" in low
    # The "say you don't know" instruction is what makes the client's fallback
    # to the user correct rather than a guess dressed up as an answer.
    assert "do not know" in low
    assert "When is the GV?" in prompt and "drafting the agenda" in prompt
    print("ok: the answering prompt states the read-only, no-guessing contract")


def test_bad_arguments_are_reported_as_tool_errors():
    """A tool fault is content with isError, not a JSON-RPC error — the model
    should see it and react."""
    payload, is_error = tool("ask_ara", {"question": "   "})
    assert is_error is True and "required" in payload["error"]
    payload, is_error = tool("get_answer", {"job_id": "not-a-job"})
    assert is_error is True
    payload, is_error = tool("get_answer", {"job_id": "0" * 32})
    assert is_error is True and "expired" in payload["error"]
    payload, is_error = tool("tell_ara", {"note": ""})
    assert is_error is True
    print("ok: bad arguments surface as isError content")


def test_unknown_tool_is_a_protocol_error():
    err = call("tools/call", {"name": "rm_rf", "arguments": {}})["error"]
    assert err["code"] == -32602 and "rm_rf" in err["message"]
    print("ok: an unknown tool name is rejected")


def test_a_raising_tool_does_not_break_the_protocol():
    real = mcp.TOOL_IMPL["list_projects"]
    mcp.TOOL_IMPL["list_projects"] = lambda args: (_ for _ in ()).throw(
        RuntimeError("gateway down"))
    try:
        payload, is_error = tool("list_projects")
    finally:
        mcp.TOOL_IMPL["list_projects"] = real
    assert is_error is True and "gateway down" in payload["error"]
    print("ok: a raising tool becomes an error result, not a dropped call")


def test_rate_limit_refuses_and_points_at_the_user():
    real_limit, real_hits = mcp.RATE_LIMIT, list(mcp._rate_hits)
    mcp.RATE_LIMIT = 2
    mcp._rate_hits.clear()
    real = mcp._run_claude
    mcp._run_claude = lambda prompt: ("done", "ok")
    try:
        assert tool("ask_ara", {"question": "a"})[0]["status"] == "done"
        assert tool("ask_ara", {"question": "b"})[0]["status"] == "done"
        payload, is_error = tool("ask_ara", {"question": "c"})
        assert is_error is True
        assert "Rate limit" in payload["error"]
        # It must tell the client what to do instead of just failing.
        assert "user" in payload["error"]
    finally:
        mcp._run_claude = real
        mcp.RATE_LIMIT = real_limit
        mcp._rate_hits.clear()
        mcp._rate_hits.extend(real_hits)
    print("ok: the rate limit refuses and redirects the client to the user")


def test_job_pruning_keeps_pending_work():
    real_retention = mcp.JOB_RETENTION_SECONDS
    mcp.JOB_RETENTION_SECONDS = 0
    try:
        with mcp._jobs_lock:
            mcp._jobs.clear()
            mcp._jobs["a" * 32] = {"status": "done", "answer": "x",
                                   "created": 0, "finished": 0,
                                   "event": mcp.threading.Event()}
            mcp._jobs["b" * 32] = {"status": "pending", "answer": "",
                                   "created": 0,
                                   "event": mcp.threading.Event()}
        mcp._prune_jobs()
        assert "a" * 32 not in mcp._jobs
        assert "b" * 32 in mcp._jobs, "a pending job must never be pruned"
    finally:
        mcp.JOB_RETENTION_SECONDS = real_retention
        with mcp._jobs_lock:
            mcp._jobs.clear()
    print("ok: finished jobs expire, pending ones survive")


def main():
    test_initialize_carries_instructions()
    test_initialize_negotiates_protocol_version()
    test_notifications_get_no_reply()
    test_ping_and_unknown_method()
    test_tools_list_is_read_only()
    test_forbidden_tools_are_stripped_from_the_session()
    test_the_prompt_is_fed_on_stdin_not_as_an_argument()
    test_ask_ara_returns_the_answer_synchronously()
    test_ask_ara_hands_back_a_job_when_slow()
    test_ask_ara_reports_a_failed_session_as_an_error()
    test_prompt_states_the_read_only_contract()
    test_bad_arguments_are_reported_as_tool_errors()
    test_unknown_tool_is_a_protocol_error()
    test_a_raising_tool_does_not_break_the_protocol()
    test_rate_limit_refuses_and_points_at_the_user()
    test_job_pruning_keeps_pending_work()
    print("\nAll Ask-Ara MCP server checks passed.")


if __name__ == "__main__":
    sys.exit(main())
