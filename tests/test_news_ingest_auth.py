#!/usr/bin/env python3
"""Checks the authorization contract of POST /internal/news.

The news rail is the one /internal/* endpoint that is open by default: a
deployment gets a working rail with no configuration, and locks it down by
setting NEWS_INGEST_TOKEN. The trap this guards against is reusing
CONVERSATION_BACKEND_TOKEN instead — the entrypoint generates that one whenever
it is missing, so "no token configured" would never be observable and every
untokened gateway call would 403 silently.

    python3 tests/test_news_ingest_auth.py
"""
import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_gateway(tmp: Path, *, news_token: str = "", backend_token: str = ""):
    os.environ["CONVERSATIONS_DIR"] = str(tmp / "convs")
    os.environ["CONVERSATION_DIR"] = str(tmp / "convlog")
    os.environ["CHAMBERS_DIR"] = str(tmp / "chambers")
    os.environ["WEB_GATEWAY_STATE"] = str(tmp / "state.json")
    os.environ["NEWS_INGEST_TOKEN"] = news_token
    os.environ["CONVERSATION_BACKEND_TOKEN"] = backend_token
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
        "web_gateway_news_auth_under_test", SCRIPTS_DIR / "web-gateway.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_open_when_no_token_configured():
    """No NEWS_INGEST_TOKEN → any caller is accepted, with or without a header."""
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp))
        assert wg._news_ingest_authorized("") is True
        assert wg._news_ingest_authorized("whatever") is True


def test_generated_conversation_token_does_not_close_the_rail():
    """The entrypoint's auto-generated CONVERSATION_BACKEND_TOKEN must not gate news.

    Reusing it would make the rail fail-closed in every default deployment: the
    web-gateway would always hold a token the gateway containers never see.
    """
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp), backend_token="a" * 64)
        assert wg._news_ingest_authorized("") is True


def test_enforced_when_token_configured():
    """NEWS_INGEST_TOKEN set → only the matching token is accepted."""
    with tempfile.TemporaryDirectory() as tmp:
        wg = _load_gateway(Path(tmp), news_token="s3cret")
        assert wg._news_ingest_authorized("s3cret") is True
        assert wg._news_ingest_authorized("") is False
        assert wg._news_ingest_authorized("wrong") is False


def test_forwarder_prefers_news_token_then_falls_back():
    """news_ingest sends NEWS_INGEST_TOKEN, falling back to the older variable.

    The fallback keeps a deployment wired before NEWS_INGEST_TOKEN existed
    working unchanged.
    """
    spec = importlib.util.spec_from_file_location(
        "news_ingest_under_test", SCRIPTS_DIR / "news_ingest.py")

    def _load(env):
        for key in ("NEWS_INGEST_TOKEN", "CONVERSATION_BACKEND_TOKEN"):
            os.environ.pop(key, None)
        os.environ.update(env)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    assert _load({"NEWS_INGEST_TOKEN": "news"}).NEWS_INGEST_TOKEN == "news"
    assert _load({"CONVERSATION_BACKEND_TOKEN": "legacy"}).NEWS_INGEST_TOKEN == "legacy"
    assert _load({"NEWS_INGEST_TOKEN": "news",
                  "CONVERSATION_BACKEND_TOKEN": "legacy"}).NEWS_INGEST_TOKEN == "news"
    assert _load({}).NEWS_INGEST_TOKEN == ""


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    print(f"\n{'FAILED' if failures else 'PASSED'} ({failures} failure(s))")
    sys.exit(1 if failures else 0)
