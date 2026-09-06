# The attention model's day, on the real stack

The prototype's twenty-four hours (`examples/attention-prototype/`), replayed
on the deployment's own code: the web-gateway with the attention model
(`docs/attention-model.md`), the home screen, the chat page and the threads.
Only what needs the outside world is stood in for — a mock life store with the
example day's message ledger and projects, three mock messenger gateways that
accept your sends, and a canned Ara whose turns follow scripted dialogues.
Everyone in the story is fictional.

```bash
python3 examples/attention-simulation/simulate.py --open     # http://localhost:8766/simulation.html
```

One of the day's scenes is the stranger: at 13:50 a number nobody knows writes
on WhatsApp — you gave it out at a workshop the day before. The real delivery
gate flags the sender unknown, so the model screens the message (sphere
`unknown`, which no mode admits): held for the 17:00 digest, listed under
Held, never rung. You pull it out, read it, and fill the contact card — her
name, the sphere she belongs to, a second group as a tag. That one write names
the chat, teaches the profile, whitelists her handle and files her in the life
store's address book, and when she writes again at 16:20 the gate — the real
one, reading the file the card just wrote — forwards her as a known sender,
the triage puts a deadline on it, and it rings.

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
