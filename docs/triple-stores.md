# Triple stores in Retinue — what they make possible

*Audience: agents running inside Retinue, and humans curious about why this
architecture was chosen.*

Retinue keeps its knowledge in **files** and queries it through **SPARQL**. That
combination is the whole point: files stay the source of truth — hand-editable,
diffable, reviewable in a pull request — while an always-on triple store gives
agents a query surface over data far larger than any context window.

This document explains what that buys you, with concrete examples.

---

## The core idea: files are the source of truth

There is no database to write to. No SPARQL UPDATE, no admin UI, no import job.
You edit a file, commit it, and the store catches up.

The `qlever-life` service (built from the [qlever-dir](https://github.com/retinue-os/qlever-dir)
submodule) mounts the shared chambers volume read-only at `/data` and indexes
**every** RDF file it finds — `.nt`, `.ttl`, `.n3` — across every mounted
chamber. It watches the tree with inotify and rebuilds **blue-green**: the new
index is built into an idle slot, health-checked, and only then does nginx swing
traffic over. A failed build leaves the previous index serving. There is no
downtime and no window in which the store is half-updated.

Each file's triples land in a named graph derived from the file's path:

```
graph IRI = <BASE_URI><path relative to the chambers root>
```

With `BASE_URI: "file:"` that gives you graphs like
`<file:health/observations/clinical/sensors/cgm/glucose_2026-05-21.nt>`. This is
quietly powerful. Provenance is free — every triple knows which file it came
from, so you can scope a query to one sensor, one chamber, or one ingest run
without anyone having to model provenance by hand:

```sparql
SELECT ?s ?p ?o WHERE {
  GRAPH ?g { ?s ?p ?o }
  FILTER(STRSTARTS(STR(?g), "file:health/observations/clinical/sensors/cgm/"))
}
```

And because the graph IRI is synthesized at index time, the files themselves
contain plain **triples**, never quads. Writers don't have to know where they'll
be mounted. Move a file, and its provenance follows automatically.

### Why this matters

Curating linked data by hand is famously miserable. Triple stores want you to
speak RDF at them; humans want to edit a text file. The usual outcome is that
the graph rots, because updating it is a chore separated from the work that
generated the fact.

Retinue removes the separation. The artifact you were going to write anyway —
a Markdown note, a CSV export, a contact list — *is* the graph. Nothing is
authored twice, nothing can drift out of sync, and `git log` is your audit
trail.

---

## Advantage 1 — Querying files that were never meant to be data

### Markdown with frontmatter → a queryable project graph

qlever-dir indexes non-RDF files too, when a converter is declared for their
extension. Drop a `.qlever/converters.json` next to the data:

```json
{ "md": "md2ttl.py" }
```

The nearest such file walking up from the source wins. A converter is any
executable invoked as `<converter> <input-file>` that emits Turtle on stdout;
the source file keeps its own path-derived graph IRI. That's the entire
contract — no plugin API, no registration.

A second reference converter, `scripts/jsonld2ttl.py`, expands **JSON-LD** the
same way (declare it with `{ "jsonld": "jsonld2ttl.py" }`). This is useful for a
document a program also needs to read as *plain JSON* without an RDF library —
the JSON-LD stays a normal JSON file to `json.load`, yet lands in the life store
too. The framework's own conversation-model list
(`config/conversation-models.jsonld`, read by `web-gateway.py`) is exactly this
pattern: one source of truth, reachable both as JSON and over SPARQL. Note that
`config/` is framework code, not a chamber, so QLever does not index it in the
base stack — a deployment that wants those triples copies (or symlinks) the file
into a chamber that declares the `jsonld` converter.

The reference converter turns YAML frontmatter into triples. A project file is
just a Markdown note a human is happy to open and edit:

```markdown
---
type: project
id: proj-thermostat-bluetooth
title: "Understand the smart thermostat Bluetooth protocol"
goal: "The thermostat control protocol is documented and reproducibly tested."
current_next_action: "Wait for the manufacturer's response."
current_actor: actor-manufacturer
waiting_since: 2026-06-20
expected_by: 2026-07-05
paused: false
links:
  - gmail:abc123
  - file:/Drive/Thermostat/Bluetooth/log-2026-06-20.pcapng
---

Prose notes for humans and agents go here.
```

The frontmatter is a mechanical key→predicate mapping — no LLM needed, and no
LLM *wanted*: this part must be deterministic. The prose body stays prose, left
to on-demand extraction if and when something there is worth promoting to a
fact.

The dashboard's projects card is then a single SPARQL query over every project
file in every chamber ([`scripts/web-gateway.py`](../scripts/web-gateway.py)):

```sparql
PREFIX k: <https://w3id.org/retinue/kb#>
SELECT ?p ?title ?actor ?next ?since ?expected ?status WHERE {
  ?p rdf:type k:Project .
  OPTIONAL { ?p k:title ?title }
  OPTIONAL { ?p k:currentActor ?actor }
  OPTIONAL { ?p k:currentNextAction ?next }
  OPTIONAL { ?p k:waitingSince ?since }
  OPTIONAL { ?p k:expectedBy ?expected }
  OPTIONAL { ?p k:status ?status }
  OPTIONAL { ?p k:paused ?paused }
  FILTER (!BOUND(?paused) || ?paused = false)
  FILTER (!BOUND(?status) || ?status != "done")
} ORDER BY ?title
```

The results split into "mine" and "waiting on someone else" by `currentActor`.
Note what *didn't* happen: nobody maintained a project database. Someone edited
Markdown files in a git repo, and a live view of everything in flight fell out.

The same trick generalizes. Contact lists, reading lists, meeting notes, expense
records — anything with a bit of frontmatter becomes joinable with everything
else in the store.

> **Caveat, honestly stated:** the inotify watcher currently fires only on
> `.nt`/`.ttl`/`.n3` changes, while the index build *does* process converter
> extensions like `.md`. So a frontmatter edit is picked up on the next rebuild
> triggered by some other RDF change, or at container restart — not within the
> usual ~15 s. Worth knowing before you debug a stale projects card.

### Big CSVs from sensors → a decade of readings, one query away

The other half of the file story is the boring, enormous stuff: fitness tracker
exports, continuous glucose monitor dumps, ketone meter logs. These arrive as
CSV with tens of thousands of rows. They are hopeless as context — you cannot
paste a year of five-minute CGM readings into a prompt, and you shouldn't want
to.

The pipeline is: `scripts/sync-garmin.py` (or a manual export) drops a CSV in
`observations/inbox/` → the **archivist** subagent files it into the right
folder → `scripts/ingest-sensors.py` writes a sibling `.nt` → qlever-life picks
it up.

Readings are modelled in **SOSA** (`http://www.w3.org/ns/sosa/`), the W3C
sensor-observation vocabulary — five triples per observation:

```
<urn:obs:ckm:X1234:42> a                     sosa:Observation ;
                        sosa:observedProperty <urn:health:property:blood-ketone-bhb> ;
                        sosa:hasSimpleResult  "1.4"^^xsd:decimal ;
                        sosa:resultTime       "2026-05-21T09:00:00"^^xsd:dateTime ;
                        sosa:madeBySensor     <urn:health:sensor:ckm:X1234> .
```

Using a standard vocabulary rather than an ad-hoc one is not pedantry. It means
a glucose reading from a CGM, a sleep score from a ring, and a step count from a
watch are all *the same shape*. One query pattern reads all of them. Adding a
new device is a new `observedProperty` URI, not a new schema.

Properties currently ingested include `blood-glucose`, `blood-ketone-bhb`,
`resting-heart-rate`, `heart-rate-variability`, `step-count`, `sleep-score`,
`deep-sleep-duration`, `rem-sleep-duration`, `sleep-efficiency`,
`sleep-duration`, `recovery-score`, `movement-score`, `stress-level`, `spo2`,
and `skin-temperature`.

---

## Advantage 2 — Agents get answers, not haystacks

This is the part that changes what an assistant can credibly say.

### "Do you too sleep worse on a full moon?"

Someone who is convinced the moon disturbs *their* sleep asks whether you have
the same experience. It is an ordinary question, and normally an unanswerable
one: nobody remembers how they slept on the nights of the last thirty full
moons, so the honest answer is a shrug and the usual answer is whatever
impression comes to mind.

A conventional AI assistant is no better placed — it answers from the
literature, or hedges, or asks you to summarise your own sleep. With the life
store it is simply a query. Every night's sleep duration is already in the graph
from the wearable, and the lunar phase is computable from the date. You group,
you compare, you report the actual numbers.

Which includes, importantly, being able to say "no — my sleep on full-moon
nights is indistinguishable from any other night." That is the answer a system
pattern-matching on vibes will never give you, and it is the one that actually
settles the question.

Sketch:

```sparql
PREFIX sosa: <http://www.w3.org/ns/sosa/>
SELECT ?date (AVG(?v) AS ?sleepMinutes) WHERE {
  ?o a sosa:Observation ;
     sosa:observedProperty <urn:health:property:sleep-duration> ;
     sosa:hasSimpleResult ?v ;
     sosa:resultTime ?t .
  BIND(SUBSTR(STR(?t), 1, 10) AS ?date)
} GROUP BY ?date
```

— then bucket the dates by lunar phase in the agent and compare the two groups.
The store does the aggregation over years of readings; the agent does the
reasoning. Neither does the other's job.

The general shape is worth noticing: the question came from someone else's
folk belief, and the answer came from your own data. No study, no averages over
strangers — just what your nights actually did.

### "You slept longer than 78% of nights"

The Coach persona routinely puts a data point in context this way, and that
sentence is only possible because the full distribution is queryable in
milliseconds. "You slept 7h12m" is a number. "You slept longer than 78% of
nights" is *information* — it tells you where you stand without you having to
remember what normal looks like.

Percentile framing like this needs the whole history at query time. Keeping the
whole history in context is impossible; keeping an LLM-written summary of it is
worse than impossible, because summaries silently go stale and quietly invent
things. A SPARQL count over the real observations cannot drift from the truth.

### What this is, architecturally

Retinue's agents have a tool that lets them reach data at a scale no context
window can hold, while getting back something with the same immediacy as data
that *is* in context. The semantic layer is what makes the round trip cheap: the
agent doesn't need to know which file, which device, or which year — it names
the property it cares about and the store finds it.

That combination — LLM reasoning over a semantically uniform, always-current
data substrate — is the meaningful difference from a chat assistant with file
access. Grep gets you text. SPARQL gets you joins.

---

## Advantage 3 — Separate stores where the shape of the data demands it

The life store's design assumes small-to-medium files that change often. Some
data is the opposite: enormous and effectively immutable. The worked example
throughout this section is a **genomics** store — a deployment-defined service
(the example override calls it `qlever-genomics`; it is not part of the
framework) serving a single `genetics.nt` (optionally gzipped) via
`qlever-static`. Such a store builds its index exactly once, into a persistent
volume, keyed on a marker file — restarts and image rebuilds do not re-index.
If the source ever changes, you reindex on purpose:

```bash
docker compose exec qlever-genomics sh -c 'rm -rf /index/*'
docker compose restart qlever-genomics
```

**Nothing prevents qlever-dir from picking up `genetics.nt`** — it is a perfectly
ordinary `.nt` file in a mounted chamber. The special-casing is a considered
trade-off, not a limitation:

- **Rebuild cost.** qlever-dir rebuilds the *entire* index on any change. Folding
  a large, static genome into it would make every unrelated edit — a project note,
  a day of step counts — pay the cost of re-indexing data that has not changed
  since the sequencing came back.
- **Blast radius.** A slow rebuild is a stale life store. Isolating the genome
  keeps everyday queries fast and everyday writes cheap.
- **Different access pattern.** Genomic queries are rare, specialist, and run by
  the Medic. Life-store queries are constant and run by everything.
- **Different lifecycle.** One is append-mostly and volatile; the other is
  written once and read for decades. Different lifecycles deserve different
  indexes.

The rule of thumb generalizes: **put data in the life store unless it is both
large and static.** If it is, give it its own `qlever-static` service and index
it deliberately.

Note that this is a deployment decision. `qlever-life` is part of the framework;
a store like `qlever-genomics` is declared in a deployment's compose override
(see `docker-compose.override.example.yml`), because which specialist stores you
need depends on what data you keep. A deployment-defined store announces itself
to agents the same way the life store does — through `SPARQL_ENDPOINT_<NAME>`
environment variables set in that same override (see the practical reference
below).

---

## A note on the Semantic Web

RDF, SPARQL, named graphs, shared vocabularies — none of this is new. The
Semantic Web has had these tools for twenty years, and by most accounts never
arrived in the form its founders imagined.

The usual diagnosis is complexity, and specifically complexity *for humans*.
Publishing linked data meant learning ontologies, minting URIs, and maintaining
alignment with vocabularies you did not write, all for an audience of machines
that mostly weren't listening. Consuming it meant writing SPARQL. Both ends
asked people to do precise, tedious, unrewarding work, and people — reasonably —
declined. HTML won because it was forgiving; RDF asked to be exact.

That constraint has changed. When the producer is an agent, minting consistent
URIs and picking the right predicate from SNOMED or LOINC or SOSA is cheap and
reliable work — exactly the kind of precise, tedious mapping that a language
model does well and a human does grudgingly. When the consumer is also an agent,
writing SPARQL is not a barrier either; it is just another tool call.

The difficulty was never in the formats. It was in the interface between the
formats and people. Put agents on both ends and what is left is the part that
was always good: a graph where everything joins to everything, provenance is
inherent, vocabularies are shared so data from different sources composes without
negotiation, and any question you can express as a pattern gets an answer that is
*true* rather than plausible.

Retinue is a small, working demonstration of that. The Semantic Web's tooling
turns out to be excellent — it was just waiting for users who don't mind angle
brackets.

---

## Practical reference for agents

**Endpoints** (read-only — no SPARQL UPDATE anywhere): every endpoint is
advertised to agents by an environment variable pair —
`SPARQL_ENDPOINT_<NAME>=<url>` plus an optional
`SPARQL_ENDPOINT_<NAME>_DESC` one-liner describing the contents. Enumerate
what the current deployment offers with:

```bash
env | grep '^SPARQL_ENDPOINT_' | sort
```

The framework itself provides exactly one,
`SPARQL_ENDPOINT_LIFE=http://qlever-life:7001` (the life store).
Deployment-defined stores — like the genomics example above — add their own
pair in the deployment's compose override, and list their service hostnames in
`SPARQL_NO_PROXY` (in the deployment's `.env`) so queries bypass the
egress-audit proxy. Env vars fit here because the stores themselves arrive as
compose services in the same override; the advertisement travels with the
service definition, needs no framework code, and (unlike Docker labels) is
readable from inside the agent container without a docker socket.

**Querying** — POST the query form-urlencoded as `query=`, with
`Accept: application/sparql-results+json`:

```bash
curl -s http://qlever-life:7001 \
  -H 'Accept: application/sparql-results+json' \
  --data-urlencode 'query=SELECT ?g (COUNT(*) AS ?n) WHERE { GRAPH ?g { ?s ?p ?o } } GROUP BY ?g ORDER BY DESC(?n) LIMIT 20'
```

That query is a good first move in an unfamiliar deployment: it shows you which
files are in the store and how much each contributes.

**Writing data** — write `.nt` files into a chamber. Triples, not quads; the
graph IRI is synthesized at index time, so never write one into the file. Follow
the archivist's ontology conventions: SOSA for observations, LOINC for lab
identifiers, UCUM for units, SNOMED CT for findings and diagnoses, RxNorm for
medications, FoodOn for nutrition, Sequence Ontology for variants, schema.org as
a fallback. In a deployment with a separate static store (like the genomics
example above), all data for that store goes into the single file it indexes —
e.g. `genetics.nt` at the chamber root, without exception.

**Troubleshooting** — if a file's triples are missing, look for the diagnostic
quad the build emits instead of failing:

```sparql
SELECT ?g ?msg WHERE { GRAPH ?g { ?g <urn:qlever-dir:parsingError> ?msg } }
```

A parse failure never breaks the build; it just replaces that file's triples
with the error message, so a malformed file degrades one graph rather than the
whole store.

---

## See also

- `CLAUDE.md` — "SPARQL endpoints", the operational summary agents read at
  session start
- `README.md` — startup sequence, chamber mounting (a host-mounted chamber needs
  a mount for `qlever-life` too, or its data won't be indexed)
- [qlever-dir](https://github.com/retinue-os/qlever-dir) — the indexing service,
  included as a submodule
- `.claude/agents/archivist.md` — ingestion conventions and the ontology table
