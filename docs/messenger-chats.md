# Messenger chats — a deterministic chat surface with an agent at its side

**Status: phases 1–4 are built.** The delivery ledger, the chat mirror and its
page, the composer with the user's own send, and the companion thread that
stages drafts into it are live — and, with the change that landed this
document, so is the piece the rest of it was for: **the delivery gate's forward
path now lands in the chat, not in triage.** Phases 5 and 6 (summaries, forking,
the polish list) are still the target picture. This supersedes the "one
dashboard conversation per inbound message" model for messenger channels.

## Problem

Messenger support today routes every inbound message through triage into
dashboard conversations, and that shape fights the medium:

- **The channel's conversation has no home.** An inbound message becomes a
  *new* dashboard conversation (or lands in an existing one, if the triage turn
  happens to find it — a heuristic, not a guarantee). A back-and-forth with one
  person is scattered across threads, each quoting fragments. Only since
  `a95b19c` does a thread's resumed session even learn about messages appended
  from outside it — and only when they were appended to *that* thread.
- **Half the conversation is not even recorded.** The delivery ledger persists
  inbound messages (`kb:InboundMessage`), but no outbound message is persisted
  anywhere: not sends via the push CLIs, not approved `/sends` items, and not
  the user's own sends from their phone (the WhatsApp gateway explicitly drops
  own-device echoes; Signal and Telegram never register them). "Show me the
  whole conversation with X" is unanswerable, in the UI and over SPARQL alike.
- **Simple replies are heavy.** The lightest path from "yes, 3pm works" to the
  wire is: triage model turn → new dashboard thread → user approves in the
  thread → Ara's model turn executes `signal-push.py --reply-to` → `verify`
  policy queues it at `/sends` → user approves *again* → sent. Two model turns
  and two approvals for a message the user could have typed in five seconds.
- **Notification costs a model turn.** A message from a sender on the triage
  whitelist spends a full triage session just to tell the user it arrived.
- **The user cannot simply write.** There is no way to compose and send a
  messenger message from the dashboard at all — everything is mediated through
  an agent turn and the send-approval machinery, even when no agent input was
  wanted.

The redesign keeps what works — the delivery gate, the never-drop ledger, the
news rail, the send policies, the conversation machinery — and gives messenger
traffic the surface it actually needs.

## Design in one paragraph

Each channel conversation (one peer or group, on one gateway account) becomes a
**chat**: a first-class entity that is a *deterministic mirror* of that
conversation — every message in and out, rendered like the messenger client
itself, with a composer that sends directly, with no model in the loop and no
approval step for what the user types. Beside every chat sits its **companion
thread**: an ordinary dashboard conversation (a new hidden kind), reached by
switching panes on a wide screen or swiping on the phone. The companion reads
the whole chat and the shared **draft**, writes *into* the draft (the chat's
text area), and presses send only where the channel's send policy allows an
agent send — under `verify`, the user's send button is the approval. A rolling
**summary** per chat bounds the model context however long the chat lives;
tangential topics **fork** off into normal dashboard conversations linked back
to the chat. A chat is *not* a conversation, and no conversation holds
messenger messages: the two rails meet only in the shared context
(summary + tail) and the draft.

## Why a chat is not a conversation

This is the load-bearing decision, so it gets its own section. The dashboard
conversation concept encodes a thread *with the user's own agent*: every user
turn triggers a model turn, the thread history is the model's context, and the
thread has a lifecycle (active → archived). A messenger chat is a thread *with
an external correspondent* — a person today, just as well someone else's agent
tomorrow; the design assumes nothing about who or what answers, so
agent-to-agent correspondence needs no special case anywhere (gate, mirror,
companion). What defines a chat is that the other side sits *outside* Retinue,
reached over a channel Retinue does not own: the thread is a record to mirror
faithfully, not a context to drive; most messages need no model turn on our
side; and it never ends. Forcing the second into the first is what produces
today's pathologies — model turns spent on messages that needed none, approval
steps on words the user typed themselves, unbounded context, and the scatter of
one relationship across many threads.

So the hybrid is split along its natural seam:

| | **Chat** (new entity) | **Companion thread** (existing entity, new kind) |
|---|---|---|
| A thread with | an external correspondent (person or agent) | the user's own agent |
| Turns driven by | correspondents sending messages | user/agent pane activity |
| Model in the loop | never | every turn (normal conversation) |
| History | the channel's record, mirrored deterministically | agent discussion about the chat |
| Lifetime | unbounded | unbounded, but context bounded by the summary |
| Storage | gateway message stores (`.nt`) + retinue chat state | `CONVERSATIONS_DIR` like any thread |

The conversation concept stays exactly what it is — nothing hybrid is added to
it. The hybrid exists only in the UI, where the two rails render side by side,
and in two well-defined shared objects: the **summary** (context both rails
contribute to) and the **draft** (the one place agent output and user input
compose into a single outgoing message).

## The chat entity

### Identity

A chat is `<channel>:<chat-key>`, where `chat-key` is the exact recipient
string the channel's own send path accepts — the WhatsApp chat JID, the Signal
number/UUID or `group:<id>`, the Telegram `chat_id`. This is deliberately the
same normalization the reply tokens and `recent-chats` already use, so inbound
records, outbound records and the send route all agree on which conversation a
message belongs to. Display names resolve through the gateway's existing
contacts/groups endpoints and are cached in the chat state.

### Messages — completing the ledger

The mirror is served from the gateways' per-message stores, which today hold
only half the story. Three additions, all in the gateways (the single writers
of their stores):

1. **`kb:chat`** on every message — the chat key above, stamped at persist
   time. Inbound records today carry `sender`/`group`, from which the key is
   derivable, but stamping it makes the mirror (and SPARQL history) a plain
   filter instead of a per-channel derivation.
2. **`kb:OutboundMessage`** — a sibling record the gateway writes on every
   *successful* send, whatever its path: a user send from the chat composer, a
   push-CLI send, an approved `/sends` item, a companion send under a
   permissive policy. It carries `kb:chat`, `kb:sentAt`, `kb:text`, attachment
   references, `kb:messageId`, and **`kb:author`** — `user` (composed in the
   dashboard), `agent` (with the agent name), or `device` (see next point).
   Queued pending sends are *not* messages and stay out of the store until
   actually sent.
3. **Own-device echoes.** The user's sends from their phone are today dropped
   (explicitly at `whatsapp-gateway.py`'s `is_from_me` check; Signal sync
   messages and Telegram outgoing events are never registered). Each gateway
   captures them as `kb:OutboundMessage` with `author: device`, so the mirror
   equals what the phone shows. This is what makes the chat page trustworthy:
   replying from the real client and replying from the dashboard produce the
   same thread.

`kb:messageId` on inbound records is the shared prerequisite issue #130 already
identifies for reactions and quoted replies; this design consumes the same
plumbing. Reactions, once received (#130), decorate the mirrored message they
target — which answers that epic's open "surfacing shape" question: the chat
page is where a reaction is seen.

The flag stays the gateway's alone, and the chat view is a **pure read** that
never touches it — the same rule the SPARQL browse path already follows. What
*does* flip it changed in phase 4: the usual writer is now the gateway acting
on the rail's acceptance, within seconds of arrival, and the drain and the
forward-confirmation path are what remain for the exceptions. `delivered`
changed meaning with it, from "a model turn accounted for this" to "the user
has this" — see *Inbound flow* below.

### Serving — raw files or the triple store?

Two purely local read paths can back the chat API, and the choice deserves
spelling out. The web-gateway runs in the retinue container, which already
mounts every gateway's message volume at
`/workspace/chambers/_generated/messenger/<channel>/` (retinue writes `policy/`
there today), so it *can* read the message files straight off disk with zero
lag. The same files are indexed by the life store — and since qlever-dir's
incremental updates, a new or edited message file is queryable **within
seconds** (a single-file change is applied straight to the active slot; only
structural changes — a new directory, converter or ignore config — still take
tens of seconds). Either local path beats asking the gateways over HTTP:
history stays browsable while a gateway container is down or unlinked. But
seconds of lag instead of the old tens changes the calculus between the two,
because of what the store buys:

- **The merged view is a query, not a program.** The chats list — every chat
  across every channel with its latest message — is one `GROUP BY`; a thread
  page is a filter on `kb:chat` with `ORDER BY`/`LIMIT`; an unread badge is a
  `COUNT` above the chat's read cut-off (see *Unread · Read · Archived*). The
  raw path re-implements all of that as
  directory scans over one-file-per-message stores that will grow to tens of
  thousands of files, on every dashboard poll.
- **One parser, not two.** `inbound_store`'s deliberately minimal N-Triples
  round-tripper is a *writer's* parser. A raw-reading chat API would have to
  learn every schema addition — `kb:chat`, `kb:author`, and especially #130's
  reactions and quoted replies, which are triples *pointing at other messages*
  and want a join, not a per-request in-memory index. The store reads whatever
  shape the gateways learn to write.
- **Channel-agnostic by construction.** The store unions all graphs: a future
  gateway that writes the same record shape (SMS, say) appears in the chat API
  with zero serving-code change and no new mount to know about.
- **Joins across the life store.** Display names can come from the chambers'
  own contact graphs instead of gateway roster calls; project links join
  naturally; and the chat page, the companion's context builder, and the
  user's ad-hoc "what did X write last week?" all become the *same query* —
  one implementation of "the thread" instead of three.
- **Provenance for free.** Named graphs map every mirrored message back to its
  ledger file — which media references and drain bookkeeping already want.

What the delay costs, and where: precisely at the hottest interactions. A
message arrives, the deterministic push fires, the user taps through within a
second — a store-only view might not yet contain the very message the
notification announced. Likewise one's own send: the bubble must not take
seconds to appear. And a store-only page makes `qlever-life` a serving
dependency of the dashboard, not just of ad-hoc queries.

**Resolution: SPARQL-first, with a deterministic live overlay.** The two
freshness-critical moments are exactly the two paths this design instruments
anyway: arrivals (and own-device echoes) flow through
`POST /internal/chats/inbound`, and sends flow through the web-gateway itself.
Each such event also drops an entry into a small in-memory overlay; the chat
API serves the store's answer plus whatever overlay entries the store has not
caught up to, deduplicated on `kb:messageId`. Entries expire after a minute or
so — by then the store holds them — and the overlay is disposable: a restart
loses nothing but a few seconds of freshness. The message a push announced is
*by construction* in the view the tap opens, because the same event produced
both.

There is deliberately **no second read path**. A raw-scan fallback would be a
second, growing reader of the same records — exactly the duplication choosing
the store avoids — and it would mask store trouble instead of surfacing it.
If `qlever-life` cannot answer, the chat list serves its last good skeleton
for a bounded while (`CHAT_LIST_STALE_SECONDS`, logged each time) and then
answers with an error — the same last-known-state the dashboard components
already show offline, held one hop earlier so a blip during a poll does not
blank the list; no record is ever read another way. That covers a restart or
a brief blip with no new code. A store that
turns out to be frequently down or behind is an infrastructure defect to fix
at the store (it ships a healthcheck and supervision since the #150 bump),
not something each consumer works around — the same stance the framework
takes on messenger link monitoring: one monitor that surfaces the problem,
no ad-hoc per-consumer liveness workarounds.

None of this touches the paths that must stay off SPARQL: the gateways'
classify hot path keeps reading `policy/` raw off their own volumes, and the
`delivered` flag is still mutated by the gateway alone — on the rail's
acceptance since phase 4, on a confirmed forward, or through the drain — never
by a serving read. The delivery-gate doc's freshness reasoning was always about
those paths, not about serving reads. Folder ownership is likewise preserved: gateways own
`messages/` (and the new outbound records), retinue owns `policy/` and the
chat state; the chat API adds no writer to any of it.

Media stays where it is: attachment references are the gateways' token-gated
`GET /media/<id>` URLs, and the web-gateway proxies them behind the dashboard's
own auth (the same shape as the `/gateways/<slug>/qr` proxy), so images render
inline in chat bubbles without exposing gateway tokens to the browser. A blob
comes with the content type its sender declared, so the proxy decides from it
what is served in place and what as a download (`docs/dashboard.md`,
"Attachments").

### Retinue-side chat state

Everything about a chat that is *not* a channel message lives in one JSON
document per chat under `CHAT_STATE_DIR` (default `/root/.retinue/chats/`, the
persistent volume — high-churn disposable data, the news-store precedent):

- `draft` + a version counter (sha-guard style, like project-file writes) — the
  shared text area;
- `last_read` — the user's **read mark**: up to here the chat is dealt with.
  Set by a reply and by *Mark as read*, never by opening the chat; forward
  only, but for the guarded undo of a *Mark as read* (see *Unread · Read ·
  Archived* below);
- `inbound_seq`, `read_inbound` — how many inbound messages have landed on
  the rail, and how many of them the newest read covered. The mark says
  *when*; these say in which order things happened here, which is what
  decides whether a message came after the user last dealt with the chat
  (same section);
- `archived`, `muted` — the conversation model's pair, adopted verbatim so
  both rails share one mental model. A chat never *ends*, but the semantics
  transfer exactly: `archived` drops the chat from the card and the default
  list, and a new inbound message **un-archives** it (the message would
  otherwise be invisible — the same contradiction the conversation flags
  already resolve); `muted` silences Web Push and keeps an archived chat
  archived (the noisy group one keeps visible is muted alone; a done-with
  contact is archived + muted). The standing rule carries over too: the user
  *clicking* Archive leaves `muted` untouched, the user *telling* Ara to
  archive sets both. Deliberately retinue-side only — the phone client's own
  archive state is not mirrored (only Telegram even exposes it, and coupling
  to it would make the three channels behave differently);
- `summary`, `summary_watermark` — the rolling summary and the timestamp up to
  which it has folded both rails;
- `companion` — the companion thread's conversation id, once created;
- display metadata (resolved name, group flag) cached from the gateway.

Single-writer: only the web-gateway writes chat state (user edits and the
token-gated agent endpoints both go through it).

**Deleting a chat** is the one operation that ends it, and it is an erasure,
not a third flag. The ledger stays single-writer: the web-gateway asks every
inbox gateway of the channel to erase the chat (`POST /chats/delete`
`{chat, account}`, token-gated). Each removes the matching records of both
directions from the shared message volume, together with the blobs only those
records reference (`inbound_store.delete_chat`). It also drops its own
traces of the peer: pending-send files and the recent-senders entry. Only
then does the web-gateway delete the state document, the overlay entries and
the companion thread. A gateway that reports anything it could not erase
fails the whole delete, and nothing else is touched. The life store drops the
erased graphs on its next pass, and a short in-memory tombstone hides them
until it has. The tombstone lists the records the gateways erased, by subject
and message id; it is not a time cut-off. A later message from the same peer
finds nothing and starts a new chat, even in the same second. Like a send, a
delete is the user's own act and is accepted only through the reverse proxy,
and never when another site's page made the browser send it (a
`Sec-Fetch-Site` other than `same-origin` or `none` is answered 403, as for a
send and a pending send's approve or reject).
The gateways' erase endpoint also requires `CHAT_ERASE_TOKEN`, a capability
that agent sessions do not inherit (unlike the `*_GATEWAY_TOKEN`s they send
with). Without it configured, deletion is refused. The
dashboard's Archived tab is where the action lives (swipe left → Delete, two
taps).

### Unread · Read · Archived

Every chat is in exactly one of three groups, on every channel:

- **Unread** — its latest inbound message has neither been replied to nor
  marked read.
- **Read** — it has: by a reply the gateway sees, or by **Mark as read**. A
  reply from the dashboard's composer is seen on every channel. One from the
  user's own phone or another client is seen on Signal, WhatsApp and
  Telegram, which echo the user's own messages. **SMS has no such echo** (the
  SMS gateway receives only `sms:received` and records only what it sends
  itself), so an SMS answered in the phone's own SMS app stays *Unread* until
  it is marked read.
- **Archived** — the `archived` flag, as before. Archived wins: a muted chat
  stays there however much arrives.

**Opening a chat does not mark it read.** Looking is not dealing with: a chat
the user glanced at on the way to something else must still be in *Unread*
when they come back. A new inbound message moves a read chat back to *Unread*
(and un-archives it unless it is muted), whatever its timestamp says: it
landed after everything that made the chat read.

**The derivation.** A chat's `unread` is taken in two ways, and is the larger
of the two.

*By timestamp:* the number of inbound messages stamped later than the chat's
*read cut-off*:

```
max( last_read,  the chat's latest outbound message authored `user` or `device` )
```

Both halves are needed. `last_read` is advanced by the paths that see a reply
as it happens — the dashboard send, and an own-device echo on the rail — and
by *Mark as read*. But an outbound record can reach the store without passing
the rail (a backfilled one), so the chat list also asks the ledgers for each
chat's latest `user`/`device` outbound message (`_CHATS_REPLIED_SPARQL`, one
lean query beside the heads and the unread count), and the live overlay
contributes replies the store has not indexed yet. That query is a
refinement: when it fails or runs out of its share of the rebuild's time, the
list is built without it, from the read marks and whatever the last skeleton
knew of the replies. **An agent-authored outbound message is not a reply**:
an agent answering on its own must not hide a message the user never saw, so
`kb:author "agent"` moves nothing, on the rail and in the ledger alike.
Timestamps are whole seconds and the comparison is strict, so in this view an
inbound message stamped in the very second of a reply or a mark counts as
covered by it.

*By arrival order:* the number of inbound messages that have landed since the
newest read. A timestamp says when a message was stamped, and by whose clock
— the sender's on Signal, the receiving phone's on SMS, where a webhook can
also be delayed or retried — so a message can land after a read and be
stamped at or before it: a late delivery, a slow clock, the read's own
second. No comparison of stamps finds such a message. The chat state
therefore counts every arrival on the rail (`inbound_seq`) and records at
each read how many it covered (`read_inbound`); what has been counted since
is unread, wherever its stamp puts it (`chat_state.arrived_since_read`). A
read covers what has landed when it is *Mark as read* with `{}` or `{seen}`,
or the dashboard's own send; an own-device echo and a `{ts}` mark cover it
when their instant is not behind the mark they find — a stale client, or a
late echo of an old reply, says nothing about what landed after a newer read.

The two views count the same messages, hence the larger and not the sum. The
arrival view is used only while the read mark is the newest word about the
chat: where the ledger or the overlay holds a reply of the user's later than
`last_read`, no read was recorded for that reply (it never passed the rail),
so nothing says which arrivals it followed, and the timestamps alone decide.

No stamp from ahead of the gateway's clock enters either view from the rail:
an arrival stamped later than now is taken as now (a message cannot have been
written after it landed), so a peer whose clock runs fast neither heads the
chat until that instant nor sits beyond a mark the user sets today.

`GET /chats` states the result so that no client derives it: every chat
carries `unread`, `list_group` (`"unread"` | `"read"` | `"archived"`),
`last_read` and `inbound_seq`, and the payload carries `counts` — the number
of chats per group. (`group` on a chat is, as it always was, the boolean
"this is a group chat".) The chat summary inside `GET /chats/<id>/messages`
carries the same four fields, so an open chat page shows and clears its own
state from its poll; its `inbound_seq` is read before the page's messages are
collected, so every arrival it counts is among them. With the store away the
list is served from its last skeleton, corrected by the marks written since:
a chat read past its head message counts nothing by timestamp, and one whose
head is an inbound message later than its cut-off counts at least that one.
The arrival count is in the chat state and needs no store; where no list can
be built at all, it is what the read endpoint answers with.

**Mark as read** is `POST /chats/<id>/read`:

- Body `{"seen": n}` is what both dashboard clients send: `n` is the
  `inbound_seq` of the summary they rendered — the list's row, or on the chat
  page the last poll whose messages are in the thread. If the chat's count
  has moved past it, a message has landed that the user was never shown:
  nothing is marked, and the answer is **409** with `{error, id, last_read,
  unread, list_group}` as they stand. The client then shows the message (the
  list is fetched again, the chat page polls) and the user marks what they
  now see. Otherwise the chat is read as it stands. The check and the mark
  are one decision under the state's lock
  (`ChatStateStore.mark_read_unless_arrived`), and an arrival counts itself
  under the same lock: it is either seen by the check or lands after the
  mark, and is then unread by its place in the count.
- Body `{}` is the same without the check, for a caller that vouches for
  itself. `{"ts": …}` reads only up to that instant: what is stamped later
  stays unread, and the answer says so. `seen` and `ts` may be combined.
- The mark is an instant of the gateway's own clock: now, or `ts` where that
  is earlier. It is never later, and never a message's stamp — a mark lifted
  to a stamp from a fast clock would count as read whatever arrives before
  the gateway's clock catches up. A `ts` that names no real instant (30
  February, an epoch no calendar holds) or one more than two minutes ahead
  (`CHAT_READ_SKEW_SECONDS`) is answered 400; within that reach it is a fast
  clock's "now", and now is where the mark goes. One case is left to the
  stamps: a *ledger record* stamped ahead of the gateway's clock (a phone
  whose clock runs fast, on SMS) is not read by a mark made before the
  clock has passed that stamp. It stays unread, the answer's `unread` says
  so, and a mark made later reads it.
- The answer is `{id, last_read, previous, unread, list_group}`; `previous`
  is the mark before the call (null for none). `unread` and `list_group` are
  the chat's state right after the mark, as `GET /chats` would state it —
  normally 0 and `read`, but a client takes both from the answer rather than
  assuming them.
- Body `{"restore": <that previous, or null>, "marked": <that last_read>}` is
  the **undo**: the mark goes back to `restore` only while it is still exactly
  `marked`. A reply or a later mark in between is newer word about the chat,
  and the undo then answers 409 with the state as it stands and changes
  nothing. This is the one way `last_read` moves backwards
  (`ChatStateStore.restore_last_read`), and it can never move it past
  `marked`. `read_inbound` goes back with it, to what the mark being undone
  found, so the messages that had landed unread before the mark are unread
  again, with any that came since. It cannot make an answered chat unread:
  the reply stands in the ledger whatever the mark says, and where a reply
  fell into the mark's own second its cover of what had landed stands too.
  Both timestamps are parsed as strictly as `ts`.

**Attention.** Marking read is the user saying "dealt with", so it settles the
chat's attention item exactly as *Mark handled* does (`done_how: "marked"`) —
when the mark leaves nothing unread and nothing has landed since it was
taken; a mark that a newer message outran settles nothing, and a refused one
(409) touches nothing at all. The undo reopens the item only if that mark is
what settled it, the mirror of un-archiving: the mark records the `done_at`
of the item it closed (`read_settled` in the chat state), and an item that a
new message reopened and something else settled since carries another. The
reverse does not hold: *Mark handled* on the home screen settles the item and
leaves the chat's read state alone.

**An instructed sender's held ring** (see *Inbound flow*) is dropped when the
user has dealt with the chat while the turn worked. With no read-on-open that
means a reply or an explicit mark: a message merely looked at still rings by
the verdict, and so does one whose mark the user took back before the ring
was due, or whose mark was refused because the page had not shown the
message. A mark taken back *after* the turn ended — the message was delivered
as seen, and never listed — puts the message on the attention list, with the
verdict's judgement and without a ring.

## The chat surface (UI)

- The **Chats page** (`chats.html`, a tab of the navigation row) is the
  messenger home screen: every chat of every channel, ordered by last
  activity, with avatar, channel glyph, last-message preview (a staged draft
  outranks it) and an unread badge (how many messages have arrived since the
  user last dealt with the chat). The dashboard home carries no Chats card:
  chats that want attention are rows of its attention list, and the
  component's capped card mode (every non-archived chat, five rows plus
  "All chats →", the `layout.js` splitter region) is unused. Pin-to-top, as
  the clients have it, is the `starred` flag the chat state carries (shared
  with the dashboard threads): a starred chat leads whichever list it is in.
  The page sorts every chat into **Unread · Read · Archived** (see that
  section above; the *Unread* tab carries the number of unread chats, its
  rows the count of unread messages). A swipe right marks an unread chat
  read, with an Undo; a swipe left archives it. The row's tray (long press,
  right-click or the context-menu key) holds star, mark read (*Unread* only),
  archive and mute — so a keyboard reaches what a swipe does — and on
  *Archived* star, restore, mute or unmute, and delete. Each chat has its own
  page/panel, where **Mark as read** sits under the latest inbound message.
- The **chat pane** renders like a messenger client: bubbles left/right, day
  separators, sender labels in groups, media inline. Outbound bubbles carry
  their author — the user, *Ara* (agent-sent), or *phone* (own-device echo) — a
  distinction the real clients cannot even show.
- The **composer**: text area, send button, and — the one deliberate
  divergence from the clients — a **clear (✕) button** beside send that empties
  the text area (and the server-side draft) in one tap. Voice input reuses the
  dashboard's existing STT + cleanup pass verbatim. Sending is **direct**: no
  approval step, ever, for what the user typed (see *Trust model*).
- The **companion pane** sits beside the chat: on the wide layout a second
  column behind a `layout.js` splitter; on the phone a horizontal swipe/drag
  (or a two-tab header) between *Chat* and *Ara*. The pane is the existing
  conversation thread UI, unchanged — model picker, pending indicator, chips.
- **Quick patterns** are chips above the composer — *Proofread*, *Translate*,
  *Draft reply*, *Summarize*, *Shorten* — each a canned prompt over the current
  draft. Tapping one switches to the companion pane with that prompt already
  running as the user's turn; the companion applies the result to the draft and
  answers in one line. No new machinery: a chip is a pre-filled companion turn.
  While that turn is on its way it shows in the pane as a *Sending* bubble (it
  is in no composer to see), and the same chip pressed again sends nothing. A
  send the gateway does not answer within its deadline is given up: the pane
  unlocks with the turn in its box and a note that it may still turn up —
  unless a re-read of the thread finds it stored after all.

## The companion thread

One per chat, created lazily on first use, kind **`companion`** — hidden from
every default conversation listing exactly like `edit` threads, reachable only
through its chat (and an *Edits*-style filter on the all-conversations page).
It is an ordinary conversation otherwise: per-thread model choice, resumed
session (`conv:<cid>` key, the week-long idle window), Web Push on its agent
turns, `dashboard-composing` rules for its replies.

**What it can do.** Read everything (the engage prompt injects chat context —
see *Context* below — and SPARQL covers anything older); **stage the draft**
(`POST /internal/chats/<id>/draft`, with a thin `chat-draft.py` CLI, token-gated
like `conversation-push.py`); **send** only as the channel's policy allows (see
*Trust model*); **fork** a topic into a normal conversation; **link projects**
exactly as triage does today. Before composing any draft it reads the Secretary
persona plus the chambers' style overrides — the existing per-action compose
rule, unchanged.

**What triggers a companion turn** (this is the whole "lightweight" point):

- the user writes in the companion pane, or taps a quick pattern;
- an inbound message the delivery gate forwards (see *Inbound flow*);
- nothing else. The user's own sends, draft edits, read events and scrolling
  never spend a turn.

**Context.** The companion's engage prompt is built from bounded parts, not
from an ever-growing transcript:

```
[Chat: Signal, 1:1 with Mara Meier (+41 79 …).
 Summary so far (updated 24.08.): …
 Messages since the summary, oldest first:
   24.08 14:02  Mara: …
   24.08 14:05  user (phone): …
 Current draft: «…» / (empty)]
+ the companion thread's own unseen tail (the a95b19c mechanism, unchanged)
```

A fresh or restarted session replays exactly this — summary + tails — never the
full chat or the full companion transcript. That closes the unbounded-time
problem for good: however old the chat, an engage prompt has a fixed ceiling.

## Inbound flow — where triage changes

The **delivery gate is untouched**: same policy axes, same classes, same
never-drop persist-before-anything, same news rail, same daily drain, same
at-least-once delivery confirmation. What changes is where a message *lands*
and what a model turn is *for*:

1. **Persist** (as today, now with `kb:chat` + `kb:messageId`).
2. **Gate** (as today).
3. **Notify — deterministically.** The gateway POSTs the message's metadata to
   a new `POST /internal/chats/inbound` on the web-gateway (the news-rail
   shape: fail-safe, `*_INGEST`-style token optional). The web-gateway updates
   the chat's index entry and fans out a Web Push — title = chat name, body =
   preview, tap-through = the chat — honouring the per-chat `muted` flag
   and the gate class (an `ignored`-group or no-action-class message updates
   the mirror silently). **Notification no longer costs a model turn.**
4. **Accepted by the chat — and, where the user asked for it, worked.** The
   same web-gateway call now *accepts* the message: the chat has it, the user
   is pushed, and the gateway forwards it nowhere else. That is the delivery,
   and it costs no model turn at all.

   Whether a **turn** also runs is one question, and the answer is the
   **sender**: their `vip` flag in the triage policy. Sender-only and
   group-independent — a VIP writing in a room of forty is still the person the
   user wanted to hear from, so the same message gets the same handling
   wherever it arrives. Nothing about a chat grants it.

   This replaced the sender whitelist, and the reason is worth stating plainly:
   the whitelist decided whose message was worth a session to *notify* about,
   and notification is free now. What a turn buys is that the message is
   **dealt with** — and there is no unknown-sender ask-flow any more, because
   nothing needs the answer.

   The **blacklist** went the same way, for the mirror-image reason: it said
   "do not bother me about this person", which is muting their chat. That is a
   button in the dashboard now, on the chat the user is looking at — **Archive**
   (out of the list until it speaks again) and **Mute** (out of the list, and
   the next message does not bring it back; muting archives too), exactly as a
   conversation behaves. Ara can set either on request; the user never has to
   go through her. A blacklist entry that mattered should be re-stated as a
   muted chat — the old triples are inert.

   For a VIP the turn starts in that chat's companion thread instead of a fresh
   `claude -p` triage session, and its job is wider than a draft: read the
   message in context, **file what it changes** (link and update a project,
   store a memory where the rest of the system should know what the user was
   just told), stage a reply when one is wanted, and fork to a dashboard
   conversation only for a decision that is not "send this reply". Filing is
   worth doing even when no reply is, and a bare "thanks!" earns no draft: an
   unwanted one costs the user more than a missing one, since they have to
   read it to discard it. The turn runs warm (per-chat session, summary +
   tail — the generalization of the `a95b19c` fix from "the thread's own
   appends" to "the channel itself"), and the job-status contract (`202` +
   `job_url`) is kept so the gateway's `confirm_delivery` / never-drop
   machinery works unchanged.

   A sender can also carry the user's **standing instruction**
   (docs/contacts.md, "The instruction"; the gate's `instructed` flag). Their
   message buys the same companion turn, briefed by the instruction, and the
   turn ends with a verdict (`chat-verdict.py`): an importance and, where the
   message has one, a deadline. For a sender who is not also a VIP the arrival
   pushes nothing; once the turn ends, the attention model decides from the
   verdict whether the message rings, so a draft the instruction asked for is
   already in the composer when the push opens the chat. A turn that fails,
   sets no verdict or runs past `CHAT_INSTRUCTION_TIMEOUT` rings the message as
   a VIP's would be. A VIP with an instruction rings on arrival, as ever, and
   the same single turn works the message as instructed; the chat page shows
   that a draft is on its way, then the verdict and what the turn flagged.
   A message the user answered or marked read while its turn worked is not
   rung afterwards; opening the chat is neither (*Unread · Read · Archived*).

   As built, five things are worth naming:

   - **The gateway asks, it does not assume.** It calls the rail
     synchronously for a forwarded message and reads the answer. A job handle
     means the chat took it, and nothing is posted to triage. **No** handle —
     `CHAT_ARRIVAL_TURNS=0`, an unreachable web-gateway, a companion thread
     that could not be opened — and the gateway forwards to triage exactly as
     it always has. The switch degrades to the old path, never to a message
     nothing looks at, and it is reversible without a revert.
   - **Every arrival is accepted; only a VIP's is a turn.** Acceptance is the
     answer to *do I still have to do something with this?* — no, the chat
     has it — and it is what the gateway marks delivered on. A job handle
     comes with it only for a VIP, and only then does the gateway wait for the
     job. Messages landing while a chat's turn runs are folded into one
     follow-up turn: a turn reads the chat as it stands, so a second one would
     re-read the first's messages and the two would fight over the draft. Each
     of those still gets its own handle and the covering turn resolves all of
     them, so `delivered` is never set for a VIP's message no turn saw.
   - **What the message carried travels with it.** Attachments ride the rail in
     the `POST /message` shape and are materialized in the retinue container,
     so a turn opens the photo rather than answering one it never saw. The
     note that tells the turn about them names each file by the path the
     gateway gave it, its type and its size, never by the sender's file name.
     The type is the one the sender's client declared, so it is kept only as
     the bare `type/subtype` of one media type, without parameters, and as
     `application/octet-stream` otherwise: text from outside never reads as
     the framework's own words in a prompt. The chat note every companion
     turn carries names the types of the recent messages' attachments under
     the same rule: the chat listing keeps a stated type only as the essence
     of one media type, the note reads it once more the same way, and a type
     that is none is left out.
   - **One message, one notification.** The arrival's own Web Push already
     fired, deterministically and at once; the turn's note lands in the
     companion thread without pushing again.
   - **`muted` and `archived` are not consulted.** They say where the user
     wants a chat on their screen; the gate says what a message is worth. The
     two are independent on purpose, and hiding a chat must not quietly stop
     its messages being worked.
5. **The drain is a recovery path, not a daily sweep.** Nothing is left
   undelivered on the normal path any more: the chat's acceptance is what
   marks a message delivered, and every message is offered. What still leaves
   `delivered=false` is the handful of cases where this rail did *not* take
   the message — a refusal, an answer lost in flight, a VIP's turn that failed
   — and `GET /undelivered` is how those are re-surfaced, into triage as
   before. So the endpoint stays and matters; it is simply no longer a daily
   pass over a backlog that exists by design. Having it walk the affected
   companions instead of opening dashboard conversations is the obvious next
   step, and is **not built**.

The simple case end-to-end: message arrives → push notification → open the
chat → read it *in the conversation it belongs to* → type (or touch up the
staged draft) → send. **Zero model turns** when the user answers themselves;
one warm companion turn when a draft was staged — strictly less than today's
cost for a strictly better result.

Control-mode accounts are out of scope: their messages are prompts, not mail,
and keep today's ask-and-answer path.

## Summaries — unbounded time, bounded context

One rolling summary per chat, covering **both rails and the actions taken**
(channel messages, companion discussion, sends, forks) — they are one story,
and a summary that omitted the agent's part would make the companion re-derive
its own past positions.

- **Deterministic trigger, zero idle credits.** The web-gateway tracks the
  unsummarized volume (characters since `summary_watermark`, both rails). Only
  when it crosses `CHAT_SUMMARY_THRESHOLD_CHARS` does one summarizer turn run —
  a cheap model (`CHAT_SUMMARY_MODEL`, default `haiku`; the
  `TRANSCRIPT_CLEANUP_MODEL` precedent) folding the overflow into the summary
  and advancing the watermark. A quiet chat is never summarized; nothing ever
  polls a model.
- **The summary is the user's too.** It is shown on the chat page (behind a
  disclosure) and editable — the Herald's `preferences.md` pattern: prose
  memory the user can correct.
- Companion engage prompts and session restarts read summary + tails only
  (previous section), so the summary is precisely what keeps a five-year chat
  as cheap to engage as a five-day one.

## Forking

A **fork** turns a tangent into a first-class conversation without dragging the
chat along: a normal `kind: chat`* conversation, seeded with the chat summary
plus the selected (or recent) messages, carrying a `chat: <chat-id>` link. It
appears in the normal conversation list *and* on its chat's page (the
project-page pattern for linked threads). Both sides can fork: the user via a
chat-page action ("Discuss with Ara" on a message or the thread), the companion
via `conversation-push.py --origin-chat <id>` when escalation is warranted
(step 4 above). The companion pane stays what it is for — composing into
*this* chat.

\* The existing kind literal `"chat"` (= a normal dashboard thread) collides
verbally with the new entity. Code keeps the literal for compatibility; prose
and UI say **conversation** for threads and **chat** for the mirror. A later
rename of the literal (`"thread"`) is cosmetic and out of scope.

## Trust model — what `verify` means here

The send policies (`SIGNAL_SEND_POLICY` / `WHATSAPP_SEND_POLICY` /
`TELEGRAM_SEND_POLICY`, keyed by sending identity) keep their exact meaning;
the chat surface maps onto them rather than around them:

- **The user's send button is the approval.** `verify` exists to put the
  user's decision between agent-composed content and the wire. Approving a pending
  send at `/sends` and pressing send on a draft in the authenticated dashboard
  are the *same act* — the second with more context (the whole thread is on
  screen) and fewer steps. The chat send endpoint sits behind the dashboard's
  edge auth, the gateway records `author: user`, and no policy category queues
  it.
- **The companion under `verify`** cannot send. Its send attempt *degrades to
  staging the draft* (plus a notification) — the draft area **replaces
  `/sends` for chat-scoped agent sends**, with the user's send tap as the
  approval. `/sends` remains for non-chat pushes (briefings, alerts, jobs).
- **Under `trust`**, the companion sends directly when the user has told it to
  in the conversation ("send it") — the existing `--user-approved` assertion,
  same semantics, now with the instruction and the send visible in one pane.
  **Under `allow`** (a dedicated agent identity), the companion may send on its
  own judgement; the bubble says Ara sent it.
- Honesty note, unchanged from today: inside the container there is no hard
  privilege boundary between agents and the approval endpoints — the guarantee
  has always been convention plus audit (single writers, logged origins,
  policies enforced at the gateways). This design narrows the surface (one
  user-send path, origins recorded on every outbound message) and adds no new
  bypass.

## What this replaces, what it keeps

**Replaced:** per-message dashboard conversations for messenger traffic; the
"did triage find the right existing thread" heuristic; `/sends` approvals for
in-chat replies; the model turn spent on mere notification; the `messages.js`
mock card.

**Kept unchanged:** the delivery gate and its policy files; the never-drop
ledger and `delivered` semantics; the news rail; the daily drain; reply tokens
and the push CLIs (still the right tool for headless sends and escalations);
the conversation concept, dashboard, and push machinery for everything that is
not a messenger chat; e-mail triage in full (pull channel, own flow — it can
adopt this pattern later, but nothing here depends on it).

**Converges with:** issue #130 — the message-id plumbing is shared, and the
chat page is the missing answer to where reactions and quoted replies surface.

## Phases

Each phase is independently shippable and useful; all are Tier 3 (gateway
serving logic, `webapp/`, `scripts/`). Phases 1–4 have shipped.

1. ✅ **Complete the ledger** (gateways ×3): `kb:chat` + `kb:messageId` on
   inbound; `kb:OutboundMessage` on every successful send; own-device echo
   capture. Pure plumbing, also unblocks #130.
2. ✅ **Read-only chats:** web-gateway chat API (SPARQL + live overlay); Chats
   card + chat page; `last_read`/unread;
   `POST /internal/chats/inbound` with deterministic Web Push; media proxy.
   Triage still runs as today — the value is *seeing whole conversations at
   last*.
3. ✅ **The composer:** draft store, direct user send (`author: user`), the ✕
   button, voice input. From here the user can answer any message with zero
   model turns.
4. ✅ **The companion:** kind `companion`, the pane, engage-prompt builder,
   `chat-draft.py`, policy-mapped send, quick-pattern chips; the gate's
   forward path redirected from triage-prompt sessions to companion turns.
   Per-message conversation spam ends here.
5. **Summaries & forking:** the summarizer job, summary on the chat page,
   fork actions on both sides, `--origin-chat`.
6. **Polish:** reactions & quoted replies in the UI (#130), archive/mute
   controls on the chat page (✅), a per-chat assist setting, ✅ `starred`
   (pin-to-top, shared with the threads), group niceties.

## Open questions

1. ~~**Companion turns for groups.**~~ **Moot: it follows the sender.**
   The question assumed turns happen by default and asked which chats to spare.
   They do not: a turn runs for a **VIP sender** and nobody else, so a busy
   group costs exactly as many turns as it has VIPs writing in it — usually
   none. And a VIP is worked in a group precisely as in a 1:1, which is the
   point: the user's interest is in a person, and a chat is only a place. That
   retires the messenger sender whitelist and blacklist — see *Inbound flow*,
   step 4 — and with them the unknown-sender ask-flow. The group flags stay:
   the news rail reads them, and `quieted` / `ignored` still keep a group's
   arrivals quiet.
2. **Draft staging threshold.** "Stage a draft when a reply is plausibly
   wanted" is the companion's judgement; if it over-stages, a per-chat or
   per-sender preference belongs in the summary/style memory, not in code.
3. **Summary tunables.** Threshold (`CHAT_SUMMARY_THRESHOLD_CHARS`), tail
   length, and whether very active chats want time-based flushing too.
4. **Pending sends in the mirror.** A `verify`-queued push-CLI send targeting a
   chat is invisible on the chat page until approved; showing it as a
   pending bubble (approvable in place) would fold `/sends` into the chat for
   those too. Deferred to phase 6.
5. **SMS.** Resolved: `sms-gateway` (opt-in, `sms` compose profile) writes
   the same store shape, so SMS chats appear here with no serving-code change —
   as this design predicted. See `docs/messaging.md`, "SMS".
