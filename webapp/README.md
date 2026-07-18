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

The dashboard card stays compact: it shows the five most recent active threads
plus an **All conversations →** link to `conversations.html`, a dedicated page
that lists every thread with an Active/Archived filter (the same
`retinue-conversations` element with the `full` attribute). Archiving is done
from inside a thread; archived threads leave the active list but remain on that
page and via `GET /conversations?archived=1`.

A retinue agent opens a thread that needs the user's decision with
`scripts/conversation-push.py` (token-gated `POST /internal/conversations`):

```bash
conversation-push.py --title "Party RSVP" \
  "You've got an invite to Mara's party. Confirm and add to your agenda, or decline?"
```

The thread shows up with an unread badge; when the user replies, Ara picks it up
with full context. The endpoint is gated by `CONVERSATION_BACKEND_TOKEN` (set by
the entrypoint) so only in-container agents can post on the user's behalf.

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

1. **Quick command** — the bar at the bottom (typed, or dictated via the same
   `/conversations/transcribe` pipeline as chat). The request becomes a
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

## Voice conversations

Threads can be spoken as well as typed, with no streaming and split by direction:

- **Input (send as audio).** A microphone button in the composer records with
  `MediaRecorder`; on stop the audio blob is POSTed to `/conversations/transcribe`.
  The web gateway proxies it to the shared STT service (`scripts/stt-service.py`),
  which owns the Whisper model (so this image ships no ASR stack). The transcript
  is dropped into the composer for the user to review and edit before sending —
  speech recognition is imperfect. Requires `STT_SERVICE_URL` (set in
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
