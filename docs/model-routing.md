# Model routing: Ara junior, Ara senior, and the tiered household

Ara has two very different job profiles rolled into one name: the errand-runner
who receives whatever comes in and routes it to the right agent, and the
supervisor who notices when something is broken and steps in as system
administrator and developer. The first profile is high-frequency and cheap in
judgement; the second is rare and demanding. Running both on the same model
means either paying frontier prices for mail-room work or trusting routine
traffic to a model that cannot supervise.

This document is the design for splitting the two — which model runs which kind
of session, who is allowed to say what to the user, and how the pieces land in
phases. Phase 1 is implemented; the later phases are recorded here so they can
be built without re-deriving the reasoning.

## The enabling fact: every Ara turn is a fresh session

There is no long-lived Ara process. Every entry point spawns a fresh
`claude -p`: each scheduler prompt job, each dashboard conversation turn, each
triage run, each `ask_ara` question from the MCP connector. "Switching models"
is therefore not an agent handoff but a launch parameter — the same persona
(`CLAUDE.md`), started on a different model. Nothing needs to be summarised
across a tier boundary, because no Ara turn carries hidden state: escalating a
turn means re-running it on a bigger model against the same engage prompt and
thread file it was going to read anyway.

Most of the plumbing predates this design:

- Scheduler prompt jobs take a per-job `"model"` field with `${VAR:-default}`
  expansion (`scheduler.py`).
- Dashboard threads carry a per-thread model choice, switchable mid-thread
  (`web-gateway.py`, `POST /conversations/<id>/model`).
- Subagents pin their model in frontmatter (`.claude/agents/*.md`).
- Triage has its own `RETINUE_TRIAGE_MODEL`; transcript cleanup already runs on
  a cheap model by default.

## The two tiers

Two environment variables declare the tiers; both are optional and both fall
back to the existing `RETINUE_CLAUDE_MODEL`, so a deployment that sets neither
behaves exactly as before:

- **`RETINUE_ROUTER_MODEL`** — the cheap, fast model for routing-shaped turns
  (Ara junior).
- **`RETINUE_FRONTIER_MODEL`** — the strong model for supervision, escalation,
  and system work (Ara senior).

Difficulty is decided by **entry point first**, because most sessions know
their difficulty before any model runs:

| Entry point | Tier | Why |
|---|---|---|
| Scheduler prompt jobs (mailbox checks, dispatch turns) | router | Routine by construction; the real work is done by the dispatched subagent on its own pinned model. |
| `news-curate.py` spawn | router | The turn only dispatches the Herald. |
| `agent-self-review.py` spawn | frontier | Supervision by construction, and rare — the free SPARQL gate means it usually spawns nothing. |
| Main remote-control session | frontier | Interactive system administration and development — Ara senior's desk. |
| Dashboard turns, `ask_ara` | phase 2 | Difficulty unknown before a model reads the message; needs the escalation flow below. |

A job manifest can still pin any prompt job explicitly
(`"model": "${RETINUE_TRIAGE_MODEL:-sonnet}"`); the per-job field always wins
over the tier defaults.

## The door: who speaks, and under which name

The persona rules (in `CLAUDE.md`) follow one metaphor: **one household, one
door**. The user always just writes into the thread; they never choose whom to
address. Behind the door:

- **Ara junior** (router tier) answers the door. She may tell you where the key
  is — relay a fact, a lookup, a status — route the matter to the right worker,
  or fetch Ara senior. She **never composes content and never answers
  substantively**. This is a whitelist, not a judgement call: cheap models are
  worst at knowing what they don't know, so junior's permission to finish a
  turn herself is enumerated (route, relay labeled worker output, acknowledge,
  file, set flags) and everything else escalates by default.
- **Workers** (Secretary, Academic, Archivist, Herald, chamber agents) do the
  actual work and are quoted under their own names, as today.
- **Ara senior** (frontier tier) handles escalations, supervision, Tier-2/3
  decisions, and anything touching the system itself.

Signatures are honest: anything signed *Ara jr.* is logistics and never
load-bearing; anything signed *Ara* (senior) came from the frontier tier;
worker output carries the worker's label. Opacity of *form* (lint, plumbing,
routing metadata) is fine; opacity of *judgement* is not — the user must always
be able to tell whose judgement they are consuming. "Take this to Ara senior"
is a phrase (and, in phase 2, a reply chip) the user can always use — explicit,
user-triggered escalation is the backstop for the router model's weak
self-assessment.

**One actor URI, two signatures.** The agent registry
(`discover-agents.py`) keeps a single `urn:retinue:actor:ara`. A project parked
on `current_actor: ara` belongs to the household; which tier picks it up is a
runtime routing decision, not workflow state. Registering junior/senior as
separate actors would let a project parked on a misspelled variant silently
drop out of the self-review sweep. Signature is presentation; the actor URI is
workflow; the model stamp (phase 2) is the ground truth beneath both.

## Phases

### Phase 1 — tier variables and identity (this document's PR)

- `RETINUE_ROUTER_MODEL` / `RETINUE_FRONTIER_MODEL`, with the entry-point
  mapping above wired into `scheduler.py`, `news-curate.py`,
  `agent-self-review.py`, and the entrypoint's main session. Strictly
  behavior-preserving when the new variables are unset.
- The junior/senior identity, whitelist, and signature rules in `CLAUDE.md`.

### Phase 2 — escalation and attribution (shipped)

- **Escalation flow** for the unknown-difficulty entry points. A gateway turn
  with no per-thread model choice now runs on the router tier
  (`RETINUE_ROUTER_MODEL`, falling back to the gateway default). When a
  frontier tier is configured and the turn runs below it, the session is
  handed **`RETINUE_ESCALATE_FILE`**: creating that file is junior's signal
  (CLAUDE.md tells her how and when), the junior reply is discarded, and
  `send_message` re-runs the same prompt on `RETINUE_FRONTIER_MODEL` against
  the same pre-turn resume point — the abandoned junior fork never enters the
  thread's session lineage. A thread that escalated carries an `escalated`
  flag and stays with senior on later turns; the user touching the model
  picker takes manual control and clears it. `ask_ara` follows the same
  contract (`ARA_MCP_MODEL` wins, else router with escalation to frontier),
  with the existing slow-answer job-id flow absorbing the extra latency.
- **Model stamps**: dashboard bubbles already carried the answering model
  (`model_name` in each message record, derived from the turn's own usage
  envelope — ground truth from the API response, not the flag). What phase 2
  added is the other half: the gateway and the MCP server now export
  `RETINUE_SESSION_MODEL` into every session they spawn (and clear an
  inherited value), so memories written from dashboard and `ask_ara` turns
  are `kb:model`-stamped like every other session's.
- The **"[[chip: Take this to Ara senior]]"** chip: CLAUDE.md instructs junior
  to offer it whenever she answers a borderline point herself; the user
  clicking it sends the explicit escalation phrase, which junior honors via
  the flag file.
- **Claude Code's built-in auto memory is disabled** deployment-wide
  (`autoMemoryEnabled: false` in `.claude/settings.json`, plus
  `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` exported by the entrypoint): the life
  store is the system's one memory, and a second, opaque memory beside it
  would be invisible to SPARQL and to the reinforce/challenge lifecycle.
  CLAUDE.md project instructions are unaffected.

### Phase 3 — personas become agents (first slice shipped: Secretary)

The persona/subagent split is historical: personas were free while Ara always
ran on a strong model. A router-tier Ara must not compose in-role, so the
composing roles become proper agents with pinned models — Secretary first
(outbound composing has the most rules and the highest cost of sloppiness),
Academic and Publisher after, on the same template.

The persona files survive as a **style layer**, not an identity: the secretary
agent's definition instructs it to read `/workspace/agents/secretary.md`, then
glob `chambers/*/style/secretary.md` and let the chamber's private conventions
override — preserving the public-framework/private-chamber split exactly as
the persona mechanism does today. Workers cannot pause to ask the user
mid-task; they return "I need a decision on X" and Ara opens a decision thread
with chips — which is the interaction model the dashboard already uses.

The dispatch is also how a role's model reaches work that is not composing.
Inbox triage is the Secretary's own job (the skill is titled for her), and its
weakest step is judgement: deciding whether the gathered facts settle a reply
or the user must. That decision now goes to the `secretary` subagent, so it
runs at the Secretary's weight without any model plumbing — no tier variable
for triage, no reading a model out of an agent definition to re-use elsewhere.
The dispatching session keeps the mechanical half (drains, contact lookup,
status files, threads), which is exactly what a router-tier Ara is for, and
the compose-only boundary stays intact because the subagent never gains the
tools that send.

As shipped (Secretary only; Academic and Publisher remain personas for now):
`.claude/agents/secretary.md` defines a compose-only subagent (Read, Glob,
Grep — deliberately no Bash and no send tooling, so the "what to say" /
"whether it goes out" boundary is structural). CLAUDE.md makes dispatch
mandatory: every outbound message addressed to a human is composed by the
subagent and sent verbatim by the dispatcher, on every tier — exempting only
the system's own voice (alerts and briefings to the owner, dashboard thread
replies). Contact lookup and sending stay with the dispatching session, where
the send policies already apply. The persona file gained a usage note marking
which of its sections address the composing subagent (style) and which the
dispatcher (tooling, triage, send control).

### Phase 4 — presentation enforcement at the choke point (shipped)

Mediation splits into two components with different homes. Mediation of
*meaning* (what reaches the user, which thread, who is labeled as speaking)
stays with Ara junior — it is routing-shaped. Mediation of *form* moves to the
gateway: a cheap-model presentation lint applied to everything that lands in a
dashboard thread, enforcing the `dashboard-composing` rules (no bare URLs,
labeled links, option chips, details chips) regardless of author. Precedents in
this codebase chose structural enforcement over prompt discipline twice —
transcript cleanup (`TRANSCRIPT_CLEANUP_MODEL`) and the send policies — and
both held; this is the same move.

As shipped: `_lint_presentation()` in `web-gateway.py` runs on Ara's own
replies (`_conv_worker`) and on the token-gated agent posts (`POST
/internal/conversations` and `…/<id>/messages` — the `conversation-push.py`
paths, triage proposals included), skipping the quiet `cowork` audit threads
and gateway-authored error replies. It mirrors the transcript-cleanup
mechanics: a one-shot `claude -p` with no tools, no MCP servers and no
project context, run outside `/workspace` so no CLAUDE.md is loaded, on
`PRESENTATION_LINT_MODEL` (default: the router tier, else the gateway
default, else `haiku`). The lint is form-only and fail-open — a timeout, a
non-zero exit, or a result that shrinks or balloons beyond the drift guards
delivers the original text unchanged. `PRESENTATION_LINT=0` disables it. The
first live tier deployment is what motivated shipping this early: a
router-tier model reliably forgot chips and composed bullet lists, which no
amount of prompt discipline fixed.

The field test also showed the flag-file escalation failing on the weakest
models — junior *echoed* `touch "$RETINUE_ESCALATE_FILE"` into her reply
instead of executing it, so the turn was never re-run. Open hardening ideas,
deliberately not yet shipped: treat the marker string appearing in junior's
reply text as the escalation signal too (echoing is what weak models do
reliably), and match the explicit "take this to Ara senior" phrase in the
*user's* message at the gateway to skip junior outright. Until then, a
router tier below Sonnet-class is not recommended for conversational entry
points; scheduler dispatch jobs tolerate weaker routers via their per-job
`model` field.

### Memory as triples (first slice shipped with phase 1)

Per-turn statelessness makes context the whole game, and the tier split raises
the stakes: briefing workers and Ara senior cheaply is what keeps the
architecture affordable. The complement is a session log in the life store,
and its first slice ships alongside phase 1: `scripts/memory.py` stores
entries as N-Triples files under `chambers/_generated/memory/` (indexed like
any chamber data) and recalls them by tag, time range, actor, or minimum
relevance. An entry is a resource, not a bare fact — content, tags, actor,
timestamp, optional relevance — and `recall`'s output is prompt-ready, so a
dispatched agent that cannot query the store itself still gets the memories
joined into its briefing. `RETINUE_MEMORY=0` disables the mechanism
deployment-wide. Still open for a later pass: retention/compaction, and what
junior may log versus what senior curates.

## Non-goals

- No change to subagent models: workers keep pinning their own model in
  frontmatter; the tier variables govern Ara's own turns only.
- No pre-classifier: a gate model that reads every message to pick a tier costs
  an extra call on all traffic. Escalation-by-re-run is cheaper and simpler.
- No second door: transparency is about who signed the answer, never about
  making the user route their own requests.
