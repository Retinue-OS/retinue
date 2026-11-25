# Chat triage — clearing the Chats page on one press

## Problem

The delivery gate spends a model turn only on a VIP's or an instructed
sender's message ([messenger-chats.md](messenger-chats.md), *Inbound flow*;
[mail-chats.md](mail-chats.md), *The gate*). Every other message lands in
its chat and nobody judges it. That is cheap, and it leaves the *Unread* tab
long: newsletters are filed as news already, but invoices from no-reply
addresses, receipts, account notices, a friend's "thanks!" and advertising
all sit side by side, and the user has to open each one to decide.

Judging each message as it arrives would undo the gate's economics. Triage
judges **on the user's press, many chats per model call**: one fast pass over
the whole backlog, which puts each chat in one of three lists, and a
higher-tier look only at the chats that need the user.

## What the user sees

The Chats page carries a **Triage** button beside its tabs. Pressing it opens
the triage view and starts a run:

| Section | What it holds | Section action |
|---|---|---|
| **Action suggested — see Ara** | the user should do or decide something | — (each row opens the chat on Ara's pane) |
| **No action — proposed archive** | nothing to do, worth keeping | **Archive all** (one Undo for the section) |
| **No action — proposed delete** | nothing to do, nothing to keep | **Delete all** (two taps, no Undo; mails move to the Trash) |
| **Not sorted** | no word yet, a failed sorting, or a message newer than the sorting | — (the next press sorts them) |

Every row is the Chats list's own row: tapping opens the chat, the swipes and
the tray (mark read, archive, mute, spam, delete) do what they do on the
chat's tab. Under the row stands the sorting's one-line note ("Invoice from
the plumber, CHF 320, due 30 Nov — pay it."), and on an action chat Ara's own
note once her follow-up lands: *Ara is looking at it…* until then. An action
row opens the chat on its companion pane (`chat.html?id=…&pane=ara`), where
the note is in full and a staged reply waits in the composer.

The lists are there as soon as the fast pass has answered — a minute or two
for several dozen chats — while Ara's notes are still being written; the
status line says how many are still on their way. A chat archived or deleted
leaves the view; back (or *‹ Chats*) returns to the tabs.

Triage never acts on a chat by itself. Archive and delete are the user's
presses; the follow-up turn is told to send nothing, change no flag and queue
nothing.

## When it runs

**Only on the press** — there is no schedule and no arrival hook. Opening
the view is the press: `POST /chats/triage {}` sorts every chat on the
*Unread* tab that has no fresh word — a chat sorted before and not spoken in
since (same `inbound_seq`) is not sorted again, so pressing twice spends
nothing the second time. **Sort again** (`{again: true}`) sorts them all.
One run at a time; a press while one runs shows that run.

Why only the *Unread* tab: that is the backlog. A read chat triage sorted
before stays in its section until it is archived, deleted or spoken in; a
read chat it never sorted is not shown.

## How it works

```
press ─► POST /chats/triage
           │  scope: the Unread tab, minus fresh results
           ▼
         batches of CHAT_TRIAGE_BATCH (12) chats ──► fast model, tool-less,
           │  CHAT_TRIAGE_CONCURRENCY (4) at once      one JSON answer a batch
           ▼
         per chat: {bucket, note, reply, seq}  ──► triage.json (chat state dir)
           │
           └─ bucket = action ─► follow-up: a companion turn on the
                                 frontier tier (CHAT_TRIAGE_FOLLOWUP_CONCURRENCY
                                 at once, CHAT_TRIAGE_FOLLOWUPS a run)
                                 → Ara's note, a staged reply where wanted
```

The parts are `scripts/chat_triage.py` (prompt, answer, store, run — no I/O
of its own, tested without a model) and the glue in `scripts/web-gateway.py`
(`_triage_*`): the scope, each chat's digest, the spawn, the follow-up.

### The sorting pass

One `claude -p` per batch, spawned like the mail trim's turn: **no tools, no
MCP server, no session kept, away from `/workspace`**, the allowlisted
environment. Everything in its prompt but the fixed instruction is a
correspondent's, so it has nothing to act with: at worst a message mis-sorts
its own chat, and the user sees that row.

Each chat is one numbered `<chat>` element — its channel, kind (chat, group
chat, mail thread) and name as escaped attributes, its messages as escaped
`<msg at from>` elements: the user's own last message and everything since,
else the last three; at most six messages, 700 characters each, 2400 a chat,
with what was cut said. The instruction defines the buckets (automated
senders can need action: a tax reminder from a no-reply address is action),
says to prefer *action* when unsure between it and the others and *archive*
when unsure between archive and delete, and asks for a one-sentence note in
the chat's own language — the mechanism is language-agnostic.

The answer must be one JSON object, `{"chats": [{"n", "bucket", "note",
"reply"}]}`, optionally fenced. An entry for a chat outside the batch, an
unknown bucket, or a chat named twice is dropped; a chat the answer leaves
out, or whose batch failed, is stored **unsorted** with the reason — never
guessed.

The model is `CHAT_TRIAGE_MODEL`, else the router tier
(`RETINUE_ROUTER_MODEL`), else `haiku`: fast enough that several dozen chats
come back in a couple of minutes.

### The follow-up

Each chat sorted *action* is handed, as soon as its batch answers, to one
turn in that chat's companion thread (created if it has none) on
`CHAT_TRIAGE_FOLLOWUP_MODEL`, else the frontier tier
(`RETINUE_FRONTIER_MODEL`), else the companion's own model. It is the
companion's *Propose* chip run for the user, with the sorting's note quoted
as data: read the chat, tell the user in two or three sentences what to do
(or that the note was wrong), have the `secretary` write a reply and stage it
in the draft where one is wanted — and nothing else: no send, no flag, no
calendar entry, nothing queued; such steps are offered as chips. The turn
carries no user-turn proof and may not type on a paired device (it is an
arrival-style turn, `_conv_worker(arrival=…)`), and it pushes nothing: the
triage view shows it. Its reply, as words, is copied onto the triage entry
(600 characters; the full note is in the companion).

A follow-up is a full-tool turn on a correspondent's words — the same
exposure as the user tapping *Propose* on that chat, which is what the press
stands for. `CHAT_TRIAGE_FOLLOWUPS` (20) bounds how many one press buys; the
rest are marked *skipped* and say so on the row.

### The store

`<CHAT_STATE_DIR>/triage.json`: the last run (`state`: running, done,
failed, interrupted; `total`, `sorted`, `failed`, times) and per chat
`{bucket, note, reply, seq, at, run, error?, followup?: {state, text?}}`.
Atomic writes; a run or follow-up a dead process left running reads as
interrupted; entries of chats that are gone are pruned on the next press. A
follow-up writes only onto the entry of its own run.

## API

- `GET /chats/triage` → `{enabled, running, model, run, entries}`. Which
  entries are shown is the page's to decide against its list (archived chats
  and chats with a newer `inbound_seq` are not in a bucket).
- `POST /chats/triage` (body `{again?}`) → `202 {started: true, total,
  skipped, …payload}` for a run started (`total` 0: nothing new to sort),
  `200 {started: false, running: true, …}` while one runs, `409` with
  `CHAT_TRIAGE` off, `502` with the life store down, `403` cross-site (the
  press spends model calls on the user's behalf).

## Configuration

| Variable | Default | |
|---|---|---|
| `CHAT_TRIAGE` | `1` | `0`/`false`/`no`/`off` switches the button's endpoint off |
| `CHAT_TRIAGE_MODEL` | router tier, else `haiku` | the sorting pass |
| `CHAT_TRIAGE_FOLLOWUP_MODEL` | frontier tier, else the companion's | the follow-up turns |
| `CHAT_TRIAGE_BATCH` | `12` | chats per call |
| `CHAT_TRIAGE_CONCURRENCY` | `4` | calls at once |
| `CHAT_TRIAGE_TIMEOUT` | `180` | seconds a call may take |
| `CHAT_TRIAGE_FOLLOWUPS` | `20` | follow-ups one press buys |
| `CHAT_TRIAGE_FOLLOWUP_CONCURRENCY` | `2` | follow-ups at once |

## Not built

- **Showing Ara's suggestions outside the view.** A chat where Ara wrote
  something — a triage follow-up, an instructed turn's flags — is marked in
  the Chats list only by a staged draft's preview. Surfacing her note on
  the list and on the home's attention row, for archived chats too, is the
  next design ([dashboard.md](dashboard.md), the instruction line).
- Moving a row to another section by hand, and learning from it.
- Triage of the *Read* tab.

## Tests

`tests/test_chat_triage.py` (prompt, answer, store, run),
`tests/test_chat_triage_gateway.py` (the endpoints over the real handler:
scope, spawn flags, follow-up model and companion, freshness, `again`, the
cross-site and off refusals), `tests/test_webapp_chat_triage.py` (the view).
