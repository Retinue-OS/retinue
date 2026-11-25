# Triage delivery gate — spend model credits only on senders that matter

## Problem

Every model turn on an inbound message is a fresh `claude -p` session that
reloads the whole static prefix (harness + tool schemas + `CLAUDE.md`) before
it does any work, and re-reads that prefix on every tool round-trip. So cost
scales as `prefix × round-trips × sessions`, and the dominant term is
**sessions**. Before the gate, every inbound messenger message spawned a
session at its gateway and an e-mail triage job polled the mailbox every half
hour — the large majority of them finding nothing that needed a model at all.

The fix is a **credit-free gate** in front of the spawn, mirroring
`agent-self-review`: the gateway that receives a message decides — from a
policy file, in-process, for free — what the message is owed, and a model
turn only starts on a hit. Nothing is lost, because delivery no longer depends
on a turn: every message is persisted first and shown in its chat.

The gate is the same on every channel, **e-mail included**: persist, classify
on two axes, notify the chat rail, and run a model turn only for a sender the
user singled out. This document describes the policy and the messenger
plumbing; what is particular to mail is summarised under *The e-mail axes*
and *Where the gate runs* below and specified in [mail-chats.md](mail-chats.md).

This affects **inbox-mode accounts only** (accounts whose incoming
mail/messages the system handles on the user's behalf). Control-mode and
outbound identities are untouched.

## Policy — two orthogonal axes (messenger)

Messenger routing is decided on **two independent axes**, so the classes below
are a *combination* of a sender status and a group's flags, not a single ladder:

- **Sender** — a handle is or is not a **VIP** (see *The VIP axis* below), and
  may carry the user's instruction. There is **no sender whitelist or
  blacklist**, on any channel: see *Why messenger has no whitelist* below.
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
| **instructed sender** (not a VIP), any group | the chat shows it silently, **and** a model turn runs by the user's instruction; the turn's verdict decides the push (a failed or slow turn pushes), unless the group is quieted/ignored | nothing |
| a non-VIP, normal group | the chat shows it and pushes | nothing |
| a non-VIP, **quieted** group | the chat shows it, silently | held `delivered:false` — a recovery sweep can find it |
| a non-VIP, **ignored** group | the chat shows it, silently | nothing, ever |

The group flags suppress the **push**, never a VIP's turn. That is the whole
point of `vip` being sender-only: a quieted group is the user saying *do not
interrupt me about this room*, not *and ignore this person when they write in
it*. A VIP in such a group is worked quietly.

E-mail is routed on the **same two axes**, with one channel-specific twist on
each: a mail's "group" is its **mailing list** (`List-Id`), and a mail that
carries no usable list header is its own group of one, keyed on the sender
address; and a sender may be matched by **domain** as well as by exact
address. See *The e-mail axes* below.

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

E-mail had a whitelist of its own for longer, because it was a pull channel:
nothing arrived on a screen by itself, so the whitelist decided which mail a
triage run looked at within the half hour and which waited for the daily one.
With the mail gateway a mail arrives in its chat like any message, and that
whitelist went the same way (see *The e-mail axes* below, and
[mail-chats.md](mail-chats.md), "What retires").

### The e-mail axes — domains for senders, lists for groups

Mail is gated like a messenger message, by the mail gateway, on arrival
([mail-chats.md](mail-chats.md), "The gate"). Only what differs from the
messengers is stated here.

**The sender axis: VIP, instructed, or neither.** A sender is a VIP or
instructed because a **contact** says so: `contacts.sync_policy` projects the
e-mail addresses of every VIP person and of every person with an instruction
into the `email` channel's policy file, in the messenger shape
(`triageVipHandle` / `triageInstructedHandle` under
`urn:retinue:triage:email:contacts`). One thing exists only here: a contact
may carry a **domain** (`kb:emailDomain`, `contacts.py … --add-domain`), which
is projected as a wildcard handle — `*@example.org` for every address at that
domain, `*@*.example.org` for the domain and everything below it — and
`gate_decision("email", sender)` matches an exact address first, then the
wildcards. That is how an organisation that writes from many addresses gets
one instruction. On every other channel a handle is matched exactly and such
a string is a handle nobody has.

A domain is never added by the system: sending a mail to `person@gmail.com`
makes nobody a VIP, and a freemail domain is covered only if the user files it
on a contact themselves. Glob support is deliberately minimal — `*@domain` and
`*@*.domain`, no regex.

| Sender | On arrival | A model turn |
|---|---|---|
| **VIP** | the chat shows the mail and it rings | yes, in the chat's companion |
| **instructed** (not a VIP) | the chat shows it silently; the turn's verdict decides the ring (a failed or slow turn rings) | yes, briefed by the instruction |
| anyone else | the chat shows it under *Unread*; **nothing is pushed** | no |

The last row is where mail differs from a messenger, on purpose: an arriving
mail **pushes nothing by default**, and a contact can say otherwise — the VIP
flag, an instruction, or the person's importance prior and permits in the
attention model. There is no sweep for the rest: mail from a sender who is
neither VIP nor instructed is listed and judged by nobody.

**The group axis: the mailing list.** A mailing list is not a sender. The
people who post to a list change; the list does not, and it is the list one
has an opinion about ("read-only", "I answer on this one"). So a mail's group
is its list, and the group carries the same `news` / `quieted` / `ignored`
flags as a messenger group.

The group id is the message's `List-Id` (RFC 2919), normalised: bracketed part,
lowercased, and rejected unless it looks like a domain — no whitespace, at least
one dot, under 200 characters. Real headers are messier than the RFC (opaque
hashes, and values whose bracketed part is a display name, e.g.
`799706515 <Brack News>`), and inventing a group nobody can flag is worse than
having none. A mail with no usable `List-Id` falls back to **its own sender
address as its group** — a newsletter that carries no list header is a group of
one, so the three flags apply to it with no second mechanism.

| Group flag | What it does to a mail |
|---|---|
| `news` | filed into the news feed for the Herald; the mail **starts no chat** (it joins one only when it continues a thread that is already mirrored, or when its sender is a VIP or instructed) |
| `quieted` | the chat shows the mail silently |
| `ignored` | the same; and where the group is also `news`, the mail is consumed once the feed has it — marked seen and moved out of the INBOX |
| none | as the sender axis says |

`quieted` and `ignored` are mutually exclusive, as on messenger; `news`
combines with either. The canonical pairs:

- a read-only newsletter → **`news` + `ignored`** (in the feed, out of the
  INBOX, never a chat);
- a list one both reads and answers on → **`news` + `quieted`** (in the feed,
  and the mail stays where it arrived).

The two axes stay **independent**: no group flag stops a VIP's or an
instructed sender's turn — a `news` group included: their mail is filed, and
is also in its chat, offered and worked, and never taken out of the INBOX by
the news rail — and declaring a group `news` makes nobody a VIP.

Group entries take two shapes — an exact id, or a `*@domain` wildcard. On a
group the wildcard also covers ids **beneath** the domain, because a `List-Id`
is a namespace a platform hands out per publication: `*@substack.com`
therefore matches both `no-reply@substack.com` and the list
`sgcarney.substack.com`. It does **not** match a mailbox at a subdomain
(`someone@sgcarney.substack.com`) — that stays the strict `*@*.domain` reading
the sender axis relies on, so loosening the group axis never widens who a
contact's domain covers.

```bash
python3 scripts/triage_policy.py email-news-add   '*@substack.com'
python3 scripts/triage_policy.py email-ignore-add '*@substack.com'
python3 scripts/triage_policy.py email-quiet-add  members.list.example.org
python3 scripts/triage_policy.py show-email            # the group flags: news / quiet / ignore
python3 scripts/triage_policy.py show --channel email  # the sender classes: vip / instructed
python3 scripts/triage_policy.py check-email stranger@x.com \
    --list-id '<members.list.example.org>'
# group      members.list.example.org
# news       no
# attention  quieted
# sender     neither
```

`check-email` exits 0 for a sender whose mail buys a turn (VIP or instructed)
and 3 otherwise, as `check-handle` does.

The group flags live in a file of their own
(`…/messenger/email/policy/email-policy.nt`), beside the channel's `policy.nt`
that holds the sender classes; `save_email_policy()` is its only supported
writer. Like the messenger flags, they are edited by talking to Ara
("newsletter X belongs in the feed"); the CLI is what she runs. A sender is
never set there: who is a VIP or instructed is said on the contact.

Address-level `news` entries written before list detection existed keep working:
news is matched against the group **and** the bare sender address, so a
newsletter that turns out to carry a `List-Id` is not silently un-filed.

### Messenger sender axis: vip

Messenger identity is a **handle**, not a domain — no aliasing problem, so no
wildcards needed there. Two flags live on it:

- **VIP** — a person whose messages are worked by a model on arrival, wherever
  they arrive. Independent of every group flag — see *The VIP axis* below.
- **instructed** — a person the user wrote a standing instruction for
  (`kb:instruction`, [contacts.md](contacts.md#the-instruction)): each message
  of theirs is worked by one model turn with that instruction as its brief,
  and for a sender who is not also a VIP that turn's verdict — an importance,
  a deadline — is what decides, through the attention model, whether the
  message rings. Projected only from the address book (`triageInstructedHandle`
  under the `…:contacts` subject); there is nothing to set by hand, since the
  words live with the person. Independent of VIP: a handle carrying both rings
  at once and is worked as instructed in the same single turn.

The handle is **the person who wrote**, which in a shared chat is the poster and
never the room: a group is matched on the group axis, and only there. Each
gateway must therefore hand the two facts over separately — Telegram keyed both
on the chat id until this was fixed, which made a VIP correspondent writing in a
group look like a handle whose id happened to be the room's. Where a post
genuinely has no individual sender — a broadcast channel — the channel itself is
the only identity there is, and it stands in for one.

The VIP flag also rides on the **chats rail** (`chat_ingest.py`: `gate.vip`),
where the attention model reads it next to what the dashboard itself knows
about the sender. A VIP always rings — unless their chat is muted or the group
is quieted/ignored — whatever mode is on. A direct message from anyone else is
ranked by what is known about them: a **contact card** (name, sphere, further
groups) or a sphere the user has taught the profile. A sender the dashboard
knows nothing about is *screened* — listed and carried by the next digest,
never rung — until the contact card says who it is. This replaces the old
`gate.unknown` flag, which went with the whitelist; see
`docs/attention-model.md`.

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

The VIP and instructed handles and the group flags are **emitted as `.nt`** — the same pattern
as the existing `_generated` registries (`agents.nt`, `conversation-models.nt`).
That choice does three jobs at once: it indexes natively in qlever (no
converter), it is trivial for a gateway to parse off disk, and it retires any
separate per-app JSON.

**The retinue side writes the policy files; the gateways are readers** — the
reverse direction of the message files. On the retinue side there are two
writers: Ara (`triage_policy.py`) and the web gateway's projection of the
address book's VIP and instructed persons; every read-modify-write holds
the file's `policy_lock`, so neither drops the other's change. The messenger policy rides on the **same
per-gateway volume as that channel's messages** (see the volume topology below),
because the gateway must read it at classify time — see the next point. The
e-mail policy rides on the mail gateway's volume the same way, as two files
in its `policy/` folder: `policy.nt`, the channel's sender classes in the
messenger shape, and `email-policy.nt`, the e-mail group flags
(`triage_policy.email_whitelist_path()`; `TRIAGE_EMAIL_WHITELIST_PATH`
overrides it — both keep the name of the whitelist the file once also held).
The flags' file used to live at `_generated/triage/email-whitelist.nt`, and
`triage_policy.ensure_email_policy_location()` moves it from there on the
retinue side — first thing at container start, since the mail gateway's mirror
waits for the file (docs/messaging.md, "E-mail (the mail gateway)"). Triples
of the retired whitelist that a file still holds (`triageWhitelistAddress`,
`triageWhitelistWildcard`, the `…email-whitelist:contacts` subject) are not
read and drop out on the file's next write; `scripts/migrate-triage.py` moves
what they held onto contacts.

**Why the gateway reads a raw file, never SPARQL.** Classification happens on the
inbound hot path, in-process. If the gateway resolved the policy over
SPARQL it would inherit the ~15 s reindex lag and a network dependency there —
exactly what this design avoids. So the gateway reads the policy `.nt` **straight
off the mounted volume** (fresh, no lag), while qlever indexes the very same file
for the *query* path. Same file, two readers, different freshness needs, both
satisfied.

**The model is the normal editor of this state, not the user with a text
editor.** Every change flows through Ara: instructions like "work everything
from `epfl.ch` as I told you", "block that group" or "Mara's messages should
be handled the moment they arrive" are conversational — Ara files the domain
on a contact, sets the group flag or the VIP flag, and confirms. The files stay plain, readable `.nt`, so they *can*
be corrected by hand, but that is a fallback. Ara also reads them (over SPARQL)
to answer "who is a VIP?".

**A VIP is a person.** When the correspondent is in the address book
([contacts.md](contacts.md)), the flag belongs on the person (`contacts.py
update <id> --vip`, the VIP switch on the contact card or the Contacts page),
not on one handle: `contacts.sync_policy` projects every handle of every VIP
person into these files as `triageVipHandle` — messenger accounts, and on the
`email` channel their addresses and their domains as wildcards — under a
subject of their own (`urn:retinue:triage:<channel>:contacts`). The
projection is replaced wholesale on every change and on the gateway's tick;
hand-set VIPs are never touched by it. Readers
need no change: the loaders read the predicate and ignore the subject.
`triage_policy.py vip-add` remains for a handle nobody has filed. The same
projection writes every handle of every person with an instruction
as `triageInstructedHandle` (the `instructed` class); `show` lists them and
`check-handle` says `instructed` on a second line, and exits 0 for it, as for
a VIP.

The one thing that is **not** hers alone is where a
chat sits on the user's screen: Archive and Mute are buttons in the dashboard,
and Ara can press them on request but is never in the way.

## Where the gate runs

The policy is shared; the plumbing is each gateway's own. A messenger
transport hands a message over exactly once, so its gateway *is* the backlog
(next section). A mailbox can be asked again, so the mail gateway mirrors it
and the mailbox stays the backlog.

### E-mail — the mail gateway

For every new mail in the INBOX that is not backfill, the mail gateway runs
the messengers' inbound flow ([messenger-chats.md](messenger-chats.md),
*Inbound flow*; the contract is [mail-chats.md](mail-chats.md), "The gate"):

1. **Persist** the record, `delivered: false`.
2. **Gate**: the group axis by `triage_policy.email_gate_decision(sender,
   list_id)` — which answers `{group, news, quieted, ignored}` and nothing
   else — and the sender axis by `triage_policy.gate_decision("email", sender,
   group)`, read for `vip` and `instructed`. The sender on both is the
   **gate sender**: the one address of a mail with exactly one `From` header
   holding exactly one address, lowercased by its ASCII letters (none where a
   character folds into ASCII) — never the `Sender` header, never the first
   of several addresses; a mail without one is from nobody and buys no turn
   ([mail-chats.md](mail-chats.md), "Who the sender is"). An unreadable
   policy fails **open** on the group axis (no flags) and **closed** on the
   sender axis: nobody is a VIP or instructed, because a mail must not buy a
   model turn for want of a readable file.
3. **News**: a `news` group is filed to the feed (*The news rail on e-mail*
   below) and starts no chat — unless the sender is a VIP or instructed, whose
   mail goes on to the next step as well.
4. **Notify the chat rail** with a handover. The rail's acceptance marks the
   record delivered; for a VIP or an instructed sender it also starts the
   turn in the chat's companion and the gateway waits on that job, exactly as
   a messenger gateway does. Attachments ride along only for those two.

Three things differ from a messenger, all deliberate:

- **A mail pushes nothing by default** (the table under *The e-mail axes*
  above).
- **There is no triage fallback for mail.** A mail the rail could not take —
  no answer, an uncertain answer, no acceptance — stays `delivered: false`
  and the gateway offers it again on later polls. No session is spawned for
  it, and the mail gateway has no `GET /undelivered`.
- **No daily sweep.** Nothing reads the mail of senders who are neither VIP
  nor instructed.

E-mail triage — the scheduler-gated poller that used to stand here, its
frequent and daily runs, the whitelist, the omnibus, the status store and the
already-answered check — is retired. `scripts/triage-gate.py` is a stub that
prints a notice and exits 0, for the jobs chambers may still declare; what
triage knew about a sender was moved into instructions on contacts by
`scripts/migrate-triage.py` ([mail-chats.md](mail-chats.md), "What retires",
"The migration").

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
synthesized backlog is what the gate and the recovery drain work from on every
messenger channel, so a future history read would be an extra source, not a replacement.

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

The **`instructed`** flag buys the same turn, and the rail answers it the same
way (`202` + `job_url`, the gateway waiting on the job before it flips
`delivered`). What differs is the push: for an instructed sender who is not
also a VIP the arrival pushes nothing, the turn runs with the user's
instruction as its brief and ends with a verdict
(`POST /internal/chats/<id>/verdict`), and the web gateway then puts the
message through the attention model with that importance and deadline. A turn
that fails, sets no verdict or runs past `CHAT_INSTRUCTION_TIMEOUT` has the
message pushed as a VIP's would be. (A mail's ring waits
`MAIL_INSTRUCTION_TIMEOUT` instead, 600 s, and a mail gets at most
`MAIL_ARRIVAL_TURN_ATTEMPTS` turns: [mail-chats.md](mail-chats.md), "The
gate".) The gate verdict carries the flag to the
rail as `gate.instructed`, and the message's attachments ride along as they do
for a VIP. On the fallback path — the rail declining — an instructed sender is
treated as a VIP is: never held, forwarded to triage.

A rail that cannot take the message answers no acceptance and no handle, and
the gateway falls back to everything it did before the chat surface existed —
the held classes' `delivered_if_held` and the triage forward. **Triage is
that fallback and nothing else** (`.claude/skills/triage/SKILL.md`): what a
messenger rail hands back. It does not own the e-mail channel any more.

- A recovery run calls each inbox-mode messenger gateway's
  `GET /undelivered?since=…` and processes the returned messages; the flag flips as
  a side effect of the fetch, so a re-run is naturally idempotent. It does **not**
  read undelivered over SPARQL (that wouldn't clear the flag). Nothing in the
  framework schedules it: it runs when the user asks or a chamber declares a
  job for it.

**Forwarded ≠ delivered.** A forward POSTs the message with `async: true`, and
the retinue gateway answers **202 Accepted** with a `job_url` — acceptance, not
completion. So the flip waits for that job: `job_delivery.confirm_delivery`
polls `GET <job_url>` on a daemon thread and calls `mark_delivered` **only** on
`status: "done"`. `status: "error"`, a 404 (the in-memory job record expired
before the turn finished), and the poll deadline all leave `delivered: false`,
so the recovery drain re-surfaces the message. Without this, a triage turn that
died — an upstream model outage, a crashed session — left the message on record
as delivered and nothing ever looked at it again: a silent loss, exactly what
the never-drop invariant exists to prevent. A gateway that answers
synchronously (no `job_url`) is marked delivered at once, since the turn has
already run by then.

**No-action-class messages** (status updates, voice-note echoes, the
daily-briefing self-echo, note-to-self, and unknown senders in an **ignored**
group) carry signal but demand nothing now; they are written with
`delivered: true` already set. History stays complete and queryable; the
recovery drain never picks them up; they never prompt.

### The news rail

A group flagged **news** feeds the news page in parallel to triage. The two rails
are decided by the *same* `gate_decision` call — it returns a `news` boolean
alongside the triage flags — but they run independently:

- **Deterministic, credit-free, immediate.** When `news` is set, the gateway
  hands the message to the web-gateway's `POST /internal/news`
  (`scripts/news_ingest.py` → `news_store.add_items`), which shapes it into a feed
  reference with no importance. The Herald scores it on the next curation tick.
  No model turn is spent on the forward itself, and it happens on arrival.
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
- **Parallel to the agent path.** An agent turn can still file a one-off item with
  `news-add.py` (a newsletter met while working a chat, a linked page). The
  `news` group flag is the *automatic* rail for a whole broadcast source; that
  stays open for the ad-hoc case.

#### The news rail on e-mail

Same idea, same feed, and since the gate phase the same place: the gateway.
The mail gateway runs the rail itself at ingest, for a mail whose group — or
whose bare sender address — is `news`:

1. Build the item — **Subject** as the title, a `MAIL_NEWS_EXCERPT_CHARS`-capped
   excerpt of the mail's text as the summary, `email:<addr>` as the source id —
   and hand it to `news_ingest.forward_news(channel="email", …)`, like a
   messenger gateway (`NEWS_INGEST_URL`, optional `NEWS_INGEST_TOKEN`).
2. The link is the newsletter's **own declared** web version: its
   `Archived-At` (RFC 5064), which names this one mail. Not `List-Archive`
   (RFC 2369): that names the list's archive, the same on every issue, and the
   feed item's id is keyed off the URL — every issue after the first would be
   taken for the first and dropped. Nothing is scraped from the body either —
   the first URL in a newsletter is as often a tracking pixel as the article,
   so a wrong link is worse than none. A mail without `Archived-At` is filed
   with no link, its item keyed on its text.
3. The mail starts no chat. It joins one only when its `References` reach a
   thread that is already mirrored — normally because the user wrote in it —
   or when its sender is a VIP or instructed, and is then in the feed *and*
   in that chat. A news mail the reader could not read is neither filed nor a
   chat; it is read again with the placeholders, and filed once it reads.
4. Where the group is also `ignored` and the feed took the item, the mail is
   consumed: `\Seen`, and a move to `MAIL_NEWS_FOLDER` (default: the archive
   folder; empty leaves it in the INBOX; the retired `TRIAGE_NEWS_FOLDER` is
   read where it is not set). Only a mail in no chat is consumed. The feed
   holds a *reference*, so the
   mail is archived, never deleted. A `news` + `quieted` mail is filed and
   otherwise left where it arrived.

Failure is backwards-safe: a feed that refuses the item leaves the mail
untouched and it is offered again on a later poll. Re-filing is a no-op — a
feed item's id is a hash of its content, and the store skips ids it already
holds — so no dedup marker is needed.

The two group flags encode **routing and whether personal interaction is
possible** — never signal quality. Whether any single item is worth surfacing is
Herald's per-item judgement, so there is no "noise channel" category. The
canonical combinations: a **feed-only broadcast source** (subscribed to purely as
an information source; nobody there addresses the user personally) is
**news + ignored** (kept in the feed, never bothers triage); a **group channel
where personal interaction is possible** (an unknown sender there may actually be
reaching out) is **news + quieted** (in the feed, shown silently in its chat,
and — when the rail could not take it — held for the recovery drain). A source Herald consistently ranks at the bottom is not a channel
flag at all — it should be unsubscribed (or never marked `news`).

The e-mail analogue is exact, with the mailing list in the group's place: a
read-only newsletter (Substack, a press release list) is **news + ignored**; a
list one both reads and posts to is **news + quieted** (see *The e-mail axes*
above).

**Schema.** Align the per-message triple shape with the session-logging
unification (retinue#85) rather than inventing a parallel vocabulary — one RDF
message log, with `delivered` as an additional property.

## Why the ~15 s SPARQL lag is harmless

There is a gap between a message arriving and it being queryable (qlever-dir's
rebuild). It costs nothing, because the gateway's inbound handler already **has
the message in-process** — it never queries the store to classify or to spawn.
The only SPARQL consumers are the user's ad-hoc history questions, which are far
past 15 s. And the recovery drain reaches the backlog through the gateway endpoint,
not through SPARQL, so it is never subject to the lag either.

## Rollout / tiers

Tier-3 across both the framework and the gateway services:

- **framework:** the policy library (`scripts/triage_policy.py`), the address
  book's projection into it (`contacts.sync_policy`), the shared state files,
  and this doc. Nothing of the gate runs as a scheduler job any more. The
  retired e-mail triage's `TRIAGE_INBOX_SCAN_*`, `TRIAGE_PROMPT_LIST_LIMIT`,
  `TRIAGE_STALL_DAYS`, `TRIAGE_SENT_DERIVE_LIMIT`, `TRIAGE_BATCH_SIZE`,
  `TRIAGE_SENT_RECONCILE`, `TRIAGE_ANSWERED_FOLDER`,
  `EMAIL_PROCESSING_INTERVAL` and `RETINUE_TRIAGE_MODEL` are no longer read by
  anything (`TRIAGE_NEWS_FOLDER` and `TRIAGE_NEWS_EXCERPT_CHARS` only by the
  mail gateway, below). What stays are the two paths `TRIAGE_MESSENGER_DIR`
  and `TRIAGE_EMAIL_WHITELIST_PATH`, and the one-time migration's settings
  (`scripts/migrate-triage.py`, [mail-chats.md](mail-chats.md), "The
  migration"): `TRIAGE_STATE_DIR` (its marker, cutoff file, snapshot and journal, and the
  retired status store it reports on; keep it set wherever a deployment moved
  it), `MAIL_OWN_ADDRESSES`, `RETINUE_TRIAGE_MIGRATION`,
  `MIGRATE_TRIAGE_MODEL_TIMEOUT` and `MIGRATE_TRIAGE_MIRROR_WAIT`.
- **gateways:** `signal-gateway` / `whatsapp-gateway` / `telegram-gateway` /
  `sms-gateway` each
  get the shared volume mounted RW, per-message `.nt` writing, the
  classification gate on inbound, and `GET /undelivered?since=…`. For the news
  rail they also carry `scripts/news_ingest.py` and forward news-flagged messages
  to the web-gateway (guarded by `NEWS_INGEST_URL`). The `mail-gateway` gates
  the same way, with no `/undelivered`; its news rail has two tunables of its
  own, `MAIL_NEWS_FOLDER` and `MAIL_NEWS_EXCERPT_CHARS` — the retired triage
  gate's `TRIAGE_NEWS_FOLDER` and `TRIAGE_NEWS_EXCERPT_CHARS`, which it still
  reads where the new name is not set (an empty value included: an upgrade
  keeps an operator's setting), saying so at start.
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
