# The attention model's day, on the real stack

The prototype's twenty-four hours (`examples/attention-prototype/`), replayed
on the deployment's own code: the web-gateway with the attention model
(`docs/attention-model.md`), the home screen, the chat page and the threads.
Only what needs the outside world is stood in for — a mock life store with the
example day's message ledger and projects, three mock messenger gateways that
accept your sends, and a canned Ara whose turns follow scripted dialogues.
Everyone in the story is fictional. The day is a workday — today's date,
on the shipped week's *Workday* plan; run on or next to a weekend, the
runner moves the story's days into that plan, so the day plays the same.

```bash
python3 examples/attention-simulation/simulate.py --open     # http://localhost:8766/simulation.html
```

One of the day's scenes is the stranger: at 13:50 a number nobody knows writes
on WhatsApp — you gave it out at a workshop the day before. Nothing vouches
for the sender: the real delivery gate says she is no VIP, and the dashboard
has no contact card and no sphere for her. So the model screens the message
(sphere `unknown`, which no mode admits): held for the 17:00 digest, listed
under Held, never rung. You pull it out, read it, and fill the contact card —
her name, the sphere she belongs to, a second group as a tag. That one write
names the chat, teaches the profile and files her in the life store's address
book, and when she writes again at 16:20 — the gate saying exactly what it
said before — the card is what places her: a customer with a deadline, and it
rings.

The deck shows the clock, the timeline (the mode bands, the digest times, the
beats), the day's feed — what you are doing, what arrives, what the gateway
decided and why, every push, everything the profile learned — and the system
state. The phone beside it is the real dashboard, served by the same gateway
with the story's clock: the mode chip, the ⓘ sheets with their corrections,
Later and Mark done, the chats with their Ara pane, the threads with their
chips all work. Touch it at any time: the story pauses ("you are driving") and
carries on from what you changed when you resume; a beat whose precondition
you removed is skipped, not forced. Click the timeline to jump; a jump replays
the day from midnight to that minute on a clean state.

The phone's status bar carries a notification bell: what the phone would
actually show. The gateway's pushes go through the filter a device stores
when its owner takes the push opt-in's defaults — new and stalled
conversations (`push_notify.DEFAULT_PREFERENCES`), so Ara's replies in a
thread already under way do not reach it — and a notification replaces one
with the same tag still in the tray, as the service worker's does: each digest
replaces the last. A new one drops a heads-up banner; the bell opens the
shade, and tapping a notification opens its link in the phone (a digest opens
on what it released) and, like any touch on the phone, pauses the story. The
feed says of every push whether it reached the phone and, if not, why.

`record.py` runs the day headlessly, screenshots the dashboard after every
beat with Chromium, and writes `dist/replay.html` — one self-contained page
to watch where nothing can run:

```bash
python3 examples/attention-simulation/record.py     # needs a Chromium; --chromium PATH
```

Files: `story.py` (contacts, messages, threads, projects, the beats and the
dialogues), `simulate.py` (the runner, mocks and routes), `simulation.html` +
`deck.js` + `deck.css` (the deck), `record.py` + `replay.template.html` (the
recording). `tests/test_attention_simulation.py` replays the day and fails on
any skipped beat.
