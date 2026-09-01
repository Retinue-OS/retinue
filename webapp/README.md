# Retinue Dashboard (PWA)

A minimalist, distraction-free dashboard served at the root of the web gateway
(`agents.example.com`, behind Traefik basic auth) and installable as
a Progressive Web App on the phone home screen.

## Architecture

- **Static shell, no server rendering.** `index.html` is the hand-editable
  configuration: it declares the active cards and app-launch buttons.
- **Conversation tabs** (`components/conversations.js`) are the active
  interactive card: standalone chat threads with Ara. The user can open a
  thread, and a retinue agent can open one when it needs a decision (e.g. an
  RSVP). It talks to the gateway's `/conversations` API rather than a static data
  file.
- **App launcher** (`components/app-launcher.js`) provides local OS launch
  buttons.
- **Curated data** lives in `data/*.json`. Today these are mock files; the
  static mock cards that consume them are commented out in `index.html` until a
  refresh job regenerates them (write target = `DASHBOARD_DATA_DIR`).
- **Offline:** `sw.js` caches the shell (cache-first) so the dashboard and the
  local app-launch buttons — notably the dialer — work without connectivity.
  Data is network-first with cache fallback.

## Serving

`scripts/web-gateway.py` serves this directory:

- `/` and `/index.html` → `WEBAPP_DIR/index.html`
- `/data/<file>.json`   → `DASHBOARD_DATA_DIR/<file>.json` (no-store)
- everything else       → `WEBAPP_DIR/<path>` (shell assets)

Env: `WEBAPP_DIR` (default `/workspace/webapp`), `DASHBOARD_DATA_DIR`
(default `WEBAPP_DIR/data`).

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
- `POST /conversations/<id>/unarchive`— restore an archived thread.

Ara answers asynchronously, so the card polls the thread until the reply lands.
Each thread maps to its own Claude session (key `conv:<id>`). Threads persist
under `CONVERSATIONS_DIR`, one file each — the deployment points this at the
persistent `/root` volume (`/root/.retinue/conversations`); the
`/tmp/web-tab-conversations` default is only for ad-hoc runs.

On a phone the dashboard card stays compact (the five most recent active
threads); in the wide layout it fills its resizable region and shows every
active thread. Either way an **All conversations →** link leads to
`conversations.html`, a dedicated page that lists every thread with an
Active/Archived filter (the same `retinue-conversations` element with the
`full` attribute). Archiving is done from inside a thread; archived threads
leave the active list but remain on that page and via
`GET /conversations?archived=1`.

The wide layout itself is resizable, VS Code style (`layout.js`): the
boundaries between conversations, news and the projects column are draggable
splitters — double-click resets one, dragging news fully down closes it — and
each card's header toggles between list and card view. Both preferences
persist per device in localStorage. Device-level settings (notifications, the
running shell version with a manual update check) live on `settings.html`,
reached via the gear in the dashboard header.

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
(Signal / WhatsApp / Telegram, one peer or group) is a **chat** — a
deterministic mirror rendered like the messenger client — with its
**companion pane** (the conversation-with-Ara rail) beside it. It runs against
the gateway's chat API (SPARQL over the message ledgers plus a live overlay;
the endpoints and their rationale live in `scripts/web-gateway.py`'s module
docstring). Pieces:

- `components/chats.js` — the Chats card on the dashboard and, with `full`,
  the whole `chats.html` page: avatar, channel mark, last-message preview,
  unread badge, non-archived chats ordered by last activity; the full page
  adds an Active/Archived filter like the conversations page. In the wide
  layout the card has its own fixed-height region above the conversations
  (`--chats-h`), resizable and snap-closable at a third `layout.js` splitter
  (`data-splitter="chats"`). The card refreshes on an ambient cadence and
  keeps its last rendered state over a failed fetch.
- `chat.html?id=<chat id>` (`components/chat-page.js`) — one chat: bubbles
  with day separators and an unread waterline, sender labels in groups, the
  author on every outbound bubble (you / Ara / your phone), inline media
  (images with a lightbox, voice-note and video players — see the Message
  contract below), a live composer (send, shared draft, one-tap clear ✕,
  dictation, image attach with client-side downscale), quick-pattern chips,
  and the companion pane — swipe between panes on a phone, a draggable
  splitter on a wide screen. The composer row keeps at most two round
  controls, since each costs the text field 46px of a phone's width: the
  paperclip (whose chooser is also the camera on a phone), and one right-hand
  button that is the mic while there is nothing to send and the send button as
  soon as there is. Back goes back where the chat was opened from within the
  app, and to the chats list for a chat opened cold (a notification, a
  bookmark). The open chat polls on the conversations cadence,
  appending only unseen messages, and posts the read watermark on open, on
  arrivals while at the bottom, and when the page becomes visible again.
  The companion pane is the chat's own conversation with Ara (see the
  `companion` field below): her turns render in the conversation thread's
  visual language, `pending` shows as her writing, and a chip is that same
  turn with a canned prompt. The two rails meet in the shared draft — Ara
  stages a reply, the chat poll adopts it into an empty composer marked as
  hers, and the send press stays the user's.

The API, as the components consume it:

- `GET /chats` — `{generated, chats: [ChatSummary]}`, ordered by last
  activity. A `ChatSummary` is `{id, channel, name, group, members?, unread,
  archived, muted, companion, last, draft, messages}` where `id` is
  `<channel>:<chat-key>` (the exact recipient string that channel's send path
  accepts), `unread` derives from the user's `last_read` watermark, `last` is
  the preview `{ts, direction, author?, sender_name?, text, kind}`, `draft`
  is the shared draft `{text, author, agent?, ts, version}` or null,
  `companion` is the conversation id of this chat's companion thread (null
  until one exists), and
  `messages` is the URL of the chat's message document — the client follows
  it and never constructs message URLs. `archived` and `muted` carry the
  dashboard-conversation semantics verbatim: an archived chat leaves the card
  and the Active list (the full page's Archived filter keeps it reachable),
  and a new inbound message **un-archives** an archived chat unless it is
  muted — the server's rule, applied on the notify rail. `muted` silences
  that chat's Web Push and keeps an archived chat archived; as with
  conversations, the Archive button leaves `muted` untouched, while "archive
  this chat" said to Ara sets both. No pinning yet: favourites-on-top would
  be a later `pinned` flag, deliberately deferred. A store outage answers an
  honest 502 (the page shows it; the card keeps its last state).
- `GET /chats/<id>/messages` — `{generated, chat: ChatSummary, messages:
  [Message]}`, ascending by `ts`, the newest page by default;
  `?before=<ISO ts>` pages older history (the page renders the newest page —
  a load-older affordance is future work). A `Message` is `{id, chat,
  direction, author? (out: user|agent|device, plus agent name),
  sender?/sender_name? (in), text, lang?, ts, attachments?: [{id, url,
  type?, size?, width?, height?}]}`. Attachment URLs are the web-gateway's
  authenticated media proxy (`/chats/media/…`); type and size are
  best-effort. `width`/`height` are the medium's real intrinsic size, sniffed
  at ingest — when present the client reserves the true aspect box before the
  bytes arrive, when absent (older records) it reserves a fixed placeholder
  frame; either way a lazy load can never shift the thread's scroll. By type:
  `image/*` renders inline and opens a full-screen lightbox on tap (same
  proxied URL — it is the original; tap, Esc or the platform back gesture
  closes, one history entry deep); `audio/*` is a voice note — the player
  above the transcript, which is already the message `text`; `video/*` is an
  inline player (`preload=metadata`, box-reserved the same way); anything
  else stays a file row. Reactions and quoted replies (issue #130) will
  decorate these records later. The companion thread is not in this payload:
  it is an ordinary conversation, named by the summary's `companion` id and
  read through `/conversations`.
- `POST /chats/<id>/send` `{text?, images?}` — sends through the chat's own
  gateway as the user (direct under every policy category: the authenticated
  send press IS the approval `verify` exists for) and returns the sent
  `Message`; the page shows an optimistic bubble and reconciles it with the
  response, and a failed send puts the words back into the composer.
  `images` is `[{content_type, data(base64)}]`, at most 5, each at most
  ~8 MB decoded (400 on violations); `text` may be absent when images are
  sent. The returned `Message` (and later polls) carries the sent
  attachments with proxied URLs and their sniffed dimensions. The client
  downscales picked photos before upload — longest edge 1600 px, JPEG — as
  the native clients do; animated GIFs pass through unchanged under the size
  cap. A failed image send keeps the staged previews (and the text) in the
  composer for retry.
- `POST /chats/<id>/draft` `{text, version}` — the shared draft, saved
  ~1 s after the user stops typing and on blur; dictation participates in the
  same debounced save, and the ✕ posts empty text. Writes are
  version-guarded: a stale version answers 409 with the current state, which
  the page adopts (with a "draft updated elsewhere" note when the words
  actually differ) rather than clobbering.
- `POST /chats/<id>/read` `{ts}` — advances the read watermark, forward-only.
- `POST /chats/<id>/companion` — `{id}`, the chat's companion conversation.
  Idempotent: it creates the thread on the first call and returns the same id
  afterwards. The page calls it lazily — on the user's first turn or chip, so
  that a chat merely opened leaves no empty thread behind. From that id on,
  the pane is a plain conversation client: `GET /conversations/<id>` for the
  thread (`pending` is Ara writing), the reply POST for a turn, the read POST
  when the pane shows it.

**Reference documents:** `data/chats.json` and `data/chats/<slug>.json` are
static documents in the API's response shapes — the contract drafts the API
was built against, kept as its reference documents and as the Playwright test
corpus (the validation suite serves them as mocked `/chats*` responses). They
are no longer a serving source. The corpus covers the media shapes: image
attachments with intrinsic dimensions, a voice note (`audio/*` with the
transcript as the message text) and a video (`video/*` with dimensions), all
with playable `data:` URLs, and both companion states: one chat names an
existing thread, the rest carry `companion: null`. Companion messages
themselves are conversation documents, not chat ones, so the corpus holds
none — the suite mocks `/conversations/<id>` for those.

## Markdown rendering

All Markdown shown by the dashboard — conversation bubbles and project pages —
goes through the one shared renderer in `components/markdown.js` (paragraphs,
headings, fenced code, blockquotes, nested and task lists, pipe tables, links,
bold/italic/strike/inline code). Input is HTML-escaped before any markup is
generated and only `http(s)`/`mailto:`/`tel:` URLs become links, so the output
is safe for `innerHTML`. Hosts can specialize rendering via hooks — the
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

## News

`components/news.js` is the news card (`index.html`) and, with the `full`
attribute, the whole `news.html` page. It reads the gateway's live endpoint —
`GET /news?scope=feed|read|hidden|all&limit=n` — which ranks items at request
time (importance decayed by age; dated items hold full weight until they lapse,
then leave the feed). There is no data file and no stored ordering: the feed
changes because the clock moved. The service worker deliberately does **not**
cache `/news`, only the `news.html` shell.

Every headline links out to the source (`target=_blank`) — items are references,
never copies. The three per-item buttons plus the page's free-text note are the
user's half of the learning loop:

- `POST /news/feedback` — `{id?, signal, note?}` with signal
  `up|down|read|hide|note`. Each signal nudges that item immediately *and* is
  logged for the Herald agent, which generalizes it into the profile.
- `GET /news/preferences` / `POST /news/preferences` — the learned profile as
  Markdown, shown at the bottom of the page and editable by hand.

Read-aloud (`components/speech.js`) walks the ranked feed with the browser's own
`speechSynthesis` — ▶ Listen, ⏭ skip, ⏹ stop, current item highlighted, each
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
  conversation acts as **✓**, so the transcript waits in that thread's draft.
  Transcription is a background job of its one conversation: every other
  thread keeps a fully usable row (text and voice) meanwhile, and is not
  re-rendered or refocused when the job — or its reply — arrives. Either way
  the audio blob is POSTed to `/conversations/transcribe`, which the web gateway proxies
  to the shared STT service (`scripts/stt-service.py`) owning the Whisper model
  (so this image ships no ASR stack). Requires `STT_SERVICE_URL` (set in
  `docker-compose.yml`); when unset the endpoint returns 503 and the mic button
  is hidden. The mic is also hidden where `MediaRecorder`/`getUserMedia` are
  unavailable.
- **Output (play replies).** Each of Ara's messages has a 🔊 play button that
  reads it aloud with the browser's built-in `speechSynthesis` — no server work,
  works offline. A per-thread **Auto** toggle (persisted in `localStorage`)
  speaks replies automatically as they arrive; the browser's own voice is used,
  so quality varies by platform, and iOS may require a tap (the play button) to
  start speech. The controls are hidden where `speechSynthesis` is unavailable.

## Installing on Android

Open the URL in Chrome/Brave/Edge → menu → **Install app**. App launching uses
`tel:` / `sms:` / `mailto:` / `geo:` (all browsers) and `intent://` (Chromium).

## Next steps

- Replace mock `data/*.json` with a scheduler-driven curation job.
- Add an audio briefing (`briefing.json.audio` → Piper-rendered MP3 under `/data/`).
