#!/usr/bin/env python3
"""Checks the `agent` sender-name override reaches a stored message (#92, #87).

_conv_add_message()/_new_conv() have long accepted an optional `agent`
override -- the displayed sender name (e.g. "Coach") for when a relay answers
on a subagent's behalf -- and the frontend has rendered `m.agent` since PR
#86, but no caller ever set it. This wires a --agent flag through
conversation-push.py (both the thread-open and --thread append paths) into
the two /internal/conversations POST handlers, and checks it lands on the
stored message unchanged when omitted.

    python3 tests/test_web_gateway_agent_sender.py
"""
import importlib.util
import io
import json
import os
import sys
import tempfile
import types
import urllib.request
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


# ── scripts/web-gateway.py: the two internal POST handlers ──────────────────

def _load_gateway(tmp: Path):
    """Load scripts/web-gateway.py with sandboxed state, as the other
    web-gateway tests do."""
    for var in ("RETINUE_CONVERSATION_MODELS", "RETINUE_LITELLM_URL",
                "ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS"):
        os.environ.pop(var, None)
    os.environ["CONVERSATIONS_DIR"] = str(tmp / "convs")
    os.environ["CONVERSATION_DIR"] = str(tmp / "convlog")
    os.environ["CHAMBERS_DIR"] = str(tmp / "chambers")
    os.environ["WEB_GATEWAY_STATE"] = str(tmp / "state.json")
    os.environ["CONVERSATION_BACKEND_TOKEN"] = "test-token"
    (tmp / "chambers").mkdir(parents=True, exist_ok=True)
    if "markdown_it" not in sys.modules:
        try:
            import markdown_it  # noqa: F401
        except ImportError:
            stub = types.ModuleType("markdown_it")
            stub.MarkdownIt = object
            sys.modules["markdown_it"] = stub
    sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "web_gateway_agent_sender_under_test", SCRIPTS_DIR / "web-gateway.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_agent_request(wg, payload: dict, token: str = "test-token"):
    """A stand-in for the request handler: headers + body in, a captured
    _send_json() out -- no real socket I/O."""
    body = json.dumps(payload).encode("utf-8")
    fake = types.SimpleNamespace(
        headers={"Content-Length": str(len(body)),
                 "X-Conversation-Backend-Token": token},
        rfile=io.BytesIO(body),
        status=None,
        response_body=None,
    )

    def _send_json(status, resp_body):
        fake.status = status
        fake.response_body = resp_body

    fake._send_json = _send_json
    fake._read_json_body = lambda: wg.Handler._read_json_body(fake)
    fake._agent_conversation_payload = lambda: wg.Handler._agent_conversation_payload(fake)
    return fake


def test_new_thread_stores_agent_override():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp))
        fake = _fake_agent_request(wg, {"message": "your PR is ready", "agent": "Coach"})
        wg.Handler._handle_agent_conversation(fake)
        assert fake.status == 201, fake.status
        stored = wg._load_conv(fake.response_body["id"])
        assert stored["messages"][0]["agent"] == "Coach", stored["messages"][0]


def test_new_thread_without_agent_stores_no_field():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp))
        fake = _fake_agent_request(wg, {"message": "plain thread"})
        wg.Handler._handle_agent_conversation(fake)
        assert fake.status == 201, fake.status
        stored = wg._load_conv(fake.response_body["id"])
        assert "agent" not in stored["messages"][0], stored["messages"][0]


def test_thread_append_stores_agent_override():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp))
        opened = wg._new_conv("agent", "Web", None, "agent", "first message")
        fake = _fake_agent_request(
            wg, {"message": "a follow-up from a relay", "agent": "Coach"})
        wg.Handler._handle_agent_conversation_message(fake, opened["id"])
        assert fake.status == 201, fake.status
        stored = wg._load_conv(opened["id"])
        assert stored["messages"][-1]["agent"] == "Coach", stored["messages"][-1]


def test_thread_append_without_agent_stores_no_field():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp))
        opened = wg._new_conv("agent", "Web", None, "agent", "first message")
        fake = _fake_agent_request(wg, {"message": "a plain follow-up"})
        wg.Handler._handle_agent_conversation_message(fake, opened["id"])
        assert fake.status == 201, fake.status
        stored = wg._load_conv(opened["id"])
        assert "agent" not in stored["messages"][-1], stored["messages"][-1]


# ── scripts/conversation-push.py: --agent wired into the payload ────────────

def _load_conversation_push():
    os.environ["CONVERSATION_BACKEND_TOKEN"] = "test-token"
    os.environ["CONVERSATION_BACKEND_URL"] = "http://gateway.invalid/internal/conversations"
    spec = importlib.util.spec_from_file_location(
        "conversation_push_under_test", SCRIPTS_DIR / "conversation-push.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeResponse:
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _run_cli_capturing_request(mod, argv):
    """Run main() with urlopen mocked; return (exit_code, captured Request)."""
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        return _FakeResponse({"id": "c" * 32, "title": "T"})

    with patch.object(mod.urllib.request, "urlopen", fake_urlopen):
        old_argv = sys.argv
        sys.argv = ["conversation-push.py"] + argv
        try:
            code = mod.main()
        finally:
            sys.argv = old_argv
    return code, captured.get("req")


def test_cli_new_thread_agent_flag_sets_payload_field():
    mod = _load_conversation_push()
    code, req = _run_cli_capturing_request(
        mod, ["--agent", "Coach", "your PR is ready"])
    assert code == 0, code
    payload = json.loads(req.data.decode("utf-8"))
    assert payload["agent"] == "Coach", payload


def test_cli_thread_append_agent_flag_sets_payload_field():
    mod = _load_conversation_push()
    thread_id = "a" * 32
    code, req = _run_cli_capturing_request(
        mod, ["--thread", thread_id, "--agent", "Coach", "a follow-up"])
    assert code == 0, code
    assert req.full_url.endswith(f"/{thread_id}/messages"), req.full_url
    payload = json.loads(req.data.decode("utf-8"))
    assert payload["agent"] == "Coach", payload


def test_cli_without_agent_flag_omits_payload_field():
    mod = _load_conversation_push()
    code, req = _run_cli_capturing_request(mod, ["plain message, no relay"])
    assert code == 0, code
    payload = json.loads(req.data.decode("utf-8"))
    assert "agent" not in payload, payload


def main():
    test_new_thread_stores_agent_override()
    test_new_thread_without_agent_stores_no_field()
    test_thread_append_stores_agent_override()
    test_thread_append_without_agent_stores_no_field()
    test_cli_new_thread_agent_flag_sets_payload_field()
    test_cli_thread_append_agent_flag_sets_payload_field()
    test_cli_without_agent_flag_omits_payload_field()
    print("all agent-sender-override tests passed")


if __name__ == "__main__":
    main()
