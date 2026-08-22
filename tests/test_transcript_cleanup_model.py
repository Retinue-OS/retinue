#!/usr/bin/env python3
"""Checks TRANSCRIPT_CLEANUP_MODEL's fallback chain (#29).

The dashboard's voice-input cleanup pass used to hard-default to "haiku"
regardless of RETINUE_CLAUDE_MODEL, so an Ollama/OpenRouter deployment's
cleanup pass silently asked its gateway for an Anthropic-only model name.
TRANSCRIPT_CLEANUP_MODEL now falls back to RETINUE_CLAUDE_MODEL when that is
set, and only then to "haiku"; an explicit TRANSCRIPT_CLEANUP_MODEL always
wins over both.

    python3 tests/test_transcript_cleanup_model.py
"""
import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_gateway(tmp: Path, env: dict[str, str]):
    """Load scripts/web-gateway.py with sandboxed state and a controlled env."""
    for var in ("TRANSCRIPT_CLEANUP_MODEL", "RETINUE_CLAUDE_MODEL"):
        os.environ.pop(var, None)
    os.environ["CONVERSATIONS_DIR"] = str(tmp / "convs")
    os.environ["CONVERSATION_DIR"] = str(tmp / "convlog")
    os.environ["CHAMBERS_DIR"] = str(tmp / "chambers")
    os.environ["WEB_GATEWAY_STATE"] = str(tmp / "state.json")
    (tmp / "chambers").mkdir(parents=True, exist_ok=True)
    os.environ.update(env)
    if "markdown_it" not in sys.modules:
        try:
            import markdown_it  # noqa: F401
        except ImportError:
            stub = types.ModuleType("markdown_it")
            stub.MarkdownIt = object
            sys.modules["markdown_it"] = stub
    sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "web_gateway_transcript_cleanup_under_test", SCRIPTS_DIR / "web-gateway.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_defaults_to_haiku_when_nothing_set():
    with tempfile.TemporaryDirectory() as td:
        wg = _load_gateway(Path(td), {})
        assert wg.TRANSCRIPT_CLEANUP_MODEL == "haiku", wg.TRANSCRIPT_CLEANUP_MODEL


def test_falls_back_to_retinue_claude_model():
    with tempfile.TemporaryDirectory() as td:
        wg = _load_gateway(Path(td), {"RETINUE_CLAUDE_MODEL": "qwen3.5"})
        assert wg.TRANSCRIPT_CLEANUP_MODEL == "qwen3.5", wg.TRANSCRIPT_CLEANUP_MODEL


def test_explicit_value_wins_over_retinue_claude_model():
    with tempfile.TemporaryDirectory() as td:
        wg = _load_gateway(Path(td), {
            "RETINUE_CLAUDE_MODEL": "qwen3.5",
            "TRANSCRIPT_CLEANUP_MODEL": "sonnet",
        })
        assert wg.TRANSCRIPT_CLEANUP_MODEL == "sonnet", wg.TRANSCRIPT_CLEANUP_MODEL


def test_blank_explicit_value_still_falls_back():
    """A leftover `TRANSCRIPT_CLEANUP_MODEL=` in .env must not defeat the
    fallback -- an empty override is treated the same as unset."""
    with tempfile.TemporaryDirectory() as td:
        wg = _load_gateway(Path(td), {
            "RETINUE_CLAUDE_MODEL": "qwen3.5",
            "TRANSCRIPT_CLEANUP_MODEL": "  ",
        })
        assert wg.TRANSCRIPT_CLEANUP_MODEL == "qwen3.5", wg.TRANSCRIPT_CLEANUP_MODEL


def main():
    test_defaults_to_haiku_when_nothing_set()
    test_falls_back_to_retinue_claude_model()
    test_explicit_value_wins_over_retinue_claude_model()
    test_blank_explicit_value_still_falls_back()
    print("all transcript-cleanup-model tests passed")


if __name__ == "__main__":
    main()
