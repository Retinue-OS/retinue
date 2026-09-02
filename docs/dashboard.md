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

- `webapp/index.html` is the hand-editable shell/config — which cards show and
  the app-launch buttons (`tel:`/`sms:`/`mailto:`/`geo:`/`intent://`).
- `webapp/components/*.js` are web components that each fetch one JSON document
  and render it, degrading to the last cached state offline.
- `webapp/data/*.json` is the curated content. The static data cards that
  consume these files are commented out in `webapp/index.html` until a
  scheduler-driven refresh job regenerates them. **Refreshing these is Ara's job**
  (currently mock data). The server serves them at `/data/` from
  `DASHBOARD_DATA_DIR` (default `webapp/data`), kept separate from the baked
  shell so data can be written without rebuilding.
- `webapp/sw.js` caches the shell so the dashboard and its local app-launch
  buttons (notably the dialer) keep working with no connectivity.

The dashboard sizes itself to the layout. On a phone the page scrolls: the
conversations card stays compact at the five most recent active threads,
projects and news keep their own caps, and there is nothing to resize. In the
wide layout (`isWideFrame` in `webapp/components/base.js`) the frame is fixed
and the cards drop their caps: conversations sit above news on the left,
projects fill the right column top to bottom, each region scrolling internally
— and every boundary is a draggable splitter (`webapp/layout.js`), VS Code
style: drag to resize, double-click to reset, drag news all the way down to
close it; sizes persist per device in localStorage. Each list card's header
also carries a list/cards toggle (tiles that reflow vs a single column — also
per-device, hidden on phones where only one column fits anyway). Either way an
**All conversations →** link leads to the dedicated `conversations.html` page,
which lists every thread with an Active/Archived/Edits/Cowork filter — the
last two being where the otherwise hidden kinds (project edit commands, and
the Ask-Ara connector's cowork audit threads) are reachable. Threads can be
archived from inside a thread (`POST /conversations/<id>/archive`,
`…/unarchive`); archived threads drop off the active list but stay on that
page.

Shell updates apply themselves: the service worker versions itself from a
content hash the gateway stamps into `/sw.js`, and
`webapp/components/update.js` reloads a controlled page once a new worker
activates (guarded — never while a thread/composer is open, a draft is unsent,
or a text field is focused) and re-checks on every visibility change, so even
an always-open window picks a deploy up within moments of being looked at. The
settings page shows the running shell version with a manual "Check for
updates" as fallback.

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
from the next turn. The picker governs **Ara's own turn only** — dispatched
subagents (the Archivist and any chamber-provided subagents) always run on their
own hard-wired models regardless of the selection. In a deployment that routes
through **LiteLLM** (the shipped default), the offered list is managed **in
LiteLLM itself**: the gateway reads `GET /model/info` (and `GET /v1/models`)
and offers the concrete models the proxy currently serves. `retinue_picker: true`
plus `retinue_label` still names a route; `retinue_picker: false` hides one.
An Ollama primary (`LITELLM_PRIMARY_MODEL=ollama/…`) drops leftover Claude
catalog seeds so the picker cannot offer a model the backend does not serve.
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
stored choice runs that default (stored as the empty string internally).
The dashboard reads the list from `GET /conversation-models` and
persists a thread's choice via `POST /conversations/<id>/model` — an id not on
the offered list is ignored (the thread falls back to the default), so a client
can never inject an arbitrary `--model`. The picker hides itself when fewer than
two models are offered. Model tiers and per-thread escalation:
`docs/model-routing.md`.

## Attachments

A thread can carry **file attachments** the user downloads straight from
the dashboard — e.g. an e-mail attachment (a PDF invoice) forwarded into a
thread, so it's reachable without an e-mail client. Pass `--attach PATH`
(repeatable) to `conversation-push.py`; the file is stored beside the thread
(under `CONVERSATIONS_DIR/attachments/<id>/`, keyed by a server-generated id so
untrusted filenames never touch the filesystem) and rendered as a download link
in the message bubble, served by `GET /conversations/<id>/attachments/<att-id>`
behind the dashboard's own auth. Prefer this over pushing a document via Signal
when the user is already working in the dashboard.

To deliver a file into a thread that **already exists** — rather than stranding
it in a fresh tab the user has to go find — pass `--thread <id>` (the thread id
from the conversation URL). It posts to the token-gated
`POST /internal/conversations/<id>/messages`, appending an agent message with
the attachments and marking the thread unread. Note that Ara's own reply to a
thread is appended by the gateway *after* her session ends and carries no
attachments, so a file must be pushed as its own message this way.

Attachments go **both ways**: the user can attach files to their own messages
from the composer (a paperclip button on the input row). These upload with the
message, are stored the same way, and their on-disk paths are handed to Ara in
her engage prompt — so she can actually open a file the user sends (a PDF, a
CSV) rather than only knowing one exists. A message may be text, files, or both.

## Push notifications

The unread badge only exists while the dashboard is
open — which is precisely not the case when an agent opens a thread that needs a
decision. So every agent→user turn that lands unread (a thread an agent opens
via `conversation-push.py`, a message it appends, and Ara's own async reply)
also fans out a **Web Push** notification to the user's registered devices;
tapping it opens that thread. This is automatic — there is no separate step
after posting to a conversation.

The plumbing lives in `scripts/push_notify.py` (VAPID keypair, one file per
device subscription, both persisted under `PUSH_DIR` — by default a sibling of
`CONVERSATIONS_DIR`, so it inherits the persistent `/root` volume) and three
gateway endpoints: `GET /push/config`, `POST /push/subscribe`,
`POST /push/unsubscribe`. The user manages the opt-in from the **settings
page** (`settings.html`, reached via the gear in the dashboard header), where
`webapp/components/push.js` in `manage` mode shows this device's state —
unsupported, blocked, off, or enabled with the delivery preferences and a
disable button. The module also runs a silent re-registration on every
dashboard load, so an already-granted device heals a rotated or server-reset
subscription without visiting settings. Two caveats worth knowing: on **iOS**
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
visible thread whose engage prompt points Ara at the project file.

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

Language handling (constrain detection to the languages the user speaks, and
re-decode when a guess falls outside that set) lives entirely in the service via
`STT_SUPPORTED_LANGUAGES`. An optional `STT_TOKEN` gates the endpoint
(defence-in-depth; the service is not published to the host). Downloaded model
weights persist in the `stt-models` volume.
