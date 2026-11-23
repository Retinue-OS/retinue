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

### Nesting — a topic within a topic

A dossier is not a folder: it holds *references* (a message belongs to one
dossier and is referenced from many, each time with a comment), it is ordered
by time, and it carries participants, state and deadline. But topics do
**nest**: a topic may name one parent topic, `within:` (today's `goal:`,
renamed). An engagement holds its pieces of work; the TreatmentBank migration
sits within the Plazi engagement. Rules:

- One parent at most, a topic only — relationships and standing dossiers have
  none, and no dossier *contains* a relationship (a person's messages in a
  sub-dossier still belong to the relationship; containment of messages is by
  reference, containment of topics by tree, and the two never mix).
- A child inherits the parent's sphere and importance unless it says
  otherwise. Opening a child is an event in the parent's stream, and the
  parent's page links its children with their states; a child's page links
  its parent.
- The parent's row shows its children's pending count; the Dossiers page
  shows the tree, children collapsed. The focus on a dossier (below) covers
  its descendants.
- The gesture's *New dossier…* offers *within* a topic; `--within` on the CLI.

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

**A referenced object is embedded, live, as its own block.** A reference is an
address, never a copy or a paraphrase, and wherever an event or an Ara turn
carries one — the gesture's event, Inbox Zero's proposals, a merge proposal,
Ara's overview of an engagement — the stream renders the object the way its
own page does: a **message** as the bubble its dossier shows (mail card,
attachments, media; read-only, headed by where it is from), a **dossier** as
the row the Dossiers or Contacts page shows, with its current state, and a
**relationship** as its Contacts row. The block is the same component in both
places, so it is as current as the list — a dossier embedded in yesterday's
turn shows today's state — and a tap opens the object where it lives. "Show
me that mail" is therefore never a turn: the mail is already in the message
that mentioned it. Ara's turns carry `kb:references` to the same end; the
Secretary's reply proposals embed the message they answer. A reference whose
object is gone renders as a short tombstone.

**The composer** on a topic writes to Ara (today's thread turn); on a
relationship it writes **to the person**, over the channel the last message
came on, with a *to Ara* switch beside it. An outbound message is a message in
the stream and goes through the send policies as today. **A mail has a subject
line** in the composer: left empty, the message is a reply in the newest
thread (`Re: …`, settling what that thread had pending); given, it is a **new
thread** — just another message in the relationship (rule 3), touching nothing
that was pending before. So writing to the tax office about a new matter is:
＋ → Message → the tax office → subject, text, send; the open letter about
2025 stays as it was.

**Ara in a relationship's stream speaks only when addressed.** Her reading of
the pending state is the row's second line and the attention sheet, and her
chips (a draft, a reminder, *nothing pending*) sit under the stream; she does
not open the stream with a summary of what the user can see. In a topic she
does — the topic is a conversation with her.

**The model picker** (which tier answers Ara's turns, `docs/model-routing.md`)
is in the page bar of a topic, where Ara is a participant; on a relationship it
is in the ⋮ menu, since Ara answers there only when addressed.

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

**A mail nobody has judged yet is Ara's, not the user's.** On arrival the mail
is embedded in Inbox Zero's stream (dated, as the mail's own bubble) and is a
row on the home under its sender — but Inbox Zero itself asks nothing: no
pending state, no badge, no "n mails waiting". It becomes pending the moment a
batch has a **proposal**. Ara's pass (the existing cadence: VIP senders soon,
the rest in the daily run) adds one event per batch: each mail **embedded** as
its own bubble, with its proposed resolution under it and its chips, one bulk
chip to apply them all. That event is what the user is asked to look at — the
triage proposal, with the mail in it. Reading the mail costs
no turn; "what does this offer
actually mean for me" is a turn right there and its answer an event; if it
becomes a decision, the
gesture makes a topic of it. Ignored proposals do not pile up: the next batch
restates only what is still in the inbox, and the dossier's row shows *n mails
waiting* with the oldest's age.

The inbox folder itself is the list Inbox Zero works down; a client used in
parallel simply agrees with it. The triage status store remains for
bookkeeping (what was proposed when), no longer for "what is open". Messenger
traffic has no inbox and is not in Inbox Zero.

## The home and the lists

### Focus and aperture

The attention model has two controls folded into one knob, the mode chip with
its presets. Pulled apart they answer the "eight minutes while the update
reboots" case — a glance at what else is going on, without switching pages or
modes:

- **Focus — what you are on.** Nothing (flow), one or more spheres, or **one
  dossier** with what is within it. That is today's *Focused* scope (sphere ⊃
  dossier), promoted to a chip in the home's header: `⌖ CRR data platform ▾`.
- **Aperture — how far from the focus you see.** Not a level of urgency but a
  **distance**, in rings, measured in the relations the data already has: the
  tree (`within`), the participants, the spheres and the spheres within spheres
  (`focus.json` `within`). A segmented chip on the phone, a slider on a wide
  screen:

| Ring | From a focused **dossier** | From a focused **sphere** |
|---|---|---|
| 0 **strictly** | the dossier and what is within it | dossiers in the sphere (and in spheres within it) |
| 1 **+ people** | its parent and siblings; anything sharing a participant with it or its children | anything sharing a participant with a dossier in the sphere (a customer who is also a friend) |
| 2 **+ nearby** | the same sphere — other customers — and the spheres within it or around it | the enclosing sphere and the spheres within it |
| 3 **everything** | no focus | no focus |

"Other customers before spare-time things" is ring 2. Beyond that — is *admin*
closer to *customers* than *friends*? — the data has nothing to say, and no
metric is invented: the spheres are a small named set with one nesting, and the
rings are an ordering of relations, not a number. A learnable refinement later
(tag co-occurrence) stays out of the first iteration. With **no focus** nothing
is near, so only the widest aperture shows anything — which is what *Open* is.

The **mode is a preset** of (focus, aperture, permits), set by the schedule as
today: *Deep work* = flow, strictly; *Open* = flow, everything; *Social* =
friends + family, strictly. The hand controls deviate from the preset for the
stint; **nothing snaps back on its own** — a hand-set focus or aperture holds
until the mode changes, by schedule or by hand, and the new mode's preset
applies. The eight-minute glance ends when Deep work ends, or when the user
narrows it back.

**Listing and ringing.** A dossier is *listed* when its distance is within the
aperture's ring, or its sender holds a permit for the mode. Urgency is the other
axis and keeps its own rules: what is listed *rings* from time-sensitive;
critical rings always, whatever the aperture; and the home's *below the
aperture* line **counts the urgent ones out of sight** — *+ 7 below the
aperture, 2 urgent — widen to "+ people"* — so that something urgent is out
there is visible without showing what, and one tap shows it. **Looking is
delivery:** what a wider aperture shows is released, as *pull* is today, and
does not return in the next digest; new arrivals outside do.

**Home** = the chat list, for everything. The row is the Chats page's row
today — avatar with the channel mark, name, age, the newest entry as preview
(*who: text*), the unread badge — and every dossier is in it: people, groups,
mail senders, topics, conversations with Ara, Inbox Zero, newest first,
starred first. The focus and aperture decide **which rows** are in the list,
not what a row looks like: within the aperture's ring a dossier is listed;
outside, one line counts them and the urgent ones among them — *+ 7 below the
aperture, 2 urgent — widen to "+ people"* — and widens on a tap. What rings
in the mode in force sits on top under *Rings now*; everything else follows by
recency. A **pending** dossier carries one quiet line under its preview —
Ara's reading of what is pending, the deadline, the ring — as decoration; a
dossier with nothing pending looks like any chat row. **Resting** dossiers
join the list when they wake; **Done** never. A row is the dossier; tapping
opens it; back returns to the same list. Nothing vanishes except what the
user closes, mutes or deletes. The digest is unchanged in timing and content:
a push about what is in *Rings now* and what climbed since the last
breakpoint, linking to rows.

**Contacts** = the address book: every person and group, with or without
messages, in the same row, sortable by latest message (default),
alphabetically, or by hand (drag; per device; starred pins). Tapping a row
opens the stream, as on the home; the **contact card** (👤 on the row, in the
page bar and the ⋮ menu of a relationship) is the person behind it — the
handles on every channel, the sphere, the importance prior and permits, the
VIP switch, the instruction, the dossiers they take part in — exactly what
`docs/contacts.md` describes, in one sheet. A screened sender's card says so
and is where they get filed. Search and *+ New contact* stay.

**Starting something** is one *＋* in the navigation row, on every page, with
four entries: a **message** to someone (opens their stream at the composer,
addressed to them, the subject line empty for a new thread), **ask Ara** (a new topic with Ara alone, optionally within
a topic), a **dossier** with people (its first message goes out from it, the
replies file themselves — rule 4), a **contact**. There is no other "new"; a
new topic from a message is the gesture.

**Dossiers** (today's Projects page) = the topics, standing dossiers and
conversations with Ara, in the same row, as a tree (children collapsed),
behind *Active · Resting · Done* and search.

**The dossier page** is the chat page: the stream newest at the bottom, day
separators (*Today*, *Yesterday*, the date), every entry with its time, the
composer at the foot.

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
6. **Home on dossiers.** States and attention fields on frontmatter, `within:`
   for the tree, the home as above, Contacts and Dossiers pages; attention
   items retire; existing threads migrate as Ara-only topics (read-only
   history); the chats' companion threads fold into the relationships.
7. **Focus and aperture.** The two chips over the mode presets in
   `focus.json`, the distance rings over `within`, participants and sphere
   nesting, the *below the aperture* line with its urgent count; the modes'
   `admits` and `threshold` become `focus` and `aperture`.
8. **Mappings** with a brief, filed by the VIP turn or the batch.
9. **One sender axis** (if open question 3 says yes).

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
7. **Sphere distance.** Ring 2 uses only the spheres' `within` nesting; is
   that enough, or does a deployment want to name neighbours by hand
   (`near: [admin]` on a sphere) before any learning?
