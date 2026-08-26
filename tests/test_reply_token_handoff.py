#!/usr/bin/env python3
"""Checks that a messenger reply token actually reaches the executing session.

The gateways mint a reply token per forwarded inbox message, but the token used
to die with the *triage* session: its prompt told "the Secretary" to pass
--reply-to to the push CLI while also forbidding any reply, and nothing carried
the token into the dashboard proposal thread — so the session that later acted
on the user's approval had no token and fell back to resolving the sender's
name, the exact wrong-conversation failure reply_tokens.py exists to prevent.
The daily drain (GET /undelivered) returned no token at all.

Covers the repaired chain of custody end to end:

  * web-gateway: an agent message's `context` (the reply command, token
    included) is stored via both /internal/conversations endpoints and
    replayed in Ara's engage prompt — full-transcript and fresh-unseen paths —
    while staying out of the message `text` the dashboard renders. No-context
    threads keep byte-identical prompts.
  * conversation-push.py: --context is wired into both payload paths and
    rejected on a flags-only call.
  * all three gateways: the forward prompt carries a resolvable token and
    instructs the hand-off via --context; _attach_reply_tokens decorates
    drained ledger rows with resolvable, channel-native-origin tokens.

    python3 tests/test_reply_token_handoff.py
"""
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

_REPLY_TO_RE = re.compile(r"--reply-to (\S+)")


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


def _capture_post(module):
    """Replace module.requests.post with a recorder returning HTTP 202."""
    calls = []

    class _Resp:
        status_code = 202

        def raise_for_status(self):
            pass

        def json(self):
            return {"status": "pending"}

    module.requests = types.SimpleNamespace(
        post=lambda url, json=None, timeout=None: calls.append({"url": url, "json": json}) or _Resp(),
        exceptions=module.requests.exceptions,
    )
    return calls


# ── web-gateway: agent context stored and replayed, never user-rendered ──────

def _load_web_gateway(tmp: Path):
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
            # Callable stand-in: web-gateway instantiates MarkdownIt at import
            # time, so a bare `object` stub (enough for the sibling tests when
            # the real library is installed) breaks in a minimal environment.
            class _MarkdownItStub:
                def __init__(self, *args, **kwargs):
                    pass

                def enable(self, *args, **kwargs):
                    return self

                def render(self, text):
                    return str(text)

            stub = types.ModuleType("markdown_it")
            stub.MarkdownIt = _MarkdownItStub
            sys.modules["markdown_it"] = stub
    return _load("web_gateway_reply_token_under_test", "web-gateway.py")


def _fake_agent_request(wg, payload: dict, token: str = "test-token"):
    """A stand-in for the request handler: headers + body in, a captured
    _send_json() out — no real socket I/O."""
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

REPLY_CMD = ('Reply via: python3 /workspace/scripts/whatsapp-push.py '
             '--reply-to v1.payload.sig "<text>"')


def test_new_thread_stores_and_replays_context():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_web_gateway(Path(tmp))
        fake = _fake_agent_request(wg, {
            "message": "Neue Nachricht von Mara … Senden, anpassen oder verwerfen?",
            "context": REPLY_CMD,
        })
        wg.Handler._handle_agent_conversation(fake)
        assert fake.status == 201, fake.status
        stored = wg._load_conv(fake.response_body["id"])
        first = stored["messages"][0]
        assert first["context"] == REPLY_CMD, first
        # The dashboard renders only `text`; the token must not leak into it.
        assert "reply-to" not in first["text"], first["text"]

        # The user replies -> stale/full-transcript engage carries the context.
        wg._conv_add_message(stored["id"], "user", "Ja, senden.")
        prompt = wg._conv_engage_prompt(wg._load_conv(stored["id"]), fresh=False)
        assert REPLY_CMD in prompt, prompt
        assert "not shown to the user" in prompt, prompt
    print("ok: new-thread context is stored and replayed in the engage prompt")


def test_append_context_reaches_fresh_session_via_unseen_tail():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_web_gateway(Path(tmp))
        conv = wg._new_conv("user", "Web", None, "user", "hi")
        wg._conv_add_message(conv["id"], "assistant", "hello")
        # Triage files a message with its reply command while a session is live.
        fake = _fake_agent_request(wg, {
            "message": "Neue WhatsApp-Nachricht von Mara …",
            "context": REPLY_CMD,
        })
        wg.Handler._handle_agent_conversation_message(fake, conv["id"])
        assert fake.status == 201, fake.status
        wg._conv_add_message(conv["id"], "user", "Bitte antworte ihr zu.")
        prompt = wg._conv_engage_prompt(wg._load_conv(conv["id"]), fresh=True)
        assert "since your last reply" in prompt, prompt
        assert REPLY_CMD in prompt, prompt
    print("ok: appended context replays to a fresh session with the unseen tail")


def test_no_context_prompts_stay_identical():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_web_gateway(Path(tmp))
        conv = wg._new_conv("user", "Web", None, "user", "hi")
        wg._conv_add_message(conv["id"], "assistant", "hello")
        wg._conv_add_message(conv["id"], "user", "what about tomorrow?")
        conv = wg._load_conv(conv["id"])
        assert wg._conv_engage_prompt(conv, fresh=True) == "what about tomorrow?"
        assert "Agent context" not in wg._conv_engage_prompt(conv, fresh=False)
    print("ok: threads without context keep byte-identical prompts")


# ── conversation-push.py: --context wired into the payload ───────────────────

def _load_conversation_push():
    os.environ["CONVERSATION_BACKEND_TOKEN"] = "test-token"
    os.environ["CONVERSATION_BACKEND_URL"] = "http://gateway.invalid/internal/conversations"
    return _load("conversation_push_reply_token_under_test", "conversation-push.py")


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


def test_cli_context_flag_on_both_paths():
    mod = _load_conversation_push()
    code, req = _run_cli_capturing_request(
        mod, ["--context", REPLY_CMD, "proposal text"])
    assert code == 0, code
    assert json.loads(req.data.decode("utf-8"))["context"] == REPLY_CMD
    thread_id = "a" * 32
    code, req = _run_cli_capturing_request(
        mod, ["--thread", thread_id, "--context", REPLY_CMD, "follow-up"])
    assert code == 0, code
    assert req.full_url.endswith(f"/{thread_id}/messages"), req.full_url
    assert json.loads(req.data.decode("utf-8"))["context"] == REPLY_CMD
    code, req = _run_cli_capturing_request(mod, ["no context here"])
    assert code == 0, code
    assert "context" not in json.loads(req.data.decode("utf-8"))
    print("ok: conversation-push --context reaches both payload paths")


def test_cli_context_rejected_on_flags_only_call():
    mod = _load_conversation_push()
    code, req = _run_cli_capturing_request(
        mod, ["--thread", "b" * 32, "--archive", "--context", REPLY_CMD])
    assert code == 2 and req is None, (code, req)
    print("ok: conversation-push rejects --context on a flags-only call")


# ── gateways: prompt hand-off instruction + drain-side tokens ────────────────

def _forward_prompt(calls):
    assert len(calls) >= 1, calls
    return calls[-1]["json"]["message"]


def _assert_handoff_prompt(module, prompt, push_script, expected_origin):
    token_match = _REPLY_TO_RE.search(prompt)
    assert token_match, prompt
    assert module.REPLY_TOKENS.resolve(token_match.group(1)) == expected_origin
    assert push_script in prompt, prompt
    # The custody chain: this session proposes; the thread carries the command.
    assert "--context" in prompt, prompt
    assert "You do not send the reply" in prompt, prompt


def _load_whatsapp_gateway(tmp: Path):
    os.environ["WHATSAPP_DATA_DIR"] = str(tmp / "data")
    os.environ["WHATSAPP_TMP_DIR"] = str(tmp / "tmp")
    os.environ["WHATSAPP_PENDING_SENDS_DIR"] = str(tmp / "pending")
    os.environ["INBOUND_STORE_DIR"] = str(tmp / "inbound")
    os.environ["WHATSAPP_REPLY_TOKENS_DIR"] = str(tmp / "reply-tokens")
    return _load("whatsapp_gateway_reply_token_under_test", "whatsapp-gateway.py")


def test_whatsapp_forward_prompt_hands_off_token():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_whatsapp_gateway(Path(tmp))
        calls = _capture_post(wg)
        wg._forward_to_inbox("hallo", "de", "+15551234567",
                             origin="+15551234567@s.whatsapp.net")
        _assert_handoff_prompt(wg, _forward_prompt(calls), "whatsapp-push.py",
                               "+15551234567@s.whatsapp.net")
    print("ok: whatsapp forward prompt hands the token to the proposal thread")


def test_whatsapp_drain_rows_get_tokens():
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_whatsapp_gateway(Path(tmp))
        rows = [
            {"sender": "15551234567", "group": None, "text": "hi"},
            {"sender": "15551234567", "group": "1203633@g.us", "text": "hi all"},
            {"sender": "", "group": None, "text": "orphan"},
        ]
        wg._attach_reply_tokens(rows)
        assert wg.REPLY_TOKENS.resolve(rows[0]["reply_token"]) == "15551234567"
        assert wg.REPLY_TOKENS.resolve(rows[1]["reply_token"]) == "1203633@g.us"
        assert "reply_token" not in rows[2], rows[2]
    print("ok: whatsapp drain rows carry resolvable tokens (group over sender)")


def _load_signal_gateway(tmp: Path):
    _stub_langdetect()
    os.environ["PIPER_DATA_DIR"] = str(tmp / "models")
    os.environ["SIGNAL_ATTACHMENTS_DIR"] = str(tmp / "attachments")
    os.environ["SIGNAL_DATA_DIR"] = str(tmp / "signal-data")
    os.environ["INBOUND_STORE_DIR"] = str(tmp / "inbound")
    os.environ["SIGNAL_REPLY_TOKENS_DIR"] = str(tmp / "reply-tokens")
    os.environ.setdefault("SIGNAL_ACCOUNT", "+15550000000")
    return _load("signal_gateway_reply_token_under_test", "signal-gateway.py")


def test_signal_forward_prompt_hands_off_token():
    with tempfile.TemporaryDirectory() as tmp:
        sg = _load_signal_gateway(Path(tmp))
        calls = _capture_post(sg)
        sg._forward_to_inbox("hallo", "de", "+41791234567")
        _assert_handoff_prompt(sg, _forward_prompt(calls), "signal-push.py",
                               "+41791234567")
        sg._forward_to_inbox("hallo zusammen", "de", "+41791234567",
                             group_id="gAbc==")
        _assert_handoff_prompt(sg, _forward_prompt(calls), "signal-push.py",
                               "group:gAbc==")
    print("ok: signal forward prompt hands the token to the proposal thread")


def test_signal_drain_rows_get_tokens():
    with tempfile.TemporaryDirectory() as tmp:
        sg = _load_signal_gateway(Path(tmp))
        rows = [
            {"sender": "+41791234567", "group": None, "text": "hi"},
            {"sender": "+41791234567", "group": "gAbc==", "text": "hi all"},
        ]
        sg._attach_reply_tokens(rows)
        assert sg.REPLY_TOKENS.resolve(rows[0]["reply_token"]) == "+41791234567"
        assert sg.REPLY_TOKENS.resolve(rows[1]["reply_token"]) == "group:gAbc=="
    print("ok: signal drain rows carry resolvable tokens (group: prefix kept)")


def _load_telegram_gateway(tmp: Path):
    _stub_langdetect()
    os.environ["TELEGRAM_TMP_DIR"] = str(tmp / "tmp")
    os.environ["TELEGRAM_DATA_DIR"] = str(tmp / "data")
    os.environ["TELEGRAM_PENDING_SENDS_DIR"] = str(tmp / "pending")
    os.environ["INBOUND_STORE_DIR"] = str(tmp / "inbound")
    os.environ["TELEGRAM_REPLY_TOKENS_DIR"] = str(tmp / "reply-tokens")
    return _load("telegram_gateway_reply_token_under_test", "telegram-gateway.py")


def test_telegram_forward_prompt_hands_off_token():
    with tempfile.TemporaryDirectory() as tmp:
        tg = _load_telegram_gateway(Path(tmp))
        calls = _capture_post(tg)
        tg._forward_to_inbox("hallo", "de", "123456789")
        _assert_handoff_prompt(tg, _forward_prompt(calls), "telegram-push.py",
                               "123456789")
    print("ok: telegram forward prompt hands the token to the proposal thread")


def test_telegram_drain_rows_get_tokens():
    with tempfile.TemporaryDirectory() as tmp:
        tg = _load_telegram_gateway(Path(tmp))
        rows = [
            {"sender": "123456789", "group": None, "text": "hi"},
            {"sender": "123456789", "group": "-100987", "text": "hi all"},
        ]
        tg._attach_reply_tokens(rows)
        assert tg.REPLY_TOKENS.resolve(rows[0]["reply_token"]) == "123456789"
        assert tg.REPLY_TOKENS.resolve(rows[1]["reply_token"]) == "-100987"
    print("ok: telegram drain rows carry resolvable tokens")


def main():
    test_new_thread_stores_and_replays_context()
    test_append_context_reaches_fresh_session_via_unseen_tail()
    test_no_context_prompts_stay_identical()
    test_cli_context_flag_on_both_paths()
    test_cli_context_rejected_on_flags_only_call()
    test_whatsapp_forward_prompt_hands_off_token()
    test_whatsapp_drain_rows_get_tokens()
    test_signal_forward_prompt_hands_off_token()
    test_signal_drain_rows_get_tokens()
    test_telegram_forward_prompt_hands_off_token()
    test_telegram_drain_rows_get_tokens()
    print("\nAll reply-token hand-off checks passed.")


if __name__ == "__main__":
    main()
