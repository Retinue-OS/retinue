# Retinue Dashboard (PWA)

A minimalist, distraction-free dashboard served at the root of the web gateway
(`agents.example.com`, behind Traefik basic auth) and installable as
a Progressive Web App on the phone home screen.

## Architecture

- **Static shell, no server rendering.** `index.html` is the hand-editable
  configuration: it declares the active cards. (The app-launch dock — phone,
  SMS, mail, map — left the home: those are the Android shell's launcher bar
  now; see "Inside the Android shell".)
- **A conversation** (`components/conversation.js`) is one element,
  `<retinue-conversation>`: the thread with its bubbles (Markdown, copy
  buttons on quotes and code, click-to-fill chips, attachments — images the
  gateway serves inline get a preview in the bubble, sized from the dimensions it
  recorded at store time; a file's view link opens the file viewer,
  `viewer.html` / `components/viewer-page.js`, which keeps the navigation row
  and its ‹ above the image, text or PDF — a PDF only where a frame shows it
  whole, see docs/dashboard.md "Attachments" — model and cost meta), the pending state while Ara answers, the composer with text,
  file attachments (picked or pasted) and voice dictation, the model picker,
  and the read-aloud player (`<retinue-read-aloud>`, its bar, placeable by
  any host). Given a
  `conversation-id` it reads, polls and replies; given `for-project` and no
  id it is the composer whose first message opens the thread, then goes on
  as it. It reports outward with events (`retinue-back`,
  `retinue-created`, `retinue-sent`, `retinue-archived`, `retinue-open`,
  `retinue-thread`); drafts, dictation jobs and the reader outlive one
  instance, so a thread left and reopened has its text, and a reading goes
  on — and drafts and dictations outlive the page as well (see "Unsent
  input"). A host can shape the frame without forking the thread: `bar="none"` or
  `bar="actions"` for a header of its own, `continuous` to open it in
  continuous conversation (see "Voice conversations"), `stamp="clock"` beside a
  clock-stamped timeline, `no-autofocus` where the host keeps it mounted
  whether or not it is on screen (taking focus scrolls the element into view,
  which on the chat page's phone strip is a tab switch), and `create-url`
  where the thread belongs to
  something else and is minted by that thing's endpoint rather than opened by
  the first message. It takes `fill(text)` (into the composer, for the user to
  send) and `ask(text)` (send now, for a host whose control is the send
  press). Every surface that shows a conversation embeds this element.
- **Conversation tabs** (`components/conversations.js`) are the active
  interactive card: the list of threads with Ara, the Active/Archived filter
  and the location-hash routing, with the open thread (or the new-thread
  composer) being a `<retinue-conversation>`. The user can open a thread,
  and a retinue agent can open one when it needs a decision (e.g. an RSVP).
  It talks to the gateway's `/conversations` API rather than a static data
  file.
- **Curated data** lives in `data/*.json`. Today these are mock files; the
  static mock cards that consume them are commented out in `index.html` until a
  refresh job regenerates them (write target = `DASHBOARD_DATA_DIR`).
- **Offline:** `sw.js` caches the shell (cache-first) so the dashboard opens
  without connectivity. Data is network-first with cache fallback.
- **No inline script.** Every page is served with a Content-Security-Policy
  that runs scripts only from these files: no inline `<script>`, no handler
  attributes (`onclick=` …) in pages or rendered markup, no `javascript:`
  URLs, no `eval`. A page's script is a file of its own (`sw-register.js`,
  `components/*-page.js`; `gateway/` for the pages the gateway builds). See
  docs/dashboard.md, "The Content-Security-Policy".

## Serving

`scripts/web-gateway.py` serves this directory:

- `/` and `/index.html` → `WEBAPP_DIR/index.html`
- `/data/<file>.json`   → `DASHBOARD_DATA_DIR/<file>.json` (no-store)
- `/sparql`             → the life store, read-only (a query), or `sparql.html`
                          (a browser without one) — see `docs/dashboard.md`
- `/docs/<name>.md`     → `DOCS_DIR/<name>.md`, the framework docs that page renders
- `/sync-health`        → `sync-health.html`, the target of "update your health
                          data" links (see "Inside the Android shell")
- `/instance`           → GET/POST, the instance's name and accent colour the
                          Settings page stores (`scripts/instance_branding.py`,
                          docs/dashboard.md "Several instances")
- `/inbox/…`            → the letterbox's WebDAV share (`scripts/webdav_share.py`),
                          not a page — see `docs/dashboard.md`
- `/csp-report`         → POST: what browsers report of the pages'
                          Content-Security-Policy; GET: the distinct reports
                          kept (`scripts/content_policy.py`)
- everything else       → `WEBAPP_DIR/<path>` (shell assets)

`vendor/` is not committed: `scripts/vendor-yasgui.sh webapp/vendor/yasgui`
fetches YASGUI (pinned, checksummed) for the SPARQL page; the image build runs
it.

Env: `WEBAPP_DIR` (default `/workspace/webapp`), `DASHBOARD_DATA_DIR`
(default `WEBAPP_DIR/data`), `DOCS_DIR` (default `/workspace/docs`).

## The home: the attention list

The home screen is one list of what wants attention (`components/attention.js`
over `GET /attention`, design in `docs/attention-model.md`, mechanics in
`docs/dashboard.md`): threads with Ara, messenger chats and running projects as
one kind of thing, each with an importance, a deadline against a lead time, a
sphere, and the delivery the gateway decided — pushed, held for the next
digest, or listed. Sections Now · Next · Held · Waiting; every row shows its
preview and a one-line reason; ⓘ opens the details sheet
(`components/attention-sheet.js`, shared with the thread bar and the chat
page) with the three fields explained and correctable, the sender's permit
and the sphere's Focus rule for the mode in force, and Later / Pull / Mark
done. The mode chip opens the mode menu (`POST /attention/mode`). Rows open
where the item lives: a thread in place — the `retinue-conversations` element
sits on the home as a `viewer`, invisible until `#conversation-<id>` or `#new`
— a chat on `chat.html`, a project on `project.html`. `?item=<id>` deep-links
to an item's sheet. The list refreshes on the conversations cadence and after
every action on the sheet (`retinue-attention-change` on `window`).

`GET /attention` returns `{generated, now, timezone, mode: {id, name, blurb,
threshold, admits, admit_tags, manual, scheduled: {id, name, until}, day: {plan,
days, holiday}}, modes, schedule and digest_times (today's, by today's day
plan), week, holidays, spheres, next_breakpoint, sections: {now, next, held,
waiting}, counts, last_digest: {at, count}, unseen_digest: {at, count} | null, degraded, learned}`
(`unseen_digest`: the last digest sent, while it has open items and nobody marked it Done on any device —
`POST /attention/seen {digest: <its time>}` marks it); a row is `{id, kind: thread|chat|project,
title, preview, href, sphere, tags, sender, channel, group, agent, count,
unread, pending, level, critical, importance, importance_from, importance_text,
due, lead (minutes), lead_from, kind_label, urgency, delivery, reason, actor,
waiting_since, state, released, snoozed_until, pushed, digest_at, permit, admits_sphere,
admission: {by: vip|project|scope|sphere|tag, what, via?} | null}` — `admission` is the rule of the
mode in force that lets the row through, which the details sheet's switch changes;
`via` names the item's own sphere when `what` is one it lies within, and the sheet
then offers no switch (it would change the rule for everything the outer sphere holds).
The home shows what the unseen digest released first, on every device, until
*Done* on any of them; `/?digest=<time>` — the digest push's link — shows that
digest even once seen.
The actions: `POST /attention/items/later|pull|done|reopen|correct` with the
id in the body, `POST /attention/permits`, `POST /attention/admit`,
`POST /attention/seen`, `GET|POST /attention/profile`.

## Conversation tabs

The same gateway also backs the conversation-tabs card with a small JSON API
(all behind the dashboard's basic auth):

- `GET  /conversations`               — list of active threads (tabs). Edit-kind
  threads are excluded by default; `?kind=edit|all` includes them, and
  `?project=<uri>` narrows to one project's threads.
- `GET  /conversations?archived=1`    — list of archived threads.
- `GET  /conversations?all=1`         — list of all threads.
- `GET  /conversations/<id>`          — one thread with its messages.
- `POST /conversations`               — open a thread (`{"message": "..."}`,
  optional `kind: "chat"|"edit"`, `project: <uri>`, `project_title`).
- `GET  /projects/item?id=<uri>`      — a project's raw Markdown + `sha256`.
- `POST /projects/item`               — save a project file
  (`{id, content, base_sha}`; 409 with the current content on a conflict).
- `POST /conversations/<id>/messages` — reply in a thread.
- `POST /conversations/transcribe`    — voice input: POST recorded audio (raw
  body, MediaRecorder MIME type in `Content-Type`); returns `{"text","lang"}`.
- `POST /conversations/<id>/read`     — clear a thread's unread badge.
- `POST /conversations/<id>/archive`  — archive a thread (drop from active list).
- `POST /conversations/<id>/unarchive`— restore an archived thread (and clear
  `muted`, as the list's Restore does).
- `POST /conversations/<id>/flags`    — the Threads page's swipe and tray:
  `{archived?, muted?, starred?}` (booleans, at least one); answers the
  thread's summary.
- `POST /conversations/<id>/delete`   — erase a thread for good (no body);
  accepted only through the reverse proxy, never for a chat's companion. A
  deleted thread answers 410 Gone from then on.

Ara answers asynchronously, so the open conversation polls itself until the
reply lands (faster while a turn is pending), and the card polls the list.
Each thread maps to its own Claude session (key `conv:<id>`). Threads persist
under `CONVERSATIONS_DIR`, one file each — the deployment points this at the
persistent `/root` volume (`/root/.retinue/conversations`); the
`/tmp/web-tab-conversations` default is only for ad-hoc runs.

On the home the element is a `viewer` only — threads open from the attention
list's rows. *Threads* in the navigation row leads to `conversations.html`, a
dedicated page that lists every thread with an Active/Archived/Edits/Cowork
filter (the same `retinue-conversations` element with the `full` attribute).
Its tiles swipe with the chats page's rows' gesture — one implementation,
`components/swipe-rows.js`, so the two lists cannot drift apart; the chats
page's Unread and Read tabs opt into a full swipe left that commits beyond the
one that reveals the tray (a row's `data-quick-left`), and into selection by
long press, which threads do not:

| Tab | Swipe right | Swipe left (tray) |
|---|---|---|
| Active | archive at once | Star · Archive · Mute |
| Archived | restore at once | Star · Restore · Mute *or* Unmute · Delete |
| Edits, Cowork | archive or restore, as the row's state calls for | the tray for the row's state |

A long press, a right-click or the context-menu key opens the tray without
swiping; the click a swipe ends in never opens the thread, and a tap on an
open tile closes its tray. **Archive** is the thread bar's button (out of the
list until something new is filed into it), **Mute** keeps it out
(`{archived, muted}` together), **Unmute** lets the next message bring it
back, **Restore** brings it back and clears the mute — and so does the thread
bar's *Unarchive*. A commit that takes a row out of view offers **Undo** for
15 s (`UNDO_MS`; the toast stays while a finger or the pointer is on it),
writing back exactly the flags the row had.
**Delete** asks for a second tap and cannot be undone. **Star** keeps a thread
at the top of whichever list it is in — `starredFirst` in `base.js` sorts the
client side, starred first, each group still newest first — and does nothing
else; the open thread's bar has a ☆/★ for it too. Archived threads stay
reachable on that page and via `GET /conversations?archived=1`.

The wide layout itself is resizable, VS Code style (`layout.js`): the
boundaries between conversations, news and the projects column are draggable
splitters — double-click resets one, dragging news fully down closes it — and
each card's header toggles between list and card view. Both preferences
persist per device (`components/device-store.js`: `localStorage` in a browser,
the app inside the Android shell). Every page opens with the
navigation row (`components/nav.js`: Home · Chats · Threads · Projects · News
· Contacts, then the pending-sends outbox icon and the settings gear), pinned
while a list page scrolls. On a drill-down (a chat, a project, an open
thread) it starts with the back button: `<retinue-nav back>` dispatches
`retinue-back` and the page listening on the row decides where back goes.
The settings live on `settings.html`, reached via that gear: *This browser*
(notifications, the voice timeouts, the layout, the shelf size, the running
shell version with a manual update check) and *This instance*
(`components/instance-settings.js`: name and colour, agent models, system
pages). Inside the Android shell from RetinueShell/4 the gear opens the app's
own settings page instead, which embeds *This instance* from
`settings-instance.html` (docs/dashboard.md, "The settings page"). The address book (`contacts.html`,
`components/contacts.js`; docs/contacts.md) is the *Contacts* tab — people,
their handles on every channel, the e-mail domains they write from (sent as
`domains` on a new contact, `add_domains` / `remove_domains` on a change),
their attention settings, the VIP flag and
the instruction their messages are worked by, mail included — over the live `/contacts` API
(`tests/test_webapp_contacts.py`). The outbox icon opens the gateway's `/sends`
page, the queue of messages waiting for approval: a queue that wants action,
one tap away from every page. Those pages are the gateway's own; their one
script is `gateway/sends.js`, which on the status page of a decided send
polls `GET /sends/<account>/<id>/status` (`{status, error?,
outcome_unknown?, note?, next?}`) and says how it ended. An `error` with
`outcome_unknown: true` is no failure — the mail server was handed the mail
and its answer was lost — and reads "Not confirmed — it may have gone out"
with a question mark, never the cross and never "could not deliver"; the
gateway's own sentence (`note`) is shown where it sends one.

Shell updates apply themselves (`components/update.js`): when a new service
worker activates, controlled pages reload once — never while a thread or the
composer is open, a draft is unsent, or a text field is focused — and every
visibility change triggers a `registration.update()`, so an always-open
window doesn't wait for the browser's daily check.

A retinue agent opens a thread that needs the user's decision with
`scripts/conversation-push.py` (token-gated `POST /internal/conversations`):

```bash
conversation-push.py --title "Party RSVP" \
  "You've got an invite to Mara's party. Confirm and add to your agenda, or decline?"
```

The thread shows up with an unread badge; when the user replies, Ara picks it up
with full context. The endpoint is gated by `CONVERSATION_BACKEND_TOKEN` (set by
the entrypoint) so only in-container agents can post on the user's behalf.

## Messenger chats

The chat surface of the messenger-chats redesign: each messenger conversation
(Signal / WhatsApp / Telegram / SMS, one peer or group — and on e-mail one
mail thread, see *Mail threads* below) is a **chat** — a
deterministic mirror rendered like the messenger client — with its
**companion pane** (the conversation-with-Ara rail) beside it. It runs against
the gateway's chat API (SPARQL over the message ledgers plus a live overlay;
the endpoints and their rationale live in `scripts/web-gateway.py`'s module
docstring). Pieces:

- `components/chats.js` — the list, with `full` the whole `chats.html` page
  (the dashboard no longer carries a Chats card; the element's card mode, a
  capped glance at every chat that is not archived, is unused): avatar,
  channel mark, last-message preview and unread badge, ordered by last
  activity, in three tabs — **Unread · Read · Archived** — by each chat's
  `list_group` (below). The *Unread* tab carries the number of unread chats;
  each row keeps its badge with the number of unread messages, since one new
  message and twelve read differently. Rows swipe
  (`components/swipe-rows.js`, shared with the threads page: pointer events,
  so touch, pen and mouse drag alike):

  | Tab | Swipe right | Swipe left |
  |---|---|---|
  | Unread | mark read at once | uncover the tray; a full swipe (or a fling) archives at once |
  | Read | nothing (the row does not move) | uncover the tray; a full swipe (or a fling) archives at once |
  | Archived | restore at once | uncover the tray |

  The tray offers the same actions on every tab and every channel — **Star**,
  on *Unread* **Mark read**, **Archive** (out of the list until it speaks
  again) and **Mute** (out of the list, and the next message does not bring
  it back), on *Archived* **Restore** and **Mute** / **Unmute** (stays
  archived) in their place, then **Spam** and **Delete**. Spam is the one
  exception: mail threads only (`POST /chats/<id>/spam`: the mailbox moves the
  thread's inbound mails to its Junk folder, then the chat is muted and
  archived; a messenger chat has no junk folder and is muted instead). A
  right-click or the context-menu key opens the tray too, which is how a
  keyboard reaches what a swipe does. Muting archives — the server's rule —
  so both land under *Archived*. **Delete** (`POST /chats/<id>/delete`) is
  armed by a first tap and done by a second; the armed button reads "Delete
  for good?", and on a mail thread "Move mails to Trash?" — its mails go to
  the mailbox's Trash before the chat is erased, and its title says so.
  Everything else that takes a row out of view offers Undo for 15 s
  (`UNDO_MS`, the toast staying while a finger is on it): Mark read, Archive
  and Mute are made at once and Undo writes the previous state back (Mark
  read's the previous read mark, `POST /chats/<id>/read`, below); **Spam** is
  deferred — the row leaves, nothing is sent until the window ends, Undo
  sends nothing, and a page hidden or left sends it at once (`fetch`
  `keepalive`). An action that fails brings the row back, marked failed,
  with the server's one-sentence `error` in place of its preview (and for
  Delete and Spam a Retry in its opened tray; a 503 on Delete reads "Delete
  not set up").

  **Multi-select**: a long press on a row (or the list's *Select*) starts
  selection mode with that row selected — the long press no longer opens
  the tray. A tap (Space on a focused row) toggles a row, Shift-click on a
  wide screen selects a range, and a bar counts the selection and offers the
  tab's actions for all of it: Mark read, Archive, Mute, Restore / Unmute,
  Star / Unstar, Spam (only when every selected chat is a mail thread) and
  Delete. Esc, Cancel, the back gesture and a tab switch end the mode; a row
  that leaves the tab leaves the selection. One request per chat, four at a
  time (no bulk endpoint), and one Undo for the batch ("Archived 7 · Undo")
  that writes every chat's previous state back; failures are counted
  ("Archived 5 of 7 — 2 failed") and each failed row says why. A bulk Delete
  takes two taps ("Delete 7 chats? Mails move to the Trash"); a bulk Spam is
  deferred like a single one — a page left in its window sends every chat's
  request at once, with keepalive, not four at a time, and one left while the
  batch still goes out four at a time sends what has not gone yet at once — and where some of it fails once the window
  ends a notice — no Undo, it waits for any Undo toast on screen — counts
  them ("Marked as spam 5 of 7 — 2 failed"). The chat page's ⋮ has **Spam** (mail only:
  handed to the list, which carries it out with its Undo) and **Delete** (two
  taps; the page leaves for the list once the chat is erased, and shows the
  reason where it is not).
  A Mark read is about the row as shown: it names the row's `inbound_seq`,
  and where a message has landed that the list has not fetched yet (it
  refreshes every 15 s) the server marks nothing — the row slides back and
  the list is fetched again, showing what came in;
  a starred chat leads whichever list it is in. The tabs are a tablist
  (arrow keys move between them); the choice holds across refreshes and
  writes, and a fresh page opens on *Unread*. The list refreshes on an
  ambient cadence and keeps its last rendered state over a failed fetch.

  **Triage** (beside the tabs; [docs/chat-triage.md](../docs/chat-triage.md))
  opens a view of the chats sorted three ways: *Action suggested — see Ara*,
  *No action — proposed archive* (with **Archive all**, one Undo for the
  section) and *No action — proposed delete* (with **Delete all**, two taps,
  no Undo), then *Not sorted* (an unread chat with no word yet, one whose
  sorting failed, one spoken in since it was sorted). Opening the view is the
  press: `POST /chats/triage {}` sorts the unread chats that have no fresh
  word, and **Sort again** posts `{again: true}`. Every row is the list's own
  row — it opens the chat, swipes and has the tray as on its tab — with the
  sorting's one-line note under it, and on an action chat Ara's note from her
  follow-up in the chat's companion ("Ara is looking at it…" until it lands);
  an action row opens the chat on Ara's pane (`chat.html?id=…&pane=ara`).
  The view polls `GET /chats/triage` while a run sorts or a note is on its
  way, and is a history entry (back closes it); there is no multi-select
  inside it. A chat archived or deleted leaves the view.
- `chat.html?id=<chat id>` (`components/chat-page.js`) — one chat: bubbles
  with day separators and a "new" waterline (before the first message that
  was unread when the chat was opened — the last `unread` inbound messages of
  the page — and staying there while the page is open), a **Mark as read**
  button under the latest inbound message while the chat is unread (below),
  sender labels in groups, the
  author on every outbound bubble (you / Ara / your phone), inline media
  (images with a lightbox, voice-note and video players — see the Message
  contract below), a live composer (send, shared draft, one-tap clear ✕,
  dictation, file attach — any type, picked or pasted; images are downscaled
  client-side),
  quick-pattern chips,
  and the companion pane — swipe between panes on a phone, a draggable
  splitter on a wide screen. The composer row is the conversation composer's
  row: mic on the left, send on the right, both always there, and the paperclip
  inside the text field (whose chooser is also the camera on a phone) — one
  layout across both surfaces, and one place to reach for each affordance.
  Keeping only two round controls is what buys the field its width, since each
  one costs 46px of a phone's; the chat field additionally carries the clear ✕,
  beside the clip and only while there is something to clear. A clear is
  **undoable**: the ✕'s slot becomes an undo that restores the draft through
  `POST /chats/<id>/draft/undo` — verbatim, its "drafted by Ara" marker
  included — and lapses after 12s or as soon as the user has moved on (typing,
  dictating, sending, or Ara staging a new draft). One tap emptying the box is
  only safe if the tap can be taken back, and a staged draft is not something
  the user can retype.
  Back — the navigation row's button above the header — goes back where the
  chat was opened from within the app, and to the home — the attention list
  the chat is a row of — for a chat opened cold (a notification, a
  bookmark). The header's ⋮ menu holds
  *Attention details* — the chat's attention sheet, with the contact card —
  and the **Star** / **Archive** / **Mute** switches, the same
  `POST /chats/<id>/flags` the list's swipe actions use; a starred chat's
  name carries a ★, and a dot on the ⋮ shows an archived or muted chat with
  the menu shut. What the companion pane does sits behind a **?**
  in its bar rather than on a row of its own. While the phone keyboard is up —
  `data-keyboard` on `<html>`, which `viewport.js` sets when a text field
  has focus and the visible frame is well short of its keyboard-less height
  at that width (re-learned whenever no field is focused, carried across a
  rotation with the keyboard up, dropped while pinch-zoomed) — the header
  and the companion's bar are hidden, and the pane being typed in is kept
  scrolled to its newest message; the navigation row steps aside with them.
  On a short frame (the Android shell's, above its launcher bar) the thread
  keeps its room: the chips — one sideways-scrolling row on a touch screen —
  and a standing instruction's report (its flags first) share a region capped
  at a fifth of the pane that scrolls inside and steps aside while the
  keyboard is up, and the field grows to at most 30% of the pane; the thread
  gives way before the send row does. A **grip** on the line between the
  thread and the composer hands that split to the user: dragged up, the
  composer takes the height it is given — the chips and the report stay in
  view even with the keyboard up (snapping in once the drag is nearer the
  composer with them than without), the field fills what is left, and the
  thread keeps a strip of at least one message wherever the composer's own
  contents leave room for it; dragged back down to the composer's own
  height, or tapped twice (Enter from the keyboard, arrows to step), it is
  the composer it was. The keyboard-up and keyboard-down frames each keep
  their own position, per device beside the splitter's. A press on the grip
  leaves the focus where it was, so the phone keyboard stays up through a
  drag, and a press that dragged is never also a tap on whatever has moved
  under the finger. The grip is a separator with a value (the composer's
  share of the pane), stepped and reset from the keyboard as above. A draft
  longer than its box shows an
  expand control in the field's top-right corner: the field then takes the
  whole pane at its full width (the thread and the chips step aside, the mic
  and send sit under its corners) until the control is pressed again, Escape
  is pressed, a recording starts or the draft is sent. Shrunk again by the
  control or Escape, the thread is back at the message it was left at. A tap
  on the control leaves the focus where it was, so the phone keyboard stays
  as it was.
  The open chat polls on the conversations cadence, appending only unseen
  messages. **Opening a chat, looking at it or scrolling it reads nothing.**
  A chat is read by a reply — a send from the composer (the server reads the
  chat with it, so the page posts nothing for it), or, on Signal, WhatsApp
  and Telegram, one from the phone or any other client, which the next poll
  shows (an SMS sent outside Retinue is never seen, so such a chat needs
  Mark as read) — or by **Mark as read**: the button under the latest
  inbound message, there whenever the chat is unread. It posts
  `POST /chats/<id>/read` `{seen}` — the `inbound_seq` of the last poll whose
  messages are in the thread — and gives way to *Marked as read* with an
  **Undo** for a few seconds (the restore call below; "nothing to undo" when
  the mark has moved on since). A 409 means a message landed that the page
  had not shown: nothing was marked, a note says so, and the poll brings the
  message with the button under it. The Undo ignores a press in its first
  half second, since it appears where the button was and with its focus — a
  double tap or a double Enter would otherwise undo the read it made. Focus
  stays on the row, and moves to the message it sat under when the row goes
  with nothing in its place (the Undo lapsed, or failed).
  Every poll takes the chat summary's `unread`, `list_group`, `last_read`,
  `inbound_seq` and flags from the answer rather than keeping the ones from
  the first load, so the button moves under a newer inbound message, goes
  when the chat was read elsewhere, and the ★, the ⋮ switches and its dot
  follow a flag changed elsewhere. A poll that left before a read or a flag
  switch made on this page does not draw the older state back over it. A
  minimized chat on the shelf carries the server's unread count.
  The companion pane is the chat's own conversation with Ara (see the
  `companion` field below), and it *is* `<retinue-conversation>` — the same
  element the conversations card embeds, so the two surfaces cannot drift
  apart. It therefore has everything that element has, the model picker with
  its escalated state and the read-aloud player included, rather than the
  subset a hand-written copy happened to carry. What this page supplies is
  where the pane sits and how its thread comes to exist: a companion belongs
  to its chat, so the chat mints it (`create-url` → `POST
  /chats/<id>/companion`) on the first turn and never on merely opening a
  chat. `bar="actions"` keeps the picker and the speak-replies toggle while
  dropping the title (the bar above names the pane) and Archive (a companion
  is not filed away separately from its chat); `stamp="clock"` matches the
  mirror beside it. A chip is a canned turn the page hands the element
  (`ask`), exactly as if the user had typed it — **Propose** being the manual
  form of what a forwarded arrival now runs by itself, so the pane may already
  carry Ara's note on a message before the user asks anything. The two rails meet in the shared draft — Ara
  stages a reply, the chat poll adopts it into the composer marked as hers
  — into an empty box, or over the text she was asked to rework, whenever
  nothing unsaved is in it (unsaved keystrokes meet the newer draft at
  their own save, as a conflict) — and the send press stays the user's.
- **Mail threads** (channel `email`, `docs/mail-chats.md` for the whole
  design) are chats in every sense above; what differs is this. The **row**
  carries the thread's subject as its name, an `@` channel mark, and a third
  line between name and preview: the participants by name (the address where
  neither the address book nor the mail gave one), with "+n" past the
  server's cap. Its avatar is keyed on the summary's `peer` address rather
  than the chat key — the key is the thread's root Message-ID, so keying on
  it would give one person a colour per thread — and shows the peer's
  initials. The preview reads "Name: text" for every inbound mail (the row's
  name is a subject, so nothing else says who wrote), "You (mail client)" for
  a mail sent from another client, and "Removed in a mail client" where the
  latest mail is gone. The account label follows the rule every channel now
  has: a name repeated **across accounts** carries the account; two threads
  on one account that share a subject carry none (the label would be the
  same on both). The **header** shows the subject, the participants and
  `via <account>` on two lines — never the Message-ID key. **Bubbles**: an
  inbound mail in a group carries its sender's name (the address without a
  display name), in a 1:1 none, as on every channel; a mail whose own subject
  is not the thread's shows it as a small line above the text, reply and
  forward markers aside (told by their shape — a short token and a colon, a
  bracketed list tag — so no language's "Re:" is privileged); a mail sent
  from another client is "You · mail client". The text is the server's
  trimmed text, rendered like any message (escaped, http(s) links, line
  breaks kept), attachments under the rules below. **Original**: a bubble
  whose message names an `original` offers a control that expands the whole
  mail in the bubble — From, To, Cc, Date and Subject, the full header block
  behind an *All headers* disclosure, then the HTML part in a sandboxed frame
  (`<iframe sandbox="allow-popups allow-popups-to-escape-sandbox">`, plus
  `allow-top-navigation-by-user-activation` inside the Android app, whose
  WebView opens no new windows — a tapped link there goes to the phone's
  browser, docs/dashboard.md; built by
  DOM calls, sized by an estimate from the text within a cap, resizable, with
  its own scroll) or the full plain text when there is no HTML part, then
  every attachment; a second tap gives the trimmed text back. It is fetched
  on the first tap and cached per message, with a loading line and an error
  line with *Try again*; the open state, and the disclosure's, live on the
  element, so neither a poll nor a re-render collapses it. The page takes the
  `original` URL only when it is exactly this chat's own
  `/chats/<id>/messages/<token>/original` (the id segment, decoded, is the
  open chat's id; the token one URL-safe segment that is no dot segment), and
  frames only `html_url` equal to that URL plus `.html`; anything else gets no
  control and no frame. A **tombstone** — a mail deleted or moved away in a
  mail client (`removed`) — is a quiet outlined bubble saying so, with who it
  was from and when (the removal time on hover), and no text, Original or
  attachment. The poll **reconciles**: a message already on screen whose
  removal, text, trim, attachments, subject or original changed is
  re-rendered in its place, the rest of the thread, the scroll position and
  an Original that still applies left as they are. **The model trim**
  (`docs/mail-chats.md`, "The model trim (phase 4)"): a mail whose `trim` is
  `wanting` is asked for on first view — its bubble watched by an
  IntersectionObserver (the viewport as root, so a phone's pane scrolled
  aside does not count) and asked for once it has been at least half
  visible (half the thread's view, for a bubble taller than that) for
  600 ms without a break — never while the page is hidden, the companion's
  pane is the one on a phone, or one of the page's full-screen layers (the
  image lightbox, the attention sheet, a modal) covers the thread, and the
  time starts again whenever the page comes back (shown again, restored
  from the back-forward cache, or thawed from a freeze, which a look that
  comes far too late betrays). One `POST` per
  mail per page life to the sibling of its accepted `original` URL
  (`…/<token>/trim`, never built from parts); an answer `wanting` names
  when it may be asked again (`retry_after`, held to 30 s – 24 h, ten
  minutes where it names none), no answer at all (offline, a 5xx) a minute.
  A settled answer replaces the text in place like a poll's change — held
  over a poll whose document predates it — and a `202` (`queued`/`running`)
  shows a quiet "Tidying up…" under the text, the page's own words, until a
  poll shows the record settled or for two minutes at most — put into the
  bubble's own node and taken out of it, never a bubble drawn anew (a
  selection, the focus and every frame stay), and nothing at all while the
  bubble's Original is open, which shows no such line. A server without the
  endpoint — `405`, `501`, or a `404` that is not the endpoint's own (the
  route fallback's `{error: "not found"}`, or no JSON) — switches the
  feature off for the page's life, quietly; the endpoint's own `404` (a
  token that names nothing in this chat) and `410` (a mail gone from the
  mailbox) end that one mail, and the others stay watched. Nothing is watched on a messenger chat, a settled mail, a
  tombstone or a page that stopped polling; the observer lets go of a
  bubble re-rendered or removed and of everything on a chat switch, and an
  answer that comes back after one touches nothing. The Original of a mail
  whose `trim` is `model` is headed by one line, "Shortened by a model —
  this is the full mail."; nothing else marks it. And a chat whose summary
  says `can_send: false` (its gateway unknown or not connected) shows
  `cannot_send_reason` in one line where the composer, its chips, the draft
  and the grip would be; nothing is loaded into, adopted by or saved from a
  draft there. That is one switch (`_canSend`), followed on every poll.
- **Writing mail** (`docs/mail-chats.md`, "Writing"). The **composer** of a
  mail chat says where the reply goes — and that line is the server's word,
  never the page's. `GET /chats/<id>/reply-preview` answers how the gateway
  would address a reply right now, for Reply all and for Sender only
  (`replyPreview` reads it whole or not at all); the page works out nothing
  of it — no Reply-To, no "the user's own mail is the latest", no alias — and
  asks on load, after every poll that changed the thread (a mail added,
  changed, removed or gone, a pending send, the participants) and again at
  the press. The line reads "To <name> <address> · Cc …": every recipient
  with its **address**, a name only beside it (the address book's before the
  mail's own, and none at all where a display name reads as an address
  itself), and a To that is not where the latest mail came from saying why
  ("the Reply-To of the mail from …"). It wraps, so no recipient is out of
  sight; the send button's label names them too. **Reply all / Sender only**
  (two radio buttons under the line, Reply all by default) is offered
  exactly when the preview's `choice` is true, and is put on the page and
  taken off it as that changes. Until the server has answered — or where it
  has no such answer (an older server: "Recipients unavailable"), cannot be
  reached, or says no reply is possible (409/400 with its sentence, e.g. a
  chat that holds only a queued mail) — the line says so, the send button is
  off, and **no reply is sent**: recipients are never guessed. The box reads
  "Write your reply …"; **Enter** breaks a line, the send button or
  **Ctrl/⌘+Enter** sends (an input method's Enter only commits) — as on every
  chat, which the field's label says on mail. Files are staged as on every
  chat and go as the mail's attachments.
- **The press.** The reply mode is the one on screen at the press (read
  before anything is awaited; the choice resets to Reply all there and comes
  back with the words if the mail does not go). The recipients are then asked
  for once more, and the mail is posted only if that answer is there, is not
  older than the thread on screen, and addresses the reply as the line did;
  otherwise nothing is posted, the words come back and the line shows what
  the server now says. The request carries `reply` and a **`request_key`** —
  one per press — and its answer is read strictly: **200** is the mail;
  **202** `{pending}` a send that waits for approval; **202**
  `{unconfirmed, message_id?, request_key}` a mail handed over whose outcome
  nobody knows yet (below). The server's word that **nothing went out**
  with this request — any **4xx** (its own refusal; **424** where the mail
  server refused the mail) and any answer that carries **`"sent": false`**
  (a 502 for a mail gateway that was never reached, a 503 for one that
  cannot send) — is "Not sent: <its sentence>", and the words stay in the
  box. Whose word it is decides what becomes of the key (**`"by"`**):
  `"gateway"` is the mail gateway's own answer about this request, made
  after it looked the key up — the press is over, and the next press is a
  new attempt under a new key; `"dashboard"` is the web gateway answering
  for itself (the mail gateway never reached, no gateway for the mailbox, a
  sign-in run out) — the key stays with the words, which costs nothing,
  since the gateway takes a key it never saw as a new attempt. An answer
  without `"by"` (an older server, a proxy) counts as the gateway's for a
  first press. A press **made again** under a kept key whose first outcome
  nobody knows is told "Not sent" only by the mail gateway: refused by anyone
  else, the request was never put to it, so the note says the server could
  not be asked and that it is still not known whether the first attempt went
  out, and the key stays — dropping it there could send the mail twice.
  Anything else — no answer, a 5xx that does not say `"sent": false` (a
  proxy, a crash), a 200 that is no document — says nothing about whether
  the mail went out and is never reported as "not sent": the words come back
  with the key they went under (kept on the device with them, for twelve
  hours), so the same words sent again are the same mail to the gateway and
  not a second one; a press made again whose first mail had gone out says
  so. Messenger sends are as they were.
- **The key's life.** One key per press, never the next mail's. It is kept
  (`_retry`) while the box still holds its words as they were sent — with a
  mark (`sure`) where it is known that nothing went out under it, so that a
  press made again under it is not told "not known". It is dropped on the
  mail gateway's word (`"by": "gateway"` with `"sent": false` or a 4xx;
  for a first press also an answer without `"by"`); as soon as the box
  holds other words, even for a moment (typed, cleared, dictated, a draft
  from elsewhere — the same words typed again later are a new mail); and
  when the press turns up in the thread (`_settleRetry`, on every load and
  poll; never for a key marked `sure`): the server lists a send
  under its key, or the thread holds one more of the user's mails with those
  words than at the press. Then the words leave the box too, offered back as
  after a ✕ — left there, one press would send them a second time — unless
  the listed send failed for certain, which is the server's word that the
  press did *not* go: the key is dropped and the words stay. While a press
  is on its way, a poll that lists it (`sending`, under its `request_key`)
  draws no second bubble beside the optimistic one.
- **A mail that was never confirmed** (202 `{unconfirmed}`). Its bubble
  stays, "Sending — not confirmed yet"; the words are **never** put back in
  the box unasked. They are then in no box and no draft, so the press is
  kept on the device (`chat-unsure:<chat id>` in drafts.js: its words, reply
  mode, the recipients its line named, its key and Message-ID) and the
  bubble **comes back after a reload**, in its place by time, until it is
  accounted for: the mail shows in the thread (by `message_id`), the server
  lists the send (by `message_id` or `request_key` — being sent, or failed
  with its reason — and that bubble takes over, with the focus the old one
  had), or the user asks. **Check again** posts the very same body under the
  same key — but since a gateway that never had the key would *send* on that
  request, it is held to what the press was held to: the recipients are
  asked for first, and nothing is posted unless a reply in the press's mode
  still reaches exactly whom the line named then ("Not asked: the thread has
  changed …", and the bubble no longer offers the question). Its answer is
  the mail (200), the pending bubble (202 `{pending}`), still unknown (202
  `{unconfirmed}`), or the mail gateway's word on this very press (`"by":
  "gateway"`, with `"sent": false` or a 4xx) — "Not sent: …", with the
  words back in the box. Any other answer (the dashboard's own — a gateway
  that could not be reached *now*, a sign-in that ran out —, a proxy, a
  server too old to say `"by"`) is about the question, not the mail, and
  leaves it unconfirmed. Check again is offered where the server echoed the
  key, the body can be rebuilt (no attachments after a reload) and the press
  is not older than twelve hours. Once asking has settled nothing, or cannot
  be done, the bubble offers the two ways out a failed send has: **Copy
  text back to the composer** and **Dismiss** (the page's own bubble;
  nothing is asked of the server). Where the chat cannot be written to there
  is no composer: the control reads **Copy text** and puts the words on the
  clipboard, saying whether that worked; the bubble keeps them until it is
  dismissed. A mail nobody can account for never ends in silence. The
  record is shared by every tab on the chat: each tab writes it as it
  stands, less what that tab let go, with its own mails in it — never its
  own list alone, which would erase another tab's mail. What it holds is
  read as data (a key that is none, a time before the epoch or ahead of the
  clock are not used), and a record that still cannot be read leaves the
  chat to open without those bubbles, the record kept.
- **Pending sends.** A mail goes under its account's send policy, and a send
  the policy queues answers **202** `{pending}`: the optimistic bubble gives
  way at once to a **pending bubble** at the thread's end — the words, "You
  · not sent yet", "Waiting for approval" in words (and in its accessible
  name; the note line, a live region, says it was queued) and an **Approve
  or reject** link to its page on `/sends`. The link is built by DOM calls,
  and only from an `approval_url` that is exactly a path `/sends/<slug>/<id>`
  (each segment one run of URL-safe characters, not starting with a dot; no
  query, fragment, host or escape); anything else gets a plain sentence
  instead. The messages document's `pending` carries every queued send of
  the chat, whoever queued it (composer, companion, `mail-push.py`), oldest
  first, and the page follows it on every load and poll: new entries join at
  the end (messages arriving go in above them), a changed one is redrawn in
  place, one no longer listed leaves — unless it was queued from here after
  that poll left — and focus on its link goes to the mail it became, else to
  the thread. Pending bubbles are matched by `request_id`, and to the mail a
  released one became by its `message_id` (the Message-ID the gateway minted
  when it queued it) — never by their words, which on mail are long and come
  back trimmed. An entry also says what became of a send that was let go
  (`status`): **`sending`** ("Sending — not confirmed yet", nothing to
  approve) and **`failed`**, which stays until the user dismisses it — "Not
  sent" with the server's reason (`error`, as text), or, with
  `outcome_unknown`, "Not confirmed — this mail may have gone out" — and two
  controls: **Copy text back to the composer** (after what the box holds,
  never over it; **Copy text**, to the clipboard, where the chat cannot be
  written to) and **Dismiss** (`POST
  /chats/<id>/pending/<request id>/dismiss`; a refusal leaves the bubble
  with the reason). Recipients the mail server refused (`refused`) are named
  on the pending bubble and on the sent mail's. An entry marked
  `dismissed` is over — the gateway keeps one for a day so that its key
  stays known — and is never drawn, whoever passes it on. A page left while
  a send was on its way does not put a mail that was queued back into the
  box. A chat
  in the list whose latest entry is a queued send previews it as "Waiting
  for approval: …" (or "Sending:", "Not sent:", "Not confirmed:" where
  `last.status` says so).
- **New mail.** The Chats page's **＋** (only where `GET /chats/compose`
  offers something; the mailboxes it names are asked for again each time
  the ＋ is opened) opens a short menu — *New e-mail* — and that the
  **compose sheet** (`components/compose-sheet.js`): a modal dialog (the page
  behind inert, focus in To, Tab kept inside; Escape, ✕ or a press on the
  dimmed page closes it and gives the focus back to the ＋) with From (a choice only when the offer names
  several mailboxes), **To** and **Cc** as removable chips — typed (a comma,
  a semicolon, Enter or leaving the field makes one; Backspace in an empty
  field takes the last back), pasted (`"Doe, Jane" <jane@x.org>, b@y.org`
  is two), or picked from the address book's suggestions (`GET
  /contacts?q=`, the matching people's e-mail addresses, a combobox walked
  with the arrow keys; leaving the field ends them, and an answer never
  opens a list under a field that does not have the focus) — Subject and the
  text. Addresses are checked by shape only; what is not one stays in the
  field with its error under it, and so do the addresses past the cap of 50,
  which the error names. An input method's Enter only commits what it
  composes (also WebKit's, told by keyCode 229). Send (or Ctrl/⌘+Enter in
  the text) posts `POST /chats/compose` with a `request_key` and, on 200 or
  202 with the new chat's id, goes to the new chat (`/chat.html?id=<id>`),
  which may hold only the pending bubble. **One press is one mail**: while
  the request is on its way nothing starts a second one — closing the sheet
  and opening it again brings back the same sheet, still sending — and a
  send that fails after the sheet was closed opens it again with what
  happened. The server's word that nothing went out — a 4xx, or any answer
  with `"sent": false` — shows as "Not sent: …" with everything still
  there, and ends the press where it is the mail gateway's (`"by":
  "gateway"`; for a first press also one without `"by"`): the next one is a
  new attempt under a new key. The dashboard's own keeps the key with the
  mail, known to have sent nothing. A press made again under a key whose
  outcome nobody knows, refused by anyone but the mail gateway, says that
  the server could not be asked and that it is still not known whether the
  first attempt went out, and keeps the key. An answer that leaves open
  whether the mail went out (202 `{unconfirmed}` without a chat, a 5xx that
  does not say `"sent": false`, none) says exactly that, and the press stays
  with the mail, so that sent again unchanged within twelve hours (the chat
  page's bound) it goes out under the same key; a press older than that is
  dropped — the gateway knows a key for a day, and the same request would be
  a second mail — and the sheet says only to look in the chat list before
  sending it again, as a new mail. Closing keeps what the sheet
  holds on the device (`compose:<channel>:<mailbox>` in drafts.js, with the
  unanswered press) for the next opening **from that mailbox**: a kept mail
  never comes back into a sheet writing from another one, and changes its
  sender only where the user picks another mailbox under From. A mail kept
  for another mailbox is not forgotten either: the index
  (`compose:<channel>`: `{account, waiting}`) remembers which mailboxes hold
  one beside the mailbox written from last, and every opening names each —
  "It comes back when that mailbox can send again" for one that is not
  offered now, "Choose that mailbox under From to go on with it" for one
  that is — and opens on such a mailbox when the one written from last has
  nothing waiting. A mail whose mailbox is not offered at all may never be
  again, so the sheet shows it ("Show the mail from …": its recipients,
  subject and words, as text) with **Copy into this mail** (into the form,
  after what it holds — recipients joining the chips, its words after the
  form's, its subject where the form has none — leaving from the mailbox
  under From; the kept one goes) and **Discard it**. Every opening renews
  each kept mail it names, so drafts.js never drops one for its age while
  the sheet still speaks of it. Discard empties a mail; a send that went
  through drops it.

The API, as the components consume it:

- `GET /chats` — `{generated, counts: {unread, read, archived}, chats:
  [ChatSummary]}`, ordered by last activity; `counts` is how many chats each
  tab holds. A `ChatSummary` is `{id, channel, account, key, name, group,
  members?, unread, list_group, last_read, inbound_seq, archived, muted,
  starred, contact, unknown_sender, companion, instructed, last, draft,
  messages}`.

  A chat is one peer **on one account**, and `id` names both:
  `<channel>:~<account>:<chat-key>`, or `<channel>:<chat-key>` for history
  written before the ledger recorded the account. `key` is the chat key on its
  own — the exact recipient string that channel's send path accepts — and
  `account` the gateway account, or null for that older history. Both are
  surfaced because the name identifies neither: two accounts talking to the
  same person are two chats called the same thing, so the client labels a
  name repeated across accounts with its account (not one repeated on a single
  account, where the label would be the same on each), and keys the avatar
  colour on the *peer* so one person keeps one colour across all of them. Clients must treat the id as
  opaque and read `key`/`account` rather than splitting it.

  `unread` is the number of inbound messages the user has neither answered
  nor marked read: those stamped later than both the read mark (`last_read`,
  set only by Mark as read and by a reply; null for a chat never marked) and
  the chat's latest outbound message of the user's own — so a reply that
  reached the ledger without the dashboard still reads the chat — or, where
  that is more, those that have landed since the newest read, whatever they
  are stamped (a delayed delivery is stamped before a mark it arrived
  after). `inbound_seq` is how many arrivals this summary includes — one per
  message, however often the channel delivered it; a client
  hands it back as `seen` with Mark as read (below) and does nothing else
  with it. `list_group` is the tab the chat belongs
  in: `archived` when archived, else `unread` while `unread > 0`, else
  `read`; a new inbound message moves a read chat back to `unread`. (It is
  not `group`, which stays the boolean "this is a group chat".) A client
  meeting a payload without `list_group` derives it by that same rule.
  `contact` is the address-book card the chat is linked to (or null), and
  `unknown_sender` whether its peer is a stranger: nothing on the rail
  vouched for the last sender, and no card names them. `last` is
  the preview `{ts, direction, author?, sender_name?, text, kind}`, `draft`
  is the shared draft `{text, author, agent?, ts, version}` or null, and
  `companion` is the conversation id of this chat's companion thread (null
  until one exists). `instructed` is null unless a sender's standing
  instruction applied to a message here (docs/contacts.md, "The
  instruction"): then `{state: working|done|failed, person, instruction, vip,
  since, finished?, verdict?: {importance, due, actions, flags}, delivery?,
  reason?, fallback?, failure?}` for the newest such message — what the chat
  page shows above the composer while the turn works and after.

  Nothing here says whether Ara works a message on arrival: that follows the
  **sender** (their VIP flag or standing instruction), not the chat, so it
  holds wherever that person writes. See `docs/triage-delivery-gate.md`.

  `messages` is the URL of the chat's message document — the client follows
  it and never constructs message URLs. `archived` and `muted` carry the
  dashboard-conversation semantics verbatim: an archived chat leaves the
  Unread and Read tabs (the Archived tab keeps it reachable),
  and a new inbound message **un-archives** an archived chat unless it is
  muted — the server's rule, applied on the notify rail. `muted` silences
  that chat's Web Push and keeps an archived chat archived; setting it also
  sets `archived`, so a caller never has to send both. `starred` is the
  user's mark: the chat sorts first in whichever list it is in, and nothing
  else changes — mute, restore and an arrival all leave it. `POST
  /chats/<id>/flags` (body `{archived?, muted?, starred?}`, any of them) is
  the one way in, and the answer carries the flags as they ended up — a
  client reads them back rather than assuming what it asked for. Like a
  send, it is accepted only through the reverse proxy: `403` from inside the
  container where `EDGE_PROXY_PEERS` names the proxy — without it only from
  loopback and from the address the caller dialled, so a caller coming in
  from another of the container's addresses, or through a sibling that
  relays, passes. An agent uses `scripts/chat-flags.py`.

  `POST /chats/<id>/spam` (no body; mail chats only, 400 for a messenger
  chat) asks the thread's mail gateway to move the thread's inbound mails in
  INBOX and the archive to the account's Junk folder (`POST /chats/spam`
  `{chat, account, message_ids}`) and, only once it has, mutes and archives
  the chat on the user's word: `{id, spam: true, moved, archived, muted,
  starred}`. On any failure nothing changes and the answer is `{error}` —
  one sentence (no Junk folder: it names `MAIL_JUNK_FOLDER`), 502 or 503.
  Same-origin and through the reverse proxy only, like delete. The mails
  stay in Junk when the chat is restored.

  `POST /chats/<id>/delete` (no body) is not a flag but an erasure: every
  inbox gateway of the channel is asked to erase the chat (its own
  token-gated `POST /chats/delete` `{chat, account}`) — the ledger records of
  both directions and the media only they reference, plus that gateway's
  pending-send files and recent-senders entry for the peer — and then the
  chat's state document, live overlay entries and companion thread (with its
  attachments and Claude session transcript) go. No trace stays beyond a
  minutes-long in-memory tombstone naming exactly the erased records (as the
  gateways report them), which hides the store's not-yet-reindexed copy; the
  next message from the peer starts a new chat, however soon it lands. The
  answer is 502, and the chat, its state and companion stay for a retry,
  when no gateway could erase or one reports a file it could not remove
  (an unreadable record, or one that names the chat but no longer parses,
  counts). Like a send, a delete is accepted only through the reverse proxy
  (403 otherwise): an agent may archive or mute a chat, never erase it. The
  gateway hop carries, beside the ordinary gateway token (which agent
  sessions hold, to send), `X-Chat-Erase-Token`: the `CHAT_ERASE_TOKEN` set
  on retinue and the gateways, which no session inherits. Without it the
  answer is 503 and the Delete button says it is not set up. For the
  tombstone window, a late rail event for an erased message is accepted and
  dropped (no state, no push, no turn), and a leftover companion request for
  the deleted chat answers 404 until a new message recreates the chat.
  Deliberately out of reach: what agents derived from the chat elsewhere
  (memories, news-feed items, project notes), notifications already
  delivered, and the messenger app's own copy on the phone.

  These two flags replaced the messenger **sender blacklist**: not wanting to
  hear from someone is a chat one mutes, in the interface, on the chat one is
  looking at — not an entry in a policy file only Ara can edit. (E-mail has
  no whitelist either: a mail chat is archived or muted like any other, and
  whose mail is worked on arrival is a contact's VIP flag or instruction.)
  Ara can set either flag too, through the same
  endpoint, but the user never has to go through her.

  Both are **independent of the triage delivery gate**, on purpose. Whether a
  group's messages are filed to the news feed for the Herald, and whether they
  are worth a model turn, is the policy's business (`scripts/triage_policy.py`
  `news-add` / `ignore-add`, see `docs/triage-delivery-gate.md`); whether the
  user wants the chat in their list is this flag's. A subscribed channel one
  keeps only for its content is `news` + `ignored` **and** muted — three
  separate statements, because a list one both reads as news and answers in is
  `news` + `quieted` and stays visible. Favourites-on-top is the `starred`
  flag above, shared with the threads. A store outage is answered
  from the last good list for up to `CHAT_LIST_STALE_SECONDS` (10 min by
  default), then with an honest 502 (the page shows it; the card keeps its
  last state).
- `GET /chats/<id>/messages` — `{generated, chat: ChatSummary, messages:
  [Message]}`, ascending by `ts`, the newest page by default. Its `chat`
  carries `unread`, `list_group`, `last_read` and `inbound_seq` on every
  poll — what the page's Mark as read button and waterline follow, and what
  its mark names: the count is taken before the messages are collected, so
  every arrival it includes is on the page;
  `?before=<ISO ts>` pages older history (the page renders the newest page —
  a load-older affordance is future work). A `Message` is `{id, chat,
  direction, author? (out: user|agent|device, plus agent name),
  sender?/sender_name? (in), text, lang?, ts, attachments?: [{id, url?,
  type?, size?, width?, height?, name?}]}`. Attachment URLs are the web-gateway's
  authenticated media proxy (`/chats/media/…`), which serves a file in place
  only as an inline-safe image, audio, video, PDF or plain text and as a
  download otherwise (`docs/dashboard.md`, "Attachments"). A reference that
  names no blob is passed on as the `url` only when it is an absolute http(s)
  URL, and the attachment has no `url` otherwise; the page renders an image,
  a player or the lightbox only for a proxy URL, a link only for a proxy URL
  or an http(s) URL, and the name as plain text for anything else, so a
  reference from outside never becomes an `href` or a `src` of another kind.
  An http(s) URL is a link to open whatever the type, never loaded as an
  image or a player: the origin's Content-Security-Policy allows media from
  this origin only (`docs/dashboard.md`, "The Content-Security-Policy"). The
  page also takes a `data:image/…`, `data:audio/…` or `data:video/…` URL (the
  reference documents' media; the gateway passes none on, and the policy
  allows none on the chat page), only for the `<img>`, `<audio>` or `<video>`
  of its own kind (and an image's lightbox), never for a link. Type, size and name are what
  the storing gateway stated about the blob in its ledger record (absent on
  records older than that statement). `width`/`height` are the medium's real intrinsic size, sniffed
  at ingest — when present the client reserves the true aspect box before the
  bytes arrive, when absent (older records) it reserves a fixed placeholder
  frame; either way a lazy load can never shift the thread's scroll. By type:
  `image/*` renders inline and opens a full-screen lightbox on tap (same
  proxied URL — it is the original; tap, Esc or the platform back gesture
  closes, one history entry deep); `audio/*` is a voice note — the player
  above the transcript, which is already the message `text`; `video/*` is an
  inline player (`preload=metadata`, box-reserved the same way); anything
  else (a document, an animated sticker) is a file row showing the stated
  `name` and `size` that links the file: a PDF or plain text opens in a new
  tab, anything else is saved, as the proxy serves it. Reactions and quoted replies (issue #130) will
  decorate these records later. The companion thread is not in this payload:
  it is an ordinary conversation, named by the summary's `companion` id and
  read through `/conversations`.
- `POST /chats/<id>/send` `{text?, images?}` — sends through the chat's own
  gateway and returns the sent `Message` (a mail chat's send can come back
  queued instead: *Mail additions*, "Writing", below). It does not skip the account's send
  policy: under `allow` the gateway sends outright, and otherwise the send is
  **queued and then released** through that gateway's own approve endpoint in
  the same request — the press IS the approval `verify` exists for, but it is
  spent on the mechanism rather than around it, so the send is recorded in the
  pending store with its approval. (What that does and does not prevent is in
  `_chat_send_via_gateway`: an agent calling `/send` gets a queued message; an
  agent that also calls approve has simulated the press, which nothing inside a
  shared container can stop.) The page shows an optimistic bubble and reconciles
  it with the response, and a failed send puts the words back into the composer.
  `images` — the key predates the other file types — is the message's
  attachments, files of any type (a photo, a video, a PDF):
  `[{content_type, data(base64), filename?}]`, at most 5 and at most 16 MB
  decoded together (`CHAT_SEND_MAX_BYTES`; 400 on violations); `filename` is
  the name a document goes out under; `text` may be absent when attachments
  are sent. The returned `Message` (and later polls) carries the sent
  attachments with proxied URLs and their sniffed dimensions. The client
  downscales picked photos before upload — longest edge 1600 px, JPEG — as
  the native clients do; animated GIFs pass through unchanged under the size
  cap. An image pasted into the text box (a screenshot, a picture copied off
  a page) is staged exactly as a picked one, through the shared
  `components/clipboard.js`, and the words of a paste that also carries text
  still land in the box. A failed image send keeps the staged previews (and
  the text) in the composer for retry.

  **The send honours the account's send policy rather than skipping it.** The
  message goes to the gateway as author `user` — provenance, nothing more —
  and carries no field that asks it to bypass anything. Under `allow` it goes
  out on that hop; under `verify` (or `trust`) the gateway queues it as a
  pending send and the web-gateway releases it through the gateway's own
  `/pending-sends/<id>/approve` in the same request, so the press still
  completes in one action while the send is recorded in the pending store with
  its approval. This is what makes an accidental send impossible: an agent that
  POSTs a gateway's `/send` gets a queued message somebody still has to
  release, whatever it writes in the body. `author: "user"` used to bypass the
  policy outright, which is how a message once went out that nobody pressed
  send on.

  As a second, lesser layer, this endpoint (and the `/sends` approve action)
  also refuses a request that did not arrive through the reverse proxy — the
  TCP peer address decides, since the edge auth is a forward-auth Traefik
  consults and so never sees an in-container caller. Refusals answer 403 naming
  the reason and are logged loudly. Pin the proxy's peers with
  `EDGE_PROXY_PEERS` (addresses or CIDRs); a deployment whose proxy arrives on
  loopback must set it. Neither layer is a boundary against a determined agent:
  the web-gateway shares a container with the agents and they hold the
  gateways' tokens, so an agent can make the approve call itself — a deliberate
  simulation of the button press, not an accident. An agent proposes a message
  by staging the shared draft; the user's press is what sends.
- `POST /chats/<id>/draft` `{text, version}` — the shared draft, saved
  ~1 s after the user stops typing and on blur; dictation participates in the
  same debounced save, and the ✕ posts empty text. Writes are
  version-guarded: a stale version answers 409 with the current state, which
  the page adopts (with a "draft updated elsewhere" note when the words
  actually differ) rather than clobbering.
- `POST /chats/<id>/draft/undo` (no body) — restores the draft the ✕ last
  cleared, with its original `author`/`agent`, and answers the new
  `{id, draft, version}`. 409 with the current state when there is nothing to
  put back: nothing was cleared, a draft has been written since, or the stash
  has aged out. Server-side on purpose — the draft endpoint stamps every write
  `user`, so a client resubmitting the text would quietly reattribute Ara's
  words to the user about to send them in their own name — and one guarded
  step, so it cannot race the clear that produced it whichever order the two
  requests arrive in.
- `POST /chats/<id>/read` `{seen: <inbound_seq>}` — **Mark as read**, about
  what the client showed: `seen` is the `inbound_seq` of the summary it
  rendered. If a message has landed since, nothing is marked and the answer
  is **409** `{error, id, last_read, unread, list_group}` — the chat as it
  stands; the client fetches and shows it before the user marks again.
  Otherwise the chat is read as it stands and the answer is `{id, last_read,
  previous, unread, list_group}`: `previous` is the mark it replaced (null
  when there was none), and `unread` / `list_group` are the chat's state
  right after the mark, as `GET /chats` would state it — normally 0 and
  `read`, but a client takes both from the answer rather than assuming them.
  The mark is the server's clock, never a message's timestamp and never in
  the future; a message it covers that a fast clock stamped up to two
  minutes ahead is read with it, and stays read. `{}` is the same without
  the check (for a caller that vouches
  for itself), and `{ts}` reads only up to that instant; a `ts` that is no
  real instant, or lies more than two minutes ahead of the server's clock,
  is a 400 (one a little ahead is taken as now).
  `{restore: <previous>, marked: <last_read>}` is its Undo: the mark
  goes back to `previous` only while it is still the one that read set, and
  the answer is 409 otherwise (a reply or another read moved it since, and
  that stands). The list's swipe and tray and the chat page's button are the
  only callers; nothing posts a read when a chat is opened, looked at or
  scrolled, and a send reads the chat server-side.
- `POST /chats/<id>/companion` — `{id}`, the chat's companion conversation.
  Idempotent: it creates the thread on the first call and returns the same id
  afterwards. The page calls it lazily — on the user's first turn or chip, so
  that a chat merely opened leaves no empty thread behind. From that id on,
  the pane is a plain conversation client: `GET /conversations/<id>` for the
  thread (`pending` is Ara writing), the reply POST for a turn, the read POST
  when the pane shows it.
- **Mail additions** (`docs/mail-chats.md`). An e-mail `ChatSummary` also
  carries `participants` (`[{address, name, contact}]`, at most 6),
  `participants_more` (how many the cap left out), `peer` (`{address, name}`:
  the latest inbound sender, or whom a thread the user started went to),
  `last.removed`, and — on the messages document's `chat` — `can_send` and
  `cannot_send_reason` (absent or true on every other chat). An e-mail
  `Message` also carries `subject` (its own), `recipients` and `cc`, `trim`
  (`deterministic` | `wanting` | `model`), `removed` and `removed_at`, and
  `original` — the URL of its full form, present exactly when there is one to
  show (never on a tombstone, which has empty `text` and no attachments).
  `GET <original>` answers `{id, headers: {from, to, cc, date, subject},
  header_block, text, html_url, attachments}`: the full, untrimmed plain
  text, and `html_url` either null or the original URL plus `.html`, which
  serves the sanitised HTML part as a document of its own under a policy
  that lets nothing load or run (the chat page's own policy gains
  `frame-src 'self'` for it). `POST /chats/<id>/messages/<token>/trim`
  (same-origin, and only through the reverse proxy like a chat's flags; the
  original URL with `/trim` for `/original`) asks for a
  `wanting` mail's model trim: `200 {state: model | deterministic, text}`
  (settled), `202 {state: queued | running}` (a turn on its way; asking
  again never queues twice) or `200 {state: wanting, retry_after}` (nothing
  started; not asked again before `retry_after` seconds); `404 {error: "no
  such mail in this chat"}` for a token that names nothing and `410` for a
  mail gone from the mailbox (a tombstone, an erased record), or for a chat
  merged away (`merged_into`).

  Writing: `GET /chats/<id>/reply-preview` → `{all: Addressing, sender:
  Addressing, choice, subject}`, `Addressing` being `{to: [Party], cc:
  [Party]}` and `Party` `{address, name, contact, contact_id}` — exactly what
  a reply would be addressed to right now in either mode, `name` the display
  name on record or null, `contact` the address book's **name** for the
  person who holds the address (a string, or `{name}`) and `contact_id` that
  person's id (null for nobody's address: then there is no address-book
  name). With `contact_id` in the answer the name is shown as it is filed,
  also a lowercase one-word one ("mum"); only an answer without the field
  can carry an id in `contact`, and a string that reads as an identifier is
  then not shown as a name. `choice` is whether the two modes differ;
  `409`/`400` `{error}` where no reply is possible, and a `404` is taken as
  a server that cannot say (the page then sends no reply). `POST
  /chats/<id>/send` takes `reply: "all" | "sender"` and `request_key` (an
  opaque token of at most 64 characters, one per press) on a mail chat and
  answers `200` with the Message (sent; `refused: [addresses]` where the
  mail server took only some recipients), `202 {pending: Pending}` (queued)
  or `202 {unconfirmed: true, message_id?, request_key}` (handed over,
  outcome unknown). An error answer is `{error, sent?, request_key?, by?}`:
  a **4xx** (424 where the send failed for certain at the mail server) and
  any status with **`"sent": false`** mean nothing went out with this
  request; a 5xx without it means nobody knows. `by` says whose word that
  is — `"gateway"` (the mail gateway's own answer, after it looked the key
  up: the key is over) or `"dashboard"` (the web gateway's, the mail gateway
  not having spoken: it says nothing about the key). `Pending` is `{request_id, approval_url, text, ts,
  author, agent?, to, cc, subject, message_id?, request_key?, status?,
  error?, outcome_unknown?, refused?, dismissed?}` (`approval_url`
  path-only, `/sends/<slug>/<id>`; `status` `pending` — the default —
  `sending` or `failed`; an entry with `dismissed: true` is over and is not
  drawn). `POST /chats/<id>/pending/<request id>/dismiss` takes a failed
  one off the list. `GET /chats/<id>/messages` carries top-level `pending:
  [Pending]` (oldest first; `[]` on other channels, and a document without
  the field leaves what the page shows), every mail Message `reply_to:
  [addresses]`, and a `GET /chats` entry `pending: n`, its `last` a queued
  send's with `pending: true` where that is the latest. `GET /chats/compose`
  → `{channels: [{channel, label, accounts}]}`, what ＋ offers (a 404 is
  taken as nothing); `POST /chats/compose` `{channel, account?, to, cc,
  subject, text, request_key}` → `200 {id, message}`, `202 {id, pending}` or
  `202 {unconfirmed: true, id?, message_id?, request_key}`, an error
  `{error, sent?, request_key?}` read as on a reply otherwise. Mark as read, Archive and Delete on a mail
  chat are the same calls as on every chat; the server carries them into the
  mailbox.

**Reference documents:** `data/chats.json` and `data/chats/<slug>.json` are
static documents in the API's response shapes — the contract drafts the API
was built against, kept as its reference documents and as the Playwright test
corpus (the validation suite serves them as mocked `/chats*` responses). They
are no longer a serving source. The corpus covers the media shapes: image
attachments with intrinsic dimensions, a voice note (`audio/*` with the
transcript as the message text) and a video (`video/*` with dimensions), all
with `data:` URLs, which the page takes only for an `<img>`, `<audio>` or
`<video>` of the same kind and for an image's lightbox, never for a link (a
link's url is the proxy's or an http(s) URL); the origin's
Content-Security-Policy allows no `data:` media on the chat page, since no
record the gateway serves carries one, so the corpus shows its media only
where it is served without that policy. It covers both companion states: one chat names an
existing thread, the rest carry `companion: null`. It holds a mail thread
too (`data/chats/email-quarterly.json`): a group thread with an inline image
and an attached sheet, a mail sent from another client, an HTML mail whose
subject is not the thread's, a tombstone and a reply waiting for approval
(`pending`), with the Original of its HTML mail beside it
(`email-quarterly.original.json`) and the reply preview the composer's
recipients line is drawn from (`email-quarterly.reply-preview.json`).
Companion messages
themselves are conversation documents, not chat ones, so the corpus holds
none — the suite mocks `/conversations/<id>` for those.

## The shelf: minimized pages

A thread, a chat or a project can be **minimized** (the `–` in its bar) onto
the shelf at the foot of every page (`components/shelf.js`, issue #282), and
reopened from there where it was left. A thread or chat the user **leaves while
Ara is still answering** goes onto the shelf by itself, so waiting for an answer
never means coming back to check. Every page carries `<retinue-shelf>` as the
last child of `<main>`: a row of icons on a phone (no labels; long press,
right-click or the context-menu key opens *Open · Close · Close all quiet
ones*), a taskbar of chips with titles on a wide screen.

Markers, two corners: top right a chat's unread count (blue); bottom right
Ara — `···` while she works (outlined, never animated) and a green dot once she
has answered. When the answer lands the dots gather into the dot once.

All rules live in `components/shelf-store.js`, a pure reducer over one item per
key (`thread:<id>` · `chat:<id>` · `project:<uri>`) with independent membership
(`origin: explicit | automatic`) and status (`pending`, `lastAraTs`, `seenTs`,
`unread`), pinned by `tests/test_webapp_shelf.py`:

- Explicit wins over automatic. An automatic item lives only for the turn that
  created it: it leaves once that answer has actually been on screen (in the
  viewport of a visible tab — for a chat, in the companion pane, not the mirror).
- *Close* on a working item suppresses re-adding for that same turn.
- Capacity (*Shelf size* on the settings page, default 5) evicts only quiet
  items (no marker), least recently used first; items with a marker are never
  dropped, so the shelf may hold more than its size and shrinks back as they go
  quiet.
- Projects are plain parked pages, never with an Ara state.
- Archiving drops an automatic item (and suppresses it for the running turn);
  archive + mute drops any item.
- Restoring reopens the page with current data and only puts the place back
  (pane; the first message in view by its `ts`/id plus an offset; a project's
  scroll position) — handed from the shelf to the next page load of that tab
  through `sessionStorage`.

The shelf is per device and browser: `localStorage`, shared by all tabs, which
follow each other's changes through the `storage` event. Its size is a
per-device preference (`components/device-store.js`): inside the Android
shell the app holds it, one size for every endpoint, and a change made on the
app's settings page reaches an open page as a `device-settings` event. It is event-driven
first — a page showing an item (`<retinue-conversation shelf-key=…>`, the chat
page, the project page) feeds its facts as it reads and marks the key as the
current page — and reads only the items no page in the tab is showing
(`GET /conversations/<id>`, `GET /chats`), every 4 s while one is waiting on
Ara and every 20 s otherwise, never in a hidden tab. A thread's unsent draft
is kept on the device (see "Unsent input"), so it survives a minimize, a
reload and a later visit alike.

## Unsent input

Leaving a page is never held back by what it holds. A tab of the navigation
row is a plain link, and the Android shell's Home press and wake always
navigate. What the user put in waits where they left it instead, and is in
its field again when they come back to the same place — on any later page
load of this device and browser:

- **Drafts** (`components/drafts.js`). Every composer, command bar and editor
  writes what it holds to `localStorage` as it changes (`retinue-draft:<key>`:
  `conv:<thread id | new | new:<project>>` for the conversation composer,
  `chat:<chat id>` for a chat's composer, `chat-unsure:<chat id>` for a mail
  chat's unconfirmed sends, `compose:<channel>[:<mailbox>]` for the new-mail
  sheet, `project-cmd:<id>` and
  `project-edit:<id>` for a project's command bar and raw editor), and reads
  it back when its place opens again. Synchronous storage, so a write made as
  the page unloads (`pagehide`) is not lost; written on every change, so a page
  the system kills without an unload loses nothing either. Attachments (a
  thread's picked files, a chat's staged attachments) are too large for it and go
  to IndexedDB (`retinue-drafts`) when they are picked or dropped. A project
  edit is kept with the file version it was started from, so saving it on a
  later visit still answers 409 when the file changed meanwhile. A chat's
  composer text also goes to the server's shared draft as the page unloads (a
  keepalive request); the device copy is put back only while the server's
  draft has not moved past it. A draft nobody came back to within a month is
  dropped.
- **Dictation** (`components/dictation.js`, and the "Dictation" section of
  `sw.js`). Transcription runs in the service worker, not in the page that
  recorded it: a page that is left takes its fetches with it. The worker
  keeps each job, audio and all, in IndexedDB (`retinue-dictation`) under the
  place it was recorded for, and its result until a page showing that place
  takes it — the page that recorded it if it is still open, else the next one
  to open the place, whose box shows "Transcribing …" while the job still
  runs. A job has one owner, a live client: a page that goes releases its
  jobs, and a second tab on the same place takes nothing that is not its own.
  Recorders run in 250 ms slices, so a page left with the mic live hands the
  worker everything said up to the last fraction of a second, and leaving acts
  as **✓**: the transcript waits in the draft for review. A dictation the
  user *sent* — **➤**, or a take of the continuous loop that ended on a pause
  — is a message to the thread it was recorded in, as if typed there: should
  its page be gone before the transcript is in, the worker posts it into that
  thread itself (the thread fixed when recording started, never whatever is on
  screen by then), and the next page showing the thread just reads it back.
  A take that cancelled a call in the continuous loop carries that call to
  the worker (`call`), whose send sends it along as `cancelled_call`, as the
  page's own would (docs/dashboard.md, "Inside the Android shell") — while
  the take is fresh: one sent more than 90 s after its transcript came (a
  page opened later that takes it over), or after it was recorded where the
  worker was stopped in the middle and restarted it, goes as words alone,
  so no call is placed again that nobody is waiting for. A slow
  transcription the worker sends the moment it is in keeps its call.
  A send the worker could not make, or may not have made (no answer, a worker
  stopped in the middle), is never retried blindly: its text waits in the
  draft with a note. A first message (no thread yet) waits in its composer's
  draft, as before. Without a
  controlling worker (the very first load, a browser without one), and with
  an older worker that does not take the job up within 4 s, transcription is
  the page's own fetch, as before.
- **Sends on the wire.** A turn small enough (all text-only turns in
  practice) goes out as a `keepalive` request, which the browser finishes even
  when the page is left meanwhile — but not offline, so that is never taken
  for delivery. A conversation turn still on the wire as the page goes is
  written into its draft, marked with when and where it went, and settled when
  the composer is next shown: dropped from the box once the thread holds it
  (read twice, a moment apart, as a keepalive request may still be landing),
  left in it with a note when it does not; when the server cannot be asked at
  all, the note hedges and the mark stays for the next visit to look again.
  A first message, which has no thread to read, is looked for in the threads
  the user opened from that moment on. A chat message is looked for in the chat's own mirror on the
  next visit; one carrying attachments is too large for a keepalive request and is
  the likelier to need it. A project command is taken as delivered.

The page-side guard that held a tab or a Home press on such a page is gone,
and with it the note that popped up at the bottom of the screen asking for a
second press — on a phone the keyboard came up with that note and hid the
navigation, so the second press could not even be made. Pop-up notes at the
bottom of the screen are an anti-pattern here. Shell updates still wait for
a moment that costs nothing (`components/update.js`): a reload under a user
who is writing is not one they asked for.

## Markdown rendering

All Markdown shown by the dashboard — conversation bubbles and project pages —
goes through the one shared renderer in `components/markdown.js` (paragraphs,
headings, fenced code, blockquotes, nested and task lists, pipe tables, links,
bold/italic/strike/inline code). Input is HTML-escaped before any markup is
generated and only `http(s)`/`mailto:`/`tel:` URLs become links, so the output
is safe for `innerHTML`. A link to the dashboard's own origin (Ara's link to
another thread, an approval page) navigates in place, so it stays in the PWA
window and the back gesture returns to where it was tapped; every other web
link opens in a new tab. Like a tab of the navigation row, such a press is
never held: what the page holds unsent waits in its field (see "Unsent
input"). Hosts can specialize rendering via hooks — the
conversations card uses the blockquote hook to keep its copy-to-clipboard
button on Ara's ready-to-send drafts.

## Project pages

Every project on the projects card links to its own page,
`project.html?id=<project URI>` (`components/project-page.js`). The gateway
resolves the URI back to the project's source Markdown file through the life
store (the file **is** the named graph the project's triples live in) —
`GET /projects/item?id=…` returns the raw Markdown plus a `sha256`.

The page renders the frontmatter as meta chips and the body through the shared
Markdown renderer, and offers three ways to change the project, in increasing
weight:

1. **Quick command** — the bar at the bottom: a multi-line field that grows
   with its content (Enter breaks the line, Cmd/Ctrl+Enter sends), or dictation
   with the same recording controls as the chat composer (shared UI in
   `components/voice.js`, same `/conversations/transcribe` pipeline — waveform,
   ✕ discard, ✓ transcribe for review, ➤ transcribe and send without the
   keyboard reappearing). The request becomes a
   conversation of **kind `edit`**, linked to the project: Ara applies it to
   the file and the page shows her one-line confirmation, then reloads. Edit
   threads are marked as such and **hidden from the normal conversation
   list** (`GET /conversations` filters them out unless `?kind=edit|all`);
   they stay reachable under the *Edits* filter on `conversations.html`.
2. **Direct editing** — the muted pencil swaps the page for a raw-Markdown
   editor over the whole file. Saving does `POST /projects/item` with
   `{id, content, base_sha}`; the gateway re-resolves the path server-side
   (the client can never name a file), answers **409 with the current
   content** when the file changed meanwhile, and best-effort commits+pushes
   the chamber (data paths carry standing commit permission).
3. **Discuss with Ara** — opens the conversation composer pre-linked to the
   project (`#new?project=…&title=…`). The resulting thread is a normal,
   visible conversation whose engage prompt points Ara at the project file.

The link runs both ways. A thread linked to a project shows an
**About: <project>** chip above its messages that opens the project page, and
the project page lists the project's threads above its notes
(`GET /conversations?all&project=<uri>`): open ones as rows (unread dot, last
activity), archived ones folded. Edit-command threads are left out of that
list — each is one applied change, reachable from its confirmation and under
the Edits filter.

## News

`components/news.js` is the news card (`index.html`) and, with the `full`
attribute, the whole `news.html` page. It reads the gateway's live endpoint —
`GET /news?scope=feed|read|hidden|all&limit=n` — which ranks items at request
time (importance decayed by age; dated items hold full weight until they lapse,
then leave the feed). There is no data file and no stored ordering: the feed
changes because the clock moved. The service worker deliberately does **not**
cache `/news`, only the `news.html` shell.

A headline links out to the source (`target=_blank`) — items are references,
never copies. An item's link is an absolute `http(s)` URL or the item has
none: the gateway serves nothing else as `url`, and the page puts nothing else
into an `href` (`webLink`, the rule of `news_store.web_link`; `docs/news.md`,
"The item"). An item without a link, a channel post that carried none, shows
its headline as text. The three per-item buttons plus the page's free-text note are the
user's half of the learning loop:

- `POST /news/feedback` — `{id?, signal, note?}` with signal
  `up|down|read|hide|note`. Each signal nudges that item immediately *and* is
  logged for the Herald agent, which generalizes it into the profile.
- `GET /news/preferences` / `POST /news/preferences` — the learned profile as
  Markdown, shown at the bottom of the page and editable by hand.

Read-aloud (`components/speech.js`) walks the ranked feed with the browser's own
`speechSynthesis` — ▶ Listen, ⏭ skip, ⏹ stop (▶ Resume after an interruption),
current item highlighted, each
item spoken in the language it declares. See `docs/news.md` for the collector,
the manifest format and the agent.

## Voice conversations

Threads can be spoken as well as typed, with no streaming and split by direction:

- **Input (send as audio).** A microphone button in the composer records with
  `MediaRecorder`. While recording, the text field is replaced by a live
  waveform (Web Audio analyser; a simulated wave where that API is missing)
  with three controls: **✕** on the left discards the recording, and on the
  right a green **✓** transcribes it into the composer for review — speech
  recognition is imperfect — while **➤** transcribes *and* sends in one go,
  showing only a status line until the send completes (the textarea never
  reappears mid-flow, so the phone keyboard stays down). The transcript lands
  in the conversation where **✓**/**➤** was tapped; navigating away from the
  conversation acts as **✓**, so the transcript waits in that thread's draft —
  and so does leaving the page, recording or transcribing: the service worker
  finishes the job (see "Unsent input").
  Transcription is a background job of its one conversation: every other
  thread keeps a fully usable row (text and voice) meanwhile, and is not
  re-rendered or refocused when the job — or its reply — arrives. Either way
  the audio blob is POSTed to `/conversations/transcribe` (by the service
  worker, `components/dictation.js`), which the web gateway proxies
  to the shared STT service (`scripts/stt-service.py`) owning the Whisper model
  (so this image ships no ASR stack). Requires `STT_SERVICE_URL` (set in
  `docker-compose.yml`); when unset the endpoint returns 503 and the mic button
  is hidden. The mic is also hidden where `MediaRecorder`/`getUserMedia` are
  unavailable.
- **Output (play replies).** Each of Ara's messages has a 🔊 button that
  reads it aloud with the browser's built-in `speechSynthesis` — no server work,
  works offline. The player behind it is `components/speech.js` (shared with
  the news page): the text is spoken in sentence-sized pieces, because the
  engines fail on long utterances (Chrome goes silent after ~15 s, Android
  rejects a few thousand characters outright — the reason a tap used to do
  nothing on long replies), and a **player bar** above the composer shows what
  is being read and how far: a position slider to drag, ⏮/⏭ a passage back or
  forward (a sentence or two, about ten seconds of speech — the unit the text
  is spoken in), play/pause, and ✕ to stop. Pausing and seeking restart at a
  passage boundary. The bar follows into the thread list, so going back does
  not end the reading; leaving the *page* does (the browser silences its
  engine), but the position is remembered per passage in `localStorage`
  (`retinue-voice-position`, kept for a week) and the next time that thread
  opens the bar is back, paused right there — ▶ on the message or on the bar
  carries on from that passage, ✕ forgets it. The speaker toggle in the
  thread bar (one setting per device, persisted in `localStorage`) speaks
  replies automatically as they arrive;
  the browser's own voice is used, so quality varies by platform, and iOS may
  require a tap (the play button) to start speech — the bar says so when the
  engine refuses. The controls are hidden where `speechSynthesis` is
  unavailable.
- **Continuous conversation (hands-free).** The loop button in the thread bar
  (also in `bar="actions"`, and in the new-thread composer's bar) puts *this*
  thread into continuous mode; the flag is kept per thread on the device
  (`retinue-continuous` in `localStorage`, with when the thread was last used
  in the mode). In the mode Ara's answers are read aloud whatever the speaker
  toggle says; when the reading ends — or there is nothing to read — the mic
  opens with a short Web Audio chime, and a voice-activity detector over the
  recorder's own stream (RMS level from an `AnalyserNode` against a learned
  noise floor) ends the take after a pause of about 1.5 s once speech was
  heard; a level that holds above the threshold without a dip for 3 s is a
  louder room, not a voice, and becomes the new floor. The take goes out on
  the dictation's ➤ path (transcribe, send), the loop waits for the answer,
  and goes round again — only for an answer to its own turn: other messages
  pushed into the thread may be read, but do not open the mic. A take that
  reaches the 120 s cap goes to the draft for review instead. No speech for
  about 8 s closes the mic with the mode left on — the mic button resumes;
  leaving the thread or hiding the page ends listening (a take with speech in
  it lands in the draft for review, as on leaving any dictation; one without
  is dropped). Where the page cannot hear the stream, plain timers stand in
  for the detector. A listen wanted while the page is hidden waits up to 60 s
  for it to return. The recording row keeps its ✕/✓/➤ throughout. Both
  timeouts are per-device settings (Settings → Voice; in the Android shell,
  the app's settings page, one pair for every endpoint). Opening it: the
  toggle; `#new?voice=1`, the composer in the mode, whose first take opens
  the thread that then goes on in it; and a wake (`/?wake=1`, or the shell's
  `wake` event), which reopens the thread used in the mode within the last
  15 minutes, else that voice composer. Both flags are dropped from the
  address once read and open the mic by themselves only inside the shell;
  elsewhere the conversation offers a **Start talking** button. Closing it:
  the toggle, or Ara — told by voice to switch the mode off, she stamps the
  thread (`conversation-push.py --end-continuous` → `continuous_off`), and
  every device showing it in the mode leaves it on its next read; a toggle
  pressed after her stamp is the user's and stays on (docs/dashboard.md). An
  answer that ends in a call directive (`[[call: <number> | <name>]]`) places
  the call instead of opening the mic, in a shell with `placeCall`: the name,
  the number and a **Cancel** button for 5 s with the mic open — speech
  cancels, and goes to Ara as the next turn with a `cancelled_call` note (to
  review instead, like any take, when the box holds text or files), so she
  places the call again when the words did not mean to stop it (no words at
  all: it goes ahead at once); `| no-voice-cancel` keeps the mic shut,
  honoured only on a second attempt at the number — then the shell dials on
  speaker (docs/dashboard.md, "Inside the Android shell"); everywhere else the
  directive is a tap-to-dial link. The pure parts — the detector's state
  machine, the wake's choice of thread and whether Ara's stamp ends the mode —
  are `components/continuous.js`
  (`tests/test_webapp_continuous.py`).

## Installing on Android

Open the URL in Chrome/Brave/Edge → menu → **Install app**. The manifest's
`launch_handler` (`focus-existing`) makes a second launch bring the open
window to the front as it was left, rather than start another or reload it. Or use the Android shell (below), which
can also be the phone's home screen.

## Inside the Android shell

The Android app (`ara-android`) is a specialised browser and home launcher
around this web app: a full-height WebView on the configured origin, a
launcher bar under it (the phone, SMS, mail and map buttons that used to be
the home's dock, plus whatever the user pins), and the few things a page
cannot do — the home-screen role, a wake phrase, native speech output, the
client certificate. Everything that is Retinue stays here, so it behaves the
same in the shell, in Chrome and on a desktop. The contract between them:

- **User agent**: the shell appends ` RetinueShell/6 (Android)` (`/5` before
  `placeCall`, `/4` before the keyboard members, `/3` before
  the app's own settings page and the per-device preferences, `/2` before the
  endpoint members, `/1` before the Health Connect members below).
- **`window.RetinueShell`**, installed at document start on the configured
  origin only: `platform`, `version`, `setVoiceActive(bool)` and
  `openSettings(section?)`. `setVoiceActive(true)` means "a voice conversation is
  engaged: keep the screen on, pause the wake phrase". The page asserts it
  before asking for the mic (so the wake detector lets go of it first) and
  holds it for as long as a continuous conversation is engaged — Ara's
  thinking and reading time included — then sends `false` once the loop
  stops (mode off, no speech, leaving, an error). `components/shell.js` wraps
  both.
- **Settings** (RetinueShell/4): `settingsPage: true` — the app has a settings
  page of its own, and the gear opens it (`openSettings()`); `settings.html`,
  reached anyway, shows a card leading there instead of the browser part. With
  RetinueShell/3, Settings → Phone shell — shown only when the object exists,
  and first on the page there — calls `openSettings()`.
- **Per-device preferences** (RetinueShell/4): `deviceSettings()` → a JSON
  string `{key: value}`, `setDeviceSetting(key, value)` and `device-settings`
  events with the `keys` that changed: the app holds the voice timeouts, the
  shelf size and the layout for every endpoint. `components/device-store.js`
  is the only caller (`deviceGet`, `deviceSet`, `onDeviceChange`); in a
  browser it is plain `localStorage`.
- **Endpoints** (RetinueShell/3): the shell keeps a list of Retinue servers
  and shows one. `endpoints()` → `[{id, label, current}]` as a JSON string,
  the label being the instance's own name (its manifest `short_name`, which
  Settings › Instance sets); `selectEndpoint(id)` switches (the page is left
  for the other dashboard); `openSettings('endpoints')` opens the shell's
  endpoint list or, with one endpoint, its editor. With RetinueShell/3 the
  Phone shell card shows the menu and *Edit endpoints*; from RetinueShell/4
  the app's own settings page has them. The page never writes an address,
  certificate or password — those stay in the shell. An `endpoints` event says the list
  changed while the page is open.
- **Shell → page**: `window.dispatchEvent(new CustomEvent('retinue-shell',
  {detail: {type}}))` with `type` `wake` (the wake phrase), `home` (a Home
  press while the shell is in front), `keyboard` (RetinueShell/5: the
  outcome of a keyboard request, information only; see the bullet below) or
  `call` (what became of a `placeCall`, information only; see Phone calls
  below). The home answers both in place: a wake
  opens continuous conversation (above), a home press closes an open thread or
  sheet back to `/` (every view entry unwound, not just one) and scrolls the
  list to its top. Any other page of the web app answers by loading
  `/?wake=1` or `/` — always: unsent input on the page waits in its field
  for the user to come back (see "Unsent input"). The gateway's own pages (`/sends`, `/claude-auth`)
  carry no `shell.js`, so the shell itself loads the home (or `/?wake=1`)
  when the page in front is not the dashboard's. A wake while the page is not
  loaded makes the shell load `/?wake=1` itself; the home drops the flag from
  the address on load.
- **Files**: the WebView renders no PDF, so a PDF row's view link
  (`?inline=1`, answered `Content-Disposition: inline`) reaches the shell,
  which opens it in the phone's viewer app from a private copy; the ↓ link
  (`attachment`) saves it to Downloads. Images and text open in the
  dashboard's file viewer, as anywhere else. The shell names the file from that
  header; the URL ends in the name too, for anything that names a file after
  its URL.
- **Health Connect** (RetinueShell/2): `healthConnectAvailable()`,
  `healthSync()`, `healthSyncStatus()`, `setHealthSyncInterval(minutes)`,
  `setHealthAutoSync(bool)` and `health-sync` events. With RetinueShell/3,
  Settings → Phone shell shows "Update health data now", the last result, the
  auto-sync toggle and interval when the shell reports Health Connect
  available (from RetinueShell/4 the app's settings page does, beside the
  endpoint's switch); `sync-health.html`
  (`/sync-health`, `#sync` starts a sync) is the link target for threads. The
  shell uploads by WebDAV `PUT` to `/inbox/` — the page never handles the
  data. From RetinueShell/3 the status carries `enabled`, the current
  endpoint's own health switch: off, both pages show one line and the way to
  the shell's endpoint editor instead of the controls, and a sync asked for is
  `skipped` with reason `disabled`. `components/shell.js` wraps all of it,
  feature-detected.
- **Keyboard** (RetinueShell/5): the shell can be paired with another device
  as a Bluetooth keyboard and type into it. `keyboardStatus()`,
  `keyboardType(text, opts)`, `keyboardKey(name, opts)`,
  `keyboardSecret(opts)` (a native field the user's password manager fills;
  the secret never reaches the page), `keyboardOpen()` and
  `keyboardCancel()`, with `keyboard` events for each request's outcome. The
  phone asks itself before anything that can act on the device. What drives
  them is a reply's shell actions, through `components/shell-keyboard.js`
  and `components/shell.js`; the contract and the policy are in
  `docs/dashboard.md` (the "Keyboard (RetinueShell/5)" row and "Shell
  actions: typing on a paired device").
- **Speech output**: Android's WebView has no Web Speech API, so the shell
  installs a `speechSynthesis` / `SpeechSynthesisUtterance` stand-in backed by
  the native TTS where the page has none. `components/speech.js` is unchanged;
  it simply finds an engine there.
- **Phone calls** (RetinueShell/6): `placeCall(number)` calls the number
  without a tap, on speakerphone, and only while the page holds
  `setVoiceActive(true)`. It needs *Call directly by voice*, a switch per
  endpoint in the app's settings (*This app with <endpoint>* → *Phone
  calls*, off for a new endpoint), and Android's phone-call permission; short
  of either, the shell opens the dialer with the number instead. The shell
  normalises the number (separators, a leading `tel:` and the `(0)` of
  `+44 (0)20` dropped); it must then be an optional `+` followed by digits,
  `*` and `#`, with 3 to 20 digits and at most 32 characters. The answer is a
  `call` event `{state, reason?, number?}`: `placed` (with `number`);
  `dialer`, the dialer opened instead (`emergency`, `service-code` for any
  number with `*` or `#`, `disabled` or `no-permission`, each with
  `number`); or `refused` (`invalid-number`, `not-in-voice`, `no-telephony`,
  or `failed` with `number`). `placeCall('')` is answered `refused` /
  `invalid-number`, while `placeCall()` and `placeCall(null)` send nothing.
  `components/shell.js` wraps it as `canPlaceCall`, `placeCall` and `onCall`;
  the continuous loop is its only caller (above). The full text is in
  docs/dashboard.md, "Inside the Android shell".

## Next steps

- Replace mock `data/*.json` with a scheduler-driven curation job.
- Add an audio briefing (`briefing.json.audio` → Piper-rendered MP3 under `/data/`).
