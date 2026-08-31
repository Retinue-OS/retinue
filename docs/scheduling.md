# Scheduling — data refresh, agent jobs, and the project alarm clocks

*Reference depth for the "Data refresh", "Scheduled tasks" and project-parking
digests in `CLAUDE.md`. Read this before adding or editing a `.refresh.json` /
`.schedule.json`, changing the self-review or recurring-projects jobs, or
debugging why a resting project did not wake.*

## Data refresh (`.refresh.json`)

External data sources (e.g. Garmin) are kept up to date by the generic refresh
dispatcher at `/workspace/scripts/refresh.py`. `--ensure <source-id>` is a
no-op when the source was updated within its configured `max_age_seconds`;
when stale it fetches synchronously, commits the result, and pushes — so the
current session always works with current data.

Any chamber may declare refreshable sources in a **`.refresh.json`** at its
root (`chambers/<chamber>/.refresh.json`); the entrypoint starts a dispatcher
per chamber. Example:

```json
{
  "sources": [
    {
      "id": "garmin",
      "command": "python3 /workspace/scripts/sync-garmin.py",
      "max_age_seconds": 86400,
      "lock_path": "/tmp/refresh-garmin.lock"
    }
  ]
}
```

Per-source state (last successful run) is stored in
`chambers/<chamber>/.refresh/<id>.json`. On container start the dispatcher runs
all stale sources in the background; its log is appended to
`chambers/<chamber>/.refresh/startup.log`.

## Scheduled agent tasks (`.schedule.json`)

Recurring **agent** tasks (as opposed to data freshness) are driven by
`/workspace/scripts/scheduler.py`, a daemon forked by the entrypoint in
remote-control mode. Each mounted chamber declares its own jobs in
**`chambers/<chamber>/.schedule.json`**; the scheduler runs each on its
`interval_seconds`. A job either dispatches an agent task via `prompt`
(run as a fresh `claude -p` session, so it reads `CLAUDE.md` and Ara can route
to a subagent) or runs a shell `command`.

```json
{
  "jobs": [
    {
      "id": "<chamber>-mailbox",
      "prompt": "Dispatch the <chamber>'s subagent to check its mailbox and handle new mail.",
      "interval_seconds": 1800,
      "enabled": true,
      "run_at_start": false
    }
  ]
}
```

A prompt job may pin its model with an optional `"model"` field, which
supports `${VAR:-default}` expansion and overrides the tier default (see
`docs/model-routing.md`). Per-job state lives outside the chambers (default
`/root/.retinue/scheduler/<id>.json`, log `scheduler.log`) so it creates no git
noise. The manifest is re-read every tick, so adding or editing a
`.schedule.json` takes effect without a restart. Tunables:
`SCHEDULER_TICK_SECONDS`, `SCHEDULER_JOB_TIMEOUT`, `SCHEDULER_STATE_DIR`.

Besides the per-chamber manifests, the scheduler always loads a **framework base
manifest** at `/workspace/.schedule.json` for cross-cutting jobs that belong to
the framework itself rather than any single chamber. A chamber manifest cannot
shadow a base job id (first-seen wins).

## Agent self-review (proactivity over own backlog)

Every other scheduled job is **reactive** — it fires on inbound mail, an inbound
message, or a calendar date. Nothing wakes an agent to work down projects where
the ball is already in *its* court, so such a project stays invisible until a
human pokes it. The **`agent-self-review`** base job closes that gap.

It is a scheduler `command` job — so the scheduler spends **no Claude credits**
to invoke it — that runs `scripts/agent-self-review.py`. The script's gate is a
plain SPARQL `SELECT` against the life store (also free): unresolved `kb:Project`
whose `kb:currentActor` is typed `kb:AiAgent`. An **empty result spawns nothing**
— zero credits when no agent owes work. Only on a non-empty result does it start
a single `claude -p` session, handed the already-fetched tuples so the agent does
not re-query; the agent then does each next action or opens a dashboard
conversation with a concrete proposal, routing each project to its owning agent.

Two facts make this work, both **derived, never hand-maintained**:

- **Who is an AI agent** is store-native. At boot, `scripts/discover-agents.py`
  walks the same three agent locations the entrypoint knows (core personas in
  `/workspace/agents/`, the core subagent in `/workspace/.claude/agents/`, and
  chamber agents in `chambers/*/.retinue/agents/`), plus Ara (the main-session
  persona, defined in `CLAUDE.md`, so seeded explicitly), and emits an
  N-Triples registry typing each `urn:retinue:actor:<name>` as `kb:AiAgent`. It
  writes to a framework-owned path under the chambers root (`_generated/`) so the
  life store indexes it. Human/external actors (`reto`, an `iv-stelle`, a
  correspondent) have no agent definition, so they never match — the AI-vs-human
  distinction falls out of the join, not a list. The emit is **deterministic**
  (sorted N-Triples, no blank nodes) and **write-if-changed**, so an unchanged
  roster never triggers a qlever-dir rebuild.
- **The actor URI is the agent's basename.** When a project is parked on an
  agent, its frontmatter's `current_actor` must carry the agent's basename
  (`coach`, `ari`, `ara`, …) — the same string the registry types. This is the
  one convention the mechanism depends on: a project parked on an agent under
  any other name is invisible to the sweep.

## Waking resting projects (cadences and deadlines)

A project that rests until a date is invisible until something wakes it. The
**`recurring-projects`** base job is that alarm clock. Like `agent-self-review`,
it is a scheduler `command` job (so the scheduler spends **no Claude credits**)
whose gate is a plain SPARQL `SELECT` against the life store (also free) — run
by `scripts/recurring-projects.py`. "Which resting project wants attention
today?" is a store question, not a per-chamber filesystem scan: every chamber's
project frontmatter is already in the store, so one query covers notes,
operations, and any chamber added later.

Both kinds of resting project rest as `paused: true` (the dashboard card hides
them; unlike `status: done` they stay alive and queryable).

**Standing cadences** — an action due a day each month (an IV assistance-cost
filing) or each quarter (a VAT return):

```yaml
recurring: monthly | quarterly
due_day: 8                 # informational: day of the period the action is due
next_due: 2026-09-08       # the date it wakes up
paused: true               # resting between cadences
reminder_title: …          # optional; shown when it wakes
reminder_message: …        # optional; kept in the file, never in this code
```

**One-off deadlines** — a single future date: a follow-up ("check on 29 August
whether the second weighing happened"), or a statutory deadline years out. No
new vocabulary is needed, because `expected_by` already means exactly this; it
just has to be acted on. Such a project declares **no** `recurring` cadence:

```yaml
expected_by: 2028-10-01    # the date this wants attention
remind_before: 3m          # optional lead time: 10 / 10d / 2w / 3m
paused: true
```

By default a deadline wakes **on** `expected_by`, which is what that field means
for a follow-up. A deadline that needs acting on *before* it arrives says so
with `remind_before` (days / weeks / calendar months) — waking early is the
project's explicit choice, not a default that shifts every follow-up in every
chamber. A date already in the past wakes on the next run, so nothing is lost
when the container was down on the day. When a project carries both a cadence
and an `expected_by`, the cadence wins: the deadline is then the end of the
whole standing arrangement, not the next occurrence.

For the store to carry these, the chamber's Markdown→triples converter must map
`recurring`/`due_day`/`next_due`/`expected_by` (in `md2ttl.py`'s scalar table);
`remind_before` need not be mapped, since it is read from the file. The store is
read-only, so the job splits **detect** (the free, chamber-agnostic SELECT,
which returns each candidate with its `file:` named graph) from **reactivate**
(resolve the graph to the file, flip `paused: false` and set `waiting_since`,
open one dashboard conversation with the project's own reminder text). It leaves
the file's existing `current_actor` untouched — a resting project already has
its owner set, so no owner identity is hardcoded in this public-repo script. It
does **not** advance `next_due` — that happens when the human marks the cadence
done (via Ara), so an overdue period stays active rather than silently skipping.

De-dup needs no state file: the gate requires `paused: true`, so a reactivated
project no longer matches. **The file, not the store, is the authority** — it is
re-read just before acting, which closes any store-lag window and, importantly,
is where `resolved: true` / `status: done` are actually checked: a chamber's
converter maps only the keys it chose, so a finished project may carry no
`kb:resolved` in the store at all. The query's own exclusions are an
optimisation, not the guarantee.

The reminder wording lives in the project frontmatter, not in framework code —
this public repo carries no chamber-specific or personal text.
