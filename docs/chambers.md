# Chambers — mounting, plugins, and the sync loop

*Reference depth for the "Chambers" digest in `CLAUDE.md`. Read this before
changing how chambers are mounted, how plugins are detected or cached, or when
a chamber's edited plugin does not seem to reach its subagents.*

A **Chamber** is one mounted repository: a self-contained collection of data
**and** agents/skills. Chambers declared in `/workspace/chambers.json` are
mounted at container start into `/workspace/chambers/<name>` (cloned from a
`url`, used in place when pre-mounted, or linked from a local `path`). Each
chamber may carry a Claude Code plugin in a dedicated subdirectory — by
convention `.retinue/`, containing `.claude-plugin/plugin.json` plus `agents/`,
`skills/`, … — that provides its domain capabilities. Scoping the plugin to a
subdirectory matters: plugin installation copies the plugin root into the Claude
cache, and the subdirectory keeps the chamber's data out of that copy.

## Plugin autodetection

The entrypoint **autodetects** plugins: for each chamber that has
`chambers/<name>/.retinue/.claude-plugin/plugin.json`, it appends an entry
(name/description read from that `plugin.json`) and **generates**
`/workspace/.claude-plugin/marketplace.json` from the identity template
`marketplace.template.json` (marketplace name: `retinue`), then installs the
plugins — so chamber-provided subagents are available in every session.

To add a chamber: add it to the deployment's `chambers.json`. If it ships a
plugin it is autodetected — no marketplace edit needed.

## The plugin cache and sync-plugins

Installing a plugin **copies** it into a version-keyed cache
(`/root/.claude/plugins/cache/retinue/<name>/<version>/`). Both `claude plugin
install` and `claude plugin update` are no-ops once that version is present, and
the cached copy is keyed by the plugin's version — which, for a manifest that
declares none (as all chambers here do), is the source repo's commit at
**install** time. Since install and update are no-ops for an already-installed
plugin, later commits and uncommitted edits alike stay out of the cache. The
cache is on the persistent `/root` volume, so neither a restart nor an image
rebuild clears it. `scripts/sync-plugins.py` closes this gap: it compares each cached copy
against its chamber source file-by-file and reinstalls (uninstall + install, the
only way to overwrite an identical version) the ones that drifted. The entrypoint
runs it once at start and then forks it in `--watch` mode, so a chamber edited at
runtime propagates within `PLUGIN_SYNC_INTERVAL` seconds (default 60). A resynced
plugin reaches a subagent at the next **session start** — which is how scheduler
jobs run anyway, each being a fresh `claude -p`.

## The example chambers

Chambers are deployment content, not part of this framework. The framework
itself ships only `chambers.example.json`, which mounts the two example
chambers under `examples/chambers/`:

| Chamber | Path | Plugin provides |
|---------|------|-----------------|
| `westworld` | `/workspace/chambers/westworld` | `dolores` subagent |
| `hitchhiker` | `/workspace/chambers/hitchhiker` | `marvin` subagent |

A real deployment bind-mounts its own `chambers.json` over that and mounts its
own domain chambers (e.g. a health chamber providing clinical subagents, a
mailbox-persona chamber, an operations/data chamber). Whatever the mix, each
chamber describes itself through its own instructions rather than through
`CLAUDE.md`.

## Chamber instructions — the aggregate import

A chamber provides session-start guidance at
**`chambers/<name>/.retinue/INSTRUCTIONS.md`** (a chamber may ship this with or
without a plugin). At container start the entrypoint concatenates the
`INSTRUCTIONS.md` of every mounted chamber into
`/workspace/.retinue/chamber-instructions.md`, which `CLAUDE.md` **imports at
the end** (`@` import). So each mounted chamber's instructions are already in
context in every session — the main session, scheduled `claude -p` jobs, and
dashboard conversation turns (all run from `/workspace`, so the import loads
with no approval prompt). The aggregate is regenerated on each start and is
present-but-empty when no chamber ships instructions. If for any reason it is
not in context, sessions read the per-chamber `INSTRUCTIONS.md` files directly.

Keep such a file to orchestrator-level facts, in `CLAUDE.md`'s voice:

- **Where its data lives** — key files and directories, and any `STRUCTURE.md`.
- **Routing** — which of its subagents handles what.
- **Branch policy for its paths** — which are Tier 1 (direct to `main`), which
  need in-conversation consent (Tier 2), which need a PR (Tier 3).
