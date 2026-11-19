# Dossiers — one entity for people, topics and the inbox

**Status: design draft, nothing built.** Second iteration; the first was
`docs/conversations-redesign.md` on this branch. It proposes replacing the
three surfaces that grew separately — messenger chats
(`docs/messenger-chats.md`), dashboard threads (`docs/dashboard.md`,
"Conversations") and the e-mail triage flow with its omnibus digest
(`.claude/skills/triage/SKILL.md`) — and the attention item as a visible
object (`docs/attention-model.md` keeps its arithmetic) with **one entity, the
dossier**, and one gesture. Every phase below is Tier 3. The prototype of a
day under this model is `examples/dossier-prototype/` (the attention prototype’s day, same arrivals, under this model).

## Problem

- **Three containers for one thing.** Talking with someone is a chat if it
  happens on a messenger, a triage thread if it arrives by e-mail, and a
  dashboard thread if Ara is involved. One topic with three people spans all
  three, and none of them *is* the topic.
- **Archive has no job.** A chat is archived "until the next message", which
  answers no question the user has; in practice chats are archived at random
  or not at all. Chat apps don't have archive as a workflow, and nobody misses
  it.
- **The attention item is a ghost.** It has actions of its own (*Later*,
  *Mark done*) that differ from the thread or chat behind it, and it is
  ephemeral: open it from the home, leave it, and it may be gone — the
  conversation then has to be found again somewhere else.
- **E-mail has no surface.** A mail is visible only once triage has turned it
  into a thread or an omnibus line. "I just sent you an e-mail" (a code, a
  document) has no answer other than asking Ara.
- **The digest is an artificial container.** The omnibus is a thread that
  exists to hold proposals about other things; curiosity about one of its
  lines ("tell me more about this offer") has no natural place to go.

## Design in one paragraph

Everything the user attends to is a **dossier**: a container with
participants, a stream of correspondence and events, and a state. A dossier is
either a **relationship** (one person or one durable group; its stream is
every message exchanged with them, on every channel, mail included), a
**topic** (a matter with several participants or none but Ara: a migration, a
dispute, a question to Ara), or **standing** (never done: the relationship
dossiers, and *Inbox Zero*, which keeps the mailbox empty). A message is
stored once, in the channel ledger, and belongs to **exactly one dossier** by
a rule that needs no judgement: a messenger 1:1 chat is the relationship, a
group chat is the group's dossier, a mail goes where its thread already is,
and a new mail thread to several people opens a topic dossier of its own. Any
message can additionally be **referenced** from another dossier as an
**event** — by a rule or by a long-press gesture with a comment — and shows a
backlink to it. The home is the list of dossiers, grouped by state and
filtered by the attention mode; the Contacts page is the same list filtered to
relationships and sorted by latest message.

## The dossier

### Kinds

| Kind | Participants | Stream | State |
|---|---|---|---|
| **Relationship** | one person, or one durable group / list | every message with them, in and out, every channel | standing (ongoing) |
| **Topic** | several people and/or agents; or Ara alone | its own mail threads and messenger messages, plus events | active · waiting · resting · done |
| **Standing** (Inbox Zero, …) | Ara | events | ongoing |

A relationship is stored as the contact file (`docs/contacts.md`) — it
already carries the participant, the handles (its mapping) and the attention
priors; it gets an `overview.md`/`log.md` only once the user writes a note or
attaches something. A topic is today's project file (`overview.md`
frontmatter + `log.md`, in a chamber). A dashboard thread with Ara is a topic
whose only other participant is Ara.

### Fields

The attention model's fields live on the dossier (today's attention item
goes away). Topic frontmatter already has most of them:

| Attention field | Frontmatter |
|---|---|
| `kb:importance` | `importance:` (new; prior from kind/sphere as today) |
| `kb:due`, `kb:leadTime` | `expected_by:` / `next_due:`, `remind_before:` |
| `kb:sphere`, `kb:tag` | `sphere:`, `tags:` (new) |
| `kb:currentActor` | `current_actor:` |
| participants | `stakeholders:` — contacts by id, plus agents |
| state | `status:`, `paused:`, `resolved:` (below) |

A relationship's importance and sphere are the contact's priors; a message
from the person sets the dossier's deadline and actor (the Secretary's or the
VIP turn's classification, as today), and *done* clears them.

### States

"Not working on it" is a **state**, not a lower importance. Importance says
how much a thing matters and barely changes; working on something is a
commitment that changes week to week. If parking a dossier meant lowering its
importance, the tax return would have to be called unimportant to leave the
list in November. Kanban's point: a short active list, not a ranked long one.

| State | Meaning | Frontmatter |
|---|---|---|
| **Active** | the user expects to act on it within the week | `status: active`, not paused |
| **Waiting** | the ball is with someone else — derived, never set | `current_actor` ≠ the user |
| **Resting** | not now, not over; with a wake date or none | `paused: true`, optional `expected_by` / `next_due` |
| **Done** | achieved or abandoned; history | `resolved: true`, `status: done` or `abandoned` |
| **Ongoing** | relationships and standing dossiers: never done, never "active" in the weekly sense | no status |

Who moves a topic from active to resting: the user — it is a commitment. Ara
nudges: a topic quiet for two weeks gets one question in its own stream,
"still working on this, or rest it?", with chips; the existing stall pass does
nearly that already. A resting dossier wakes on its date
(`recurring-projects`, unchanged) or on a new message in it.

### Stream and events

A dossier's page shows its stream, newest at the bottom like a chat, with a
composer. The stream interleaves two things:

- **Messages** that belong to this dossier (by the one-dossier rule below),
  rendered as the chat page renders them: bubbles with channel glyph; a mail
  as a card (subject, sender, first lines, attachments as chips; tap opens the
  full mail under the existing CSP rules), replies stacked under the first
  card.
- **Events**, one shape:

```
event:  at, author (user | agent:<name> | job:<name>),
        comment?            — free text, the user's or Ara's
        messages?: [id…]    — references into other dossiers' messages, in order
        attachments?, kind? (note | turn | sent | status | decision)
```

A note is an event without messages; a referenced message is an event
without a comment; Ara's answer is an event of kind `turn`. `log.md` is the
written form (a dated entry; referenced messages as a blockquote with their
link, so the file reads without the gateway); the life store gets
`kb:DossierEvent` with `kb:references` to the message IRIs, so "which dossiers
quote this message" is one query.

**The composer** writes to Ara by default (today's thread turn). A dossier with
human participants has a channel switch — *to Ara* / *to the participants*
(mail, Signal, …) — and an outbound message sent from a dossier is a message
in the stream and goes through the send policies as today. On a relationship
the default is the person's channel, the one the last message came on.

## Where a message belongs — one rule

A message is stored once and belongs to exactly one dossier:

1. **A reply** (`In-Reply-To` / `References`; a messenger quote) goes where
   its thread already is. Nothing to decide.
2. **A messenger 1:1 chat** is the relationship with that person. **A group
   chat or mailing list** is the group's dossier (a durable group is a
   relationship; the `news` / `quieted` / `ignored` flags become its
   settings).
3. **A new mail thread from one person to the user alone** goes to that
   person's relationship — otherwise every fresh mail from a friend would open
   a dossier, which is the per-message sprawl this design ends.
4. **A new mail thread to several people** opens a **topic dossier**: title
   from the subject, participants from the recipients. Its replies follow it
   there (rule 1). The user's own first mail to several people does the same
   — "discuss the migration with A, B and C" is: a new dossier, the Secretary
   drafts the mail, sending it is the first message, the replies file
   themselves.
5. **Mass mail** (newsletter, notification) goes to the sender's relationship
   and the news rail, as today.

Two consequences, accepted:

- **Many small dossiers.** A group that writes ten separate threads produces
  ten dossiers. Merging is the gesture below, dossier to dossier, and the
  natural thing for Ara to **propose** when participants and subject match an
  existing dossier ("looks like TreatmentBank — add it there?"). Never
  automatic: a wrong merge is worse than two dossiers.
- **A relationship shows 1:1 traffic only.** What a person wrote in a group
  or in a shared thread sits there. *Everything with this person* on the
  relationship page runs the same query across every dossier they participate
  in — "what did Guido and I exchange" without storing anything twice.

**Unknown senders** become a screened relationship at once (as today: no VIP,
no card, no learned sphere), named by the address or number, at the top of the
contacts list because they wrote last. A short-code SMS sender or a `noreply@`
address is a relationship like any other: there is no unmapped bucket, because
a message always has a sender. "I sent you a code" is: open Contacts, the
sender is the top row, the code is in the preview.

## Messages into other dossiers

### By hand — the one gesture

In any stream, **long-press a message**, select more if wanted, then **Add to
dossier…**: a picker (recent dossiers first, search, *New dossier…* with a
title Ara proposes from the selection) and a comment field. Confirm → **one**
event in the target, carrying the selection in order and the comment. The
originals stay where they are. From inside a dossier, *Add messages…* does the
same from the other side (pick a relationship, then messages) — how a topic is
built from last month's mails. **Merging** two dossiers is the same gesture
applied to a whole dossier.

**Backlinks.** A message referenced by another dossier shows that dossier's
chip under its bubble; tapping it opens the dossier at that event. Removing the
event removes the chip. A message may be referenced from several dossiers; it
*belongs* to one.

### By rule — mappings

| Mapping | Decided by | Example |
|---|---|---|
| a relationship's whole stream → topic | rule, deterministic | a friendship kept as a topic with notes, 1:1 with one person |
| some of a relationship's messages → topic | judgement | an employee's messages about the employment, not the small talk |

Rules live on the topic (`maps:` in frontmatter: a contact id, or a contact
plus a brief). The first is applied on arrival at no cost. The second needs a
model turn and must never delay anything: the message is in the relationship
at once and is **referenced** when a turn runs anyway (the sender is VIP or
instructed) or in the next batch. A wrong filing is corrected by the gesture —
remove here, add there — and recorded as an example for that topic's brief.

## Inbox Zero — e-mail housekeeping as a standing dossier

One per mailbox, Ara as actor. Its job is a state, not a schedule: **every
mail still in the inbox has a proposed resolution that takes it out** —
delete, archive, file (Archivist), reply, add to a dossier, unsubscribe / news
rail. It replaces the omnibus thread and the per-mail triage threads; the
triage skill's classification, sender rules and memories carry over, only the
output changes shape.

**Mails the user dealt with leave by themselves, early**, with no model turn:
replied to (from any dossier or any client — the outbound mirror sees it) →
archived; referenced into a dossier → archived (it is held there now); marked
done in its stream → archived; filed by the Archivist → deleted or archived per
the existing receipt rules. The gateway does this on its next pass.

**What remains gets a proposal.** Ara's pass (the existing cadence: VIP
senders soon, the rest in the daily run) adds one event to Inbox Zero per
batch: each mail a referenced line with its proposed resolution and an inline
*more* chip, one bulk chip to apply them all. "Tell me more about this offer"
is a turn right there and its answer an event; if it becomes a decision, the
gesture makes a topic of it. Ignored proposals do not pile up: the next batch
restates only what is still in the inbox, and the dossier's row shows *n mails
waiting* with the oldest's age.

The inbox folder itself is the list Inbox Zero works down; a client used in
parallel simply agrees with it. The triage status store remains for
bookkeeping (what was proposed when), no longer for "what is open". Messenger
traffic has no inbox and is not in Inbox Zero.

## The home and the lists

**Home** = dossiers that want something, using the attention arithmetic
unchanged (`docs/attention-model.md`):

1. **Now** — admitted by the current mode.
2. **Active** — the rest, the current mode's spheres first, then *Other
   spheres* collapsed. Importance, deadline and who holds the ball as
   decoration on the row; the subtitle is the newest message or event.
3. **Waiting** — collapsed, with ages.

A **relationship appears on the home only while something is pending** in it
(a message classified with a deadline or an actor), and leaves it when that is
done — the row then looks like any quiet chat in Contacts. **Resting** dossiers
appear only when they wake; **Done** never. A row is the dossier; tapping opens
it; back returns to the same list. Nothing vanishes except what the user
closes, mutes or deletes. The digest is unchanged in timing and content: a
push about what is in *Now* and what climbed since the last breakpoint,
linking to rows.

**Contacts** = relationships, sortable by latest message (default),
alphabetically, or by hand (drag; per device; starred pins). Each row: name,
newest message's preview and channel, unread badge, and the chip of any topic
with something pending that references their messages. Search, edit and *+ New
contact* stay.

**Dossiers** (today's Projects page) = everything, behind *Active · Resting ·
Done* and search.

**No archive.** Topics close or rest; relationships are only ever sorted.
**Mute** stays (silences pushes). Swipe right on any row = done (closes a
topic; clears a relationship's pending state), swipe left = mute. No action
exists on a row that does not exist on the page.

## The gate, one sender axis

With mail visible on arrival, the e-mail whitelist loses the reason it exists
(a mail was invisible until triage). Proposed: **one sender axis for every
channel** — *VIP* (a turn on arrival, pushes), *instructed* (a turn with the
brief, its verdict decides), *everyone else* (shown at once, judged in the next
batch). Per-channel push defaults stay: a messenger message pushes, a mail does
not, unless the person's card says otherwise. Group flags unchanged. Open
question 3 asks when.

### Relation to #382 (declarative per-contact rules)

Issue #382 proposes rules on the contact — match, extract, show a card with a
copy box, ring, expire — for senders whose handling is fixed and must be
instant, the bank's 3-D Secure SMS above all. It fits this design unchanged
and is not blocked by it: the rules live on the contact, and the contact *is*
the relationship dossier; they run in the gateway's inbound path, which this
design keeps. What the issue calls the *attention item* is here the
relationship's pending state and its row: a rule's `card` is the row's
subtitle, `importance`/`due` its pending fields, and `expire` clears the
pending state and redacts the copy value in the stream — in the store, as the
issue already proposes, since the stream is the store's view. A `ruled` gate
class sits beside `vip` and `instructed` on the single sender axis. The copy
box is a message decoration like the backlink chip. #382 can be built before
any phase here; it is the part of "I sent you a code" that needs more than a
mirror.

## What this replaces, what it keeps

**Replaced:** the Chats page (by Contacts); dashboard threads as a separate
kind (by Ara-only topics); companion threads (by the dossier page: Ara is the
composer's default addressee everywhere); attention items as objects; the
omnibus digest and per-mail triage threads (by Inbox Zero); archive.

**Kept:** the delivery ledger and gate; the messenger mirror and its composer
(they become the relationship's stream); send policies and `/sends`; contacts
and their file format; project files in chambers; the attention arithmetic,
modes, digest timing and pushes; the triage skill's rules and memories; the
news rail.

## Phases

1. **Mail mirror.** Inbound and outbound mail into the ledger shape
   (`kb:InboundMessage` / `kb:OutboundMessage`, `kb:channel email`, thread
   keys from `Message-ID`). Read-only.
2. **Relationships and the Contacts page.** Cross-channel stream per person
   and group; Contacts sortable by latest message; unknown senders as screened
   relationships; the mail composer. From here "I sent you an e-mail" is
   solved.
3. **The one-dossier rule for mail**, and topic dossiers from multi-recipient
   threads; Ara's merge proposals.
4. **Events and the gesture.** `kb:DossierEvent`, `log.md` writing, *Add to
   dossier…* with comment, backlinks; the dossier page as a stream with a
   composer (Ara turns).
5. **Inbox Zero.** The standing dossier, the early auto-resolutions, triage
   output as events; the omnibus retires.
6. **Home on dossiers.** States and attention fields on frontmatter, the home
   as above, Contacts and Dossiers pages; attention items retire; existing
   threads migrate as Ara-only topics (read-only history); the chats' companion
   threads fold into the relationships.
7. **Mappings** with a brief, filed by the VIP turn or the batch.
8. **One sender axis** (if open question 3 says yes).

## Open questions

1. **Ara-only topics.** Is every question to Ara a topic dossier (in a
   framework-owned area until moved), or does a quick question want a lighter
   container that can be promoted?
2. **Where a dossier lives.** Chamber choice for a topic opened by rule 4 or
   the gesture: the chamber of its first participant, the last one used, or
   asked?
3. **The e-mail whitelist.** Collapse into VIP now, or after phase 2?
4. **`log.md` writes.** Events are written into chamber repos by the gateway
   (a new writer) or by a job batching them; how commits stay quiet.
5. **Auto-rest.** Does a quiet topic rest by itself after two weeks, or only
   after the user's answer to Ara's question?
6. **Several pending things with one person.** Separate topics by design;
   confirm that a relationship never needs more than one pending state.
