#!/usr/bin/env python3
"""Per-agent model overrides for the subagents Retinue dispatches.

Every subagent — the core ones under `.claude/agents/` and the ones chamber
plugins install — pins its model in its frontmatter. Those files are baked
into the image, so a deployment that wants to try the Secretary on a local
Ollama tag, or the Herald on a cheaper Claude, had no knob. This module is
that knob.

Claude Code's `--agents <json-or-file>` flag defines agents for one session,
and a definition passed that way takes precedence over the same name from
`.claude/agents/` or a plugin (docs: "Managed settings > --agents > project >
user > plugin"). It is a replacement, not a merge: the JSON must carry the
whole agent (description, prompt, tools, …), or Claude refuses the flag. So
an override here is the agent's own file, re-read and handed back with only
the `model` field swapped — nothing else about the agent changes.

Two sources, dashboard over deployment:

- `RETINUE_AGENT_MODELS` — the deployment default. A JSON object
  (`{"secretary": "ollama/qwen3.6:latest"}`) or the shorter
  `secretary=ollama/qwen3.6:latest,herald=claude-haiku-5-5`.
- `RETINUE_AGENT_MODELS_FILE` (default `/root/.retinue/agent-models.json`,
  the persistent volume) — what the dashboard's Settings page writes:
  `{"agents": {"secretary": "ollama/qwen3.6:latest", "herald": ""}}`. An
  empty value is an explicit "built-in" that also cancels the env default for
  that agent.

Every framework spawner (`web-gateway.py`, `scheduler.py`, the base-job
scripts, the MCP connector) appends `cli_args()` to its `claude -p` command:
`[]` when nothing is overridden, else `["--agents", <generated file>]`. The
generated file is rewritten only when its content changes and is read at
each spawn, so a change on the Settings page reaches the very next dispatch
with no restart.

Agent names are what the Agent tool takes: the frontmatter `name` for core
agents, `<plugin>:<name>` for plugin agents (`ari:ari`). Plugin agents are
discovered through `~/.claude/plugins/installed_plugins.json`, the same
registry `sync-plugins.py` reads.

Frontmatter is parsed without a YAML library (none is in the image): one
`key: value` per line, `tools`/`disallowedTools`/`skills` split on commas. An
agent whose frontmatter carries anything this parser cannot carry faithfully
(a nested `hooks:` or `mcpServers:` block, a YAML list) is left alone with a
warning rather than redefined with half its settings missing.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

CORE_AGENTS_DIR = Path(os.environ.get("RETINUE_CORE_AGENTS_DIR",
                                      "/workspace/.claude/agents"))
INSTALLED_PLUGINS = Path(os.environ.get(
    "RETINUE_INSTALLED_PLUGINS",
    str(Path.home() / ".claude" / "plugins" / "installed_plugins.json")))
SETTINGS_FILE = Path(os.environ.get("RETINUE_AGENT_MODELS_FILE",
                                    "/root/.retinue/agent-models.json"))
ENV_VAR = "RETINUE_AGENT_MODELS"

# Frontmatter keys carried into a --agents definition, with how each is typed.
# `name` is the JSON key, not a field; `color`/`experimental` are ignored by
# Claude anyway. Anything else present makes the agent non-overridable.
_LIST_KEYS = ("tools", "disallowedTools", "skills")
_STR_KEYS = ("description", "model", "permissionMode", "effort", "memory",
             "isolation", "initialPrompt")
_INT_KEYS = ("maxTurns", "autoCompactWindow")
_BOOL_KEYS = ("background", "omitClaudeMd")
_DROP_KEYS = ("name", "color", "experimental")

_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][\w.:/@-]{0,199}$")


def _warn(msg: str) -> None:
    print(f"[agent-models] {msg}", file=sys.stderr)


# --- agent discovery ---------------------------------------------------------

def _split_frontmatter(text: str) -> tuple[str, str] | None:
    m = re.match(r"\A---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z", text, re.S)
    if not m:
        return None
    return m.group(1), m.group(2)


def parse_frontmatter(block: str) -> dict | None:
    """`key: value` lines → dict; None when a line is not a flat scalar.

    A continuation line (indented, or a `- item` list entry) means a nested
    structure this parser would mangle — the caller then leaves that agent's
    file as the only definition."""
    out: dict = {}
    for raw in block.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw[:1] in (" ", "\t") or raw.lstrip().startswith("- "):
            return None
        key, sep, value = raw.partition(":")
        if not sep or not key.strip():
            return None
        out[key.strip()] = value.strip()
    return out


def _coerce(fields: dict) -> dict | None:
    """Frontmatter scalars → the typed fields a --agents definition takes."""
    definition: dict = {}
    for key, value in fields.items():
        if key in _DROP_KEYS:
            continue
        if key in _LIST_KEYS:
            if value.startswith("[") and value.endswith("]"):
                value = value[1:-1]
            definition[key] = [v.strip().strip("'\"") for v in value.split(",")
                               if v.strip()]
        elif key in _STR_KEYS:
            definition[key] = value.strip("'\"")
        elif key in _INT_KEYS:
            try:
                definition[key] = int(value)
            except ValueError:
                return None
        elif key in _BOOL_KEYS:
            if value.lower() not in ("true", "false"):
                return None
            definition[key] = value.lower() == "true"
        else:
            return None
    return definition


def read_agent_file(path: Path) -> tuple[str, dict] | None:
    """(name, definition) for one agent file, or None when it cannot be
    carried into a --agents definition faithfully."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        _warn(f"cannot read {path}: {exc}")
        return None
    parts = _split_frontmatter(text)
    if parts is None:
        return None
    fields = parse_frontmatter(parts[0])
    if fields is None:
        _warn(f"{path}: nested frontmatter, not overridable")
        return None
    name = fields.get("name", "").strip() or path.stem
    definition = _coerce(fields)
    if definition is None:
        _warn(f"{path}: unsupported frontmatter field, not overridable")
        return None
    if not definition.get("description"):
        return None
    definition["prompt"] = parts[1]
    return name, definition


def _plugin_agent_dirs() -> list[tuple[str, Path]]:
    """(plugin name, agents dir) for every user-scope installed plugin."""
    try:
        data = json.loads(INSTALLED_PLUGINS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out: list[tuple[str, Path]] = []
    plugins = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins, dict):
        return []
    for key, records in plugins.items():
        plugin = str(key).split("@", 1)[0]
        for record in records if isinstance(records, list) else []:
            if not isinstance(record, dict) or not record.get("installPath"):
                continue
            agents_dir = Path(record["installPath"]) / "agents"
            if agents_dir.is_dir():
                out.append((plugin, agents_dir))
                break
    return out


def discover() -> list[dict]:
    """Every dispatchable agent: {"id", "label", "description", "builtin",
    "source", "definition"} — `definition` is None when not overridable."""
    agents: list[dict] = []
    seen: set[str] = set()

    def _add(path: Path, scope: str | None, source: str) -> None:
        parsed = read_agent_file(path)
        if parsed is None:
            # Still list it, so the Settings page can say why it is fixed.
            text = ""
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                pass
            parts = _split_frontmatter(text)
            fields = {}
            if parts:
                for raw in parts[0].splitlines():
                    k, sep, v = raw.partition(":")
                    if sep and raw[:1] not in (" ", "\t"):
                        fields[k.strip()] = v.strip()
            name = fields.get("name") or path.stem
            definition = None
            builtin = fields.get("model", "")
            description = fields.get("description", "")
        else:
            name, definition = parsed
            builtin = definition.get("model", "")
            description = definition.get("description", "")
        aid = f"{scope}:{name}" if scope else name
        if aid in seen:
            return
        seen.add(aid)
        agents.append({
            "id": aid,
            "label": name.replace("-", " ").capitalize()
                     + (f" ({scope} plugin)" if scope else ""),
            "description": description,
            "builtin": builtin,
            "source": source,
            "definition": definition,
        })

    if CORE_AGENTS_DIR.is_dir():
        for path in sorted(CORE_AGENTS_DIR.glob("*.md")):
            _add(path, None, "core")
    for plugin, agents_dir in _plugin_agent_dirs():
        for path in sorted(agents_dir.glob("*.md")):
            _add(path, plugin, f"plugin:{plugin}")
    return agents


# --- the two sources ---------------------------------------------------------

def valid_model_id(mid: object) -> bool:
    return isinstance(mid, str) and bool(_MODEL_ID_RE.match(mid))


def parse_env(raw: str | None) -> dict[str, str]:
    """RETINUE_AGENT_MODELS → {agent: model}; JSON object or `a=m,b=m`."""
    raw = (raw or "").strip()
    if not raw:
        return {}
    out: dict[str, str] = {}
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
        except ValueError:
            _warn(f"{ENV_VAR}: invalid JSON, ignored")
            return {}
        items = parsed.items() if isinstance(parsed, dict) else []
    else:
        items = []
        for part in raw.split(","):
            if not part.strip():
                continue
            k, sep, v = part.partition("=")
            if not sep:
                _warn(f"{ENV_VAR}: expected agent=model, got {part!r}")
                continue
            items.append((k.strip(), v.strip()))
    for k, v in items:
        if isinstance(k, str) and k and valid_model_id(v):
            out[k] = v
        else:
            _warn(f"{ENV_VAR}: ignoring {k!r}: {v!r}")
    return out


def read_settings() -> dict[str, str]:
    """The dashboard's choices: {agent: model-or-empty}."""
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        _warn(f"{SETTINGS_FILE}: unreadable ({exc}), ignored")
        return {}
    agents = data.get("agents") if isinstance(data, dict) else None
    if not isinstance(agents, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in agents.items():
        if not isinstance(k, str) or not k:
            continue
        if v in (None, ""):
            out[k] = ""
        elif valid_model_id(v):
            out[k] = v
    return out


def write_setting(agent: str, model: str) -> dict[str, str]:
    """Record one choice (empty model = built-in, which also cancels the env
    default for that agent). Atomic replace; returns the stored map."""
    current = read_settings()
    current[agent] = model
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(SETTINGS_FILE.parent), prefix=".agent-models-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"agents": current}, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, SETTINGS_FILE)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return current


def overrides() -> dict[str, str]:
    """Effective {agent: model}: dashboard file over env; empties removed."""
    merged = parse_env(os.environ.get(ENV_VAR))
    merged.update(read_settings())
    return {k: v for k, v in merged.items() if v}


# --- what a spawner appends --------------------------------------------------

def definitions() -> dict[str, dict]:
    """The --agents JSON object for the current overrides."""
    wanted = overrides()
    if not wanted:
        return {}
    out: dict[str, dict] = {}
    for agent in discover():
        model = wanted.get(agent["id"])
        if not model:
            continue
        if agent["definition"] is None:
            _warn(f"{agent['id']}: not overridable, keeps {agent['builtin'] or 'its default'}")
            continue
        definition = dict(agent["definition"])
        definition["model"] = model
        out[agent["id"]] = definition
    return out


def generated_path() -> Path:
    return SETTINGS_FILE.with_name(SETTINGS_FILE.stem + ".generated.json")


def cli_args() -> list[str]:
    """`["--agents", <file>]` for a `claude -p` command, or `[]`.

    The file is rewritten only when its content changes, under an atomic
    replace, so concurrent spawns always read a complete document."""
    defs = definitions()
    path = generated_path()
    if not defs:
        return []
    content = json.dumps(defs, ensure_ascii=False, sort_keys=True)
    try:
        if path.read_text(encoding="utf-8") == content:
            return ["--agents", str(path)]
    except OSError:
        pass
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".agents-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except OSError as exc:
        _warn(f"cannot write {path}: {exc}; subagents keep their built-in models")
        return []
    return ["--agents", str(path)]


def status() -> list[dict]:
    """What the Settings page shows: every agent with its built-in model, the
    env default, the dashboard choice and the effective model."""
    env = parse_env(os.environ.get(ENV_VAR))
    chosen = read_settings()
    rows = []
    for agent in discover():
        aid = agent["id"]
        pick = chosen.get(aid)
        effective = pick if pick else ("" if pick == "" else env.get(aid, ""))
        rows.append({
            "id": aid,
            "label": agent["label"],
            "description": agent["description"],
            "builtin": agent["builtin"],
            "source": agent["source"],
            "overridable": agent["definition"] is not None,
            "env_default": env.get(aid, ""),
            "chosen": pick if pick is not None else None,
            "model": effective,
        })
    return rows


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "args":
        print(" ".join(cli_args()))
    else:
        for row in status():
            fixed = "" if row["overridable"] else "  (fixed)"
            print(f"{row['id']:<24} built-in={row['builtin'] or '-':<22} "
                  f"model={row['model'] or '(built-in)'}{fixed}")
