#!/usr/bin/env python3
"""Checks for the per-agent model overrides (scripts/agent_models.py).

Covers discovery of core and plugin agents, the two sources and their
precedence (dashboard file over env default, an empty choice cancelling the
env default), the generated --agents file (content, rewrite-if-changed,
nothing when no override is set), and the refusal to redefine an agent whose
frontmatter the flat parser cannot carry.

Standalone, no third-party deps:

    python3 tests/test_agent_models.py
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "agent_models.py"

CORE_AGENT = """---
name: secretary
description: Composes messages.
model: opus
tools: Read, Glob, Grep
---

# Secretary

Body of the prompt, with `code` and — dashes.
"""

PLUGIN_AGENT = """---
name: ari
description: A bear with a mailbox.
model: sonnet
---

Ari's prompt.
"""

NESTED_AGENT = """---
name: hooked
description: Has hooks.
model: haiku
hooks:
  PreToolUse:
    - matcher: Bash
---

Nested frontmatter.
"""


def _load(tmp: Path, env_models: str | None = None):
    core = tmp / "agents"
    core.mkdir(exist_ok=True)
    (core / "secretary.md").write_text(CORE_AGENT, encoding="utf-8")
    (core / "hooked.md").write_text(NESTED_AGENT, encoding="utf-8")
    plugin_dir = tmp / "plugins" / "ari" / "0.1.0"
    (plugin_dir / "agents").mkdir(parents=True, exist_ok=True)
    (plugin_dir / "agents" / "ari.md").write_text(PLUGIN_AGENT, encoding="utf-8")
    installed = tmp / "installed_plugins.json"
    installed.write_text(json.dumps({"version": 2, "plugins": {
        "ari@retinue": [{"scope": "user", "installPath": str(plugin_dir)}]}}))
    os.environ["RETINUE_CORE_AGENTS_DIR"] = str(core)
    os.environ["RETINUE_INSTALLED_PLUGINS"] = str(installed)
    os.environ["RETINUE_AGENT_MODELS_FILE"] = str(tmp / "state" / "agent-models.json")
    if env_models is None:
        os.environ.pop("RETINUE_AGENT_MODELS", None)
    else:
        os.environ["RETINUE_AGENT_MODELS"] = env_models
    spec = importlib.util.spec_from_file_location("agent_models_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_discovery():
    with tempfile.TemporaryDirectory() as d:
        am = _load(Path(d))
        rows = {a["id"]: a for a in am.discover()}
        assert set(rows) == {"secretary", "hooked", "ari:ari"}, set(rows)
        assert rows["secretary"]["builtin"] == "opus"
        assert rows["secretary"]["definition"]["tools"] == ["Read", "Glob", "Grep"]
        assert rows["secretary"]["definition"]["prompt"].startswith("\n# Secretary")
        assert "name" not in rows["secretary"]["definition"]
        assert rows["ari:ari"]["source"] == "plugin:ari"
        assert rows["ari:ari"]["definition"]["model"] == "sonnet"
        assert rows["hooked"]["definition"] is None, "nested frontmatter must not be redefined"
        assert rows["hooked"]["builtin"] == "haiku"


def test_no_override_means_no_flag():
    with tempfile.TemporaryDirectory() as d:
        am = _load(Path(d))
        assert am.cli_args() == []
        assert not am.generated_path().exists()


def test_env_source_and_generated_file():
    with tempfile.TemporaryDirectory() as d:
        am = _load(Path(d), "secretary=claude-haiku-5-5, ari:ari=ollama/qwen3.6:latest, bogus")
        assert am.overrides() == {"secretary": "claude-haiku-5-5",
                                  "ari:ari": "ollama/qwen3.6:latest"}
        args = am.cli_args()
        assert args[0] == "--agents" and Path(args[1]).is_file(), args
        defs = json.loads(Path(args[1]).read_text(encoding="utf-8"))
        assert set(defs) == {"secretary", "ari:ari"}
        assert defs["secretary"]["model"] == "claude-haiku-5-5"
        assert defs["secretary"]["description"] == "Composes messages."
        assert defs["secretary"]["tools"] == ["Read", "Glob", "Grep"]
        assert "— dashes" in defs["secretary"]["prompt"]
        assert defs["ari:ari"]["model"] == "ollama/qwen3.6:latest"
        assert "tools" not in defs["ari:ari"]
        # Unchanged content is not rewritten.
        before = os.stat(args[1]).st_mtime_ns
        am.cli_args()
        assert os.stat(args[1]).st_mtime_ns == before

        # JSON form of the variable works too.
        am2 = _load(Path(d), json.dumps({"hooked": "claude-haiku-5-5", "secretary": "x y"}))
        assert am2.overrides() == {"hooked": "claude-haiku-5-5"}, am2.overrides()
        # A fixed agent is skipped, so nothing is generated.
        assert am2.cli_args() == []


def test_dashboard_file_wins_and_cancels():
    with tempfile.TemporaryDirectory() as d:
        am = _load(Path(d), "secretary=claude-haiku-5-5,ari:ari=claude-haiku-5-5")
        am.write_setting("secretary", "ollama/gemma4:12b")
        am.write_setting("ari:ari", "")
        assert am.overrides() == {"secretary": "ollama/gemma4:12b"}
        status = {r["id"]: r for r in am.status()}
        assert status["secretary"]["model"] == "ollama/gemma4:12b"
        assert status["secretary"]["chosen"] == "ollama/gemma4:12b"
        assert status["secretary"]["env_default"] == "claude-haiku-5-5"
        assert status["ari:ari"]["model"] == ""  # built-in, env default cancelled
        assert status["ari:ari"]["chosen"] == ""
        assert status["hooked"]["overridable"] is False
        assert status["hooked"]["chosen"] is None
        stored = json.loads(Path(os.environ["RETINUE_AGENT_MODELS_FILE"]).read_text())
        assert stored == {"agents": {"secretary": "ollama/gemma4:12b", "ari:ari": ""}}
        # A corrupt settings file is ignored, not fatal.
        Path(os.environ["RETINUE_AGENT_MODELS_FILE"]).write_text("{not json")
        assert am.read_settings() == {}
        assert am.overrides() == {"secretary": "claude-haiku-5-5",
                                  "ari:ari": "claude-haiku-5-5"}


def test_model_id_validation():
    with tempfile.TemporaryDirectory() as d:
        am = _load(Path(d))
        assert am.valid_model_id("ollama/qwen3.6:latest")
        assert am.valid_model_id("claude-opus-5-5")
        assert not am.valid_model_id("")
        assert not am.valid_model_id("has space")
        assert not am.valid_model_id("-leading-dash")
        assert not am.valid_model_id(42)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all agent_models tests passed")
