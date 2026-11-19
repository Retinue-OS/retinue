# Projects, people and their messages — one model instead of chats, threads and triage

**Status: design draft, nothing built.** This proposes replacing three surfaces
that grew separately — messenger chats (`docs/messenger-chats.md`), dashboard
threads (`docs/dashboard.md`, "Conversations") and the e-mail triage flow with
its omnibus digest (`.claude/skills/triage/SKILL.md`) — with two entities and
one gesture. It also retires the attention item as a visible object
(`docs/attention-model.md` keeps its arithmetic). Every phase below is Tier 3.

## Problem

- **Three containers for one thing.** Talking with someone is a chat if it
  happens on a messenger, a triage thread if it arrives by e-mail, and a
  dashboard thread if Ara is involved. One topic with three people spans all
  three, and none of them is the topic.
- **Archive has no job.** A chat is archived "until the next message", which
  answers no question the user has. In practice chats are archived at random
  or not at all. Chat apps don't have archive as a workflow, and nobody is
  missing it.
- **The attention item is a ghost.** It has actions of its own (*Later*,
  *Mark done*) that differ from the thread or chat behind it, and it is
  ephemeral: open it from the home, leave it, and it may be gone — the
  conversation has to be found again somewhere else.
- **E-mail has no surface.** A mail is visible only once triage has turned it
  into a thread or an omnibus line. "I just sent you an e-mail" (a
  verification code, a document) has no answer other than asking Ara.
- **The digest is an artificial container.** The omnibus is a thread that
  exists to hold proposals about other things; curiosity about one of its
  lines ("tell me more about this offer") has no natural place to go.

## Design in one paragraph

There are **people** and there are **projects**. A *person* (the contact, as
`docs/contacts.md` already defines it) carries the complete, deterministic
stream of every message exchanged with them on every channel — mail included —
in and out, with no model in the loop; the Contacts page, ordered by latest
message, *is* the chat list. A *project* is a topic with participants and the
attention fields (importance, deadline, who holds the ball, spheres); its
stream consists of **events**: its own (notes, Ara's turns, decisions, a sent
mail) and **references** to messages from people's streams, attached by rule
or by hand, each with an optional comment, and backlinked from the message.
The home is the list of projects, filtered and grouped by the attention mode.
E-mail housekeeping is itself a project — **Inbox Zero** — in which Ara
proposes, for every mail still in the inbox, the resolution that takes it out,
and in which what the user already dealt with leaves on its own.

## The entities

### Person — the stream of everything exchanged

The contact file stays the source of truth for who someone is (handles,
spheres, VIP, instruction). What changes is that the person gets a **page
with a stream**: the messenger chat page generalised across channels.

- **Every message, every channel, in one timeline.** The messenger ledger
  (`kb:InboundMessage`, `kb:OutboundMessage`, `kb:chat`, `kb:messageId`) is
  joined on the person's handles — the query `docs/contacts.md` already shows.
  Mail joins through a **mail mirror** (below). A channel glyph on each
  bubble; the composer picks the channel (default: the one last used, or the
  one the message being answered came on).
- **Mail in the stream** renders as a card rather than a bubble: subject,
  sender, first lines, attachments as chips; tap opens the full mail (HTML
  sanitised under the existing CSP rules). A mail thread (`Message-ID` /
  `References`) is grouped: replies stack under the first card, collapsed past
  the newest two.
- **Groups and lists** are not people. A messenger group and a mailing list
  each get a stream of the same kind, listed with the people (a group glyph),
  so "the team chat" is as findable as a person.
- **Unknown senders become contacts at once** — screened (as today: no VIP,
  no card, no learned sphere), named by the address or number, at the top of
  the list because they wrote last. Naming them is the existing contact-card
  flow. A short-code SMS sender or a `noreply@` address is a contact like any
  other; there is no "unmapped" bucket, because a message always has a sender.

**The Contacts page** becomes the second main view: every person and group,
sortable by **latest message** (default), **alphabetically**, or **manually**
(drag; stored per device, the starred flag still pins). Each row: name, the
newest message's preview and channel, an unread badge, and — when the person's
messages belong to a project with something pending — that project's chip.
The rest of today's address-book functions (search, edit, *+ New contact*)
stay on the same page. *"I sent you a code by e-mail"* is: open Contacts, the
sender is the top row, the code is in the preview.

### Project — a topic, its participants, its events

The project file (`overview.md` frontmatter + `log.md`, in a chamber) stays
the store. It already carries the attention fields in substance
(`stakeholders`, `current_actor`, `expected_by`, `remind_before`, `status`,
`paused`); they become the attention model's fields directly:

| Attention field | Project frontmatter |
|---|---|
| `kb:importance` | `importance:` (new; prior from kind/sphere as today) |
| `kb:due`, `kb:leadTime` | `expected_by:` / `next_due:`, `remind_before:` |
| `kb:sphere`, `kb:tag` | `sphere:`, `tags:` (new) |
| `kb:currentActor` | `current_actor:` |
| participants | `stakeholders:` — contacts by id, plus agents |

A **project page** shows the stream of events, newest at the bottom like a
chat, with a composer. Writing in the composer is an Ara turn by default
(today's thread); with participants other than Ara, the composer has a channel
switch — *to Ara* / *to the participants (mail, Signal …)* — and an outbound
message sent from a project is both a message in each recipient's stream and
an event here.

**Events** are one shape:

```
event:  at, author (user | agent:<name> | job:<name>),
        comment?            — free text, the user's or Ara's
        messages?: [id…]    — references into people's streams, in order
        attachments?, kind? (note | turn | sent | status | decision)
```

A note is an event without messages; a forwarded message is an event without
a comment; Ara's answer is an event of kind `turn`. `log.md` is the written
form (a dated entry; referenced messages as a blockquote with their link, so
the file reads without the gateway); the life store gets `kb:ProjectEvent`
with `kb:references` to the message IRIs, so "which projects quote this
message" is one query.

### Conversations with Ara alone

A dashboard thread today becomes a project whose only participant besides the
user is Ara (or another core agent). Asking something new is starting a
project — lightweight: no chamber choice, no frontmatter prompt; it lives in
the framework's own project area until it is moved into a chamber or simply
goes quiet. *Open question 1* asks whether that is right, or whether quick
questions want a lighter container.

## Messages into projects

### By hand — the one gesture

In any stream (a person's, a group's, or Inbox Zero), **long-press a
message**, select more if wanted, then **Add to project…**: a picker (recent
projects first, search, *New project…* with a title Ara proposes from the
selection) and a comment field. Confirm → **one** event in the target project,
carrying the selection in order and the comment. The originals stay where they
are. From inside a project, *Add messages…* does the same from the other side
(pick a person, then messages) — how a project is built from last month's
mails.

**Backlinks.** A message referenced by a project shows the project's chip
under its bubble; tapping it opens the project at that event. Removing the
event removes the chip. A message may sit in several projects.

### By rule — mappings

| Mapping | Decided by | Example |
|---|---|---|
| A person's whole stream → project | rule, deterministic | a friendship kept as a project, 1:1 with one person |
| A mail thread → project | `Message-ID` chain, deterministic | a project's first mail went out from the project; every reply files itself |
| Some of a person's messages → project | judgement | an employee's messages about the employment, not the small talk |

Rules live on the project (`maps:` in frontmatter: a contact id, a group, a
mail thread root, or a contact plus a brief for the filtered kind). The first
two are applied by the gateway on arrival, at no cost. The third needs a model
turn and must never delay anything: the message is in the person's stream at
once and is **filed** when a turn runs anyway (the sender is VIP or
instructed) or in the next batch. A wrong filing is corrected by the gesture —
remove it here, add it there — and the correction is recorded as an example
for that project's brief.

### Starting a topic

"Let's discuss migrating the data set with A, B and C": a new project with
those participants; the Secretary drafts the first mail (send policies as
today); sending it makes event one and registers the mail thread as the
project's mapping, so the replies file themselves.

## Inbox Zero — e-mail housekeeping as a project

A standing project, one per mailbox, with Ara as `current_actor`. Its job is a
state, not a schedule: **every mail in the inbox has a proposed resolution
that takes it out of the inbox** — delete, archive, file (Archivist), a reply,
add to a project, unsubscribe/news rail. It replaces the omnibus thread and the
per-mail triage threads; the triage skill's classification, sender rules and
memories carry over unchanged, only their output changes shape.

**Mails the user dealt with leave by themselves, early.** Dealing with a mail
*is* the resolution, so it needs no proposal:

- replied to it (from the stream or from any client — the outbound mirror sees
  it) → archived;
- added it to a project → archived (the project holds it now);
- marked it done in its stream → archived;
- the Archivist filed its document → deleted or archived per the existing
  receipt rules.

This runs on the gateway's next pass, without a model turn.

**What remains gets a proposal.** Ara's pass (the existing cadence: whitelisted
or VIP senders soon, the rest in the daily run) adds one event to Inbox Zero
per batch, each mail a referenced line with its proposed resolution and an
inline *more* chip; one bulk chip applies them all. "Tell me more about this
offer" is a turn right there — the answer is an event in Inbox Zero; if it
turns into a decision, the gesture makes a project of it. A proposal the user
ignores does not pile up: the next batch restates only what is still in the
inbox, and the project's row on the home shows *n mails waiting* with the
oldest's age.

**The mailbox becomes the state.** Today triage keeps handled-state outside the
mailbox so no client is needed; with a mirror, the inbox folder itself is the
list Inbox Zero works down, and a client used in parallel simply agrees with
it. The triage status store remains for bookkeeping (what was proposed when),
no longer for "what is open".

Messenger traffic has no inbox to empty and is not in Inbox Zero: a messenger
message is seen in its stream; what matters is filed into projects by rule,
by the VIP/instructed turn, or by hand.

## The home and attention

The home is the **list of projects**, filtered and grouped by the focus mode,
using the attention model's arithmetic unchanged (`docs/attention-model.md`):
*Now* (may interrupt in this mode), then the rest by recency, with importance,
deadline and who holds the ball as decoration on each row, and filter chips
(*waiting on others · due · unread*) in place of more sections. A row is a
project; tapping opens it; back returns to the same list. Nothing vanishes
except what the user closes, mutes or deletes.

- **No attention items.** The fields live on the project. *Done* closes or
  rests the project (`status`, or `paused` with a date), the same actions on
  the row as on the page.
- **No archive.** Projects close or rest; people's streams are only ever
  sorted. Muting stays (a person, a group, a project): it silences pushes.
- **A person's message with no project** pushes as the messenger push does
  today (gate classes and group flags unchanged) and opens the person's stream.
  It is never on the home: the home is what is pending, and a message that
  belongs to no project has not been made pending by anyone. Inbox Zero and
  the VIP turn are the two ways a stray message gets a project when it should.

## The gate, one sender axis

With mail visible on arrival, the e-mail whitelist loses the reason it exists
(a mail was invisible until triage). Proposed: **one sender axis for every
channel** — *VIP* (a turn on arrival, pushes), *instructed* (a turn with the
brief, its verdict decides), *everyone else* (shown at once, judged in the next
batch). Per-channel push defaults stay: a messenger message pushes, a mail does
not, unless the person says otherwise. The group/list flags (`news`, `quieted`,
`ignored`) are unchanged. *Open question 3* asks whether to collapse the
whitelist now or after the mail mirror has been lived with.

## What this replaces, what it keeps

**Replaced:** the Chats page (by Contacts); dashboard threads as a separate
kind (by projects); companion threads (the project page with a channel switch,
or the person's Ara pane — open question 2); attention items as objects; the
omnibus digest and per-mail triage threads (by Inbox Zero); archive on chats
and threads.

**Kept:** the delivery ledger and gate; the messenger mirror and its composer
(they become the person's stream); send policies and `/sends`; contacts and
their file format; project files in chambers; the attention arithmetic, modes,
digests' timing and pushes; the triage skill's rules and memories; the news
rail.

## Phases

1. **Mail mirror.** The mail gateway (or a mirror job beside `email_client.py`)
   writes inbound and outbound mail into the same ledger shape
   (`kb:InboundMessage`/`kb:OutboundMessage`, `kb:channel email`, thread
   keys from `Message-ID`). Read-only.
2. **Person streams and the Contacts page.** Cross-channel stream per person
   and group; Contacts sortable by latest message; unknown senders as screened
   contacts. Mail composer in the stream. From here "I sent you an e-mail" is
   solved.
3. **Events and the gesture.** `kb:ProjectEvent`, `log.md` writing, *Add to
   project…* with comment, backlinks; the project page as a stream with a
   composer (Ara turns).
4. **Inbox Zero.** The project, the early auto-resolutions, triage output as
   events; the omnibus retires.
5. **Home on projects.** Attention fields on project frontmatter, the home as
   the project list, filter chips; attention items retire; existing threads
   migrate as Ara-only projects (read-only history).
6. **Mappings.** Whole-stream and mail-thread rules on arrival; filtered rules
   with a brief, filed by the VIP turn or the batch.
7. **One sender axis** (if open question 3 says yes).

## Open questions

1. **Ara-only conversations.** Is every question to Ara a project (in a
   framework-owned area until moved), or does a quick question want a lighter
   container that can be promoted?
2. **The Ara pane on a person.** Keep the companion as a pane on the person's
   stream (drafting a reply to a friend needs no project), or only on projects?
3. **The e-mail whitelist.** Collapse into VIP now, or after phase 2?
4. **Where projects live.** Chamber choice for a project created by the
   gesture: the chamber of its first participant, the last one used, or asked?
5. **`log.md` writes.** Events are written into chamber repos by the gateway
   (a new writer) or by a job batching them; how commits stay quiet.
6. **Several pending things with one person.** Handled by separate projects;
   confirm that a person's stream never needs its own pending state.
