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

- **Sender** — a handle is or is not a **VIP** (messenger only; see *The VIP
  axis* below). There is **no sender whitelist or blacklist on messenger**: see
  *Why messenger has no whitelist* below.
- **Group** — three independent flags: **news** (its messages are also forwarded
  to the news feed / Herald), and at most one of **quieted** or **ignored**.

The **news** flag is orthogonal to everything else: a message can go to the news
feed whether or not it also earns a model turn. And VIP is orthogonal in the
other direction — it is not about attention (every message reaches the chat
surface anyway) but about *handling*: whether a model works the message as it
arrives.

| Class | On arrival | Owed afterwards |
|---|---|---|
| **VIP sender**, any group | the chat shows it, **and** a model turn runs; it pushes unless the group is quieted/ignored | nothing |
| a non-VIP, normal group | the chat shows it and pushes | nothing |
| a non-VIP, **quieted** group | the chat shows it, silently | held `delivered:false` — a recovery sweep can find it |
| a non-VIP, **ignored** group | the chat shows it, silently | nothing, ever |

The group flags suppress the **push**, never a VIP's turn. That is the whole
point of `vip` being sender-only: a quieted group is the user saying *do not
interrupt me about this room*, not *and ignore this person when they write in
it*. A VIP in such a group is worked quietly.

E-mail keeps **both** axes, including its own whitelist, with one
channel-specific twist: a mail's "group" is its **mailing list** (`List-Id`),
and a mail that carries no usable list header is its own group of one, keyed on
the sender address. See *E-mail groups* below.

### Why messenger has no whitelist

The whitelist decided whose message was worth a *session to notify about*. With
the chat surface every arrival is already in front of the user, within seconds,
pushed, for no model turn at all — so the whitelist was answering a question
that no longer gets asked, and whitelisted and unknown came out identical on
every column.

The blacklist said "do not bother me about this person". That is **muting their
chat**, which the user does in the interface, on the chat they are looking at,
rather than through a policy file only Ara can edit. A chat can be **archived**
(out of the list until it speaks again) or **muted** (out of the list, and the
next message does not bring it back; muting archives too) — the same two
gestures a dashboard conversation takes, and Ara can apply either on request as
well. See `webapp/README.md`, *Messenger chats*.

Existing `triageWhitelistHandle` / `triageBlacklistHandle` triples are simply
no longer read: they are inert and drop out on the next policy write. A
blacklist entry that mattered should be re-stated as a **muted chat**.

E-mail is unaffected — there the whitelist still decides something real
(frequent versus daily triage on a pull channel, where nothing arrives on a
screen by itself).

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
  frequent run, anything else waits for the daily sweep (this axis is e-mail's
  alone; messenger retired it, see above);
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
  model turn). On messenger, that still leaves the chat in the dashboard's
  list: whether a chat is *shown* is a separate pair of flags the user sets
  there (Archive / Mute, on the full chats page — `POST /chats/<id>/flags`),
  deliberately not implied by anything here. A group can be a news source and
  still be a chat one reads and answers in, which is what the next line is;
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

### Messenger sender axis: vip

Messenger identity is a **handle**, not a domain — no aliasing problem, so no
wildcards needed there. One flag lives on it:

- **VIP** — a person whose messages are worked by a model on arrival, wherever
  they arrive. Independent of every group flag — see *The VIP axis* below.

The handle is **the person who wrote**, which in a shared chat is the poster and
never the room: a group is matched on the group axis, and only there. Each
gateway must therefore hand the two facts over separately — Telegram keyed both
on the chat id until this was fixed, which made a VIP correspondent writing in a
group look like a handle whose id happened to be the room's. Where a post
genuinely has no individual sender — a broadcast channel — the channel itself is
the only identity there is, and it stands in for one.

### Messenger group axis: news / quieted / ignored

A group carries up to three flags, all set through Ara's policy editor:

- **news** — the group is a broadcast source worth keeping in the news feed. Its
  messages are forwarded to the Herald in addition to (and independently of) any
  triage decision. See *The news rail* below.
- **quieted** — the chat shows the message but nothing else happens: no push, no
  turn. The record stays `delivered: false`, so a recovery sweep can still find
  it.
- **ignored** — the same, and the message is accounted for on arrival: nothing
  is ever owed for it. This is the strong "don't bother me" flag, seeded with
  known no-action groups from day one.

Neither says anything about whether the chat is *in the list* — that is the
user's Archive / Mute, set in the dashboard (`webapp/README.md`).

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

### The unknown-sender ask-flow is gone (messenger)

There used to be one: an unknown handle in a normal group got a model turn
flagged *unknown*, and the model opened a dashboard thread asking whether to
whitelist or blacklist the sender. Nothing asks that any more — the question
belonged to the whitelist, and went with it. A message from a stranger simply
shows up in the chat, like every other message, and the user decides what to do
with the chat.

## State

The VIP flag, the e-mail whitelist and the group flags are **emitted as `.nt`** — the same pattern
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
inbound hot path, in-process. If the gateway resolved the policy over
SPARQL it would inherit the ~15 s reindex lag and a network dependency there —
exactly what this design avoids. So the gateway reads the policy `.nt` **straight
off the mounted volume** (fresh, no lag), while qlever indexes the very same file
for the *query* path. Same file, two readers, different freshness needs, both
satisfied.

**The model is the normal editor of this state, not the user with a text
editor.** Every change flows through Ara: instructions like "trust everyone at
`*@epfl.ch`", "block that group" or "Mara's messages should be handled the
moment they arrive" are conversational — Ara emits the wildcard, the group id or
the VIP handle and confirms. The files stay plain, readable `.nt`, so they *can*
be corrected by hand, but that is a fallback. Ara also reads them (over SPARQL)
to answer "who is a VIP?". The one thing that is **not** hers alone is where a
chat sits on the user's screen: Archive and Mute are buttons in the dashboard,
and Ara can press them on request but is never in the way.

## The two channels need the gate in different places

E-mail is **pull**; messenger is **push**. The policy is shared; the plumbing is
not.

### E-mail — scheduler-gated poller (pull)

IMAP has a queryable backlog. The gate is a scheduler `command` job (zero Claude
credits):

1. List **the INBOX** — all of it, not the unread subset. `unread` is a mailbox
   flag the user can flip from any mail client; the status store (step 4) is
   what decides whether a message is handled. "All of it" is literal: the
   first listing is a small newest-first window, widened once when it
   saturates, and a mailbox larger than the wide window is walked to the end
   in pages by UID cursor (`search --uid-max`), so the oldest mail — the mail
   the oldest-first slice wants — is never out of view behind a cap. A page
   is one bulk header FETCH, not one per message, and whether it was full
   is read off what the server matched (`scanned`), not off how many
   summaries came back, so the walk is cheap and an unreadable header cannot
   end it early.
2. **Settle what has already been answered.** Each message whose thread subject
   appears in a Sent listing with a later date is *nominated*, then confirmed
   exactly by `email_client answered` — a server-side IMAP SEARCH for replies
   citing its Message-ID, filtered back down to the same correspondent, the
   same base subject and a strictly later timestamp; untracked replies go
   through the same filter after an exact recipient search. The Sent listing
   is bounded by **date**, not by count — `--since` the day before the oldest
   INBOX message, because nothing sent earlier can answer anything still
   open — so it is complete by construction and small in proportion to the
   backlog's age. A residual cap guards against one very old open mail
   dragging in years of Sent; when it bites the listing is incomplete, so the
   gate nominates from what it listed, says so, and a reply older than the
   window is not settled that tick — the mail stays in the INBOX and is
   proposed, where the skill's own already-answered check sees it. The exact
   checks themselves are bounded per run (oldest mail first; the rest wait
   for the next run), and a negative is remembered under the Sent state it
   was checked against (`.answered-checks.json` in the status dir), so the
   cap is spent on candidates not yet checked rather than on the same ones
   every tick. It never
   widens to an exact check of the whole INBOX, which would put unbounded
   work in front of the bounded slice. (No date read off a capped listing is
   a safe boundary either: the cap keeps the newest UIDs, and UID order need
   not be date order.)
   A confirmed one is moved to `TRIAGE_ANSWERED_FOLDER` and recorded
   `resolved`, so it never reaches a proposal again. Only the exact check ever
   archives, and the action is a move, never a delete.
3. **Route both rails in one pass** (`route()`, below): each message is asked for
   a decision on its sender *and* its group. A `news` group is filed to the feed;
   whether the mail is *also* left for triage is the group's `ignored`/`quieted`
   flag, so a read-only newsletter can never buy a model turn while a list one
   answers on still reaches triage.
4. Dedup by message-id against the existing triage status (same sanitized
   id-scheme triage already uses). A message that already has a status record
   is **not new work** and does not arm the gate — whatever its status. Triage
   never marks mail read (`unread ≠ unhandled`), so a classified message stays
   unread in the INBOX until its disposition is executed, which for an omnibus
   batch means waiting on the user; without this step every tick would re-spawn
   a session over the same settled stack. Two exceptions, both because the gate is the *only*
   thing that spawns a triage session — a state nothing else revisits is a
   state nothing else can ever finish:
   - **Stalled** (`_stalled`): a record on a non-terminal status untouched for
     `TRIAGE_STALL_DAYS` (default 7) is abandoned rather than in progress, and
     re-arming it is the only way its mail ever leaves the INBOX.
   - **A due omnibus digest** (`omnibus_due`): the skill accrues
     archive/delete candidates on `omnibus_pending` and sends one digest per
     `EMAIL_PROCESSING_INTERVAL` — that accrual is what keeps the user from
     being pinged several times a day. Since accrued mail sits on an open
     status, the gate arms on the *bundle* instead: it reads
     `omnibus_pending` records off the INBOX listing (not by walking the
     status store — on a 30-minute tick that is not free) and, when the
     interval since `.last-omnibus` has elapsed, spawns a run told to send the
     digest. A missing or unparseable marker counts as due: the cost is one
     digest, the alternative is bundled mail nobody sees. Due-ness is read
     across the whole INBOX listing rather than the whitelisted subset, since
     a bundle accrued by a daily run can hold mail the frequent pass does not
     whitelist.
5. Keep only whitelisted senders → spawn the model for those.
6. The **daily** job runs for **any** sender (fixed morning hour, before the
   briefing).
7. **Hand over a bounded slice, oldest first.** The spawn payload is the
   oldest `TRIAGE_BATCH_SIZE` (default 25) of the messages that armed the run
   — never-seen and stalled ones — with each message's UID, so the session
   can read, flag and move it without a listing of its own; recorded mail is
   not handed over. A due omnibus bundle rides along as a *count*, not a
   listing: the digest is composed from the status store and is one unit of
   work whatever its size, so listing it would only unbound the prompt. The
   prompt lists the slice in full and says it is the whole scope: the
   session does not enumerate the INBOX for
   more, records each message the moment its disposition is settled, and runs
   the whole-picture passes (Phase 1's store→INBOX, done-but-still-there and
   stalled repairs, Phase 5's reminders) only on the run told it drains the
   backlog. When more is left, the gate exits **75** and the scheduler records
   a `partial` run, so a job with `resume_after_seconds` comes back for the
   next slice after minutes rather than after its interval
   (`docs/scheduling.md`). The point is that the model's progress is durable
   only per message — its status record — so a run that takes a slice it can
   finish, and records it, beats one that takes the whole backlog and is
   killed at its budget with nothing recorded. That was the failure mode: a
   sweep that never finished never reduced its own backlog, so every run had
   more to do than the last.

### Messenger — gateway-owned store + delivery flag (push)

On messenger the gateway **cannot ask for a message twice**. Each transport
hands one over exactly once, when the client is connected, and will not hand it
over again — so from that moment the gateway's own record is the only copy.
There is nothing to query later the way IMAP is queried. So the messenger
backlog is **synthesized in the life store**, and the gateway owns it.

That is not the same as losing what arrives while the system is down. All three
transports queue for an offline client and deliver the queue on reconnect:
signal-cli's `receive` drains it, Telethon fetches the updates it missed and
replays them as ordinary new-message events, and neonize does the same. The
gateway still only ever sees a message as it arrives — it just arrives later
than it was sent, and the gate decides on it then.

The three differ in what they *could* offer, which is worth stating plainly
because the constraint above is a choice for one of them:

| Channel | History available to this client |
|---|---|
| Signal | none — signal-cli keeps no queryable message history |
| WhatsApp | none exposed to neonize |
| Telegram | **yes** — Telethon can read it; this gateway deliberately does not |

The Telegram gateway reads `iter_dialogs` for the conversation list and never
reads messages that way. Nothing below depends on that staying true: the
synthesized backlog is what the gate and the daily drain work from on every
channel, so a future history read would be an extra source, not a replacement.

**Write path — one volume per gateway, not one across all of them.** Each gateway
has its **own** volume; three gateways → three independent volumes, so Signal's
messages never touch WhatsApp's volume. It has **three mounters**:

| Mounter | Mode | Writes | Reads |
|---|---|---|---|
| the gateway | RW | `messages/` (one `.nt` per inbound) | `policy/` (to classify) |
| the retinue container (Ara) | RW | `policy/` (vip + group-flags `.nt`) | — |
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
  operation that mutates the flag. Each returned message also carries a
  `reply_token` for its origin conversation (minted at drain time; tokens are
  stateless), so a reply proposed from the drain is addressed by token exactly
  like one proposed from a live forward — never by resolving the sender's name.
- Marking delivered = rewriting the message's one small `.nt` file → one reindex.
  One-file-per-message keeps that flip cheap.
- **A SPARQL query never touches the flag.** Reading the messages over SPARQL is
  a pure read of the store replica — so the user (via Ara) can browse full
  messenger history ("what did X say last week?") without draining the
  undelivered queue.

This is the IMAP analogy, renamed: "fetch unseen → mark `\Seen`" ≡ "fetch
undelivered → mark delivered." Both are stateful fetches owned by the message
store; a read-only query of either changes nothing.

**What the classes decide now.** On arrival the gateway classifies on both
axes (`triage_policy.gate_decision`), and on the normal path the verdict has
one job left: whether the arrival is worth interrupting the user for. Every
message is then offered to the chats rail, accepted, and recorded delivered —
see *The VIP axis* below for what does buy a model turn.

| Class | notifies? | what happens |
|---|---|---|
| any sender, normal group | yes | accepted by the chat; delivered |
| quieted group | no | accepted silently; delivered |
| ignored group | no | accepted silently; delivered |

A **VIP** in any of these rows also gets a turn — that is the other axis, and
it reads `vip`, not this column.

A muted chat does not push either — but that is the chat state's doing, on the
web-gateway side, not the gate's.

The `delivered_if_held` flag the gate also returns belongs to the **fallback**
path, where the rail refused and the old behaviour runs whole: `false` means
"held, a recovery sweep picks it up" (quieted group); `true` means "accounted
for, never drained" (ignored group). On the normal path nothing reads it,
because the chat's acceptance is what sets the flag.

### The VIP axis, and what became of `delivered` on messenger

Since `docs/messenger-chats.md` phase 4, **every** inbound messenger message —
held classes included — is handed to the chats rail
(`POST /internal/chats/inbound`), which **accepts** it: the chat has the
message, the user is pushed unless the gate says to stay quiet, and the gateway
marks it `delivered`. That is the delivery, and it costs no model turn.

Two consequences, both deliberate:

- **The messenger whitelist and blacklist are retired**, and with them the
  unknown-sender ask-flow. The whitelist existed to decide whose message was
  worth a session to *notify* about, and notification is free now; the
  blacklist said "do not bother me about this person", which is muting their
  chat — a button, in front of the user. See *Why messenger has no whitelist*
  above. All that is left on the sender axis is **VIP**.
- **Nothing is left undelivered on the normal path, so there is no daily
  drain on messenger.** `delivered` used to mean "a model turn accounted for
  this"; it now means "the user has this", which the chat surface makes true
  within seconds, for every message. What still leaves `delivered=false` is
  the handful of cases where the rail did *not* take the message — it refused,
  its answer was lost in flight, or a VIP's turn failed — and the gateway's
  `GET /undelivered` is how those are recovered, into triage as before. The
  endpoint stays and is still load-bearing; what is gone is the standing
  backlog it used to sweep.

What buys a **model turn** is the new sender-level **`vip`** flag
(`triageVipHandle`): a person whose messages the user wants dealt with the
moment they arrive. It is **sender-only and group-independent** — a VIP writing
in a room of forty is still the person the user wanted to hear from — and
independent of every other flag here, which govern attention rather than
handling. For a VIP the rail also starts a turn in that chat's companion thread
and answers `202` with its job handle, which the gateway waits on before
flipping `delivered`; that turn reads the message, files what it changes (a
project, a memory), and stages a reply where one is wanted.

```bash
python3 scripts/triage_policy.py vip-add --channel signal --handle +41791234567
python3 scripts/triage_policy.py vip-remove --channel signal --handle +41791234567
python3 scripts/triage_policy.py show --channel signal      # …and a vip line
```

Like every other entry here, this is normally edited by talking to Ara.

A rail that cannot take the message answers no acceptance and no handle, and
the gateway falls back to everything it did before the chat surface existed —
the held classes' `delivered_if_held` and the triage forward. **Triage itself
is unchanged**, and still owns the e-mail channel and anything the rail hands
back.

- The daily catch-all calls each inbox-mode gateway's
  `GET /undelivered?since=…`, processes the returned messages; the flag flips as
  a side effect of the fetch, so a re-run is naturally idempotent. It does **not**
  read undelivered over SPARQL (that wouldn't clear the flag).

**Forwarded ≠ delivered.** A forward POSTs the message with `async: true`, and
the retinue gateway answers **202 Accepted** with a `job_url` — acceptance, not
completion. So the flip waits for that job: `job_delivery.confirm_delivery`
polls `GET <job_url>` on a daemon thread and calls `mark_delivered` **only** on
`status: "done"`. `status: "error"`, a 404 (the in-memory job record expired
before the turn finished), and the poll deadline all leave `delivered: false`,
so the daily drain re-surfaces the message. Without this, a triage turn that
died — an upstream model outage, a crashed session — left the message on record
as delivered and nothing ever looked at it again: a silent loss, exactly what
the never-drop invariant exists to prevent. A gateway that answers
synchronously (no `job_url`) is marked delivered at once, since the turn has
already run by then.

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
  `NEWS_INGEST_URL` like the gateways do. The sweep adds three more:
  `TRIAGE_BATCH_SIZE` (default 25; how many never-seen or stalled messages one
  run hands the model, oldest first — non-positive means as many as the
  prompt lists, `TRIAGE_PROMPT_LIST_LIMIT`), `TRIAGE_SENT_RECONCILE` (default
  `1`; `0` switches off the credit-free archiving of already-answered mail,
  the one thing the gate does to the mailbox on its own) and
  `TRIAGE_ANSWERED_FOLDER` (where answered mail goes; defaults to
  `TRIAGE_NEWS_FOLDER`, so a deployment names its archive once). The scan
  scope has no knob: the gate scans the INBOX, since `unread` is a mailbox
  flag and the status store is what decides handled-state.
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
