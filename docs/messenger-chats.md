# Messenger chats — a deterministic chat surface with an agent at its side

**Status: design proposal.** Nothing below is implemented yet; this document is
the target picture and the migration path. It supersedes the "one dashboard
conversation per inbound message" model for messenger channels.

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
- **Notification costs a model turn.** A whitelisted sender's message spends a
  full triage session just to tell the user it arrived.
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

The ledger semantics are untouched: `kb:delivered` remains triage bookkeeping,
owned by the gateway, flipped only by the drain and the forward-confirmation
path. The chat view is a **pure read** and never touches the flag — the same
rule the SPARQL browse path already follows.

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
  `COUNT` above `last_read`. The raw path re-implements all of that as
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
both. Should the store be unreachable, the API degrades to a raw directory
scan — the LiteLLM-to-static fallback pattern the model picker already uses.

None of this touches the paths that must stay off SPARQL: the gateways'
classify hot path keeps reading `policy/` raw off their own volumes, and the
`delivered` flag is still mutated only through the gateway drain — the
delivery-gate doc's freshness reasoning was always about those, not about
serving reads. Folder ownership is likewise preserved: gateways own
`messages/` (and the new outbound records), retinue owns `policy/` and the
chat state; the chat API adds no writer to any of it.

Media stays where it is: attachment references are the gateways' token-gated
`GET /media/<id>` URLs, and the web-gateway proxies them behind the dashboard's
own auth (the same shape as the `/gateways/<slug>/qr` proxy), so images render
inline in chat bubbles without exposing gateway tokens to the browser.

### Retinue-side chat state

Everything about a chat that is *not* a channel message lives in one JSON
document per chat under `CHAT_STATE_DIR` (default `/root/.retinue/chats/`, the
persistent volume — high-churn disposable data, the news-store precedent):

- `draft` + a version counter (sha-guard style, like project-file writes) — the
  shared text area;
- `last_read` — the user's read watermark, from which unread badges derive;
- `notify` — per-chat notification setting (`all` | `none`), the chat-level
  mute (groups want this);
- `summary`, `summary_watermark` — the rolling summary and the timestamp up to
  which it has folded both rails;
- `companion` — the companion thread's conversation id, once created;
- display metadata (resolved name, group flag) cached from the gateway.

Single-writer: only the web-gateway writes chat state (user edits and the
token-gated agent endpoints both go through it).

## The chat surface (UI)

- A **Chats card** on the dashboard (finally replacing the static
  `messages.js` mock): the messenger home screen — chats ordered by last
  activity, unread badges, last-message preview, channel glyph. A dedicated
  `chats.html` lists all chats; each chat has its own page/panel.
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
   preview, tap-through = the chat — honouring the per-chat `notify` setting
   and the gate class (an `ignored`-group or no-action-class message updates
   the mirror silently). **Notification no longer costs a model turn.**
4. **Forward — as a companion turn.** Where the gate says `forward`, the same
   web-gateway call starts a turn in that chat's companion thread instead of a
   fresh `claude -p` triage session opening a new dashboard conversation. The
   turn runs warm (per-chat session, summary + tail — the generalization of
   the `a95b19c` fix from "the thread's own appends" to "the channel
   itself"). Its job, in order: read the message in context; stage a draft
   reply *when a reply is plausibly wanted* (a bare "thanks!" stages nothing);
   link the message to a project if substantive; and only when something
   needs a decision that is not "send this reply" — the unknown-sender
   ask-flow, an action item, a scheduling conflict — **fork** it into a
   normal dashboard conversation. The job-status contract (`202` + `job_url`)
   is kept so the gateway's `confirm_delivery` / never-drop machinery works
   unchanged.
5. **Daily drain** — unchanged mechanics; the drained messages are already in
   their chats' mirrors, and the drain turn walks the affected companions
   instead of opening per-message threads.

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
serving logic, `webapp/`, `scripts/`).

1. **Complete the ledger** (gateways ×3): `kb:chat` + `kb:messageId` on
   inbound; `kb:OutboundMessage` on every successful send; own-device echo
   capture. Pure plumbing, also unblocks #130.
2. **Read-only chats:** web-gateway chat API (SPARQL-first with the live
   overlay; raw-scan fallback); Chats card + chat page; `last_read`/unread;
   `POST /internal/chats/inbound` with deterministic Web Push; media proxy.
   Triage still runs as today — the value is *seeing whole conversations at
   last*.
3. **The composer:** draft store, direct user send (`author: user`), the ✕
   button, voice input. From here the user can answer any message with zero
   model turns.
4. **The companion:** kind `companion`, the pane, engage-prompt builder,
   `chat-draft.py`, policy-mapped send, quick-pattern chips; the gate's
   forward path redirected from triage-prompt sessions to companion turns.
   Per-message conversation spam ends here.
5. **Summaries & forking:** the summarizer job, summary on the chat page,
   fork actions on both sides, `--origin-chat`.
6. **Polish:** reactions & quoted replies in the UI (#130), per-chat `notify`
   and assist settings surfaced, group niceties.

## Open questions

1. **Companion turns for groups.** 1:1 chats keep gate parity (a forwarded
   message = one warm companion turn). Busy groups would burn turns on chatter;
   proposal: groups default to notification-only (companion strictly
   on-demand), revisit per-chat once an `assist` setting exists.
2. **Draft staging threshold.** "Stage a draft when a reply is plausibly
   wanted" is the companion's judgement; if it over-stages, a per-chat or
   per-sender preference belongs in the summary/style memory, not in code.
3. **Summary tunables.** Threshold (`CHAT_SUMMARY_THRESHOLD_CHARS`), tail
   length, and whether very active chats want time-based flushing too.
4. **Pending sends in the mirror.** A `verify`-queued push-CLI send targeting a
   chat is invisible on the chat page until approved; showing it as a
   pending bubble (approvable in place) would fold `/sends` into the chat for
   those too. Deferred to phase 6.
5. **SMS.** The triage skill names SMS as a channel; no SMS gateway exists.
   The design is channel-agnostic (a future gateway that writes the same store
   shape gets a chat page for free), but SMS itself stays out of scope.
