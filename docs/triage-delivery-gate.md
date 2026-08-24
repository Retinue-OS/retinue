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

## Policy — two orthogonal axes (messenger)

Messenger routing is decided on **two independent axes**, so the classes below
are a *combination* of a sender status and a group's flags, not a single ladder:

- **Sender** — a handle is **whitelisted**, **blacklisted**, or **unknown**.
- **Group** — three independent flags: **news** (its messages are also forwarded
  to the news feed / Herald), and at most one of **quieted** or **ignored**
  (which govern *unknown* senders in that group).

The sender axis wins for a known handle: a **whitelisted** handle is always
forwarded live and a **blacklisted** handle is never forwarded live, regardless
of the group. The group's quieted/ignored flag only bites for an **unknown**
sender — matching the user's model, "new senders in quieted or ignored groups".
The **news** flag is orthogonal to all of it: a message can go to the news feed
whether or not it also earns a triage turn.

| Class | Fast loop (frequent) | Daily catch-all |
|---|---|---|
| **Whitelisted handle** | model runs now | (already handled) |
| **Unknown handle**, normal group | model runs now, flagged *unknown* → asks user to whitelist/blacklist | — |
| **Blacklisted handle** | held, no prompt | model runs |
| **Unknown handle**, **quieted** group | held, no prompt | model runs (drained) |
| **Unknown handle**, **ignored** group | never triggers | never drained (see below) |

E-mail has the **same two axes**, with one channel-specific twist: a mail's
"group" is its **mailing list** (`List-Id`), and a mail that carries no usable
list header is its own group of one, keyed on the sender address. See *E-mail
groups* below.

The tradeoff the user accepts: cold senders (blacklisted or in a quieted group)
wait up to 24 h for a model turn. The daily run bounds that latency; nothing is
dropped. Only an **ignored** group is deliberately never drained.

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

### E-mail groups — the list axis, and the same three flags

A mailing list is not a sender. The people who post to a list change; the list
does not, and it is the list one has an opinion about ("read-only", "I answer on
this one"). So e-mail is routed on the same two axes as messenger:

- the **sender** decides *how urgently* a mail is triaged — whitelisted means the
  frequent run, anything else waits for the daily sweep;
- the **group** decides *where else it goes* — the same `news` / `quieted` /
  `ignored` flags PR #114 introduced for messenger groups.

The group id is the message's `List-Id` (RFC 2919), normalised: bracketed part,
lowercased, and rejected unless it looks like a domain — no whitespace, at least
one dot, under 200 characters. Real headers are messier than the RFC (opaque
hashes, and values whose bracketed part is a display name, e.g.
`799706515 <Brack News>`), and inventing a group nobody can flag is worse than
having none. A mail with no usable `List-Id` falls back to **its own sender
address as its group** — a newsletter that carries no list header is a group of
one, so the three flags apply to it with no second mechanism.

| class | frequent | daily | in the feed |
|---|---|---|---|
| whitelisted sender | yes | yes | if the group is `news` |
| unknown sender, `ignored` group | no | no | if the group is `news` |
| unknown sender, `quieted` group | no | yes | if the group is `news` |
| unknown sender, unflagged group | no | yes | no |

The last two triage rows are deliberately identical: on a pull channel the daily
sweep already *is* the quiet tier. `quieted` earns its keep as the explicit
opposite of `ignored` — "in the feed **and** in the triage" — which is exactly
the distinction between a list one only reads and one one also writes to:

- a read-only newsletter → **`news` + `ignored`** (filed to the feed, never a
  model turn);
- a list one both reads and answers on → **`news` + `quieted`** (filed to the
  feed *and* still triaged).

Group entries take the same two shapes as whitelist entries — an exact id, or a
`*@domain` wildcard. On a group the wildcard also covers ids **beneath** the
domain, because a `List-Id` is a namespace a platform hands out per publication:
`*@substack.com` therefore matches both `no-reply@substack.com` and the list
`sgcarney.substack.com`. It does **not** match a mailbox at a subdomain
(`someone@sgcarney.substack.com`) — that stays the strict `*@*.domain` reading
the whitelist relies on, so loosening the group axis never loosens trust.

```bash
python3 scripts/triage_policy.py email-news-add   '*@substack.com'
python3 scripts/triage_policy.py email-ignore-add '*@substack.com'
python3 scripts/triage_policy.py email-quiet-add  members.list.example.org
python3 scripts/triage_policy.py show-email       # whitelist / news / quiet / ignore
python3 scripts/triage_policy.py check-email stranger@x.com \
    --list-id '<members.list.example.org>'
# group   members.list.example.org
# news    no
# triage  group-quieted (frequent: no, daily: yes)
```

`quieted` and `ignored` are mutually exclusive, as on messenger; `news` combines
with either. The axes stay **independent**: declaring a group `news` does not
whitelist anyone, and a whitelisted correspondent is not turned into a feed
source — a whitelisted sender writing to an `ignored` list is still triaged now.

Everything lives in the **same** `.nt` file, which is why `save_email_policy()`
is the only supported writer — a caller that rendered just the whitelist would
silently erase the group flags (the Sent-folder refresh used to be exactly that
shape).

Like the messenger flags, this is edited by talking to Ara ("Newsletter X gehört
in den Feed"); the CLI is what she runs.

Address-level `news` entries written before list detection existed keep working:
news is matched against the group **and** the bare sender address, so a
newsletter that turns out to carry a `List-Id` is not silently un-filed.

### Messenger sender axis: whitelist / blacklist

Messenger identity is a **handle**, not a domain — no aliasing problem, so no
wildcards needed there.

- **Whitelist:** handles the user has replied to / contacts, seeded from the
  gateway's contact directory + recent chats, extended by the ask-flow below.
- **Blacklist:** an unknown sender the user declines to whitelist goes here so
  the user is **never asked again**. Permanent until hand-edited.

### Messenger group axis: news / quieted / ignored

A group carries up to three flags, all set through Ara's policy editor:

- **news** — the group is a broadcast source worth keeping in the news feed. Its
  messages are forwarded to the Herald in addition to (and independently of) any
  triage decision. See *The news rail* below.
- **quieted** — an *unknown* sender in this group is not forwarded live, but the
  message is held for the daily drain, so it still reaches triage within a day.
- **ignored** — an *unknown* sender in this group never reaches triage at all
  (accounted for, never drained). This is the strong "don't bother me" flag,
  seeded with known no-action groups from day one.

"Group" here means **any shared chat**, not only a group proper: a Telegram
broadcast channel is one too, and is the typical `news` source. The gateway has
to say so explicitly, because Telethon reports a channel as `is_channel` and
*not* `is_group` — reading `is_group` alone leaves a channel post with no group
id, so none of these flags can match and every post arrives as if it were a
private message from an unknown sender.

`quieted` and `ignored` are mutually exclusive (a group is one or the other, or
neither); `news` is independent and combines with either. The legacy
`triageBlockedGroup` predicate is read as `ignored`, so a policy file written
before this split keeps its old "never reaches triage" behaviour and is migrated
to the `ignored` predicate on the next write.

### Unknown-sender ask-flow (messenger only)

An inbound message from an unknown handle in a **normal group (or no group)** —
not whitelisted, not blacklisted, not in a quieted or ignored group — **does**
get a model turn, flagged as coming from an unknown sender. The model opens a
dashboard thread asking whether to whitelist the sender. On "no", the handle is
added to the blacklist. An unknown handle in a **quieted** group is held for the
daily drain instead; in an **ignored** group it is never asked about.

## State

Whitelist, blacklist and the group flags are **emitted as `.nt`** — the same pattern
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
2. **Route both rails in one pass** (`route()`, below): each message is asked for
   a decision on its sender *and* its group. A `news` group is filed to the feed;
   whether the mail is *also* left for triage is the group's `ignored`/`quieted`
   flag, so a read-only newsletter can never buy a model turn while a list one
   answers on still reaches triage.
3. Dedup by message-id against the existing triage status (same sanitized
   id-scheme triage already uses).
4. Keep only whitelisted senders → spawn the model for those.
5. The **daily** job runs for **any** sender (fixed morning hour, before the
   briefing).

### Messenger — gateway-owned store + delivery flag (push)

Signal / WhatsApp / Telegram have **no queryable backlog** — a message exists
only at the instant the gateway receives it. So the messenger backlog is
**synthesized in the life store**, and the gateway owns it.

**Write path — one volume per gateway, not one across all of them.** Each gateway
has its **own** volume; three gateways → three independent volumes, so Signal's
messages never touch WhatsApp's volume. It has **three mounters**:

| Mounter | Mode | Writes | Reads |
|---|---|---|---|
| the gateway | RW | `messages/` (one `.nt` per inbound) | `policy/` (to classify) |
| the retinue container (Ara) | RW | `policy/` (whitelist/blacklist/group-flags `.nt`) | — |
| qlever-life | RO | — | both, to index |

Both writing containers mount RW; **separation is by folder-ownership
convention, not by mount flags** — nothing at the mount level stops a gateway
writing policy, we simply don't, and that convention is what makes each file
single-writer. qlever is a read-only indexer on top. The retinue-side mount lands
under `chambers/_generated/messenger/<channel>/` so qlever-dir picks it up — no
write endpoint in the `retinue` container. (~15 s reindex lag affects only the
SPARQL view; the gateway reads `policy/` raw off disk, so its classify hot path
sees no lag — see below.)

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

**Fast loop vs. daily drain.** On arrival, the gateway classifies on both axes
(`triage_policy.gate_decision`) and acts on the two flags it returns —
`forward` (spend a model turn now) and, when not forwarding, `delivered_if_held`
(the `delivered` flag to persist):

| Class | forward | held `delivered` | drained daily |
|---|---|---|---|
| whitelisted handle | yes (marked delivered) | — | — |
| unknown, normal group | yes, flagged unknown | — | — |
| blacklisted handle | no | `false` | yes |
| unknown, quieted group | no | `false` | yes |
| unknown, ignored group | no | `true` | **no** |

So the `delivered` flag encodes exactly the drain decision: `false` means "held,
the daily drain picks it up" (blacklisted handle, quieted group); `true` means
"accounted for, never drained" (ignored group — the message is on record and
queryable, but no model ever looks at it unprompted).

- The daily catch-all calls each inbox-mode gateway's
  `GET /undelivered?since=…`, processes the returned messages; the flag flips as
  a side effect of the fetch, so a re-run is naturally idempotent. It does **not**
  read undelivered over SPARQL (that wouldn't clear the flag).

**No-action-class messages** (status updates, voice-note echoes, the
daily-briefing self-echo, note-to-self, and unknown senders in an **ignored**
group) carry signal but demand nothing now; they are written with
`delivered: true` already set. History stays complete and queryable; the daily
drain never picks them up; they never prompt.

### The news rail

A group flagged **news** feeds the news page in parallel to triage. The two rails
are decided by the *same* `gate_decision` call — it returns a `news` boolean
alongside the triage flags — but they run independently:

- **Deterministic, credit-free, immediate.** When `news` is set, the gateway
  hands the message to the web-gateway's `POST /internal/news`
  (`scripts/news_ingest.py` → `news_store.add_items`), which shapes it into a feed
  reference with no importance. The Herald scores it on the next curation tick.
  No model turn is spent on the forward itself, and it happens on arrival — not on
  the up-to-a-day triage drain.
- **Cross-container by necessity.** The messenger gateways run in their own
  containers and cannot touch `NEWS_DIR` (the web-gateway owns it), hence the HTTP
  hand-off rather than a direct `news-add.py` call. `NEWS_INGEST_URL` defaults to
  the in-network web-gateway address in the base compose file, so the rail needs no
  deployment configuration; emptying it turns the forward into a no-op.
- **Open by default, lockable.** `POST /internal/news` is the one `/internal/*`
  endpoint that accepts an untokened call. Authenticating it would buy no
  integrity — the rail carries broadcast content written by whoever posts in the
  source channel — while a fail-closed default fails *silently*, since the forward
  swallows a 403 by design. Filing a feed reference is also not an outward action,
  unlike `/internal/conversations` (pushes to the user's devices) and
  `/internal/email` (sends mail), which stay fail-closed. A deployment that wants
  it locked down sets `NEWS_INGEST_TOKEN` on the gateways and the web-gateway.
  It is deliberately *not* `CONVERSATION_BACKEND_TOKEN`: the entrypoint generates
  that one when it is missing, so "unset" would be unreachable.
- **Parallel to the agent path.** A triage turn can still file a one-off item with
  `news-add.py` (an e-mail newsletter met during triage, a linked page). The
  `news` group flag is the *automatic* rail for a whole broadcast source; that
  stays open for the ad-hoc case.

#### The news rail on e-mail

Same idea, same feed, different plumbing — because e-mail is pull. `triage-gate.py`
runs the rail itself (`route()`), inside the retinue container, in the one pass
that opens both modes:

1. Ask `tp.email_gate_decision(sender, list_id)` — one call answers both axes.
2. `read --uid` the message, build the item — **Subject** as the title, a
   `TRIAGE_NEWS_EXCERPT_CHARS`-capped body excerpt as the summary, `email:<addr>`
   as the source id.
3. The link is the newsletter's **own declared** web version: `Archived-At`
   (RFC 5064) or `List-Archive` (RFC 2369), surfaced by `email_client.py read`.
   Nothing is scraped from the body — the first URL in a newsletter is as often a
   tracking pixel as the article, and the feed item's id is keyed off the URL, so
   a wrong link is worse than none.
4. On a successful forward: `flag --read`, then `move` to `TRIAGE_NEWS_FOLDER`
   (default `Archive`, non-destructive — the feed holds a *reference*, so the mail
   is archived, never deleted; set it empty to leave the mail in place).
5. Write the triage **status file** — `disposition: news`, and `resolved` only if
   the move actually happened. Triage's status store, not `\Seen`, is what stops a
   message being re-proposed; a terminal status while the mail is still in the
   INBOX is precisely the drift Phase 1's third pass repairs.

Steps 4 and 5 — marking the mail read, moving it, writing a terminal status —
run only when the decision says triage is **not** owed a look at it, i.e. for a
`news` + `ignored` group. Otherwise (`news` + `quieted`, or a whitelisted sender
on a `news` list) the item is filed and **nothing else is touched**: the mail
stays unread in the INBOX so triage still sees it. Re-filing it on the next
tick is a no-op — a feed item's id is a hash of its content, and the store skips
ids it already holds — so no extra dedup marker is needed.

Failure is always backwards-safe: if the feed rejects the item the mail is left
untouched and falls through to normal triage — even for an `ignored` group — so a
broken rail degrades to "a model turn looks at it", never to a silently swallowed
message.

The two group flags encode **routing and whether personal interaction is
possible** — never signal quality. Whether any single item is worth surfacing is
Herald's per-item judgement, so there is no "noise channel" category. The
canonical combinations: a **feed-only broadcast source** (subscribed to purely as
an information source; nobody there addresses the user personally) is
**news + ignored** (kept in the feed, never bothers triage); a **group channel
where personal interaction is possible** (an unknown sender there may actually be
reaching out) is **news + quieted** (in the feed, and reaching triage on the
daily drain). A source Herald consistently ranks at the bottom is not a channel
flag at all — it should be unsubscribed (or never marked `news`).

The e-mail analogue is exact, with the mailing list in the group's place: a
read-only newsletter (Substack, a press release list) is **news + ignored**; a
list one both reads and posts to is **news + quieted**.

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
  the shared state files, and this doc. The e-mail news rail adds two tunables on
  the `retinue` service: `TRIAGE_NEWS_FOLDER` (default `Archive`; empty leaves the
  mail in the INBOX) and `TRIAGE_NEWS_EXCERPT_CHARS` (default 600). It needs
  `NEWS_INGEST_URL` like the gateways do.
- **gateways:** `signal-gateway` / `whatsapp-gateway` / `telegram-gateway` each
  get the shared volume mounted RW, per-message `.nt` writing, the
  classification gate on inbound, and `GET /undelivered?since=…`. For the news
  rail they also carry `scripts/news_ingest.py` and forward news-flagged messages
  to the web-gateway (guarded by `NEWS_INGEST_URL`).
- **web-gateway:** the `POST /internal/news` endpoint that shapes a forwarded
  message into a feed item via `news_store` — open unless `NEWS_INGEST_TOKEN` is
  set (`_news_ingest_authorized`).
- **compose:** one volume per gateway, each mounted into its gateway (RW) and
  into `chambers/_generated/messenger/<channel>/` (RO for qlever). Each messenger
  gateway gets `NEWS_INGEST_URL` pointed at the web-gateway's `/internal/news`
  by default, plus an optional `NEWS_INGEST_TOKEN` passthrough.

Takes effect on merge → `scripts/self-update.py` (rebuilds the gateway images).

## Open implementation questions (to resolve during build)

1. Exact triple predicates for a message, reconciled with retinue#85.
2. Whether the daily e-mail run and the daily messenger drain are one job or two.
3. Recompute cadence for the derived e-mail whitelist (Sent-folder scan).
