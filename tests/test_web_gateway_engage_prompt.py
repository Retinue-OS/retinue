#!/usr/bin/env python3
"""Checks that a resumed thread session is shown the messages it missed.

_conv_engage_prompt(conv, fresh=True) used to send only the latest user
message. A message appended from outside that session — triage, a gateway
alert, any agent running `conversation-push.py --thread` — landed in the thread
file but never reached the running session, so the user's reply to it read as a
non-sequitur. Only the 1-hour session expiry, which replays the whole
transcript, ever surfaced such a message.

Covers: the single-message case still sends the bare text (no framing added);
a message pushed since Ara's last reply is replayed with it; a thread with no
assistant message to anchor on falls back to the latest message; and the stale
path still replays everything, mentioning an attachment exactly once.

    python3 tests/test_web_gateway_engage_prompt.py
"""
import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_gateway(tmp: Path):
    """Load scripts/web-gateway.py with sandboxed state, as the sibling
    web-gateway tests do."""
    for var in ("RETINUE_CONVERSATION_MODELS", "RETINUE_LITELLM_URL",
                "ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS"):
        os.environ.pop(var, None)
    os.environ["CONVERSATIONS_DIR"] = str(tmp / "convs")
    os.environ["CONVERSATION_DIR"] = str(tmp / "convlog")
    os.environ["CHAMBERS_DIR"] = str(tmp / "chambers")
    os.environ["WEB_GATEWAY_STATE"] = str(tmp / "state.json")
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
        "web_gateway_engage_under_test", SCRIPTS_DIR / "web-gateway.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _conv(*messages):
    return {"id": "t1", "messages": list(messages)}


def _msg(role, text, attachments=None):
    m = {"role": role, "text": text}
    if attachments:
        m["attachments"] = attachments
    return m


def check_fresh_single_message_is_bare(gw):
    """The common case must stay byte-identical: no framing around one message."""
    conv = _conv(_msg("user", "hi"),
                 _msg("assistant", "hello"),
                 _msg("user", "what about tomorrow?"))
    prompt = gw._conv_engage_prompt(conv, fresh=True)
    assert prompt == "what about tomorrow?", prompt
    print("PASS fresh single message sent bare")


def check_fresh_replays_pushed_message(gw):
    """An agent push between Ara's reply and the user's next message is replayed."""
    conv = _conv(_msg("user", "hi"),
                 _msg("assistant", "hello"),
                 _msg("agent", "New WhatsApp message: I have an emergency"),
                 _msg("user", "that takes priority of course"))
    prompt = gw._conv_engage_prompt(conv, fresh=True)
    assert "since your last reply" in prompt, prompt
    assert "Retinue agent: New WhatsApp message: I have an emergency" in prompt, prompt
    assert "User: that takes priority of course" in prompt, prompt
    # Nothing from before Ara's own reply — the session already holds it.
    assert "hello" not in prompt, prompt
    assert "User: hi" not in prompt, prompt
    print("PASS fresh replays messages pushed since the last reply")


def check_fresh_replays_attachment_paths(gw):
    """A file pushed into the thread is replayed with its on-disk path."""
    att = [{"id": "abc123", "suffix": ".pdf", "filename": "invoice.pdf",
            "content_type": "application/pdf", "size": 42}]
    conv = _conv(_msg("assistant", "hello"),
                 _msg("agent", "Invoice for you", attachments=att),
                 _msg("user", "please file it"))
    prompt = gw._conv_engage_prompt(conv, fresh=True)
    assert "invoice.pdf" in prompt, prompt
    assert str(gw.CONVERSATION_ATTACHMENTS_DIR / "t1" / "abc123.pdf") in prompt, prompt
    # An agent's push is not "the user attached".
    assert "This message attached" in prompt, prompt
    print("PASS fresh replays a pushed attachment's stored path")


def check_fresh_without_anchor_falls_back(gw):
    """With no assistant message we cannot tell what the session saw."""
    conv = _conv(_msg("agent", "opening note"), _msg("user", "ok"))
    prompt = gw._conv_engage_prompt(conv, fresh=True)
    assert prompt == "ok", prompt
    print("PASS fresh without an assistant anchor sends the latest message only")


def check_stale_replays_whole_transcript(gw):
    """The expired-session path is unchanged, and names a file exactly once."""
    att = [{"id": "def456", "suffix": ".csv", "filename": "data.csv",
            "content_type": "text/csv", "size": 7}]
    conv = _conv(_msg("user", "hi"),
                 _msg("assistant", "hello"),
                 _msg("user", "here", attachments=att))
    prompt = gw._conv_engage_prompt(conv, fresh=False)
    assert "continuing a conversation tab" in prompt, prompt
    assert "User: hi" in prompt and "You (Ara): hello" in prompt, prompt
    assert prompt.count("data.csv") == 1, prompt
    assert "The user attached" in prompt, prompt
    print("PASS stale path replays the transcript with one attachment note")


def check_companion_replays_only_the_tail(gw):
    """A companion thread's new session sees its last few messages, not the
    whole thread — the chat note carries the correspondence itself."""
    gw._conv_chat_note = lambda conv: "\n\n[Context: chat note]"
    n = gw.CHAT_COMPANION_THREAD_TAIL + 4
    msgs = [_msg("user" if i % 2 == 0 else "assistant", f"turn {i:02d}")
            for i in range(n)]
    conv = dict(_conv(*msgs), kind="companion")
    prompt = gw._conv_engage_prompt(conv, fresh=False)
    assert "turn 00" not in prompt and "turn 03" not in prompt, prompt
    assert f"turn {n - 1:02d}" in prompt and "turn 04" in prompt, prompt
    assert "4 earlier messages of this thread omitted" in prompt, prompt
    assert prompt.endswith("[Context: chat note]"), prompt
    # Other thread kinds still replay everything.
    prompt = gw._conv_engage_prompt(_conv(*msgs), fresh=False)
    assert "turn 00" in prompt and "omitted" not in prompt, prompt
    print("PASS companion new session replays only the thread's tail")


def check_companion_tail_keeps_latest_message(gw):
    """A tail of 0 (clamped at load) still replays the message being answered:
    a companion turn's new session has no other way to see it."""
    saved = gw.CHAT_COMPANION_THREAD_TAIL
    gw.CHAT_COMPANION_THREAD_TAIL = 1
    try:
        gw._conv_chat_note = lambda conv: "\n\n[Context: chat note]"
        conv = dict(_conv(_msg("user", "old"), _msg("assistant", "reply"),
                          _msg("user", "the latest ask")), kind="companion")
        prompt = gw._conv_engage_prompt(conv, fresh=False)
        assert "the latest ask" in prompt and "old" not in prompt, prompt
        assert "2 earlier messages of this thread omitted" in prompt, prompt
    finally:
        gw.CHAT_COMPANION_THREAD_TAIL = saved
    print("PASS companion tail always carries the latest message")


def main():
    with tempfile.TemporaryDirectory() as tmp:
        gw = _load_gateway(Path(tmp))
        check_fresh_single_message_is_bare(gw)
        check_fresh_replays_pushed_message(gw)
        check_fresh_replays_attachment_paths(gw)
        check_fresh_without_anchor_falls_back(gw)
        check_stale_replays_whole_transcript(gw)
        check_companion_replays_only_the_tail(gw)
        check_companion_tail_keeps_latest_message(gw)
    print("all engage-prompt checks passed")


if __name__ == "__main__":
    main()
