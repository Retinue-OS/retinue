#!/usr/bin/env python3
"""Focused checks for the conversation-model picker sources in the web gateway.

Covers the pure logic without an HTTP server or a live LiteLLM: parsing a
GET /model/info response (picker flag, labels, wildcard filtering, duplicate
name and duplicate label collapsing),
the env > LiteLLM > file > default precedence, the cache (TTL, last-good on
failure, forced refresh on a validation miss), and the auth-header derivation
from ANTHROPIC_CUSTOM_HEADERS.

    python3 tests/test_web_gateway_models.py
"""
import importlib.util
import json
import os
import sys
import tempfile
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_gateway(tmp: Path, env: dict[str, str]):
    """Load scripts/web-gateway.py with sandboxed state and a controlled env."""
    for var in ("RETINUE_CONVERSATION_MODELS", "RETINUE_CONVERSATION_MODELS_FILE",
                "RETINUE_LITELLM_URL", "RETINUE_LITELLM_KEY",
                "RETINUE_MODELS_CACHE_SECONDS", "ANTHROPIC_BASE_URL",
                "ANTHROPIC_CUSTOM_HEADERS", "LITELLM_MASTER_KEY",
                "LITELLM_PRIMARY_MODEL", "RETINUE_CLAUDE_MODEL", "ANTHROPIC_MODEL",
                "RETINUE_OLLAMA_URL", "OLLAMA_API_BASE", "OLLAMA_HOST"):
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
        "web_gateway_models_under_test", SCRIPTS_DIR / "web-gateway.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _model_info_response(*entries):
    return {"data": list(entries)}


def _route(name, picker=None, label=None, info_extra=None):
    info = dict(info_extra or {})
    if picker is not None:
        info["retinue_picker"] = picker
    if label is not None:
        info["retinue_label"] = label
    return {"model_name": name, "model_info": info}


def test_coerce_litellm_models(wg):
    parsed = _model_info_response(
        _route("retinue-claude"),                       # unflagged plumbing
        _route("claude-*", picker=True),                # wildcard, dropped
        _route("claude-opus-5", picker=True, label="Opus (deepest reasoning)"),
        _route("claude-opus-5", picker=True, label="DB duplicate"),
        # Same route under its target id, same label -> one picker entry.
        _route("anthropic/claude-opus-5", picker=True,
               label="Opus (deepest reasoning)"),
        _route("claude-sonnet-5", picker=True),         # label falls back to id
        _route("my-gpt", picker=False, label="off"),    # explicitly off
        {"model_name": "broken"},                       # no model_info at all
    )
    models = wg._coerce_litellm_models(parsed)
    assert models == [
        {"id": "claude-opus-5", "label": "Opus (deepest reasoning)"},
        {"id": "claude-sonnet-5", "label": "claude-sonnet-5"},
        {"id": "broken", "label": "broken"},
    ], models
    assert wg._coerce_litellm_models({"data": None}) == []
    assert wg._coerce_litellm_models([]) == []


def test_coerce_unflagged_advertised_models(wg):
    # No retinue_picker flags: offer concrete advertised routes, hide plumbing.
    parsed = _model_info_response(
        _route("retinue-claude"),
        _route("retinue-openrouter"),
        _route("claude-opus-5"),                    # unflagged Claude catalog
        _route("ollama/*"),
        _route("ollama/qwen3.6"),
        _route("ollama/llama3.2"),
        {"id": "from-v1-only"},
    )
    models = wg._coerce_litellm_models(parsed)
    assert models == [
        {"id": "ollama/qwen3.6", "label": "qwen3.6"},
        {"id": "ollama/llama3.2", "label": "llama3.2"},
        {"id": "from-v1-only", "label": "from-v1-only"},
    ], models


def test_coerce_intersects_v1_models_when_unflagged(wg):
    parsed = _model_info_response(
        _route("claude-opus-5"),                    # still hidden when unflagged
        _route("ollama/qwen3.6"),
        _route("ollama/ghost"),
    )
    models = wg._coerce_litellm_models(
        parsed, listed_ids=["ollama/qwen3.6", "ollama/mistral", "claude-sonnet-5"])
    assert models == [
        {"id": "ollama/qwen3.6", "label": "qwen3.6"},
        {"id": "ollama/mistral", "label": "mistral"},
    ], models


def test_ollama_backend_hides_leftover_claude_flags(wg):
    parsed = _model_info_response(
        _route("claude-opus-5", picker=True, label="Opus (deepest reasoning)"),
        _route("claude-sonnet-5", picker=True, label="Sonnet (balanced)"),
        _route("claude-haiku-4-5", picker=True, label="Haiku (fastest)"),
        _route("ollama/qwen3.6"),
    )
    models = wg._coerce_litellm_models(parsed)
    assert models == [{"id": "ollama/qwen3.6", "label": "qwen3.6"}], models
    # Primary pinned to Ollama even when the proxy has not expanded tags yet.
    os.environ["LITELLM_PRIMARY_MODEL"] = "ollama/qwen3.6"
    only_claude = _model_info_response(
        _route("claude-opus-5", picker=True, label="Opus (deepest reasoning)"))
    assert wg._coerce_litellm_models(only_claude) == []
    os.environ.pop("LITELLM_PRIMARY_MODEL")


def test_coerce_ollama_tags(wg):
    tags = {
        "models": [
            {"name": "qwen3.6:latest"},
            {"name": "gemma4:12b"},
            {"model": "llama3:latest"},
            {"name": "qwen3.6:latest"},
            {},
        ]
    }
    assert wg._coerce_ollama_tags(tags) == [
        {"id": "ollama/qwen3.6:latest", "label": "qwen3.6:latest"},
        {"id": "ollama/gemma4:12b", "label": "gemma4:12b"},
        {"id": "ollama/llama3:latest", "label": "llama3:latest"},
    ]


def _fetch_ollama_with_fake_opener(wg, base_url):
    """Run _fetch_ollama_models() against base_url, capturing opener handlers."""
    import io
    os.environ["OLLAMA_API_BASE"] = base_url
    captured = {}

    class FakeOpener:
        def open(self, req, timeout=None):
            captured["url"] = req.full_url
            return io.BytesIO(b'{"models":[{"name":"qwen3.6:latest"}]}')

    def fake_build_opener(*handlers):
        captured["handlers"] = handlers
        return FakeOpener()

    orig = wg.urllib.request.build_opener
    wg.urllib.request.build_opener = fake_build_opener
    try:
        captured["models"] = wg._fetch_ollama_models()
    finally:
        wg.urllib.request.build_opener = orig
        os.environ.pop("OLLAMA_API_BASE", None)
    return captured


def _bypasses_proxy(wg, handlers):
    return any(
        type(h) is wg.urllib.request.ProxyHandler and getattr(h, "proxies", None) == {}
        for h in handlers
    )


def test_fetch_ollama_bypasses_http_proxy_for_local_host(wg):
    for base in ("http://host.docker.internal:11434", "http://localhost:11434",
                 "http://127.0.0.1:11434"):
        captured = _fetch_ollama_with_fake_opener(wg, base)
        assert captured["url"] == base + "/api/tags"
        assert _bypasses_proxy(wg, captured["handlers"]), (base, captured["handlers"])
        assert captured["models"] == [
            {"id": "ollama/qwen3.6:latest", "label": "qwen3.6:latest"}]


def test_fetch_ollama_keeps_proxy_for_remote_host(wg):
    """A non-local Ollama URL is ordinary egress — it must stay auditable."""
    captured = _fetch_ollama_with_fake_opener(wg, "http://ollama.example.com:11434")
    assert captured["url"] == "http://ollama.example.com:11434/api/tags"
    assert not _bypasses_proxy(wg, captured["handlers"]), captured["handlers"]
    assert captured["models"] == [
        {"id": "ollama/qwen3.6:latest", "label": "qwen3.6:latest"}]


def test_merge_replaces_stale_ollama_catalog(wg):
    merged = wg._merge_ollama_tags(
        [{"id": "ollama/llama2", "label": "llama2"}],
        [{"id": "ollama/qwen3.6:latest", "label": "qwen3.6:latest"},
         {"id": "ollama/gemma4:12b", "label": "gemma4:12b"}],
    )
    assert merged == [
        {"id": "ollama/qwen3.6:latest", "label": "qwen3.6:latest"},
        {"id": "ollama/gemma4:12b", "label": "gemma4:12b"},
    ]


def test_dynamic_list_and_default_entry(wg):
    wg._fetch_litellm_models = lambda: [
        {"id": "claude-opus-5", "label": "Opus"}]
    models = wg._conversation_models()
    assert models == [{"id": "", "label": "Default"},
                      {"id": "claude-opus-5", "label": "Opus"}], models
    # The offered ids drive validation.
    assert wg._model_offered("claude-opus-5")
    assert wg._valid_model_id("claude-opus-5") == "claude-opus-5"
    assert wg._valid_model_id("") is None


def test_static_fallback_when_litellm_empty_or_down(wg):
    # Reachable but nothing advertised -> Default only, not the Claude aliases.
    wg._fetch_litellm_models = lambda: []
    assert wg._conversation_models(force=True) == [wg._DEFAULT_MODEL_ENTRY]
    # Unreachable with no last-good list -> Default only.
    def boom():
        raise OSError("connection refused")
    wg._fetch_litellm_models = boom
    assert wg._conversation_models(force=True) == [wg._DEFAULT_MODEL_ENTRY]
    assert not wg._model_offered("claude-opus-5")
    assert not wg._model_offered("opus")


def test_last_good_survives_refresh_failure(wg):
    wg._fetch_litellm_models = lambda: [{"id": "claude-opus-5", "label": "Opus"}]
    assert wg._conversation_models(force=True)[1]["id"] == "claude-opus-5"
    def boom():
        raise OSError("restarting")
    wg._fetch_litellm_models = boom
    models = wg._conversation_models(force=True)
    assert models[1]["id"] == "claude-opus-5", models


def test_cache_ttl_and_forced_refresh(wg):
    calls = []
    def fetch():
        calls.append(1)
        return [{"id": f"m{len(calls)}", "label": f"m{len(calls)}"}]
    wg._fetch_litellm_models = fetch
    wg._conversation_models()
    wg._conversation_models()
    assert len(calls) == 1, "second read within TTL must hit the cache"
    # Routine lookups (thread summaries, turn dispatch) are cache-only: a miss
    # must NOT reach upstream, or one list request over N stale threads would
    # mean N fetches.
    assert not wg._model_offered("m2")
    assert wg._valid_model_id("m2") is None
    assert len(calls) == 1, calls
    # The human-picked path (thread creation, picker POST) refreshes once on a
    # miss, so a model just added in LiteLLM is selectable before the TTL.
    assert wg._model_offered("m2", refresh=True)
    assert len(calls) == 2, calls
    # An id offered by the fresh cache never refetches, even with refresh.
    assert wg._model_offered("m2", refresh=True)
    assert len(calls) == 2, calls


def test_fetch_does_not_block_cache_readers(wg):
    import threading
    wg._fetch_litellm_models = lambda: [{"id": "base", "label": "base"}]
    wg._conversation_models(force=True)  # warm the cache
    entered, release = threading.Event(), threading.Event()
    def slow_fetch():
        entered.set()
        release.wait(5)
        return [{"id": "slow", "label": "slow"}]
    wg._fetch_litellm_models = slow_fetch
    t = threading.Thread(target=lambda: wg._conversation_models(force=True))
    t.start()
    assert entered.wait(5)
    # While the forced refresh is in flight, cache-hit readers must not queue
    # behind the upstream call.
    assert wg._conversation_models()[1]["id"] == "base"
    release.set()
    t.join(5)
    assert not t.is_alive()
    assert wg._conversation_models()[1]["id"] == "slow"


def test_env_override_wins_over_litellm(wg_env):
    wg = wg_env
    wg._fetch_litellm_models = lambda: [{"id": "claude-opus-5", "label": "Opus"}]
    assert wg._conversation_models() == [
        {"id": "", "label": "Standard"}, {"id": "opus", "label": "Opus"}]
    assert wg._model_offered("opus")
    assert not wg._model_offered("claude-opus-5")


def test_litellm_headers(wg):
    os.environ.pop("LITELLM_MASTER_KEY", None)
    os.environ["ANTHROPIC_CUSTOM_HEADERS"] = "x-litellm-api-key: Bearer sk-abc"
    assert wg._litellm_headers() == {"x-litellm-api-key": "Bearer sk-abc"}
    os.environ["RETINUE_LITELLM_KEY"] = "sk-xyz"
    assert wg._litellm_headers() == {
        "x-litellm-api-key": "Bearer sk-xyz", "Authorization": "Bearer sk-xyz"}
    os.environ.pop("RETINUE_LITELLM_KEY")
    os.environ.pop("ANTHROPIC_CUSTOM_HEADERS")
    assert wg._litellm_headers() == {}
    os.environ["LITELLM_MASTER_KEY"] = "sk-master"
    assert wg._litellm_headers() == {
        "x-litellm-api-key": "Bearer sk-master",
        "Authorization": "Bearer sk-master",
    }
    os.environ.pop("LITELLM_MASTER_KEY")


def test_anthropic_api_host_disables_dynamic(tmp: Path):
    wg = _load_gateway(tmp, {"ANTHROPIC_BASE_URL": "https://api.anthropic.com"})
    assert wg._LITELLM_URL == ""
    assert wg._litellm_conversation_models() is None
    assert wg._conversation_models() == wg._STATIC_CONVERSATION_MODELS


def test_master_key_implies_in_stack_litellm(tmp: Path):
    wg = _load_gateway(tmp, {"LITELLM_MASTER_KEY": "sk-master"})
    assert wg._LITELLM_URL == "http://litellm:4000"


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp_s:
        tmp = Path(tmp_s)
        wg = _load_gateway(tmp / "a", {
            "RETINUE_LITELLM_URL": "http://litellm:4000",
            # Keep TTL generous so only `force` refreshes within a test run.
            "RETINUE_MODELS_CACHE_SECONDS": "3600",
        })
        test_coerce_litellm_models(wg)
        test_coerce_unflagged_advertised_models(wg)
        test_coerce_intersects_v1_models_when_unflagged(wg)
        test_ollama_backend_hides_leftover_claude_flags(wg)
        test_coerce_ollama_tags(wg)
        test_fetch_ollama_bypasses_http_proxy_for_local_host(wg)
        test_fetch_ollama_keeps_proxy_for_remote_host(wg)
        test_merge_replaces_stale_ollama_catalog(wg)
        test_dynamic_list_and_default_entry(wg)
        test_static_fallback_when_litellm_empty_or_down(wg)
        test_last_good_survives_refresh_failure(wg)
        test_litellm_headers(wg)

        wg = _load_gateway(tmp / "b", {
            "RETINUE_LITELLM_URL": "http://litellm:4000",
            "RETINUE_MODELS_CACHE_SECONDS": "3600",
        })
        test_cache_ttl_and_forced_refresh(wg)
        test_fetch_does_not_block_cache_readers(wg)

        wg = _load_gateway(tmp / "c", {
            "RETINUE_LITELLM_URL": "http://litellm:4000",
            "RETINUE_MODELS_CACHE_SECONDS": "3600",
            "RETINUE_CONVERSATION_MODELS": json.dumps([
                {"id": "", "label": "Standard"}, {"id": "opus", "label": "Opus"}]),
        })
        test_env_override_wins_over_litellm(wg)

        test_anthropic_api_host_disables_dynamic(tmp / "d")
        test_master_key_implies_in_stack_litellm(tmp / "e")
    print("all web-gateway model-picker tests passed")


if __name__ == "__main__":
    main()
