# From the prototype to the dashboard — the attention model as the home screen

*The plan for making `examples/attention-prototype/` the dashboard: which
decisions move into the gateway, which files change, in which slices, and what
the replacement keeps. Read `docs/attention-model.md` first for the model
itself and `docs/dashboard.md` for the dashboard as it is today. Everything
below is Tier 3 (webapp, gateway, scripts) and lands through PRs.*

## The one rule

**The gateway decides, the browser shows.** In the prototype the engine runs in
the page because there is no server. In the dashboard every delivery decision
— push now, hold for the digest, list silently — is made where the Web Push
fan-out happens and where the half-hourly sweep can run without a browser
open: in `scripts/web-gateway.py` and the scheduler. The browser renders what
the gateway hands it and posts the user's actions back. The prototype's
`engine.js` therefore has a Python twin, `scripts/attention.py`, which is pure
policy: it takes the gateway's own documents and returns decisions and
effects, and never sends or saves anything itself (tests:
`tests/test_attention.py`).

## What carries the four properties

| Entity | Store today | Gains | Set by |
|---|---|---|---|
| Thread | one JSON per thread under `CONVERSATIONS_DIR` | an `attention` block: `importance`, `due`, `lead`, `sphere`, `tags`, `kind`, plus the delivery state (`released`, `snoozed_until`, `boost`, `last_level`, `pushed`) | `conversation-push.py --importance --due --lead --sphere --tag --kind` → `POST /internal/conversations`; the Secretary's triage sets them on the threads it opens |
| Chat | one JSON per chat under `CHAT_STATE_DIR` | the same block | the gateways' notify rail, from the triage classification of a whitelisted sender (importance, deadline, kind) and the contact's sphere; a sender prior from the profile otherwise |
| Project | frontmatter in the chamber | `importance:`, `sphere:`, `tags:`, `kind:`; `expected_by` / `next_due` are the deadline, `remind_before` the lead | the chamber's author; `recurring-projects.py` passes them when it opens the wake-up thread |

`attention.item_from_doc` reads that block with the brief's fallbacks
(importance 2.5, the kind's lead time), `item_to_attention` writes it back. A
boot emitter (the `discover-agents.py` discipline: sorted N-Triples, no blank
nodes, write-if-changed) publishes the four properties of open threads and
chats into `chambers/_generated/attention/` under `kb:importance`, `kb:due`,
`kb:leadTime`, `kb:sphere`, `kb:tag` and `kb:currentActor`, so "what wants
attention now, at which level" is one SELECT for the dashboard and the jobs
alike, projects included through their own converters.

## Two small documents the gateway keeps

`ATTENTION_DIR` (default a sibling of `CONVERSATIONS_DIR`, so it inherits the
persistent volume) holds:

- **`focus.json`** — the modes (admitted spheres, admitting tags, threshold,
  blurb), the schedule as minute-of-day → mode, the digest times, and the
  manual override. `attention.default_focus()` is the shipped default; a
  deployment edits the file or, later, drives it from the calendar.
- **`profile.json`** — importance priors per sender or kind, lead times per
  kind, permits per mode, and the learned log. Slice 3 mirrors it as prose the
  user can read and edit, as `preferences.md` is for news.

## The API the home screen uses

| Route | Does |
|---|---|
| `GET /attention` | the union of open threads, chats and projects as sections Now · Next · Held · Waiting, each row with its three fields explained, plus the mode in force, the next breakpoint, the counts and `degraded` (a source the store could not answer for) |
| `GET /attention/item?id=…` | one item, explained, with the mode and the sphere vocabulary — what the sheet shows |
| `POST /attention/mode` `{mode}` / `{mode: null}` | set the mode by hand or release it to the schedule; a change is a breakpoint |
| `POST /attention/items/later` `{id, when: next\|tomorrow}` | snooze |
| `POST /attention/items/pull` `{id}` | pull out of Held ahead of the digest |
| `POST /attention/items/done` `{id}` / `…/reopen` | mark handled (a sent reply does this on chats) / put it back |
| `POST /attention/items/correct` `{id, importance? due? lead? sphere? tags? critical?}` | the field correction; writes the prior, the lead time or the sender's sphere into the profile and re-evaluates |
| `POST /attention/permits` `{sender, mode?, on}` | let a sender interrupt in a mode (the mode in force by default) |
| `POST /attention/admit` `{sphere, mode?, on}` | change a Focus rule |
| `GET /attention/profile`, `POST /attention/profile` | read and replace the profile and the focus rules |
| `POST /internal/attention/set` `{id, …}` | an agent declares or revises an item's properties (token-gated; `scripts/attention-set.py`) |

Item ids are the existing ones (`thread:<id>`, `chat:<id>`, the project URI)
and travel in the body — chat ids and project URIs carry characters no path
segment should — so the chat page, the thread and the project page stay the
drill-downs.

## Slices

*Status: slices 1 and 2 are built on this branch (the deferral runs on the
gateway's own tick rather than a scheduler job — the tick lives where the
push fan-out and the documents live, and needs no second process); slice 3's
first pieces (sphere priors, the rail's `attention` for the triage's
judgement, `attention-set.py`) are in, the rest is open. What was built, how
it is configured and where it is tested: `docs/dashboard.md`, "The home".*

**1 — the model and the home screen** (most of the value, no digest yet)

- `scripts/attention.py` with tests — *done on this branch*.
- The `attention` block on threads and chats; the new flags on
  `conversation-push.py`; the notify rail sets a chat's block from the triage
  classification (`chat_ingest.py`) with the contact's sphere from the
  chamber's contact groups.
- `GET /attention` and the action routes above in `web-gateway.py`; `focus.json`
  and `profile.json` under `ATTENTION_DIR`.
- Push obeys the mode: `_push_conv_notification` and `_chat_push_notification`
  ask `attention.on_arrival` first; a held item badges but does not push. The
  per-device `notification_mode` keeps working as a second filter until slice 2
  replaces it with the mode (issue #66's four choices collapse to *follow the
  mode* / *everything* / *off*).
- `webapp/components/attention.js` from the prototype's list: rows with the
  preview and the three-field line, the mode chip and menu, the collapsed Held
  and Waiting sections, the details sheet with the corrections. `index.html`
  shows it as the home; the chats, conversations and projects cards go, their
  pages stay. Opening a row goes where it goes today — the chat page with its
  Ara pane, the thread, the project page — with the ⓘ sheet and *Later* /
  *Mark done* added to the chat page and the thread bar.
- The boot emitter into `_generated/attention/`.

**2 — deferral**

- The gateway's own tick (`ATTENTION_TICK_SECONDS`), credit-free: at digest
  times and scheduled mode changes `attention.breakpoint` releases what was
  held with one `Topic`-collapsed digest push (`Urgency: normal`); every 30
  minutes `attention.sweep` re-evaluates — a crossing into the next urgency
  band climbs, an item the mode now admits is pushed (`Urgency: high`;
  `push_notify.py` passes both headers to pywebpush). Quiet hours are the Off
  mode; a manual change is a breakpoint immediately. *Built.*
- The repeat policy per sender class, applied on the notify rail
  (`attention.repeat_policy`). *Built.*
- The per-device notification mode stays as a second filter for now;
  retiring it is the one item of this slice still open.

**3 — learning**

- The Secretary's triage judgement writes importance, deadline and kind on
  what it files; corrections keep writing priors and lead times; the profile
  gets its prose mirror; calendar-driven modes through the CalDAV gateway; the
  Secretary's reply to a held sender where the contact's send policy allows.

## What the replacement keeps, and what it removes

Kept, unchanged in behaviour: the chat page with its companion pane, drafts and
sends; conversation threads with Ara, chips, the model picker, attachments and
voice through the stt service; project pages and edit threads; the news feed
and its page; the sends page; the gateways and Claude-auth pages; settings and
the push opt-in; the PWA shell and its self-update; the presentation lint.

Removed: the three home cards (chats, conversations, projects) as the top
level — the attention list is the home — and, in slice 2, the per-device
notification modes.

The prototype's canned dialogues do not move: in the dashboard those turns are
Ara's real ones. Its draft rewrites become a chip on the companion pane
(*Shorter* / *Warmer* / *More formal*) that runs a cheap model turn, the way
transcript cleanup does.

## Decisions taken with slice 1

1. The sphere set and the default modes and schedule are the brief's list,
   shipped as `attention.default_focus()`; a deployment edits `focus.json`
   (the sphere vocabulary included) or, later, drives it from the calendar.
2. The news card stays on the home below the attention list, in its own
   resizable region; the chats, conversations and projects cards are gone,
   their pages linked from the list's footer.
3. The per-device notification modes stay as the second filter behind the
   model until a later slice retires them.
4. What a message is worth before anyone has judged it: a person writing
   directly is *active* (held for the next digest; rung at once where they
   hold a permit), a group is chatter (passive, listed). An agent thread
   that declares nothing is passive — the agents now have to say how much
   something matters, and the base scripts (the monitors,
   `recurring-projects.py`) do.
5. `ATTENTION_PUSH_GATE=0` keeps the pre-model push behaviour while the
   list, the digest and the sweep run — the rollback for a deployment whose
   agents do not yet declare importance.
