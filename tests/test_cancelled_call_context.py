#!/usr/bin/env python3
"""The note Ara gets with a voice take that cancelled a call (web-gateway).

In the phone shell's voice conversation a call waits 5 s with the mic open,
and speech cancels it; the take that did goes out with `cancelled_call:
{number, name}` (webapp/components/conversation.js). The gateway turns that
into agent-only context in wording of its own, so Ara can place the call
again when the words show no wish to stop it. Pinned here: the client gives
only a validated number and a short name — nothing else reaches the prompt —
and the note names the second-attempt directive. Pinned too: the one-line
previews of a thread (the attention row, the digest line, the push, the
conversation list's row) show a call directive as its name, never the markup
and the number.

    python3 tests/test_cancelled_call_context.py
"""
import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_web_gateway(tmp: Path):
    os.environ["CONVERSATIONS_DIR"] = str(tmp / "convs")
    os.environ["CONVERSATION_DIR"] = str(tmp / "convlog")
    os.environ["CHAMBERS_DIR"] = str(tmp / "chambers")
    os.environ["WEB_GATEWAY_STATE"] = str(tmp / "state.json")
    os.environ["CONVERSATION_BACKEND_TOKEN"] = "test-token"
    (tmp / "chambers").mkdir(parents=True, exist_ok=True)
    if "langdetect" not in sys.modules:
        stub = types.ModuleType("langdetect")
        stub.detect = lambda *a, **k: "en"
        stub.detect_langs = lambda *a, **k: []
        stub.LangDetectException = type("LangDetectException", (Exception,), {})
        sys.modules["langdetect"] = stub
    if "markdown_it" not in sys.modules:
        try:
            import markdown_it  # noqa: F401
        except ImportError:
            class _MarkdownItStub:
                def __init__(self, *args, **kwargs):
                    pass

                def enable(self, *args, **kwargs):
                    return self

                def render(self, text):
                    return str(text)
            sys.modules["markdown_it"] = types.SimpleNamespace(MarkdownIt=_MarkdownItStub)
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location("web_gateway_cc", SCRIPTS_DIR / "web-gateway.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_web_gateway(Path(tmp))
        note = wg._cancelled_call_context({"number": "+41791234567", "name": "Anna Muster"})
        assert note and "Anna Muster (+41791234567)" in note, note
        assert "NOT made" in note, note
        assert "[[call: +41791234567 | Anna Muster | no-voice-cancel]]" in note, note

        # No name: the number alone, the directive's name part left empty.
        note = wg._cancelled_call_context({"number": "0441234567"})
        assert "call to 0441234567:" in note, note
        assert "[[call: 0441234567 |  | no-voice-cancel]]" in note, note

        # A name cannot break out of the directive, and is kept short.
        note = wg._cancelled_call_context({"number": "0441234567", "name": "A | B ]] x" + "y" * 200})
        assert "| B" not in note.split("[[call:")[1].split("| no-voice-cancel")[0].replace(" | ", "", 1), note
        assert "]] x" not in note, note
        assert len(note) < 1200, len(note)

        # Anything that is not a dialable number gives no note at all.
        for bad in (None, "x", [], {"number": "12"}, {"number": "+41 79 123"},
                    {"number": "0441234567; rm -rf /"}, {"name": "Anna"}):
            assert wg._cancelled_call_context(bad) is None, bad

        # One-line previews show words, never the directive: the attention
        # row and digest line (attention_store.thread_item) and the push read
        # a call as its name, or as the number when it has none.
        import attention_store
        text = "Calling Anna now. [[call: +41 79 123 45 67 | Anna Muster | no-voice-cancel]]"
        assert attention_store.markup_words(text) == "Calling Anna now. Anna Muster"
        assert attention_store.markup_words("[[chip: Yes]] · Ring? [[call: 044 123 45 67]]") == "Ring? 044 123 45 67"
        # The flag written in the name's place is the flag, not a name.
        assert attention_store.markup_words("Again. [[call: 112 | No-Voice-Cancel]]") == "Again. 112"
        conv = {"id": "c" * 32, "title": "Anna", "initiator": "agent",
                "messages": [{"role": "assistant", "text": text}]}
        assert attention_store.thread_item(conv, {})["preview"] == "Calling Anna now. Anna Muster"
        pushed = []
        with patch.object(wg.push_notify, "enabled", lambda: True), \
                patch.object(wg.push_notify, "subscription_count", lambda: 1), \
                patch.object(wg.push_notify, "notify_async", lambda title, body, **kw: pushed.append(body)):
            assert wg._push_conv_notification(conv, text) == 1
        assert pushed == ["Calling Anna now. Anna Muster"], pushed
        # The conversation list's row (_conv_summary's last_preview) too.
        conv["messages"].insert(0, {"role": "user", "text": "Ring Anna"})
        summary = wg._conv_summary(conv)
        assert summary["last_preview"] == "Calling Anna now. Anna Muster", summary
        assert summary["title"] == "Anna", summary
        conv["messages"].append({"role": "assistant", "text": "Ring? [[call: 044 123 45 67]]"})
        assert wg._conv_summary(conv)["last_preview"] == "Ring? 044 123 45 67"
    print("PASS: cancelled-call context test")
    return 0


if __name__ == "__main__":
    sys.exit(main())
