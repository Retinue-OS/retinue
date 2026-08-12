# Triage delivery gate — spend model credits only on senders that matter

## Problem

Every triage turn is a fresh `claude -p` session that reloads the whole static
prefix (harness + tool schemas + `CLAUDE.md` + `MEMORY.md`) before it does any
work, and re-reads that prefix on every tool round-trip. So cost scales as
`prefix × round-trips × sessions`, and the dominant term is **sessions**: the
e-mail triage job fires every 30 min (~48×/day) and every inbound messenger
message spawns a session at the gateway — the large majority of them finding
nothing that needed a model at all.

The fix is a **credit-free gate** in front of the spawn, mirroring
`agent-self-review`: a plain script (or the gateway itself) decides whether a
message is worth a model turn, and `claude -p` only starts on a hit. Nothing is
lost — anything the gate holds back is picked up by a once-daily catch-all run.

This affects **inbox-mode accounts only** (accounts whose incoming mail/messages
the system triages on the user's behalf). Control-mode and outbound identities
are untouched.

## Policy (shared by both channels)

| Class | Fast loop (frequent) | Daily catch-all |
|---|---|---|
| **Whitelisted sender** | model runs now | (already handled) |
| **Unknown sender** (messenger) | model runs now, flagged *unknown* → asks user to whitelist/blacklist | — |
| **Non-whitelisted** (e-mail) | held | model runs for **any** sender |
| **Blacklisted handle** (messenger) | held, no prompt | model runs |
| **Group-blocked / noise-class** | never triggers | never drained (see below) |

The tradeoff the user accepts: cold senders wait up to 24 h for a model turn.
The daily run bounds that latency; nothing is dropped.

### Whitelist — exact addresses by default, wildcards by hand

The whitelist is a list of match entries:

- **Auto-added:** exact addresses the user has corresponded with
  (`alice@epfl.ch`), derived from the Sent folder and refreshed periodically.
- **Manual:** a `*@domain` (or `*@*.domain`) **wildcard** the user adds
  deliberately to trust a whole domain.

A message matches if its sender equals an exact entry **or** falls under a
wildcard entry. Only exact addresses are ever auto-added — the system never
auto-adds a domain. This is what makes freemail safe: emailing one
`person@gmail.com` whitelists *that address*, never all of `gmail.com`. A
freemail domain is only ever trusted if the user types the wildcard themselves.

Glob support is deliberately minimal: `*@domain` and `*@*.domain`. No regex.

### Messenger whitelist / blacklist / group-block

Messenger identity is a **handle**, not a domain — no aliasing problem, so no
wildcards needed there.

- **Whitelist:** handles the user has replied to / contacts, seeded from the
  gateway's contact directory + recent chats, extended by the ask-flow below.
- **Blacklist:** an unknown sender the user declines to whitelist goes here so
  the user is **never asked again**. Permanent until hand-edited.
- **Group-block:** groups the user marks as never allowed to trigger an
  unknown-sender prompt (seeded with known-noise groups from day one).

### Unknown-sender ask-flow (messenger only)

An inbound message from an unknown handle (not whitelisted, not blacklisted, not
in a blocked group, not noise-class) **does** get a model turn, flagged as coming
from an unknown sender. The model opens a dashboard thread asking whether to
whitelist the sender. On "no", the handle is added to the blacklist.

## State

Whitelist, blacklist and group-block are **emitted as `.nt`** — the same pattern
as the existing `_generated` registries (`agents.nt`, `conversation-models.nt`).
That choice does three jobs at once: it indexes natively in qlever (no
converter), it is trivial for a gateway to parse off disk, and it retires any
separate per-app JSON.

**Retinue (Ara) is the sole writer of the policy files; the gateways are
readers** — the reverse direction of the message files, so single-writer-per-file
still holds and there is no write race. The messenger policy rides on the **same
per-gateway volume as that channel's messages** (see the volume topology below),
because the gateway must read it at classify time — see the next point. The
e-mail whitelist has no gateway, so it lives on the retinue side under
`_generated` purely so it is queryable over SPARQL.

**Why the gateway reads a raw file, never SPARQL.** Classification happens on the
inbound hot path, in-process. If the gateway resolved whitelist/blacklist over
SPARQL it would inherit the ~15 s reindex lag and a network dependency there —
exactly what this design avoids. So the gateway reads the policy `.nt` **straight
off the mounted volume** (fresh, no lag), while qlever indexes the very same file
for the *query* path. Same file, two readers, different freshness needs, both
satisfied.

**The model is the normal editor of this state, not the user with a text
editor.** Every change flows through Ara: the unknown-sender ask-flow writes a
whitelist or blacklist entry from the user's yes/no, and instructions like "trust
everyone at `*@epfl.ch`" or "block that group" are conversational — Ara emits the
wildcard or the group id and confirms. The files stay plain, readable `.nt`, so
they *can* be corrected by hand, but that is a fallback. Ara also reads them (over
SPARQL) to answer "who's whitelisted?".

## The two channels need the gate in different places

E-mail is **pull**; messenger is **push**. The policy is shared; the plumbing is
not.

### E-mail — scheduler-gated poller (pull)

IMAP has a queryable backlog. The gate is a scheduler `command` job (zero Claude
credits):

1. List new INBOX mail since last run.
2. Dedup by message-id against the existing triage status (same sanitized
   id-scheme triage already uses).
3. Keep only whitelisted senders → spawn the model for those.
4. The **daily** job runs for **any** sender (fixed morning hour, before the
   briefing).

### Messenger — gateway-owned store + delivery flag (push)

Signal / WhatsApp / Telegram have **no queryable backlog** — a message exists
only at the instant the gateway receives it. So the messenger backlog is
**synthesized in the life store**, and the gateway owns it.

**Write path — one volume per gateway, not one across all of them.** Each gateway
has its **own** volume, mounted into exactly two places: that gateway (RW), and
the retinue side under `chambers/_generated/messenger/<channel>/` (so qlever-dir
indexes it; RO for qlever). Three gateways → three independent volumes; Signal's
messages never touch WhatsApp's volume. On the volume the gateway writes **one
`.nt` file per inbound message**, and retinue writes that channel's policy `.nt`
(whitelist/blacklist/group-block) that the gateway reads back to classify — the
two directions share the volume, never a file. No write endpoint in the `retinue`
container. (~15 s reindex lag affects only the SPARQL view — the gateway reads
raw files, so its hot path sees no lag; see below.)

**The `delivered` flag — not `read`.** Each message carries a boolean
`delivered`. It means exactly one thing: *the gateway has handed this message to
a consuming model turn.* It deliberately is **not** "read" (which would be
ambiguous about human vs. model, and about querying vs. handling).

**The gateway is the single writer of both the message and its flag.** This is
the crucial property — it removes any multi-writer race:

- The gateway exposes `GET /undelivered?since=<date>`: returns undelivered
  messages **and flips them to `delivered`** as a side effect. This is the only
  operation that mutates the flag.
- Marking delivered = rewriting the message's one small `.nt` file → one reindex.
  One-file-per-message keeps that flip cheap.
- **A SPARQL query never touches the flag.** Reading the messages over SPARQL is
  a pure read of the store replica — so the user (via Ara) can browse full
  messenger history ("what did X say last week?") without draining the
  undelivered queue.

This is the IMAP analogy, renamed: "fetch unseen → mark `\Seen`" ≡ "fetch
undelivered → mark delivered." Both are stateful fetches owned by the message
store; a read-only query of either changes nothing.

**Fast loop vs. daily drain:**

- On arrival, the gateway classifies (whitelist / unknown / blacklist /
  group-block / noise). Whitelisted or unknown-sender → hand to a model turn now,
  marked delivered. Blacklisted / group-blocked → written `delivered: false`,
  no model turn.
- The daily catch-all calls each inbox-mode gateway's
  `GET /undelivered?since=…`, processes the returned messages; the flag flips as
  a side effect of the fetch, so a re-run is naturally idempotent. It does **not**
  read undelivered over SPARQL (that wouldn't clear the flag).

**Noise-class messages** (status updates, voice-note echoes, the daily-briefing
self-echo, news channels, note-to-self) are written as triples with
`delivered: true` already set. History stays complete and queryable; the daily
drain never picks them up; they never prompt.

**Schema.** Align the per-message triple shape with the session-logging
unification (retinue#85) rather than inventing a parallel vocabulary — one RDF
message log, with `delivered` as an additional property.

## Why the ~15 s SPARQL lag is harmless

There is a gap between a message arriving and it being queryable (qlever-dir's
rebuild). It costs nothing, because the gateway's inbound handler already **has
the message in-process** — it never queries the store to classify or to spawn.
The only SPARQL consumers are the user's ad-hoc history questions, which are far
past 15 s. And the daily drain reaches the backlog through the gateway endpoint,
not through SPARQL, so it is never subject to the lag either.

## Rollout / tiers

Tier-3 across both the framework and the gateway services:

- **framework:** the e-mail gate scheduler job + script, the daily-drain jobs,
  the shared state files, and this doc.
- **gateways:** `signal-gateway` / `whatsapp-gateway` / `telegram-gateway` each
  get the shared volume mounted RW, per-message `.nt` writing, the
  classification gate on inbound, and `GET /undelivered?since=…`.
- **compose:** one volume per gateway, each mounted into its gateway (RW) and
  into `chambers/_generated/messenger/<channel>/` (RO for qlever).

Takes effect on merge → `scripts/self-update.py` (rebuilds the gateway images).

## Open implementation questions (to resolve during build)

1. Exact triple predicates for a message, reconciled with retinue#85.
2. Whether the daily e-mail run and the daily messenger drain are one job or two.
3. Recompute cadence for the derived e-mail whitelist (Sent-folder scan).
