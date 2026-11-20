# Dossier prototype

The attention prototype's day ([`../attention-prototype/`](../attention-prototype/))
— the same messages, mails, topics and wake-ups at the same minutes — lived
under the model proposed in [`docs/dossiers.md`](../../docs/dossiers.md): one
entity, the **dossier**, for people, topics and the inbox. First iteration;
plain JavaScript, no build step or dependencies; the same files run in the
browser, in Deno and in Node.

## Run it

Serve the folder with any static server and open `index.html`:

```bash
STT_URL=http://localhost:8100/transcribe deno run --allow-net --allow-read --allow-env serve.ts   # http://localhost:8000, voice via your stt service
# or any static server, without voice transcription: python3 -m http.server 8000
```

`python3 build.py` inlines everything into one self-contained page,
`dist/dossier-prototype.html`.

`node replay.mjs` (or `deno run replay.mjs`) replays the scripted day
headlessly and prints the narration plus the home, the contacts and the inbox
count at a few checkpoints; it exits 1 if a beat of the story was skipped.
Pass minutes since midnight to choose the checkpoints (`node replay.mjs 480 725`).

Deep links: `index.html?at=16:20` opens the day at that time, `&play=1` starts
playing, `&list=contacts|dossiers` opens a list, `&open=<dossier id>` opens a
dossier (`&to=ara` addresses the composer to Ara on a relationship),
`&details=1` opens its details sheet, `&chip=<label>` taps a chip on load,
`&other=1` expands *Other spheres*. Try `?at=10:21` for the widened aperture
with its countdown, `?at=21:16&list=dossiers` for the tree.

The chrome is the dashboard's as it is today, so the layout can be judged
with every feature in place: the navigation row and the shelf frame every
page; the page bar keeps minimize, the model picker and the ⋮ menu. The shelf
is used by the story at 08:20 (the quote is minimized) and 11:30 (reopened from
there and sent); a dossier left while Ara is answering parks itself there with
her dot, as on the dashboard.

## What changed against the attention prototype

| | Attention prototype | Dossier prototype |
|---|---|---|
| Rows | attention items over chats, threads, projects | **dossiers**: relationships (one per person or group), topics (project files; threads with Ara), one standing *Inbox Zero* |
| Home | Now · Next · Held · Waiting | Now · Active (this mode's spheres) · Other spheres · Waiting — only dossiers with something pending |
| Chat list | the Chats card | **Contacts**, by latest message; a new sender is a row the moment they write |
| E-mail | arrives as a Secretary-triaged thread | **in the open on arrival** in the sender's relationship; judged by Inbox Zero at the next batch (no VIP, no turn) |
| The digest | a push listing held items | unchanged — plus **Inbox Zero's batch** at the same breakpoints, one event with the proposals and their chips |
| Ara | a pane beside the chat; the thread itself | the composer's default addressee on every dossier; on a relationship a *to* switch picks the person's channel or Ara |
| Archive | archive + mute | **none** — a settled relationship sinks in Contacts; a topic is done or rests |
| Forwarding | — | **the gesture**: ⋯ on a message → select → *Add to dossier…* with a comment → one event, a backlink under the message |
| Mode | four presets, each an allow-list of spheres and a threshold | still the four presets — now of **focus** (flow · spheres · one dossier, with or without its relations) and **aperture** (only this · + urgent · + active · everything); two chips under the mode chip override the preset for the stint, a widening snaps back after eight minutes |
| Hierarchy | projects flat (`goal:` unused by the UI) | **topics nest**: `within` one parent; the Dossiers page is a tree, a dossier's page links its parent and children, the focus on a dossier covers what is within it |

The story's morning is focused work: at 08:30 the focus goes onto the CRR
dossier (an engagement with two pieces of work within it); at 10:20 a reboot
gives eight idle minutes and the aperture is widened a notch — Beat's clause
appears, Anna's dinner does not — then once more, and at 10:30 it has snapped
back by itself. The 12:00 digest is one item shorter for it: looking is
delivery. At 21:15 the gesture opens a third sub-dossier within CRR.

Two of the old threads were letters the Secretary had triaged — the parking
card and the tax office. Here they are what they were, mails, and the story
shows both paths: the parking mail stays in the inbox all day with its
proposal restated at the next batch, never nagging twice; the tax letter
becomes a dossier of its own from Inbox Zero's proposal at 16:20, the mail
referenced as its first event, and waits on the accountant from 16:22. At
21:10 the gesture is used by hand: Petra's summary from the Quartier group
goes into a new dossier with a comment.

## Files

| File | Role |
|---|---|
| `backends.js` | The example day: contacts with their gate class (VIP / batch / group), the message ledger (messenger and mail alike), Ara's own topics, the chamber topics with their states, the standing dossier. |
| `dialogues.js` | Canned Ara: per topic and per relationship (her remarks in the stream, drafts into the composer), and Inbox Zero's proposals per mail sender with their chips (`resolve` takes the mail out of the inbox, `dossier` opens a topic with it). |
| `engine.js` | The dossier engine: the attention arithmetic unchanged (level table, modes, permits, breakpoints, sweep, corrections), on dossiers instead of items — relationships with a pending state, topics with active / waiting / resting / done, the batch, the one-dossier rule for mail, references with backlinks. |
| `simulation.js` | The scripted day: the same external events, the same choices, narrated under the new model; two added beats for Inbox Zero's proposal and the gesture. |
| `ui.js`, `style.css`, `index.html` | The phone with the dashboard's chrome: the **navigation row** at the top of every page (Home · Contacts · Dossiers · News, the pending-sends icon and the gear; the back button on a drill-down), the **page bar** under it (title and state, minimize –, the model picker, the ⋮ menu with Attention details · Star · Mute · Rest · Done), the dossier page (messages, mail cards, Ara's remarks and events in one stream; the *to* switch; ⋯ → select → *Add to dossier…*), the details sheet, and the **shelf** at the foot (minimized dossiers as icons with the unread count top right and Ara's dot bottom right; right-click or long-press for Open · Close · Close all quiet ones). News, sends and settings are stubs: unchanged by the design. |

The schedule, spheres, contacts and lead times are the attention prototype's
examples; a deployment names its own. Everyone in the story is fictional.
