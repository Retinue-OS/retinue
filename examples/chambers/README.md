# Example chambers

A **Chamber** is one mounted repository: a self-contained collection of data
**and** agents/skills that Retinue mounts at `/workspace/chambers/<name>`. This
directory ships two runnable example chambers that double as the canonical
"how to author a chamber" reference.

| Chamber | Agent (`.retinue` plugin) | Scheduled job (`.schedule.json`) |
|---------|---------------------------|----------------------------------|
| `westworld` | `dolores` — narrates the park's loop | `westworld-reveries` — hourly "reverie" |
| `hitchhiker` | `marvin` — competent, gloomy | `deep-thought` — weekly long-running compute |

## Anatomy of a chamber

```
<name>/
  .retinue/                         ← the Claude Code plugin (scoped subdir)
    .claude-plugin/plugin.json      ← plugin identity: name + description
    agents/<agent>.md               ← one or more subagents (frontmatter + body)
    skills/                         ← optional skills
  .schedule.json                    ← optional recurring jobs (scheduler.py)
  .refresh.json                     ← optional external-data refresh (refresh.py)
  ...                               ← the chamber's data (e.g. .nt/.ttl, docs)
```

The plugin lives in a dedicated `.retinue/` subdirectory so plugin installation
copies only the plugin payload, not the chamber's data. Retinue **autodetects**
the plugin: if `chambers/<name>/.retinue/.claude-plugin/plugin.json` exists, it
is registered as a plugin and its `name`/`description` are read from there
(if `name` is absent, the chamber's directory name is used).

## Declaring a chamber

Chambers are declared in `chambers.json` (see `chambers.example.json`). Each
entry carries only deployment facts:

```json
{ "name": "westworld", "path": "examples/chambers/westworld" }
```

- `name` (required) — the chamber's mount name under `/workspace/chambers/`.
- `url` (optional) — git URL to clone from.
- `url_env` (optional) — name of an env var that overrides `url` when set.
- `path` (optional) — a directory (relative to `/workspace`) to symlink in place
  of cloning, used by these bundled examples and any host-mounted chamber.

## Trying the examples

The shipped `chambers.example.json` declares both example chambers via `path`,
so a fresh `docker compose up` mounts them with no extra setup. Their scheduled
jobs ship **disabled** (`"enabled": false`) so they do nothing until you opt in.
