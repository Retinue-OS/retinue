#!/usr/bin/env python3
"""The note Ara gets with a voice take that cancelled a call (web-gateway).

In the phone shell's voice conversation a call waits 5 s with the mic open,
and speech cancels it; the take that did goes out with `cancelled_call:
{number, name}` (webapp/components/conversation.js). The gateway turns that
into agent-only context in wording of its own, so Ara can place the call
again when the words show no wish to stop it. Pinned here: the client gives
only a validated number and a short name — nothing else reaches the prompt —
and the note names the second-attempt directive.

    python3 tests/test_cancelled_call_context.py
"""
import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

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
    print("PASS: cancelled-call context test")
    return 0


if __name__ == "__main__":
    sys.exit(main())
