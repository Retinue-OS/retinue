# Session memory

Every Retinue turn is a fresh `claude -p`. Whatever a session learned — a
decision, a user preference, the outcome of an errand, a system quirk — dies
with it unless it is written down. `scripts/memory.py` is the writing-down: it
stores memory entries as N-Triples in the life store and recalls them,
prompt-ready, so a dispatched subagent that cannot query the store still gets
what matters joined into its briefing.

This document is the design. The operator-facing digest is CLAUDE.md, section
"Memory"; the command-line reference is the module docstring of
`scripts/memory.py`.

## Why it changed: five weeks in production

The first version stored and recalled, and nothing else. After five weeks one
deployment held about 650 entries — some 27 a day, 2.7 MB — and the log was
getting worse at its one job, putting the right memories into a prompt:

- **Relevance discriminated nothing.** 99.8% of entries declared one, the
  median was 0.75. A number every writer sets high carries no information.
- **Tags fragmented.** 443 distinct tags, 40% of them used exactly once — an
  entry filed under a tag nobody will ever recall by is as good as lost.
- **Rules were re-stored, not reinforced.** Four entries were ever reinforced,
  while standing rules reappeared as fresh entries, each copy competing with
  the original for recall's limit.
- **Most volume was episodic residue.** Fixed bugs, "mail queued" followed
  days later by "mail sent", one-off triage incidents, near-duplicates stored
  within the same second. 12% of all entries were already corrected or
  superseded.
- **One model, three stamps.** `anthropic/claude-opus-5`, `claude-opus-5` and
  `opus` for the same model, depending on which spawner set the variable.
- **Recall cut the wrong end.** It orders newest first with a limit of 20, so
  on a broad tag the old durable rules were exactly what got cut — and only
  the triage skill recalled explicitly at all.

Memory v2 answers each of these: entries about pending states expire, the
store refuses near-duplicates and new tags from sessions that should reuse
what exists, model stamps are normalized, and a compaction pass folds old
entries into topic summaries that recall puts first.

## Data model

A memory entry is a resource, not a bare fact. Namespaces: `kb:` is
`https://w3id.org/retinue/kb#`, `xsd:` the XML Schema datatypes.

```ntriples
<urn:retinue:memory:20260912T081500Z-3fa9c1> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://w3id.org/retinue/kb#Memory> .
<urn:retinue:memory:20260912T081500Z-3fa9c1> <https://w3id.org/retinue/kb#content> "Reply to the insurer queued for approval on /sends; waiting for the user." .
<urn:retinue:memory:20260912T081500Z-3fa9c1> <https://w3id.org/retinue/kb#tag> "insurance" .
<urn:retinue:memory:20260912T081500Z-3fa9c1> <https://w3id.org/retinue/kb#recordedAt> "2026-09-12T08:15:00Z"^^<http://www.w3.org/2001/XMLSchema#dateTime> .
<urn:retinue:memory:20260912T081500Z-3fa9c1> <https://w3id.org/retinue/kb#actor> <urn:retinue:actor:ara> .
<urn:retinue:memory:20260912T081500Z-3fa9c1> <https://w3id.org/retinue/kb#relevance> "0.3"^^<http://www.w3.org/2001/XMLSchema#decimal> .
<urn:retinue:memory:20260912T081500Z-3fa9c1> <https://w3id.org/retinue/kb#expires> "2026-09-19T08:15:00Z"^^<http://www.w3.org/2001/XMLSchema#dateTime> .
<urn:retinue:memory:20260912T081500Z-3fa9c1> <https://w3id.org/retinue/kb#session> "triage-2026-09-12" .
<urn:retinue:memory:20260912T081500Z-3fa9c1> <https://w3id.org/retinue/kb#model> "claude-sonnet-5" .
```

| Predicate | Meaning |
|---|---|
| `a kb:Memory` | the type every entry and every summary carries |
| `kb:content` | what to remember, as one self-contained statement |
| `kb:tag` | a topic, slugged (lowercase, `[a-z0-9-]`); repeatable, at least one |
| `kb:recordedAt` | when it was stored (`xsd:dateTime`, UTC) |
| `kb:actor` | the recording agent, `urn:retinue:actor:<basename>` — the agent registry's convention, so memories join with the `kb:AiAgent` roster |
| `kb:relevance` | optional, 0..1: expected durability (see the writing rules) |
| `kb:expires` | optional: after this time the entry is no longer relevant, whatever happens later |
| `kb:session` | optional session label; entries sharing it share a file |
| `kb:model` | the model that wrote it, normalized: lowercase, provider prefix up to the first `/` dropped |

Later events are written as new triples **about the old subject**, into the
current session's file. RDF merges triples by subject across named graphs, so
the original file is never edited ("subject-merge"):

| Predicate (subject: the old entry) | Written by | Meaning |
|---|---|---|
| `kb:reiteratedAt` `xsd:dateTime` | `reinforce` | the user restated it; recall counts these |
| `kb:correctedBy` `<new>` | `store --corrects` | it was plainly false; the new entry says what is true |
| `kb:supersededBy` `<new>` | `store --supersedes` | the world changed; the new entry holds now |
| `kb:questionedBy` `<new>` | `store --questions` | new evidence puts it in doubt; recall flags it |
| `kb:compactedInto` `<summary>` | `compact` | a summary carries it forward, or retired it |

A **summary** is written by `compact`. It is an ordinary memory — content,
tags, recordedAt, actor, relevance, model — that is also a `kb:MemorySummary`
and records what it was made from:

```ntriples
<urn:retinue:memory:20261005T120000Z-c0ffee> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://w3id.org/retinue/kb#Memory> .
<urn:retinue:memory:20261005T120000Z-c0ffee> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://w3id.org/retinue/kb#MemorySummary> .
<urn:retinue:memory:20261005T120000Z-c0ffee> <https://w3id.org/retinue/kb#content> "Ludmila: reach her on Signal, never e-mail (standing rule since 2026-08-30, restated 3x). Prefers voice notes for anything longer than two lines. Open: the photo album she asked for on 2026-09-18." .
<urn:retinue:memory:20261005T120000Z-c0ffee> <https://w3id.org/retinue/kb#tag> "ludmila" .
<urn:retinue:memory:20261005T120000Z-c0ffee> <https://w3id.org/retinue/kb#tag> "signal" .
<urn:retinue:memory:20261005T120000Z-c0ffee> <https://w3id.org/retinue/kb#recordedAt> "2026-10-05T12:00:00Z"^^<http://www.w3.org/2001/XMLSchema#dateTime> .
<urn:retinue:memory:20261005T120000Z-c0ffee> <https://w3id.org/retinue/kb#actor> <urn:retinue:actor:ara> .
<urn:retinue:memory:20261005T120000Z-c0ffee> <https://w3id.org/retinue/kb#relevance> "0.8"^^<http://www.w3.org/2001/XMLSchema#decimal> .
<urn:retinue:memory:20261005T120000Z-c0ffee> <https://w3id.org/retinue/kb#model> "claude-opus-5" .
<urn:retinue:memory:20261005T120000Z-c0ffee> <https://w3id.org/retinue/kb#generation> "1"^^<http://www.w3.org/2001/XMLSchema#integer> .
<urn:retinue:memory:20261005T120000Z-c0ffee> <https://w3id.org/retinue/kb#coversFrom> "2026-08-30T09:12:44Z"^^<http://www.w3.org/2001/XMLSchema#dateTime> .
<urn:retinue:memory:20261005T120000Z-c0ffee> <https://w3id.org/retinue/kb#coversTo> "2026-09-20T17:03:10Z"^^<http://www.w3.org/2001/XMLSchema#dateTime> .
<urn:retinue:memory:20261005T120000Z-c0ffee> <https://w3id.org/retinue/kb#summarizes> <urn:retinue:memory:20260830T091244Z-1a2b3c> .
<urn:retinue:memory:20261005T120000Z-c0ffee> <https://w3id.org/retinue/kb#retires> <urn:retinue:memory:20260902T140501Z-4d5e6f> .
<urn:retinue:memory:20260830T091244Z-1a2b3c> <https://w3id.org/retinue/kb#compactedInto> <urn:retinue:memory:20261005T120000Z-c0ffee> .
<urn:retinue:memory:20260902T140501Z-4d5e6f> <https://w3id.org/retinue/kb#compactedInto> <urn:retinue:memory:20261005T120000Z-c0ffee> .
```

| Predicate (subject: the summary) | Meaning |
|---|---|
| `a kb:MemorySummary` | made by compaction; always alongside `kb:Memory` |
| `kb:generation` `xsd:integer` | 1 for a summary of raw entries only; otherwise 1 + the highest generation among its members |
| `kb:coversFrom`, `kb:coversTo` | the span of what it carries: the earliest and latest member timestamps (a member summary contributes its own coverage, not its compaction time), or the span the plan states |
| `kb:summarizes` `<member>` | a member whose substance the summary carries forward |
| `kb:retires` `<member>` | a member judged obsolete and dropped — the **only** trace of a dropped entry; nothing else is written for it |

Every member, kept or retired, gets `kb:compactedInto` back to its summary in
the same compaction file. **Nothing is ever deleted.** A summary that turns out
wrong is corrected like any memory (`store --corrects <summary>`); a corrected
summary stops hiding its members, so they reappear in recall — that is the
undo.

How many entries each standing summary carries, as the store sees it:

```sparql
PREFIX kb: <https://w3id.org/retinue/kb#>

# Memory summaries, highest generation first: the span each covers and how
# many older entries it carries forward or retired.
SELECT ?summary ?gen ?from ?to (COUNT(DISTINCT ?kept) AS ?summarized)
       (COUNT(DISTINCT ?dropped) AS ?retired) WHERE {
  ?summary a kb:MemorySummary ;
           kb:generation ?gen .
  OPTIONAL { ?summary kb:coversFrom ?from }
  OPTIONAL { ?summary kb:coversTo ?to }
  OPTIONAL { ?summary kb:summarizes ?kept }
  OPTIONAL { ?summary kb:retires ?dropped }
  FILTER NOT EXISTS { ?summary kb:correctedBy ?undone }
} GROUP BY ?summary ?gen ?from ?to
ORDER BY DESC(?gen) DESC(?to)
```

## File layout

Entries live in one flat directory, `chambers/_generated/memory/` (override:
`RETINUE_MEMORY_DIR`), which the life store indexes like any chamber data.
Entries from one session share a file when a session label is known
(`--session` or `RETINUE_MEMORY_SESSION`; appending to an `.nt` is an ordinary
incremental store update, made under an exclusive lock because concurrent
sessions share files); without a label each entry gets its own file. One
compaction run writes one `compaction-<timestamp>-<hex>.nt`, written under a
`.tmp` name and renamed into place, so the store never indexes a half-written
summary that already hides members it does not yet carry.

The directory is deliberately **flat**: a subdirectory created at runtime is
invisible to the store's inotify watches until a rebuild (qlever-dir#10, see
docs/triple-stores.md), so per-month folders would silently delay a whole
month of memories.

## Writing rules

- **Reinforce, don't duplicate.** When the user restates something on record,
  `reinforce <id>` appends a `kb:reiteratedAt`; recall reports how often and
  how recently. A fresh copy instead splits the signal and crowds recall.
- **Challenge, don't edit.** When an entry stops holding, store what is known
  now with `--corrects` (it was false), `--supersedes` (the world changed) or
  `--questions` (in doubt). Never edit or delete an entry file.
- **Pending states expire.** A memory about a pending state, a deadline or an
  in-flight process ("mail queued for approval", "waiting for the insurer")
  gets `--expires` — a date (relevant through the end of that day, UTC), a
  dateTime, or a duration from now in the syntax projects use for
  `remind_before`: `10` or `10d` days, `2w` weeks, `3m` calendar months. A
  rule, preference, lesson or decision **never** expires; it is challenged
  when it stops holding.
- **Relevance is expected durability**, not importance — importance was set
  high by every writer and meant nothing. Three anchors: **1.0** a standing
  rule or preference; **0.7** a decision or a lesson; **0.3** an incident or a
  status. Compaction uses it to tell what to carry forward.
- **Tags: reuse first.** `memory.py tags` lists the tags in use with their
  counts (`--prefix`, `--contains` to narrow); pick an existing one before
  coining a new one. Sender instructions keep their identifier tags
  (`sender:<address>` or `sender:<domain>`, slugged to `sender-…`), which
  triage recalls by.
- **Name the actor.** A subagent's session inherits the `ara` default, so a
  dispatch prompt that asks a subagent to store must say `--actor <its
  basename>` explicitly.
- **Any agent may store, junior included.** The trust signal is the model
  stamp on the entry, not a gate on who may write: a reader (and compaction)
  can weigh a router-tier decision differently from a frontier one.
- Not a memory: data that already enters the store through a chamber, small
  talk, and never secrets — the store is readable by every agent.

## The store guards

`store` runs two checks after validating its arguments and before writing
anything: tags first, then duplicates. How strict they are depends on the
session's tier, derived from the environment:

- No `RETINUE_FRONTIER_MODEL` and no `RETINUE_ROUTER_MODEL`: the deployment is
  untiered, one model does everything, and every session counts as
  **frontier**.
- Otherwise a session is frontier iff its normalized stamp (`--model` or
  `RETINUE_SESSION_MODEL`) equals the normalized frontier model
  (`RETINUE_FRONTIER_MODEL`, falling back to `RETINUE_CLAUDE_MODEL` as the
  spawners do). The router model, a scheduled job's own model and a missing
  stamp are all **lower** tier: unknown is not trusted.

**New tags.** The guard fetches the distinct tags in the store. While there
are fewer than 20 it is off — a fresh deployment has no vocabulary to reuse
and must be able to start. Beyond that, a tag no memory uses yet is refused
with the closest existing tags as a hint (fuzzy matches and tags that contain
it or are contained in it). Identifier tags (`sender-…`) are exempt: one per
sender is their design, not drift.

**Duplicates.** The guard queries every *live* entry (see recall) sharing at
least one of the new entry's tags — with no time cutoff, because the entries
most worth reinforcing are the oldest standing rules — and computes the word
overlap with each: Jaccard similarity over the lowercase words of three or
more letters, any script. An overlap of **0.6** or more is a near-duplicate;
**0.3** or more is merely similar, printed as a warning while the store
proceeds. A near-duplicate that this very call challenges (`--supersedes`,
`--corrects`, `--questions`) is intended — a successor resembles what it
replaces — and does not count.

| | lower tier | frontier |
|---|---|---|
| unlinked near-duplicate | refused: reinforce it, challenge it, or escalate to Ara senior | refused unless `--duplicate-ok` |
| linked near-duplicate | stored | stored |
| similar entry (≥ 0.3) | stored, with a warning | stored, with a warning |
| new tag (≥ 20 tags in the store) | refused, closest tags listed | refused unless `--new-tag`; the hint is printed as a warning |

A refusal lists the matched ids with an excerpt of each and the ways out. The
existing `--force` keeps its one meaning — skip the existence check of
challenged ids — and overrides neither guard. A store that cannot be reached
never blocks a write: both guards warn and let it through, as the existence
check always has.

**The subagent-stamp caveat.** A subagent inherits the environment of the
session that spawned it, `RETINUE_SESSION_MODEL` included, so a subagent that
Ara senior dispatches on a smaller pinned model stores with senior's stamp and
senior's latitude.
That is accepted: the guards are a nudge toward the right operation, not a
security boundary, and the frontier flags still have to be passed
deliberately.

## Recall

`recall` returns live entries by default, leaving out:

| Excluded by default | Lifted by | Shown as |
|---|---|---|
| corrected or superseded (no longer true) | `--include-superseded` | `CORRECTED by …`, `SUPERSEDED by …` |
| past `kb:expires` (no longer relevant) | `--include-expired` | `EXPIRED <date>` |
| compacted into a summary that is not itself corrected | `--include-compacted` | `COMPACTED into <id>` |

Questioned entries always stay in, flagged — doubt is a signal, not a verdict.
The current time is injected into the query as a literal rather than read
from `NOW()`, so a query is reproducible. The same liveness definition serves
the duplicate guard and `tags`.

Filters (`--tag` any-of, `--actor`, `--since`, `--until`, `--min-relevance`)
and `--limit` (default 20) work as before. The order is **summaries first,
highest generation first, then everything newest first** — a broad recall's
limit now cuts old episodic entries rather than the consolidated rules. A
summary row reads

```text
- 2026-10-05 12:00:00Z (ara; ludmila, signal; relevance 0.8; SUMMARY gen 1 of 12 entries, 4 retired, covers 2026-08-30..2026-09-20; via claude-opus-5) [20261005T120000Z-c0ffee]
  Ludmila: reach her on Signal, never e-mail (standing rule since 2026-08-30, restated 3x). …
```

where "of 12 entries" counts every member, kept and retired.
`recall --expand <summary>` lists that summary's members regardless of any
exclusion, each labeled `kept` or `retired`, in the same row format (no limit
unless `--limit` is given); it does not combine with the filter flags.
`--json` returns the same rows as objects, including `expires`, `summary`,
`generation`, `covers_from`, `covers_to`, `summarizes`, `retires` and
`compacted_into` (and `role` under `--expand`).

## Compaction

`compact --plan PATH` (or `-` for stdin) writes summaries from a plan that a
frontier session drew up after reading the entries. The plan is one JSON
object or a list of them:

```json
{"topic": "ludmila", "content": "Ludmila: reach her on Signal, never e-mail …",
 "tags": ["ludmila", "signal"], "relevance": 0.8,
 "summarizes": ["20260830T091244Z-1a2b3c", "…"], "retires": ["20260902T140501Z-4d5e6f", "…"],
 "covers_from": "optional date or dateTime", "covers_to": "optional"}
```

Everything is validated before anything is written, and every error is
reported at once: non-empty content; at least one tag (the topic always joins
the tags — it is the tag recall finds the summary by); relevance within 0..1;
`summarizes` and `retires` disjoint and together non-empty; syntactically
valid ids; each entry in at most one summary of the plan; each member present
in the store (skipped with `--force`; an unreachable store warns, as for
`store`); and no member already compacted into a summary that stands. The
generation and coverage come from the store; when it cannot be reached
`compact` refuses rather than write invented metadata. The whole plan becomes
one `compaction-*.nt`; the summary ids are printed on stdout, one per line.
The store guards do not apply: compaction is frontier work by policy, and the
job enforces that.

### The compaction scheme

What the scheduled compaction job will do — the mechanism above is its
writing half:

- **Topic-scoped summaries.** Entries are grouped by tag; a summary is the
  current consolidated knowledge about one topic.
- **Eligibility.** An entry is eligible when it is not compacted (into a
  standing summary) and older than a **freeze age of 14 days**, so recent
  entries stay verbatim while they are still in play.
- **One cluster per entry.** Each eligible entry joins the tag cluster with
  the most eligible members, so it is compacted exactly once even when it
  carries several tags.
- **Sizes.** A cluster needs at least **5** members to be worth a summary; a
  summary takes at most **40** members per batch.
- **Cadence and tier.** Weekly, in a frontier-tier session.
- **Rolling consolidation.** A summary older than the freeze age is an
  ordinary member of its topic, so the next run folds it into a new
  generation, and each topic converges on one current document instead of a
  pile of summaries.
- **Tags.** A summary's tags are the union of the member tags occurring at
  least twice, plus the cluster tag.
- **What to keep.** Corrected, superseded and expired members are retired,
  with the outcome kept in one clause when it matters ("the August filing
  arrangement was voided"). Standing rules are kept with their dates and
  restatement counts. Chains collapse to their terminal state ("queued",
  "approved", "sent" becomes "sent on …"). Identifiers — ids, addresses,
  numbers, file names — are kept verbatim. Open items stay explicit.

**Status:** the scheduled job ships in the next PR. Until then `compact` is
runnable by hand with a plan written by Ara senior.

## Environment

| Variable | Effect |
|---|---|
| `RETINUE_MEMORY` | `0`/`false`/`off`/`no` disables writing: `store`, `reinforce` and `compact` become successful no-ops (recall still works) |
| `RETINUE_MEMORY_DIR` | where entries are written (default `$CHAMBERS_DIR/_generated/memory`) |
| `RETINUE_MEMORY_SESSION` | default session label for `store` |
| `RETINUE_MEMORY_ACTOR` | default actor (default `ara`) |
| `RETINUE_SESSION_MODEL` | the session's model, set by the spawner; stamped as `kb:model` and used for the tier |
| `RETINUE_FRONTIER_MODEL`, `RETINUE_ROUTER_MODEL`, `RETINUE_CLAUDE_MODEL` | the tiers (docs/model-routing.md); with neither of the first two set every session is frontier |
| `SPARQL_ENDPOINT_LIFE` | the life store endpoint |

Claude Code's built-in auto memory is disabled deployment-wide: this store is
the system's only memory.
