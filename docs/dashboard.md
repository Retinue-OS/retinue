# Dashboard (PWA) — architecture and mechanics

*Reference depth for the "Dashboard" digest in `CLAUDE.md`. Read this before
changing the webapp or the gateway's serving logic (both Tier 3), working with
attachments or push notifications, or debugging voice input. How to *compose*
for the dashboard is the `dashboard-composing` skill.*

## The webapp

A minimalist, curated phone dashboard is served by `scripts/web-gateway.py` at
the **site root** (`/`) of the gateway (`agents.example.com`, behind
Traefik basic auth) and is installable as a Progressive Web App. The front-end
lives in `webapp/` (baked into the image):

- `webapp/index.html` is the hand-editable shell/config — which cards show.
  The app-launch dock that sat under the list (phone, SMS, mail, map) is gone:
  those are OS actions, not Retinue, and live in the Android shell's launcher
  bar now (see "Inside the Android shell" below).
- `webapp/components/*.js` are web components that each fetch one JSON document
  and render it, degrading to the last cached state offline.
  `<retinue-conversation>` (`conversation.js`) is the one exception in
  kind: not a card but one conversation with Ara — thread, composer,
  dictation, attachments, chips, copy buttons, model picker, read-aloud —
  embedded wherever a conversation is shown (the conversations card's open
  thread and its new-thread composer today; the chat page's companion pane
  next), so every surface renders it with the same code.
- `webapp/data/*.json` is the curated content. The static data cards that
  consume these files are commented out in `webapp/index.html` until a
  scheduler-driven refresh job regenerates them. **Refreshing these is Ara's job**
  (currently mock data). The server serves them at `/data/` from
  `DASHBOARD_DATA_DIR` (default `webapp/data`), kept separate from the baked
  shell so data can be written without rebuilding.
- `webapp/sw.js` caches the shell so the dashboard opens with no
  connectivity.

**Navigation is one row at the top of every page**
(`webapp/components/nav.js`): *Home · Chats · Threads · Projects · News ·
Contacts*, then an outbox icon for the pending sends (`/sends`, the approval
queue — a queue that wants action, one tap away) and the settings gear, with
the page you are on underlined. It is the first thing
on the home, the list pages and settings, and it stays pinned while a list
page scrolls, so leaving a page never means scrolling to its end — the list
pages used to end in a "← Back to dashboard" link, and the home linked to
them from the foot of the list. The drill-downs — a chat (`chat.html`), a
project (`project.html`), an open thread — carry the row too, so none of
them is a dead end, and on them it starts with a back button (`<retinue-nav
back>`). The row only reports the press (`retinue-back`); the page decides
where it goes: back to where the drill-down was opened from inside the app,
or, opened cold (a push, a bookmark), to the home; an open thread on the
home or the Threads page closes in place, a project being edited cancels the
edit. The page's own bar (title, Archive, minimize, model picker, menu)
stays as a second row under it. On a page holding unsent input — a typed
message, an unsaved project edit, a dictation on its way — a tap on a tab is
held with the same note a Home press of the Android shell gets, instead of
dropping the input; pressed again while the note shows, it goes — the hold
stops a slip, it never keeps anyone on a page. While the phone keyboard is up the row steps aside on a
drill-down, as the page's own header does. On a phone the home never scrolls as
a page (the attention list scrolls inside a locked frame, under the
navigation); in the wide layout (`isWideFrame` in
`webapp/components/base.js`) it fills a fixed frame and scrolls internally.
The home carries no news: a feed there defeats the point of a
distraction-free list of what wants attention, so the news lives only on its
own page (`news.html`), one tap away in the navigation row. *Threads* is `conversations.html`, which lists
every thread with an Active/Archived/Edits/Cowork filter — the last two being
where the otherwise hidden kinds (project edit commands, and the Ask-Ara
connector's cowork audit threads) are reachable. Threads can be archived from
inside a thread (`POST /conversations/<id>/archive`, `…/unarchive`); archived
threads drop off the active list but stay on that page.

Shell updates apply themselves: the service worker versions itself from a
content hash the gateway stamps into `/sw.js`, and
`webapp/components/update.js` reloads a controlled page once a new worker
activates (guarded — never while a thread/composer is open, a draft is unsent,
or a text field is focused) and re-checks on every visibility change, so even
an always-open window picks a deploy up within moments of being looked at. The
settings page shows the running shell version with a manual "Check for
updates" as fallback.

## The home: the attention list

The home screen is **one list of what wants attention** — threads with Ara,
messenger chats and running projects as one kind of thing
(`webapp/components/attention.js` over `GET /attention`), each with the four
properties of `docs/attention-model.md`: an importance (0–5), a deadline
against a lead time, its spheres — a main one and any further ones (tags);
every one counts for a mode alike — and who it waits on. The gateway
turns those and the **focus mode** in force into a level (passive · active ·
time-sensitive · critical) and a **delivery decision**: pushed now, held for
the next digest, or merely listed. The list shows them as *Now* (may
interrupt in this mode), *Next* (listed, below the bar), *Held until <the
next breakpoint>* and *Waiting on others*; every row carries its preview and
a one-line reason, and its ⓘ opens the details sheet
(`webapp/components/attention-sheet.js`) with the three fields explained and
correctable — importance (writes the sender's or kind's prior), deadline and
lead (writes the kind's lead time), the main sphere and the further ones
(remembered for the sender) — plus a permit for the sender in this mode, the
switch for the Focus rule that actually lets the item through (`admission` on
the row: a sphere the mode lists, or a word it admits wherever it stands, as
Focused does *health*; where none does, the rule that would), and the
actions *Later* (next breakpoint / tomorrow), *Pull into the list now* and
*Mark done* — the last also a gesture: a row swiped right on the home is
marked done, with an Undo for a few seconds. Done is the attention item's
state, not the source's: it never archives the thread or the chat, and the
next message opens a fresh item. A chat shows its sender's spheres, as the contact card and the
corrections taught them, unless the triage judged its message to be about a
sphere in particular (or named further spheres alone) — a customer who is
also a friend, asking about Saturday's barbecue, is *friends* for that
message. A judgement — importance, deadline, kind and spheres — is one unit
and belongs to the chat's open item: a classification replaces it whole, an
unclassified follow-up keeps it ("bring a salad?" stays *friends*, with the
barbecue's deadline), and once the item is handled the next message starts
fresh, with the defaults and the sender's spheres. Spheres are the user's own subjects — a client, a hobby, a cause
— so the vocabulary grows from wherever a sphere is chosen: *+ new sphere…*
in the sphere selector, *+ new* on the contact card
(`POST /attention/spheres`); a sphere the palette does not name gets a stable
colour from its word. The same sheet opens from the thread bar and from the chat page's
header.

A chat from a number nobody has named is **screened**: the sender is no VIP
(the delivery gate's one sender flag), has no contact card, and the profile
knows no sphere for them, so the message keeps the importance of a person
writing to a person but its sphere is `unknown`, which no mode admits — it is
listed, carried by the next digest, and never rings. A VIP, the other way
round, rings in every mode unless the chat is muted. The sheet then leads with
the **contact card**: a name, the sphere they belong to, any further spheres,
and optionally a permit to interrupt right now. Saving it
(`POST /chats/<id>/contact`) names the chat, teaches the attention profile
their spheres (carrying over whatever the bare number had already been taught),
re-judges the open item — so their next message is ranked by that sphere —
and files the handle under a person in the address book — one N-Triples file in
a chamber ([contacts.md](contacts.md)). A new person needs the chamber it is
kept in (a dropdown, pre-selecting the chamber the last contact was created in);
the card also offers, one tap each, anyone the book already has for this handle
or for the same number on another channel, and a change to a person reaches
every chat of theirs. The card also carries the person's VIP switch and their
**instruction** — the standing brief a model works each of their messages by,
whose verdict decides how loudly the message rings (docs/contacts.md, "The
instruction"); *Copy from…* takes one another person carries. An empty
name unlinks the handle — the person stays — and puts the sender back into
screening. For a chat whose newest message an instruction worked, the sheet
shows an *Instruction* field: the turn's state, its verdict, what it did and
what it flagged.

The **Contacts page** (`contacts.html`, the *Contacts* tab of the navigation
row) is the address book itself: every person, searchable by name
and e-mail address, each editable in place — their handles on every channel,
their spheres, importance prior, the Focus modes they may interrupt, the VIP
flag and the instruction (with *Copy instruction from…*, for a team). A new contact names the chamber it is kept in. Rows open where the item lives: a thread in place (the conversations
element on the home is a `viewer` — invisible until a `#conversation-<id>`
hash opens a thread or `#new` the composer), a chat on `chat.html`, a project
on `project.html`. The old chats, conversations and projects cards are gone
from the home; their pages remain, one tap away in the navigation row.

**The mode is the home's title.** The first line of the page is the mode in
force, in the largest type on the screen — *● Focused · customers · until
17:00 ▾* — with the
date on the right, and it opens the mode menu; above it there is only the
navigation row the home shares with every page. The state the user is in is what
governs everything below, so it says it plainly, where a greeting used to sit
saying nothing (and saying it in the browser's own clock, unchanged from page
load, which is how it came to wish you good evening at breakfast). Modes are
moods — *Rest & relax*, *Focused*, *Chores*, *Social* — and *Focused* takes a
scope from the menu: nothing, a sphere ("all clients") or one of the projects
on the list ("this one"); only what is about the scope may ring, and the rest
folds under *Not now*. **A change by hand is a breakpoint** — what was held is
released and the digest goes out — **except a switch into Focused**: a
decision to focus on something right now is not to be met with a reminder
of everything else, so what waits keeps waiting, and only what the focus lets
through rings (at once, on its own). The schedule's switch into Focused — and
"Follow the schedule" into it — stays a breakpoint: that stretch was planned,
and the digest at its start clears the deck before it. **A mode set by hand can run for a while** — the menu offers 30 min, 1
h, 2 h and 3 h before the mode is tapped, and `POST /attention/mode` takes
`minutes` or `until` ("17:00") — after which the schedule takes over again,
and that return is a breakpoint (*Focused ended 16:00 · 3 things waited*). Past
an hour the menu suggests **a breakpoint every 55 minutes** (the first after
55, none in the last 20 minutes; untick to decline): a digest titled *Break
14:55*, the moment to look up. While a timed mode runs, its breaks and its
end are its breakpoints and the day's digest times wait; an open-ended one
keeps the digest times, since nothing else would end it. The policy
is `scripts/attention.py` (pure, tested in `tests/test_attention.py`); the
gateway's part — assembling the items, storing each decision on the item's
own document, the endpoints, the tick — is the attention section of
`scripts/web-gateway.py` (tested end to end in `tests/test_attention_api.py`);
the adapters and the two small documents are `scripts/attention_store.py`.

To see the whole mechanism at work, `examples/attention-simulation/` plays a
scripted day on this very gateway and dashboard, with mocks for the store,
the messenger gateways and Ara.

**What carries the properties.** A thread's `attention` block is set when an
agent opens it (`conversation-push.py --importance 4 --due 2026-09-04T17:00
--kind "customer request" --sphere customers --tag finance`, or `--critical`,
`--actor NAME`) and can be revised with `scripts/attention-set.py`; a thread
that declares nothing is passive — listed, never pushed — which is the point:
an agent has to say how much something matters. A chat's block is set on the
notify rail when a message arrives: from the rail's `attention` (the triage's
judgement) where it carries one, else from the profile's priors for the
sender, else the default — a person writing directly is *active* (held for the
next digest, rung at once where the sender holds a permit), a group is chatter
(passive). `attention-set.py chat:<id> …` revises it; a reply (from the
dashboard or the phone), *Mark handled* or archiving the chat settles it.
The chat page carries **Archive** and **Mute** switches in its header's ⋮ menu
(`POST /chats/<id>/flags {archived?, muted?}`), with the thread flags'
meaning: an archived chat leaves the chats list and comes back when a
message arrives unless it is muted, and muted also silences its push;
unarchiving reopens the item only where archiving settled it — for a chat as
for a thread, so one the user had marked done stays done. A project's properties
come from its frontmatter — `importance`, `sphere`, `tags`, `kind`, the
deadline `expected_by` / `next_due`, the lead `remind_before` (days, weeks or
months, as `recurring-projects.py` reads it) — where the chamber's Markdown
converter maps them; a correction on a project changes the gateway's state
for it, never the chamber file. The two meet by the latest word: the state
keeps a snapshot of the frontmatter it was stored with, so a value the author
changes afterwards — a deadline moved, `next_due` advanced — replaces the
stored one, while a correction the author has not contradicted stands.
`recurring-projects.py` passes the same fields on the wake-up thread it
opens. A sphere or tag an agent or a file declares is normalised to the word
the rules use (`Board Games` → `board-games`). The monitors declare their
alerts (a broken Claude sign-in is critical; a dead channel a high system
alert).

**Delivery.** Every push to the user's devices now goes through the model:
`_push_conv_notification` and `_chat_push_notification` are called only when
the decision is *push* (`Urgency: high`); a held item badges the dashboard but
stays quiet. The user's own threads with Ara are never gated — they asked.
Breakpoints — the day plan's digest times (08:00, 12:00, 17:00, 21:00 on a
shipped workday, 09:00 and 18:00 on a day off) and the scheduled mode changes
(leaving *Rest* excepted: the morning digest opens the day) —
release what was held with one `Topic: digest` push (`Urgency: normal`), and a
half-hourly sweep re-evaluates: an item that crossed into the next urgency
band climbs, and one the mode now admits is pushed. Both run on the gateway's
own tick (`ATTENTION_TICK_SECONDS`, default 20 s), with no browser open, in
the deployment's zone (`ATTENTION_TZ`, else `RETINUE_DISPLAY_TZ`, else `TZ`,
else the container's, which is UTC — so a deployment sets
`RETINUE_DISPLAY_TZ`, which the compose file already passes). A minute counts
as handled only once its work is done, and the next run makes up every minute
since the last handled one — a failed run, a slow store query, a restart
across a digest time (`tick.json` under `ATTENTION_DIR` keeps the mark) — up
to three hours back.
Repeats are a per-class policy: a family sender writing again while held in
*Rest* breaks through; anyone else waits with their first message. The
decision is the model's alone: the per-device notification modes of #66 (new /
stalled / reply) are **gone**, since behind the model they only suppressed,
knowing nothing of VIP or critical, what it had just released — a second
message into a chat with something unread counted as a *reply*, and the
default setting dropped it. A device is notified or disabled, nothing in
between. `ATTENTION_PUSH_GATE=0` is the rollback: every
arrival pushes as before the model, while the list, the digest and the sweep
keep working.

**The digest** is that one push, and it is ranked as the list is — level,
importance, the nearest deadline (`attention.rank_key`) — with a line per
item saying why it is there: *Backup failed — critical*, *Beat Frei — due
tomorrow 12:00*, *Card renewal — overdue since 07:00*, or the start of what it
says when it has no deadline; five lines, then "… and 3 more"
(`attention.digest_text`). Each released item is stamped `digest_at`, and the
home shows what the digest brought first, in a framed section of its own —
on every device the dashboard is open on, not only the phone whose push was
tapped — until *Done* on any of them (`unseen_digest` in `GET /attention`;
`POST /attention/seen {digest}`; the gateway keeps the last digest sent and
the last one seen in `digest.json` under `ATTENTION_DIR`). The push itself
opens `/?digest=<its time>`, which shows that digest even once seen. Afterwards the rows the
latest digest released keep a quiet *digest 12:00* marker (`last_digest` in
`GET /attention`); a new arrival, *Later* or *Pull* takes it off. Nothing held
means no digest, and Rest sends none: what waited goes out with the first
digest after it.

**The two documents** live under `ATTENTION_DIR` (default a sibling of
`CONVERSATIONS_DIR`, so the persistent volume): `focus.json` — the modes
(admitted spheres, admitting tags, threshold, blurb), the week, the holidays,
the default digest times, the sphere vocabulary and the manual override — and
`profile.json` — importance and sphere priors per sender or
kind, lead times per kind, permits per mode, expiry per sender (below), and
the learned log; plus
`projects.json` with the delivery state of projects. `GET /attention/profile`
returns both, `POST /attention/profile` replaces them, and `POST
/attention/modes` changes the rules by patch (`attention.apply_rules`: a
mode's `only_admitted`, `threshold`, `admits` or `admit`/`deny`,
`admit_tags` or `tag_on`/`tag_off`; the `week`, one day plan by `plan`, the
`holidays` or `holiday_add`/`holiday_remove`; the default `digest_times`,
times as `HH:MM` or minutes) — what the mode menu, the settings page and Ara
write; a deployment edits the files or drives the mode from the menu. The
shipped defaults are the brief's (`attention.default_focus()`,
`default_profile()`).

**Expiry.** Some messages are worthless minutes after they arrive — a
one-time login code is the case. The profile's `expiry` maps a sender to
minutes; each message from them stamps its chat's item with `expires_at`
(arrival plus that span, restarted by every new message), and the gateway's
minute tick (`_attention_expire`) settles an open item whose time is up: it
is marked done (`done_how: "expired"`) and the chat archived, not muted, so
the sender's next message brings it back and rings exactly as before. A
message still on its way into the model when the timer fires (the rail has
counted it in the chat state's `inbound_seq`, the attention block has not
taken it in yet — `attended_seq` — e.g. while an instructed sender's turn
works) keeps the chat unarchived: the old item is done, the new message stays
in view. No
model is involved — it is a clock, deterministic and free, and a deployment
with no expiry pays nothing (the tick reads only the chat state documents,
and only while some sender has one). Expiry decides when an item leaves the
list, never whether it rings: that stays with importance, sphere and
permits. Putting an expired item back (*Reopen*) unarchives the chat and
clears its timer. The user sets or clears it in a chat item's details sheet
(the *Expiry* field: a few presets from 5 minutes to a day, or *never*; `POST
/attention/items/expiry`), Ara with `attention-set.py <chat item>
--sender-expiry 10m` (`none` clears). Both take one contract: minutes as a
number or a span (`10m`, `2h`), between one minute and seven days, or `null`
to clear — anything else, an empty string included, is refused and the
profile left as it was. The row in `GET /attention` carries `expires_at`,
the sender's `expiry` and `done_how`, and the sheet shows them.

**The week.** The schedule is not one day repeated, and not seven: it is a
few **day plans**, each a day's schedule (`[minute, mode]` or `[minute,
mode, sphere]`) with the days it rules, written compactly — `mon-fri`, `sat,
sun`, `weekend`, a wrapping `fri-mon` — and optionally its own digest times.
Every weekday belongs to exactly one plan. One plan may also claim
`holiday`: the dates in `holidays` (single days or ranges, with a name) then
follow it whatever weekday they fall on, so a week off is told to the system
as a date range, not built as a new schedule. The shipped week is *Workday*
(`mon-fri`, the schedule above) and *Day off* (`sat, sun, holiday`: Rest,
Social from 09:00, Rest from 22:00; digests 09:00 and 18:00). A plan without
a 00:00 entry starts the day in the mode the night before ended in, so the
answer to "what mode is it" and "when is the next breakpoint" is always the
date's own plan, across midnight (`attention.day_plan`, `day_schedule`,
`next_breakpoint`). Changing a plan's days moves them from whichever plan had
them — a plan left with none is gone — and the patch is refused while a
weekday would be in no plan or two. Ara makes these changes when asked, with
`scripts/attention-week.py` (the week and today's plan; `holiday add
2026-12-24..2027-01-02 --name Christmas`; `plan Friday --days fri
--schedule "07:00 chores, 08:00 focused, 14:00 social, 22:00 rest"`), and
puts one sphere within another with `scripts/attention-spheres.py` (`within
acme customers`: whatever admits customers admits Acme too). A
`focus.json` from before the week keeps its one schedule for every day, as an
*Every day* plan — unless it was the shipped schedule, which gives way to
the shipped week. A document is healed as it is read
(`attention.heal_focus`): the shipped modes are put back where missing, a
schedule entry naming a mode that does not exist is dropped, and an override
to one is released — so a hand-edited or older file can never make every
attention call fail. The mode menu's *Follow the schedule* row names today's
plan, and the holiday when there is one.

**The API** (behind the dashboard's auth like the rest): `GET /attention`
(the sections, the mode, the next breakpoint, `degraded` naming a source the
store could not answer for — read without the lock inbound messages and the
tick take, the projects' rows cached for `ATTENTION_PROJECTS_CACHE_SECONDS`,
default 15, and expired by a write on the project page), `GET /attention/item?id=…`, `POST
/attention/mode {mode, subject?, project?, minutes? | until?, breaks?}` (`null`
follows the schedule), `POST
/attention/items/later {id, when: next|tomorrow}`, `…/pull`, `…/done`,
`…/reopen`, `…/correct {id, importance?, due?, lead?, sphere?, tags?,
critical?}`, `…/expiry {id, expiry: minutes | span | null}` (a chat's
sender), `POST /attention/permits {sender, mode?, on}`, `POST
/attention/admit {sphere, mode?, on}`, `POST /attention/modes {mode?, …}`
(the rule patch above); and the token-gated `POST
/internal/attention/set {id, …}` for agents (`attention-set.py`). Item ids are
`thread:<id>`, `chat:<chat id>` and a project's URI.

**The life store.** The open items' four properties are emitted as N-Triples
into `chambers/_generated/attention/items.nt` (`kb:importance`, `kb:due`,
`kb:leadTime`, `kb:sphere`, `kb:tag`, `kb:currentActor`; subjects
`urn:retinue:thread:<id>`, `urn:retinue:chat:<id>`, the project URI) — by
`scripts/emit-attention.py` at boot and by the gateway on every tick that does
something, write-if-changed — so "what wants attention, at which level" is one
SELECT for agents and jobs.

## Conversations

The dashboard has **conversation tabs** (`webapp/components/conversations.js`):
interactive chat threads with Ara, backed by the gateway's `/conversations` API
(not a static data file). A thread can be opened by the user **or by an
agent** when a decision is needed — e.g. an RSVP, an ambiguous e-mail:

```bash
python3 /workspace/scripts/conversation-push.py --title "Party RSVP" \
  "You've got an invite to Mara's party. Confirm and add to your agenda, or decline?"
```

The thread appears on the dashboard with an unread badge; when the user
replies, Ara picks it up with full context and carries out what they approve.
The endpoint is token-gated (`CONVERSATION_BACKEND_TOKEN`, set by the
entrypoint) so only in-container agents can post on the user's behalf — like
the e-mail backend and `signal-push.py`. Threads persist under
`CONVERSATIONS_DIR`, which the deployment pins to the persistent `/root`
volume (`/root/.retinue/conversations`) so threads survive container
recreation.

**Opening a thread is a side effect, so it takes an idempotency key.** The
same turn can legitimately run twice — the escalation re-run replays a junior
turn's prompt on the frontier model (its reply is discarded, but a thread it
already opened is not), and a messenger gateway can redeliver a message after
a reconnect — and each run would otherwise raise its own thread for one
message. A caller that can name what it is reacting to passes that name as
`--key` (`{key: "..."}` on the endpoint); the first thread opened under a key
is the only one, and a repeat is answered with that thread
(`"deduplicated": true`, HTTP 200).

Whether the repeat also posts depends on what it has to say, because the two
runs a key exists for differ exactly there. A **redelivery** repeats words the
thread already carries: absorbed silently, no message, no push, no badge. An
**escalation re-run** is the same turn done properly — junior's reply was
discarded and the prompt replayed on the frontier tier, but a thread junior
opened before escalating survived and holds its incomplete attempt. Dropping
senior's answer there would leave the user with only that, so a repeat carrying
something the thread does not already say is **appended and pushed**
(`"appended": true`). Appended rather than substituted: junior's words may
already have reached the user's phone, and rewriting what someone has read is
worse than showing them the correction after it. The inbox gateways
mint one per inbound and carry it in the triage prompt (`Thread key: …`), so
the key is the channel's own message identity rather than anything the session
invents. That identity is `<channel>:<receiving account>:<chat>:<message id>`,
built only by `inbound_store.thread_key()` — the account and chat are part of
it because a native message id is not unique on its own: Telegram numbers
messages per chat, Signal identifies one by (source, sent timestamp), and a
deployment may run two gateways on one channel. An inbound with no native id
falls back to the stored record's own URN, and to a random value when there is
not even that, so distinct arrivals never merge. The drain decorates its rows
with the same key, so a message forwarded live and later drained — a live turn
that died before finishing — lands on the thread it already opened. Bindings live in `CONVERSATIONS_DIR/.keys`, one
file per key; a binding whose thread was deleted counts as unbound, so a
removed thread never swallows the next message about the same item. Keys are
for *opening* a thread — appending already addresses one by id.

The gateway runs a **presentation lint** over every agent→user message that
lands in a thread — see `docs/model-routing.md` (phase 4) for the mechanism
and its configuration.

## The per-thread model choice

Each thread carries a **model choice** — which model answers Ara's turns in
that thread. Because every turn is a fresh `claude -p` (no long-lived state
pinned to a model), the model is a free per-turn choice: pickable when the thread
is created and switchable mid-thread from a dropdown in the thread bar, effective
from the next turn. A messenger chat's companion thread (the chat page's Ara
pane) offers the same dropdown in its bar, so the tier answering there is as
visible and as switchable as anywhere else. The picker governs **Ara's own turn only** — dispatched
subagents (the Archivist and any chamber-provided subagents) run on their
own pinned models regardless of the selection; those pins are changed on the
settings page instead (see **Agent models** below). In a deployment that routes
through **LiteLLM** (the shipped default), the offered list is managed **in
LiteLLM itself**: the gateway reads `GET /model/info` (and `GET /v1/models`)
and offers the concrete models the proxy currently serves. `retinue_picker: true`
plus `retinue_label` still names a route; `retinue_picker: false` hides one.
Plumbing routes (`retinue-claude`, wildcards) stay hidden. The list
is cached briefly
(`RETINUE_MODELS_CACHE_SECONDS`, default 60) and read from
`RETINUE_LITELLM_URL` (default: `ANTHROPIC_BASE_URL`) with the credentials
Claude Code already sends (`ANTHROPIC_CUSTOM_HEADERS`, override:
`RETINUE_LITELLM_KEY`). When LiteLLM is configured, a reachable-but-empty
list or a failed fetch with no last-good cache offers **nothing** (the picker
hides itself) — never the static aliases, which the proxy would not serve.
Static sources apply only when LiteLLM is *not* configured: an
inline **`RETINUE_CONVERSATION_MODELS`** JSON array of `{"id","label"}` (an
explicit override that wins over everything, LiteLLM included), else the
JSON-LD document
`config/conversation-models.jsonld` (path override:
`RETINUE_CONVERSATION_MODELS_FILE`; read as plain JSON on the serving path,
and derived into the life store by the boot emitter
`scripts/emit-conversation-models.py`, so the fallback list stays queryable
over SPARQL). Whatever the source, `id` is passed to `claude --model`. The
list carries only concrete models — no synthetic "Default" row: the entry the
gateway's configured default resolves to (through LiteLLM's route aliases)
is flagged `default: true` and labeled as the default, and a thread without a
stored choice runs that default (stored as the empty string internally). A
default no picker route names (say a newer model reached only through the
`claude-*` wildcard) is added as a row of its own under its concrete id, so the
picker always names a real model; give it a picker route in
`litellm/config.yaml` for a friendlier label.
The dashboard reads the list from `GET /conversation-models` and
persists a thread's choice via `POST /conversations/<id>/model` — an id not on
the offered list is ignored (the thread falls back to the default), so a client
can never inject an arbitrary `--model`. The picker hides itself when fewer than
two models are offered. Model tiers and per-thread escalation:
`docs/model-routing.md`.

## Agent models (the settings page)

Every subagent pins its model in its definition (`model:` in the frontmatter
of `.claude/agents/*.md`, or of a chamber plugin's `agents/*.md`), and those
files are baked into the image. **Settings › Agent models** is the knob over
them: one dropdown per dispatchable agent, offering the same list as the
thread picker (`GET /conversation-models`, so an Ollama tag the picker shows
is offered here too), with "Built-in" as the first entry and, where
`RETINUE_AGENT_MODELS` sets one for the agent, "Deployment default" second.
The card is server-side state shared by every device, not a per-device
preference.

Mechanism (`scripts/agent_models.py`): Claude Code's `--agents <file>` flag
defines agents for one session and takes precedence over the same name from
`.claude/agents/` or a plugin — as a *replacement*, not a merge. So an
override is the agent's own file re-read (frontmatter plus body) and handed
back with only `model` swapped; the agent's description, tools and prompt are
untouched. Every framework spawner (`web-gateway.py`, `scheduler.py`, the
base-job scripts, the MCP connector) appends `agent_models.cli_args()` to its
`claude -p` command — `[]` when nothing is overridden — and the generated file
is rewritten only when the choices change, so a new choice reaches the very
next dispatch of those sessions with no restart. `cli_args()` never raises: an
override that cannot be applied leaves the spawn as it was. **Interactive
sessions are not covered** — the remote-control main session `entrypoint.sh`
starts, and the ones the spawn-session skill starts: Claude accepts an
`--agents` file only with `-p`, so the subagents they dispatch keep their
pinned models.

Two sources, the dashboard winning: `RETINUE_AGENT_MODELS` (deployment
default; a JSON object or `agent=model,…`) and the file the card writes
(`RETINUE_AGENT_MODELS_FILE`, default `/root/.retinue/agent-models.json`,
where choosing "Built-in" is an explicit entry that also cancels the env
default for that agent, and choosing "Deployment default" —
`POST /agent-models {agent, reset: true}` — removes the agent's entry).
Agent ids are what the Agent tool takes: the frontmatter `name` for core
agents, `<plugin>:<name>` for plugin agents; a plugin the retinue marketplace
no longer lists (a removed chamber) is skipped, as Claude skips it.
`POST /agent-models` accepts only agents discovery lists and models on the
offered list — the thread picker's gate — so a client can never inject an
arbitrary definition.

Some agents are listed but **fixed**, left as the only definition: Claude
validates an `--agents` value as a whole and refuses to start on one bad
field (where it merely warns about the same field in an agent file), so an
agent is redefined only when every field is one the no-dependency
frontmatter parser carries faithfully. Nested YAML (`hooks:`,
`mcpServers:`), a field or value it does not know (an unrecognised `effort`
or `permissionMode`, `maxTurns: 0`), or a prompt using the plugin
placeholders Claude expands only when it loads the file itself
(`${CLAUDE_PLUGIN_ROOT}`, `${CLAUDE_PLUGIN_DATA}`, `${CLAUDE_PROJECT_DIR}`,
`${user_config.…}`) make the agent fixed. A plugin agent's `permissionMode`
and `initialPrompt`, which Claude ignores in plugin files, are left out of
its redefinition, so a model change does not switch them on.

An override does not change attribution: a subagent's memories carry the
dispatching session's stamp (`RETINUE_SESSION_MODEL`, `docs/memory.md`), and
the bubble names the turn's costliest model, usually Ara's own — the
subagent's model shows only in the turn's `modelUsage`.

Every core agent calls tools — even the Secretary (Read/Glob/Grep for its
persona and style files) and the archivist-reader (Read for the document) —
so an overriding model needs tool support, behind a route that does not drop
`tools`; `docs/ollama.md` records which local tags fail there, and its
`ollama/*` route (tools dropped, 512 output tokens) suits Ara's chat turns,
not subagents. The Secretary and the archivist-reader have no write or shell
tools, so a failed experiment there does the least harm.

## Attachments

A thread can carry **file attachments** the user downloads straight from
the dashboard — e.g. an e-mail attachment (a PDF invoice) forwarded into a
thread, so it's reachable without an e-mail client. Pass `--attach PATH`
(repeatable) to `conversation-push.py`; the file is stored beside the thread
(under `CONVERSATIONS_DIR/attachments/<id>/`, keyed by a server-generated id so
untrusted filenames never touch the filesystem) and rendered as a link in the
message bubble, served by `GET /conversations/<id>/attachments/<att-id>` behind
the dashboard's own auth. Prefer this over pushing a document via Signal when
the user is already working in the dashboard.

Images the gateway serves inline (`_INLINE_SAFE_TYPES`: PNG, JPEG, GIF, WebP,
AVIF — never SVG, which is script in the dashboard's origin) also **preview in
the bubble**: a lazy-loaded `<img>` at its true aspect ratio, capped in width
and height, linking to the full view (`?inline=1`), with the name row and the
↓ save link beneath. The gateway records an image's intrinsic size when it
stores it, so the preview's box is reserved before the bytes arrive and a
thread never jumps while it loads (older records without a size get a fixed
frame). This is the thread's own same-origin attachment, not a remote fetch:
the Markdown renderer's no-remote-images rule is unaffected.

To deliver a file into a thread that **already exists** — rather than stranding
it in a fresh tab the user has to go find — pass `--thread <id>` (the thread id
from the conversation URL). It posts to the token-gated
`POST /internal/conversations/<id>/messages`, appending an agent message with
the attachments and marking the thread unread. Use this to post into *another*
thread, or from a session that is not answering one.

To attach a file to **Ara's own reply** in the thread her turn is answering,
use `conversation-push.py --reply-attach PATH` (repeatable, nothing else on the
line). That reply is appended by the gateway *after* her session ends, so the
CLI makes no request: for every dashboard-thread turn the gateway hands the
session a fresh manifest path in `RETINUE_REPLY_ATTACHMENTS_FILE` (a per-spawn
value in `scripts/session_env.py`, never inherited), the CLI appends absolute
paths to it, and when the turn ends the gateway reads the manifest, copies
each file into the thread's attachment store through the same function every
other attachment goes through, and puts them on the reply message — rendered
at its end, like any attachment. Each entry must be an absolute path to a
non-empty regular file within the attachment size limit (at most 20 per
reply); anything else is logged and skipped, never costing the reply itself.
A reply may be files only — it is then stored without text. The manifest is
fresh per spawn and read only for the run whose reply is kept, so when Ara
junior escalates, the files her discarded run listed are discarded with it.
Outside a thread turn the variable is unset and the CLI refuses, pointing at
`--thread <id> --attach`.

Attachments go **both ways**: the user can attach files to their own messages
from the composer (a paperclip button on the input row), or **paste** them into
the text box — a screenshot from the clipboard, an image copied off a page, a
file copied in a file manager — which stages exactly what the paperclip would
(`webapp/components/clipboard.js`; a pasted screenshot's generic `image.png`
becomes `pasted-<date>-<time>.png`, a real filename is kept). These upload with the
message, are stored the same way, and their on-disk paths are handed to Ara in
her engage prompt — so she can actually open a file the user sends (a PDF, a
CSV) rather than only knowing one exists. A message may be text, files, or both.
The chat page's composer takes pasted images the same way (see `webapp/README.md`).

## Push notifications

The unread badge only exists while the dashboard is
open — which is precisely not the case when an agent opens a thread that needs a
decision. So an agent→user turn that lands unread (a thread an agent opens
via `conversation-push.py`, a message it appends, and Ara's own async reply)
fans out a **Web Push** notification to the user's registered devices when
the attention model says so (see *The home* above: pushed now, or held for the
digest — Ara's replies in the user's own threads always push); tapping it
opens that thread. This is automatic — there is no separate step after
posting to a conversation. Pushes carry the RFC 8030 headers the model uses:
`Urgency: high` on a breakthrough, `Urgency: normal` and `Topic: digest` on
the digest, so an undelivered digest is replaced rather than stacked.

The plumbing lives in `scripts/push_notify.py` (VAPID keypair, one file per
device subscription, both persisted under `PUSH_DIR` — by default a sibling of
`CONVERSATIONS_DIR`, so it inherits the persistent `/root` volume) and three
gateway endpoints: `GET /push/config`, `POST /push/subscribe`,
`POST /push/unsubscribe`. The user manages the opt-in from the **settings
page** (`settings.html`, reached via the gear in the navigation row), where
`webapp/components/push.js` in `manage` mode shows this device's state —
unsupported, blocked, off, or enabled with the one preference left (whether
archived conversations notify here too) and a disable button. There is no
per-device choice of *which* pushes arrive; that is the model's (see *The
home*). The module also runs a silent re-registration on every dashboard load,
so an already-granted device heals a rotated or server-reset subscription
without visiting settings — a device the user disabled is skipped
(`retinue_push_disabled` in its `localStorage`, which is what makes *Disable*
outlast a reload). Two caveats worth knowing: on **iOS**
push only works if the dashboard was added to the home screen (in a plain
Safari tab the settings page says so), and if `pywebpush` is unavailable the
whole feature reports itself disabled rather than failing — conversations
still work exactly as before. Set `VAPID_SUBJECT` to the operator's contact
address; deleting the stored key invalidates every existing subscription, so
devices would need to re-enable.

## Archived + muted

Archived and unread is a contradiction: the thread claims
to want attention while being invisible. So when an agent files something new
into an archived thread (`POST /internal/conversations/<id>/messages`, the
`--thread` path of `conversation-push.py`), the gateway **un-archives it** — the
message would otherwise be lost, which is exactly what happened before this
existed. The opt-out is the separate **`muted`** flag: a muted thread stays
where it is, however much arrives. `muted` is independent of `archived` (either
can be set alone) and has no dashboard control on purpose — it is set from an
agent via `POST /internal/conversations/<id>/flags`:

```bash
# archive for good — what to run when the user says "archive this conversation"
python3 /workspace/scripts/conversation-push.py --thread <id> --archive --mute
# also: --unarchive, --unmute; flags are their own call, never mixed with a message
```

The behavioural rule this implies for Ara — Archive-click vs "archive this" —
is in `CLAUDE.md`, since it applies on ordinary turns.

## The shelf (minimized pages)

Threads, chats and projects can be minimized to a shelf at the foot of every
page and reopened where they were left; a thread or chat the user leaves while
Ara is still answering goes there by itself, marked `···` until her answer
lands and then with a green dot. Nothing server-side: the shelf is per device
and browser (`localStorage`, shared by its tabs) and reads the existing
conversation and chat APIs. The rules (one item per page, automatic items
living for one turn, Close suppressing re-adding for that turn, capacity
evicting quiet items only, archive/mute) and the mechanics are in
`webapp/README.md`, "The shelf: minimized pages"; the size is a setting on the
settings page. Design and acceptance criteria: issue #282.

## Project pages and edit threads

Every project on the projects card has its **own page**
(`project.html?id=<project URI>`): the gateway maps the URI back to the
project's source Markdown file via its named graph in the life store
(`GET/POST /projects/item`), the page renders frontmatter + body with the
dashboard's shared Markdown renderer (`webapp/components/markdown.js` — also
used by conversation bubbles, so both render identically), and the file can be
edited in place (raw-Markdown editor, sha-guarded against concurrent changes,
auto-committed). A command bar hands quick change requests — typed or dictated —
to Ara as a conversation of **kind `edit`** linked to the project: apply the
change to the project file and confirm in one short sentence. Edit threads are
marked as such and hidden from the default conversation list (they stay under
the Edits filter); "Discuss with Ara" on a project page starts a normal,
visible thread whose engage prompt points Ara at the project file. The link
runs both ways: such a thread carries an "About: <project>" chip back to the
project page, and the project page lists the project's threads (open ones
first, archived folded) so an ongoing discussion is picked up, not restarted.

## The life store page (`/sparql`)

The gateway publishes the life store at **`/sparql`**, behind the dashboard's
own sign-in (the host-wide forward-auth covers it like every other path). The
store is read-only, but it is the whole personal store, so nothing about it is
public. One URL serves two audiences, the way public SPARQL endpoints usually
do:

- **Programs** use it as a SPARQL 1.1 Protocol endpoint: `query=` in the URL or
  a form, or an `application/sparql-query` body; the store's answer, in the
  format the `Accept` header asked for, comes back unchanged. Asked without a
  query by anything but a browser, it returns a SPARQL 1.1 Service Description
  (Turtle).
- **A browser** gets `webapp/sparql.html`, linked from Settings › System: the
  endpoint with a ready `curl` line, then `docs/ontology.md` itself. The page
  fetches the doc from `/docs/<name>.md` (the image's own `docs/`, read-only)
  and renders it with the dashboard's Markdown renderer, so the documentation
  and the page cannot drift apart. Every fenced `sparql` block in it gets a
  **Run** button. Run turns the block, where it stands, into a YASQE editor
  with the same query and opens its YASR results beneath it. `?doc=<name>`
  renders another doc the same way, and a `docs/<name>.md` mentioned in the
  text links there. At the foot of the page is a full YASGUI **workbench** for
  your own queries, kept in tabs on the device.

Safety lives in the gateway, not the page. Only a `query` ever reaches the
store, as a fresh form POST carrying nothing of the caller's request but the
query, the dataset it names (`default-graph-uri`, `named-graph-uri`) and its
`Accept`: no `Authorization` header, no access token, no other argument. Every
form of SPARQL Update is refused (403) before the store is asked; the store's
own front refuses writes too. So is a query that uses `SERVICE` (403): a
federated query makes the store itself call the endpoint it names and send it
bindings from the store, which is data leaving by a side door. The check is
lexical and errs towards refusing, but a variable, a prefixed name or a string
merely spelled "service" passes; QLever's in-process extensions that use the
keyword (`pathSearch`, `spatialSearch`) are refused with the rest. And so is a
query a browser sends from another site (`Sec-Fetch-Site`, else `Origin`
against the scheme and host the request arrived with: the first
`X-Forwarded-Proto` / `X-Forwarded-Host` entry, else `Host`), which is how a
page the user happens to visit would fire one with the user's credentials. Programs send neither
header, and the page itself still opens from a link anywhere. Browsers send
`Sec-Fetch-Site` only over HTTPS, so serve the dashboard over HTTPS alone:
over plain HTTP another site's image or form GET carries no such header and
could still have a query run blind (unread, and never calling out), and a
proxy in front must set `X-Forwarded-Proto` and keep `Host` or set
`X-Forwarded-Host` (Traefik does all of this by default). A store-level lock
is possible too (`qlever-server --service-allowed-iri-prefixes -`, a
qlever-dir change); the gateway does not rely on it. A result
is never served as HTML on the dashboard's origin. A body the gateway cannot
read is named as such: chunked transfer gets 411, a malformed
`Content-Length` 400.

**Timeouts.** The store cancels a query after its own default timeout
(`qlever-server --default-query-timeout`, 30 s as qlever-dir starts it) and
says so in its answer (429, "Operation timed out"). `SPARQL_PROXY_TIMEOUT`
(default 120 s) only bounds how long the gateway waits on the store's
connection, so raising it does not let a query run longer. A longer budget
needs qlever-dir to start `qlever-server` with `--default-query-timeout`
(`start_qlever` in its `orchestrator.py`), which it does not offer as a setting
today.

YASGUI is **vendored at image build** by `scripts/vendor-yasgui.sh` (pinned
version and sha256) into `webapp/vendor/yasgui/`, which is not committed; run
the script once for a checkout served directly. The page loads it on first
use, and the service worker then keeps it in the shell cache. The dashboard
makes no third-party requests, and this page keeps that rule: YASQE's stock
autocompleters, which call prefix.cc and send what is being typed to the LOV
API, are replaced by one that completes the prefixes the ontology's own
examples declare. Typing `sosa:` adds `PREFIX sosa: <…>` from the
documentation. The workbench keeps queries on the device, never results. A
shared link (`#query=…`) opens as a tab of its own with its query and nothing
else (no endpoint, header or argument the link names), and it does not run
until you press Run: a link is somebody else's query.

`tests/test_doc_sparql.py` keeps every documented query runnable: one
self-contained, read-only query per block (parsed by rdflib where installed),
and, for every vocabulary in the ontology's defaults table, an example section
of its own that uses its namespace (the fallback row names schema.org again
and shares its section).
`tests/test_web_gateway_sparql.py` pins the endpoint's behaviour, and
`tests/test_webapp_sparql_doc.py` the rendering.

## Speech-to-text (the `stt` service) and voice input

Transcription is a **shared capability**, not the business of any one gateway, so
it lives in its own compose service, `stt` (`scripts/stt-service.py`,
`stt/Dockerfile`). It owns the single Whisper model in the whole stack and
exposes one endpoint on the internal `agents` network:

```
POST http://stt:8100/transcribe   (raw audio body; optional ?lang=<iso>)
  -> {"text": "...", "lang": "<iso>"}
```

The gateways are **clients** of it, so no ASR model is loaded anywhere else:

- the **signal-gateway**, **whatsapp-gateway** and **telegram-gateway** post
  inbound voice notes to it (`STT_SERVICE_URL`);
- the **web-gateway** proxies dashboard voice input to it, exposing
  `POST /conversations/transcribe` to the PWA.

Dashboard voice input adds a **cleanup pass** on top: the raw transcript is run
through a small model (`TRANSCRIPT_CLEANUP_MODEL`; unset it falls back to
`RETINUE_CLAUDE_MODEL` — so a non-Anthropic deployment cleans up on its own
backend — with `haiku` as the last resort) with the
thread so far and the chambers' contact names as context, so what lands in the
composer is already repaired. The messengers need none of this — there the agent reads
the transcript and answers what was meant, while the dashboard is the one place
that shows the user the raw text. Set `TRANSCRIPT_CLEANUP=0` to disable the pass
(the endpoint then returns Whisper's output verbatim); the reply always carries
both `text` and `raw_text`. While recording, the dashboard's composer shows a
live waveform with three controls: discard the recording, transcribe it into
the text field for review, or transcribe-and-send in one step (no review stop,
as over Signal).

**Continuous conversation** is a per-thread mode on top of that composer
(`webapp/components/conversation.js`, its pure logic in
`webapp/components/continuous.js`). A loop button in the thread bar switches
it for *this* thread, remembered per thread on the device. In the mode Ara's
answers are read aloud (regardless of the speaker toggle); when the reading
ends, or there is nothing to read, the mic opens with a short chime and a
voice-activity detector — the RMS level of the recorder's own stream from a
Web Audio `AnalyserNode`, against a noise floor learned while nobody speaks —
ends the take after a pause (default 1.5 s) once speech was heard. A level
that stays above the threshold without a dip for 3 s, and holds flat all the
while (loudest frame under twice the quietest), is taken for a louder room (a
fan, an engine), not a voice — a gain control can squeeze speech above the
threshold with no dip, but a voice still swings: the floor jumps to it, and
if it was all the take had heard, the take goes back to waiting. Only a take
that ends on a pause after speech is transcribed and sent, on the same path
as ➤; the loop waits for the answer, and repeats. Every other end goes to the
draft for review, never out: a take that runs to the 120 s cap without
falling silent, and one whose no-speech window closes after a run was taken
back as the room (it may have been a voice). No speech within the no-speech timeout (default 8 s)
closes the mic with the mode kept on (the mic button resumes); leaving the
thread or hiding the page ends listening. Where the page cannot hear the
stream at all, plain timers stand in: the same no-speech timeout (unless the
recording row was touched) and the same cap, both ending the take for review
— nothing there knows whether the user spoke. The loop never takes the
input row from under a user who is writing: with text or files in the box,
or the cursor in it, the mic does not open by itself (the loop stops and
offers **Start talking**), and a take that ends while the box holds
anything goes to review rather than out with it. Playing another message
while the loop reads its answer ends the loop (the mode stays on), as the
bar's ✕ does. An engaged loop that has nothing going on for 90 s — no mic,
no take on its way, no answer awaited or playing — lets go, and so does one
whose thread turns out deleted (which also drops it from the wake's
choice). Both timeouts are
per device (Settings → Voice). Only an answer to the element's own turn
carries the loop on: another message pushed into a thread in the mode may be
read aloud, but never opens the mic. A mic the loop wants while the page is
hidden (a wake in the background, an answer read in a pocket) waits up to
60 s for the page to come back.
Entry points: the toggle, `/#new?voice=1` (the composer in the mode — its
first take opens the thread, which continues in it) and a wake (`/?wake=1`
or the shell's `wake` event), which opens the thread used in the mode within
the last 15 minutes, else a new one. Both flags leave the address once read,
and only inside the shell do they open the mic by themselves; elsewhere (a
link in a message, a reload) the conversation opens in the mode with a
**Start talking** button. The detector listens for energy, not words, so it
is language-agnostic.

Language handling (constrain detection to the languages the user speaks, and
re-decode when a guess falls outside that set) lives entirely in the service via
`STT_SUPPORTED_LANGUAGES`. An optional `STT_TOKEN` gates the endpoint
(defence-in-depth; the service is not published to the host). Downloaded model
weights persist in the `stt-models` volume.

## Inside the Android shell

The Android app (`ara-android`, its own repository) is a specialised browser
and home launcher: a full-height WebView on the dashboard's origin, a launcher
bar beneath it configured on the device (installed apps, `tel:`/`sms:`/
`mailto:`/`geo:` and other URIs, dashboard pages — its defaults are the four
OS actions the web dock used to hold), and the few capabilities a browser tab
cannot have: the home-screen role, a wake phrase, native speech output where
the WebView has none, and the client certificate. Android never lets a PWA be
the HOME app itself, so "the dashboard as home screen" is the shell holding
the home role with the attention list as its start page. Everything that is
Retinue — threads, voice conversation, attention, contacts, sends — stays in
the web app, so it behaves the same in the shell, in Chrome and on a desktop.

The contract the two sides share (web side: `webapp/components/shell.js`):

| Direction | What |
|---|---|
| User agent | ` RetinueShell/1 (Android)` appended. |
| `window.RetinueShell` | Installed at document start, configured origin only: `platform`, `version`, `setVoiceActive(bool)` and `openSettings()`, behind Settings → Phone shell (shown only when the object exists). `setVoiceActive(true)` means *a voice conversation is engaged: keep the screen on and pause the wake phrase*. The page asserts it before it asks for the mic (the wake detector must release it first) and keeps it asserted for as long as a continuous conversation is going — through transcription, Ara's answer and its reading, not only while the mic is open — and sends `false` once the loop stops (mode off, no speech, leaving the thread, an error). Wake detection being paused for the whole conversation is intended. |
| Shell → page | `window.dispatchEvent(new CustomEvent('retinue-shell', {detail: {type}}))`, `type` `wake` or `home`. The home answers in place: `wake` opens continuous conversation (the thread used in the mode in the last 15 minutes, else a new one), `home` unwinds every open thread or composer back to the list at `/`, closes a sheet and scrolls the list to its top. Other dashboard pages answer by loading `/?wake=1` or `/` — unless they hold unsent input (the draft of the conversation on screen, a dictation or send under way, an unsaved project edit or chat draft): then neither event navigates, a short note says why, and the composer holding it is brought into view. A Home press made again while the note shows navigates after all (a wake does not). A draft left in a thread no longer on screen does not hold the press. |
| Pages without the web app's code | The gateway's own pages (`/sends`, `/claude-auth`) do not load `components/shell.js`, so nothing on them answers `wake` or `home`. The shell handles that itself: when the page in front is not one of the dashboard's, a Home press loads `/` and a wake loads `/?wake=1` instead of dispatching the event. |
| Cold wake | With no page loaded the shell loads `/?wake=1`; the home treats it as a wake and drops the flag from the address. A wake should reach the page once it is resumed; one that arrives while the page is hidden still waits (up to 60 s) for it to come to the front before the mic opens. |
| Speech output | A `speechSynthesis` / `SpeechSynthesisUtterance` stand-in over the native TTS when the WebView has none; `webapp/components/speech.js` is unchanged. |

`manifest.webmanifest` declares `launch_handler: {client_mode:
"focus-existing"}`, so a second launch of the installed PWA brings its open
window to the front as it is — an open thread, a draft or a recording under
way are kept; `navigate-existing` would load `/` over them. Known gap: Web
Push does not reach a page inside an Android WebView, so notifications still
need the PWA installed in Chrome alongside.
