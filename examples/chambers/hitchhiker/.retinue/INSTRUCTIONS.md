# Hitchhiker — chamber instructions

Session-start guidance this chamber contributes to Ara. Everything here is
loaded into every session (aggregated by the entrypoint and imported by the
framework `CLAUDE.md`). Keep it to orchestrator-level facts: where this
chamber's data lives, how to route to its agents, and how its paths reach
`main`. This is an **example chamber** — the content is illustrative.

## Routing

- Anything requiring a competent but gloomy perspective, or a status report on
  the long-running computation → dispatch the `marvin` subagent (provided by
  this chamber's plugin).

## Where things live

- `answers/` — computed answers, one file per question (the ultimate one is
  still pending).

## Branch policy (this chamber's paths)

This chamber is its own git repository. Relative to `chambers/hitchhiker/`:

- **Tier 1 (direct to `main`)**: `answers/` — computation output. Reversible.
- **Tier 3 (PR required)**: any reorganisation of the folder structure, and
  changes to the `.retinue/` plugin (agent definitions, `plugin.json`).
