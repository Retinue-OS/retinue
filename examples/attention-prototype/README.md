# Attention prototype

A working prototype of the attention model proposed in
[`docs/attention-model.md`](../../docs/attention-model.md), on example data, with
a scripted 24-hour day playing on top of it. Everything is plain JavaScript with
no build step or dependencies; the same files run in the browser, in Deno and in
Node.

## Run it

Serve the folder with any static server and open `index.html`:

```bash
deno run --allow-net --allow-read jsr:@std/http/file-server .   # http://localhost:8000
# or: python3 -m http.server 8000
```

`python3 build.py` inlines everything into one self-contained page,
`dist/attention-prototype.html`, which is what the published artifact is.

`node replay.mjs` (or `deno run replay.mjs`) replays the scripted day headlessly
and prints the narration plus the dashboard's sections at a few checkpoints —
useful when changing the engine or the script. Pass minutes since midnight to
choose the checkpoints (`node replay.mjs 480 725`).

Deep links: `index.html?at=13:35` opens the day at that time, `&play=1` starts
playing, `&open=<item id>` opens an item (`&tab=chat` for a chat's message history),
`&details=1` opens its details sheet as well, `&held=1` expands Held.

## What is in it

| File | Role |
|---|---|
| `backends.js` | Three example-data backends that stand in for the real gateway routes — `/chats` (messenger messages with the Secretary's triage classification), `/conversations` (threads opened by agents), `/projects` (frontmatter, one paused project that the recurring-projects job wakes). Each has `list()` in the route's shape and `events(from, to)` for arrivals. |
| `dialogues.js` | Canned conversations with Ara, one per item, standing in for the model turns a deployment would run: an opening, chips as canned prompts, replies with effects (handled, parked, parked on someone else, a reply drafted into the chat composer). |
| `engine.js` | The attention engine: the four-plus-one item properties (importance, due, lead time, sphere + tags, current actor), the level table with urgency = time left ÷ lead time, focus modes with admitted spheres, admitting tags and per-sender permits, the delivery decision (push now / hold for the digest / list), breakpoints and digests, the half-hourly sweep, the repeat policy, and the three-field corrections that feed the attention profile (importance priors, lead times per kind, permits per mode). |
| `simulation.js` | The scripted day — what you are doing, whether you follow the nudge — and the runner (clock, beats, seek, pause when the viewer takes over). |
| `ui.js`, `style.css`, `index.html` | The phone dashboard (Now · Next · Held · Waiting, mode chip, push banners), the two views a tap opens — a messenger chat with its history, a composer and an Ara pane, or a thread with Ara's turns and chips — the details sheet (ⓘ) with the three fields and their corrections, and the deck (clock, timeline, narration, system state, profile, backends). |

## Tapping an item

As on the real dashboard, an item opens the place where it is dealt with, not a
checkbox. A messenger chat opens on its **Ara** tab, the chat's own conversation with
Ara: her opening restates the one-line description the dashboard row shows,
she answers questions, and she can stage a reply as a draft in the composer —
the send press stays yours, and a sent reply counts as handled. The **Chat**
tab beside it holds the full message history with a composer. A thread or a
project opens the conversation with Ara about it; her chips are canned prompts,
and she says what she can do and what needs you: the VAT return, for instance,
is prepared but goes through the tax administration's portal with your login,
by hand or through a Cowork session with the Ara connector driving the browser.
The ⓘ button shows the three fields (importance, urgency, delivery) with their
corrections; *Later* and *Mark done* stay one tap away.

## How the simulation works

The clock runs at a chosen speed and pauses briefly on every beat of the story
so it can be read. Beats are either narration or a scripted action of the user
(do it, later, pull out of Held, change the mode, correct a field). The engine
narrates its own side as it goes: arrivals, what was held and why, digests,
pushes, escalations by the sweep, what the profile learned.

The dashboard is live at all times. Any interaction with it pauses the story
("you are driving"); *Resume* continues from the state you left, and a scripted
action whose precondition no longer holds is skipped with a note. Clicking on
the timeline replays the script deterministically up to that time.

The schedule in the prototype (Off · Home · Deep work · Open · Work · Open ·
Social · Off, digests at 08:00, 12:00, 17:00 and 21:00), the spheres, the
contacts and the lead-time defaults are examples; a deployment names its own.
