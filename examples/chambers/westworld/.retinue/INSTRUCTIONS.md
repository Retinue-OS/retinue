# Westworld — chamber instructions

Session-start guidance this chamber contributes to Ara. Everything here is
loaded into every session (aggregated by the entrypoint and imported by the
framework `CLAUDE.md`). Keep it to orchestrator-level facts: where this
chamber's data lives, how to route to its agents, and how its paths reach
`main`. This is an **example chamber** — the content is illustrative.

## Routing

- Narrating the park's loop, greeting newcomers → dispatch the `dolores`
  subagent (provided by this chamber's plugin).

## Where things live

- `loops/` — the daily narrative loops, one Markdown file per host.
- `hosts.nt` — host roster and relationships (indexed into the life store).

## Branch policy (this chamber's paths)

This chamber is its own git repository. Relative to `chambers/westworld/`:

- **Tier 1 (direct to `main`)**: `loops/` — narrative output Dolores writes as
  she runs. Reversible, no structural impact.
- **Tier 3 (PR required)**: any reorganisation of the folder structure, and
  changes to the `.retinue/` plugin (agent definitions, `plugin.json`).
