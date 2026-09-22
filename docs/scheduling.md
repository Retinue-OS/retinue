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

`interval_seconds` is measured **completion to next start**, not start to
start: the scheduler writes a job's state once it has finished running, so a
job's own run time is extra spacing on top of the interval, not part of it.

A prompt job may pin its model with an optional `"model"` field, which
supports `${VAR:-default}` expansion and overrides the tier default (see
`docs/model-routing.md`). Per-job state lives outside the chambers (default
`/root/.retinue/scheduler/<id>.json`, log `scheduler.log`) so it creates no git
noise. The manifest is re-read every tick, so adding or editing a
`.schedule.json` takes effect without a restart. Tunables:
`SCHEDULER_TICK_SECONDS`, `SCHEDULER_JOB_TIMEOUT`, `SCHEDULER_STATE_DIR`.

A job may also declare an optional `"retry_after_seconds"`: when the last
recorded run did **not** end in `status: "success"` (a failure, a timeout, an
internal error), the job becomes due after that many seconds instead of the
full `interval_seconds`. Leaving it unset is the safe default — a failed run
stays due at exactly the same point a successful one would have been, so one
bad run costs at most its own interval rather than turning into a retry storm.
Set it on a job whose failures tend to be transient (a rate-limited API, a
flaky upstream) so a whole `interval_seconds` slot (a day, for a daily job)
isn't burned on one bad run:

```json
{
  "id": "herald-fetch",
  "command": "python3 /workspace/scripts/herald-fetch.py",
  "interval_seconds": 86400,
  "retry_after_seconds": 900
}
```

A command job that works through a backlog in **bounded slices** exits with
code **75** (sysexits' `EX_TEMPFAIL`) to say "this slice is done, more remains".
The scheduler records that as `status: "partial"` — logged as `[partial]`, not
`[fail]`. A job pairs it with `"resume_after_seconds"`: after a partial run it
is due again after that many seconds rather than after its full interval. That
is how the e-mail triage sweep drains a backlog: each run takes the oldest
`TRIAGE_BATCH_SIZE` messages, records what it did, and comes back for the rest,
so no single run has to fit the whole backlog into one budget.
`resume_after_seconds` is consulted for `partial` **only**. It is deliberately
not `retry_after_seconds`: a run whose model session *fails* must not be
re-spawned every few minutes on a knob meant for resuming honest work, and a
day of ten-minute retries of a failing session is a lot of credits. (A
`partial` run is still "not success", so `retry_after_seconds` alone also
brings it forward, for a job that wants one knob for both.) A job with neither
simply waits its interval. The framework's own base manifest carries no such
job; a chamber opts its e-mail sweep in, for example:

```json
{
  "id": "triage-daily",
  "command": "python3 /workspace/scripts/triage-gate.py daily",
  "interval_seconds": 86400,
  "resume_after_seconds": 600
}
```

A job may also declare an optional `"timeout_seconds"` to override the global
`SCHEDULER_JOB_TIMEOUT` for that one job. This is a backstop for a job whose
*single* unit of work is long, not a way to fit a backlog into one run — a run
that must finish everything is killed the moment the backlog outgrows any
budget, and a killed run persists nothing it had not already written. Prefer
slices and `resume_after_seconds` where the work divides.

```json
{
  "id": "triage-daily",
  "command": "python3 /workspace/scripts/triage-gate.py daily",
  "interval_seconds": 86400,
  "timeout_seconds": 3600
}
```

The value must be a **positive** integer. An omitted or `null` field simply
uses `SCHEDULER_JOB_TIMEOUT`. A present-but-unparseable or non-positive value
is treated as a malformed manifest: the scheduler logs a warning and falls back
to the global timeout rather than disabling the kill, because one un-killable
job would wedge the single-threaded tick loop behind it.

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
`recurring`/`next_due`/`expected_by` (in `md2ttl.py`'s scalar table);
`remind_before` need not be mapped, since it is read from the file. `due_day`
need not be mapped either: unlike `remind_before` it is never read back by any
code, from the store or the file — it is a plain annotation in the frontmatter
for whoever next advances `next_due` by hand, and `recurring-projects.py`'s own
SELECT does not ask the store for it. The store is
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

## Draining chamber inboxes (`.inbox.json`)

An inbox is a letterbox, not a shelf: the user drops a file in, an agent takes
it out. The Archivist's `inbox/ processing` rules say exactly *how* a dropped
file is filed and *where* it goes, and a chamber's `.inbox.json` declares the
paths — but for a long time nothing said *when*, so a chamber inbox only ever
emptied when a human happened to ask, and one quietly accumulated months of
files. The **`inbox-sweep`** base job is the missing trigger.

Like the other two base jobs it is a scheduler `command` job (so the scheduler
spends **no Claude credits**), running `scripts/inbox-sweep.py`. Its gate is a
filesystem scan rather than a SPARQL SELECT — an unfiled document is by
definition not yet in the store, so the store cannot be asked about it. **All
inboxes empty spawns nothing.** Only when a declared inbox holds files does it
start a single `claude -p` session, handed the listing it already scanned, which
dispatches the Archivist per chamber. It runs on the **router tier**
(`RETINUE_ROUTER_MODEL`): the session's whole job is to route files to the
Archivist, which is inside junior's whitelist, unlike self-review's judgement
work.

Any chamber may declare inboxes in an **`.inbox.json`** at its root; a chamber
without one is simply never swept. The `inboxes` array is what this job reads:

```json
{
  "inboxes": [
    {
      "id": "observations",
      "path": "observations/inbox",
      "description": "Raw health data awaiting review, …"
    }
  ],
  "destinations": [
    {
      "path": "observations/clinical/sensors/cgm/",
      "description": "Continuous glucose monitor exports.",
      "source": "manifest"
    }
  ]
}
```

`destinations` is the filing side of the contract, read by the Archivist (and
by a chamber's own extraction guidance), not by the sweep. A malformed
`.inbox.json` skips that one chamber with a warning rather than failing the
sweep — the other chambers' letterboxes are still worth emptying.

**The re-spawn guard.** Step 4 of `inbox/ processing` tells the Archivist to
*leave* a file it cannot classify in the inbox and flag it. That is correct
behaviour, but it means a naive gate would find the same file every hour and
spawn a session every hour, forever — a slow credit leak with no end state. So
the sweep records the listing it last spawned for (name, size and mtime per
file, in `/root/.retinue/inbox-sweep/state.json`, outside the chambers like the
scheduler's own state) and stays quiet while that listing is unchanged. Adding,
removing, or overwriting a file makes the inbox due again; so does draining it
completely, which clears the guard so a stuck file gets a fresh attempt the next
time anything arrives. The signature is recorded whatever the session made of
the files, so a session that fails outright does not re-spawn every tick either.
