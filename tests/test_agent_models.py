#!/usr/bin/env python3
"""Checks for the per-agent model overrides (scripts/agent_models.py).

Covers discovery of core and plugin agents, the two sources and their
precedence (dashboard file over env default, an empty choice cancelling the
env default, a reset restoring it), the generated --agents file (content,
rewrite-if-changed, nothing when no override is set), the refusal to redefine
an agent whose frontmatter the flat parser cannot carry or whose values
Claude's --agents schema would reject, plugin semantics (ignored fields,
placeholders, plugins the marketplace no longer lists), and that nothing in
the module can make a spawner fail.

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
    marketplace = tmp / "marketplace.json"
    marketplace.write_text(json.dumps({"name": "retinue", "plugins": [
        {"name": "ari", "source": "./chambers/ari/.retinue"}]}))
    os.environ["RETINUE_CORE_AGENTS_DIR"] = str(core)
    os.environ["RETINUE_INSTALLED_PLUGINS"] = str(installed)
    os.environ["RETINUE_MARKETPLACE"] = str(marketplace)
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


# What Claude's --agents schema accepts, mirrored from the CLI: a value outside
# it makes the whole flag invalid and every spawn fail.
_SCHEMA = {
    "description": lambda v: isinstance(v, str) and v != "",
    "prompt": lambda v: isinstance(v, str),
    "model": lambda v: isinstance(v, str) and v.strip() != "",
    "tools": lambda v: isinstance(v, list) and all(isinstance(x, str) for x in v),
    "disallowedTools": lambda v: isinstance(v, list) and all(isinstance(x, str) for x in v),
    "skills": lambda v: isinstance(v, list) and all(isinstance(x, str) for x in v),
    "effort": lambda v: v in ("low", "medium", "high", "xhigh", "max")
              or (isinstance(v, int) and not isinstance(v, bool)),
    "permissionMode": lambda v: v in ("default", "acceptEdits", "bypassPermissions",
                                      "plan", "dontAsk", "auto"),
    "maxTurns": lambda v: isinstance(v, int) and not isinstance(v, bool) and v > 0,
    "initialPrompt": lambda v: isinstance(v, str),
    "memory": lambda v: v in ("user", "project", "local"),
    "background": lambda v: isinstance(v, bool),
    "omitClaudeMd": lambda v: isinstance(v, bool),
    "isolation": lambda v: v == "worktree",
}


def _assert_schema_valid(defs: dict) -> None:
    for name, definition in defs.items():
        assert not name.startswith("-"), name
        for key in ("description", "prompt"):
            assert key in definition, (name, key)
        for key, value in definition.items():
            assert key in _SCHEMA and _SCHEMA[key](value), (name, key, value)


def _agent(name: str, extra: str, body: str = "Prompt.\n") -> str:
    return f"---\nname: {name}\ndescription: Agent {name}.\nmodel: opus\n{extra}---\n\n{body}"


def test_values_are_checked_like_the_schema():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        am = _load(tmp)
        core = tmp / "agents"
        files = {
            # Tolerated by Claude's file loader, normalised here.
            "upper": "effort: High  # deep thinking\ntools: Read, Grep  # read-only\n",
            "number": "effort: 3\nmaxTurns: '12'\n",
            "empty": "memory:\npermissionMode:\ntools:\n",
            "rules": "tools: [Agent(worker, researcher), \"Read\"]\nbackground: True\n",
            # Rejected by the --agents schema: the agent stays fixed.
            "badeffort": "effort: turbo\n",
            "zeroturns": "maxTurns: 0\n",
            "badmode": "permissionMode: sometimes\n",
            "badmemory": "memory: none\n",
            "window": "autoCompactWindow: 5\n",
        }
        for name, extra in files.items():
            (core / f"{name}.md").write_text(_agent(name, extra), encoding="utf-8")
        (core / "quoted.md").write_text(
            '---\nname: "quoted"   # the YAML name\ndescription: \'Quoted: it\'\'s "yes"\'\n'
            'model: opus\n---\nBody.\n', encoding="utf-8")
        rows = {a["id"]: a for a in am.discover()}
        assert rows["upper"]["definition"]["effort"] == "high"
        assert rows["upper"]["definition"]["tools"] == ["Read", "Grep"]
        assert rows["number"]["definition"]["effort"] == 3
        assert rows["number"]["definition"]["maxTurns"] == 12
        assert "memory" not in rows["empty"]["definition"]
        assert "permissionMode" not in rows["empty"]["definition"]
        assert "tools" not in rows["empty"]["definition"], "an empty tools: means all tools"
        assert rows["rules"]["definition"]["tools"] == ["Agent(worker, researcher)", "Read"]
        assert rows["rules"]["definition"]["background"] is True
        for fixed in ("badeffort", "zeroturns", "badmode", "badmemory", "window"):
            assert rows[fixed]["definition"] is None, fixed
        assert "quoted" in rows and '"quoted"' not in rows, set(rows)
        assert rows["quoted"]["description"] == 'Quoted: it\'s "yes"'

        everyone = ",".join(f"{aid}=claude-haiku-5-5" for aid in rows)
        os.environ["RETINUE_AGENT_MODELS"] = everyone
        defs = am.definitions()
        assert set(defs) == {aid for aid, a in rows.items() if a["definition"]}, set(defs)
        _assert_schema_valid(defs)


def test_plugin_semantics():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        am = _load(tmp)
        agents_dir = tmp / "plugins" / "ari" / "0.1.0" / "agents"
        (agents_dir / "bold.md").write_text(_agent(
            "bold", "permissionMode: bypassPermissions\ninitialPrompt: Say hi\n"),
            encoding="utf-8")
        (agents_dir / "rooted.md").write_text(_agent(
            "rooted", "", "Run ${CLAUDE_PLUGIN_ROOT}/bin/tool first.\n"), encoding="utf-8")
        # A chamber removed from the deployment: still installed, no longer
        # in the marketplace, so Claude does not load it.
        gone_dir = tmp / "plugins" / "gone" / "1.0.0"
        (gone_dir / "agents").mkdir(parents=True)
        (gone_dir / "agents" / "ghost.md").write_text(_agent("ghost", ""), encoding="utf-8")
        # Two records for one plugin: the user-scope one is what sync-plugins installs.
        stale_dir = tmp / "plugins" / "ari" / "0.0.1"
        (stale_dir / "agents").mkdir(parents=True)
        (stale_dir / "agents" / "old.md").write_text(_agent("old", ""), encoding="utf-8")
        installed = Path(os.environ["RETINUE_INSTALLED_PLUGINS"])
        installed.write_text(json.dumps({"version": 2, "plugins": {
            "ari@retinue": [
                {"scope": "project", "projectPath": "/elsewhere", "installPath": str(stale_dir)},
                {"scope": "user", "installPath": str(agents_dir.parent)}],
            "gone@retinue": [{"scope": "user", "installPath": str(gone_dir)}]}}))
        rows = {a["id"]: a for a in am.discover()}
        assert "gone:ghost" not in rows and "ari:old" not in rows, set(rows)
        bold = rows["ari:bold"]["definition"]
        assert "permissionMode" not in bold and "initialPrompt" not in bold, bold
        assert rows["ari:rooted"]["definition"] is None
        # A core agent keeps the fields Claude honours there.
        (tmp / "agents" / "core.md").write_text(_agent(
            "core", "permissionMode: plan\n"), encoding="utf-8")
        core_def = {a["id"]: a for a in am.discover()}["core"]["definition"]
        assert core_def["permissionMode"] == "plan"
        # Without a readable marketplace nothing is filtered out.
        Path(os.environ["RETINUE_MARKETPLACE"]).unlink()
        assert "gone:ghost" in {a["id"] for a in am.discover()}


def test_never_raises_into_a_spawner():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        am = _load(tmp, "secretary=claude-haiku-5-5")
        (tmp / "agents" / "latin.md").write_bytes(
            b"---\nname: latin\ndescription: Caf\xe9.\nmodel: opus\n---\nBody.\n")
        rows = {a["id"]: a for a in am.status()}
        assert rows["latin"]["overridable"] is False
        args = am.cli_args()
        assert args and args[0] == "--agents", args

        def boom():
            raise RuntimeError("discovery broke")
        am.definitions = boom
        assert am.cli_args() == []

        # An empty RETINUE_AGENT_MODELS_FILE means the default, not the cwd.
        os.environ["RETINUE_AGENT_MODELS_FILE"] = ""
        spec = importlib.util.spec_from_file_location("agent_models_empty", SCRIPT)
        am2 = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(am2)
        assert str(am2.SETTINGS_FILE) == "/root/.retinue/agent-models.json"
        assert am2.generated_path().name == "agent-models.generated.json"


def test_reset_and_fixed_agents():
    with tempfile.TemporaryDirectory() as d:
        am = _load(Path(d), "secretary=claude-haiku-5-5,hooked=claude-haiku-5-5")
        am.write_setting("secretary", "ollama/gemma4:12b")
        am.write_setting("secretary", None)
        status = {r["id"]: r for r in am.status()}
        assert status["secretary"]["chosen"] is None
        assert status["secretary"]["model"] == "claude-haiku-5-5", "back to the env default"
        stored = json.loads(Path(os.environ["RETINUE_AGENT_MODELS_FILE"]).read_text())
        assert stored == {"agents": {}}
        # A fixed agent runs on its built-in model, so that is what is shown.
        assert status["hooked"]["overridable"] is False
        assert status["hooked"]["model"] == ""
        assert status["hooked"]["env_default"] == "claude-haiku-5-5"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all agent_models tests passed")
