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
  shell so data can be written without rebuilding. What agents write there
  is answered as JSON only, the type the cards read; any other file is a
  download (see "The Content-Security-Policy").
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
home or the Threads page closes in place. A project being edited goes back
like any other page — the edit is kept on the device and the editor opens
again with it on the next visit; only the editor's own × drops the changes
(back used to cancel the edit). The page's own bar (title, Archive, minimize, model picker, menu)
stays as a second row under it. A tab always goes, whatever the page
holds: a typed message, an unsaved project edit or a dictation on its way is
kept on the device and waits in its field for the user to come back to the
same place (`webapp/components/drafts.js`), and a dictation still being
recorded or transcribed is finished by the service worker
(`webapp/components/dictation.js`; webapp README, "Unsent input"). A review
dictation (✓, or leaving with the mic live) lands in its field. A dictation
the user sent (➤, or a take of the continuous loop ending on a pause) in an
existing thread is a message, so the worker posts it into the thread it was
recorded in; only a send that failed or may have failed brings its text to
the field, with a note, and a first message (no thread yet) waits in its
composer's field. The tap used to be held instead, with a note popping up at the
bottom asking for a second press — which the phone keyboard, coming up with
the note, made impossible by hiding the row; pop-up notes at the bottom of
the screen are an anti-pattern here. While the phone keyboard is up the row steps aside on a
drill-down, as the page's own header does. On a phone the home never scrolls as
a page (the attention list scrolls inside a locked frame, under the
navigation); in the wide layout (`isWideFrame` in
`webapp/components/base.js`) it fills a fixed frame and scrolls internally.
The home carries no news: a feed there defeats the point of a
distraction-free list of what wants attention, so the news lives only on its
own page (`news.html`), one tap away in the navigation row. *Threads* is `conversations.html`, which lists
every thread with an Active/Archived/Edits/Cowork filter — the last two being
where the otherwise hidden kinds (project edit commands, and the Ask-Ara
connector's cowork audit threads) are reachable. Its tiles swipe by the
mechanism the Chats page's rows use (`components/swipe-rows.js`; what a
swipe does there is in "Chats: unread, read, archived" below):
a swipe right archives an active thread or restores an archived one at once,
a swipe left (or a long press) opens a tray of Star · Archive · Mute, or
Star · Restore · Mute/Unmute · Delete. A commit that takes a row out of view
offers Undo for 15 s (the toast stays while a finger is on it). The open thread's bar keeps its Archive button
and has a ☆/★ beside it. Archived threads drop off the active list but stay
on that page; what each action means is in "Archived, muted, starred,
deleted" below.

Shell updates apply themselves: the service worker versions itself from a
content hash the gateway stamps into `/sw.js`, and
`webapp/components/update.js` reloads a controlled page once a new worker
activates (guarded — never while a thread/composer is open, a draft is unsent,
or a text field is focused) and re-checks on every visibility change, so even
an always-open window picks a deploy up within moments of being looked at. The
settings page shows the running shell version with a manual "Check for
updates" as fallback.

## The Content-Security-Policy

Content from outside — a message, a mail, a file somebody sent — must never
become a page, a script or a link of another scheme on the dashboard's origin;
the gateway's escaping and its media rules (#402, #404; "Attachments" and
"Media in a messenger chat" below) see to that. The Content-Security-Policy is
the second line behind them: should markup from outside reach a page all the
same, it cannot run script there, because scripts run only from the
dashboard's own files. Every page of the gateway carries it — the
pages of `webapp/`, the pages the gateway builds itself (`/sends`,
`/gateways`, `/claude-auth`, the session log) and its error pages. It is
added where the handler's headers end, for any answer of type `text/html`, so
no code path can forget it. (One answer of that type is no page of the
dashboard and opts out by name: a mail's HTML part, which is served as a
sandboxed document under a stricter policy of its own — "The frame on the chat
page" below.) The policy is built in one place,
`scripts/content_policy.py`, with the page that needs each source:

```
default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline';
img-src 'self'; media-src 'none'; font-src 'none';
connect-src 'self'; frame-src 'none'; object-src 'none'; base-uri 'self';
form-action 'self'; worker-src 'self'; manifest-src 'self';
report-uri /csp-report
```

Scripts come from this origin only: no inline script, no handler attribute,
no `javascript:` URL, no `eval`. Styles stay open (the components set style
attributes; restricting styles is not the aim). Images come from this
origin. No image or medium is loaded from another origin: the chat page
shows an attachment that is a web link as a link to open (see
"Attachments"), and the session log shows an image an answer names (a web
address or a `data:` URL) as a link to it, or as its alt text, never as an
image, as the dashboard's Markdown renderer does. Every page but one plays
no audio or video, and every page but two frames nothing, so `media-src`
and `frame-src` are `'none'`: markup that slipped into a page can then not
frame another page of this origin either (an approval card, under a click
meant for something else). No page loads a font.
Three pages get more, each for itself alone (`PAGE_SOURCES` in the module;
a source added to a directive that is `'none'` takes its place):
the chat page (`/chat.html`) adds `blob:` to `img-src`, for its previews of
the images about to be sent, which it makes itself as object URLs (no other
page makes one), gets `media-src 'self'`, for the messenger media it
plays — it is the only page that plays any — and gets `frame-src 'self'`,
for the one thing it frames: a mail's HTML part (below). (The briefing card
would play audio on the start page, but `index.html` leaves it out for now;
bringing it back means giving `index.html` `media-src 'self'` there.) The
file viewer (`/viewer.html`) gets `frame-src 'self'`, for the attachment it
shows in a frame; it and the chat page are the only pages that frame
anything. `/sparql` (also served as
`/sparql.html`) adds `data:` to `img-src`, for the icons in YASGUI's
stylesheet. It needs nothing else: the vendored YASGUI compiles no strings.
Two other policies stay as they were, separate and always enforced:
`/settings-instance.html`'s `frame-ancestors` (who may frame it, see "The
settings page") and the file viewer's own `frame-src 'self'`. Inside the
Android shell, the bridge the app injects (`window.RetinueShell`) and the
scripts it evaluates are the app's, not governed by a page's policy.

**The frame on the chat page.** The chat page frames one thing: the HTML part
of a mail, when the user opens a mail's *Original* ("Mail threads as chats"
below; `GET /chats/<id>/messages/<mid>/original.html`). That is markup from
outside shown on purpose, so it is given nothing of the origin, by three
lines that each hold without the others:

- it is **sanitised** before it is served (`mail_trim.sanitize_html`): an
  allow-list of tags and attributes re-serialised from the token stream — no
  script, no handler attribute, no form control, no frame, no `meta`, nothing
  that loads. Its own inline (`cid:`) images are embedded as `data:` URLs,
  from that message's own attachments only, raster types only, up to
  `MAIL_ORIGINAL_INLINE_MAX_BYTES` (8 MiB) a mail; every other image source
  is dropped, so a tracking pixel never loads;
- it is **its own document under its own policy**, always enforced, whatever
  `RETINUE_CSP` says:
  `sandbox allow-popups allow-popups-to-escape-sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'`.
  `sandbox` gives it an opaque origin — no script, no form submission, no
  access to the dashboard's cookies, storage or API, no navigating the page
  around it — and the two `allow-popups` tokens let exactly one thing
  through: a link the user clicks opens as an ordinary tab. It also goes out
  with `X-Content-Type-Options: nosniff`, `Cache-Control: no-store`,
  `X-Frame-Options: SAMEORIGIN` (only a page of this origin frames it) and
  `Referrer-Policy: no-referrer` (the site a link leads to learns no address
  of the dashboard). The dashboard's own policy is left off this one answer:
  that policy describes the dashboard's pages, which this document is not —
  its `img-src 'self'` would stop the embedded pictures, and report each of
  them in `report` mode;
- the page frames it with the `sandbox` attribute as well
  (`allow-popups allow-popups-to-escape-sandbox`), so the same holds where a
  client ignores the header;
- **in the Android app** (a request whose user agent carries
  `RetinueShell/`) the shell's WebView opens no new windows, so a link meant
  for a new tab would go nowhere. There the sanitiser targets links at the
  top frame (`target="_top"`) and both the header and the frame's attribute
  add one token, `allow-top-navigation-by-user-activation`: a link the user
  taps navigates the top frame, which the app hands to the phone's browser
  for any address off the dashboard's origin, the dashboard staying where it
  was. Nothing navigates without the tap, and no script runs either way. The
  answer carries `Vary: User-Agent`.

A source list cannot name that one path family — a chat id is part of it —
so the directive is `'self'`, and what the frame holds is the page's doing:
it sets the frame's address from the `html_url` the gateway answered, never
from anything a message said. What `'self'` gives up is stated plainly: on
the chat page alone, markup that slipped into the page could frame another
page of this origin. The page renders what a message says as text, and
`tests/test_chat_mail.py` pins the answer's headers and what the sanitiser
leaves of a hostile mail.

The policy decides what a page loads and runs, not where a link takes the
user: following a link is a navigation, which none of its directives
restricts (`form-action` covers only where a form is sent). So the call
directive's tap-to-dial `tel:` link (see "Speech-to-text (the `stt` service)
and voice input") and the `mailto:` links work in every mode. What keeps a
link of another scheme out of the pages is the markup's own rules, not the
policy, and both renderers keep the same one. The dashboard's Markdown
renderer makes links only of `http(s)`, `mailto:` and `tel:` URLs and of
paths on this origin, and its call link only of a number it has checked
(`webapp/components/markdown.js`). The session log (`/conversation`,
`/conversation/<date>`) renders answers with markdown-it in
`scripts/web-gateway.py`, which by itself would link any scheme but
`javascript:`, `vbscript:`, `file:` and `data:` other than an image; the
gateway drops every other link there and leaves its text
(`_md_refuse_links`). The one scheme the policy itself stops is
`javascript:`, which `script-src` blocks, and only in `enforce` mode: in
`report` mode it reports it and stops nothing.

**Three modes**, `RETINUE_CSP`:

- `report` (the default) sends `Content-Security-Policy-Report-Only`: the
  browser blocks nothing and reports what the policy would have blocked;
- `enforce` sends `Content-Security-Policy`: the browser blocks it, and still
  reports it;
- `off` sends neither — a rollback, not a setting to keep.

An unknown value counts as `report` and is named in the log. The mode and the
policy are part of the service worker's shell hash: the worker serves the
shell pages from its cache with the headers they were cached with, so a change
reaches installed clients as a new worker, like a changed file.

**The reports.** The policy names the gateway's own endpoint with
`report-uri`. Not with `report-to`: Chromium, and so the Android shell's
WebView, ignores `report-uri` once a policy names a `report-to` group and
leaves delivery to the Reporting API, which in a headless Chromium delivered
nothing within a minute, while `report-uri` arrives at once.
`POST /csp-report` takes both formats all the same (`application/csp-report`,
`application/reports+json`), at most 64 KiB with a `Content-Length`, behind
the dashboard's sign-in and nothing more, and answers `204` or a fixed error;
it reflects nothing. Each report is read for the page (its path), the
directive, the blocked URL and the source line, query strings and fragments
cut off; the distinct violations (page, directive, blocked URL) are counted,
with when each was first and last seen, in one JSON file beside the
conversations (`RETINUE_CSP_REPORTS_FILE`, default
`/root/.retinue/csp-reports.json`), at most 200 of them (past that only
`not_kept` grows). A new one is logged once, as `[web-gateway] CSP report: …`
from a report-only policy and as `[web-gateway] CSP enforce: …` from an
enforced one: the word is the report's disposition as the browser states it
(`report` when it states none), so search the log for `[web-gateway] CSP `
to find both. `GET /csp-report` lists them, latest first, with the mode and
the policy.

**From report to enforce.** Leave the mode on `report` for about a week of
normal use, on every device and in the Android shell, its settings page
included. Then read `GET /csp-report`:

- empty, or only entries that are not the dashboard's (a browser extension's
  script, a `chrome-extension:` or `moz-extension:` source): set
  `RETINUE_CSP=enforce` and recreate the container; installed clients take the
  new worker on their next visit;
- an entry on a dashboard page that the page needs (a script it runs, a kind
  of image it shows): fix the page, or add the source to
  `scripts/content_policy.py` with the page that needs it (to `PAGE_SOURCES`
  when only that page needs it), deploy, and give it another few days.

Keep reading it after the switch: in `enforce` mode the browser still reports
what it blocked.

**What a page author keeps to.** No inline `<script>`: a script is a file
under `webapp/` (a classic one where the page needs it at once, a module
otherwise), and what a page needs to know goes into its markup as data
attributes, as the gateway's own pages do (`webapp/gateway/`). No handler
attributes (`onclick=`, `onload=`, …) in pages or in what the components
render: listeners are added in script. No `javascript:` URLs, no `eval`,
`new Function` or string timers, nothing from another origin. A page that
cannot work without more gets it for that page alone, stated in
`PAGE_SOURCES` of `scripts/content_policy.py` (as the chat page's `blob:`
images and its frame, `/sparql`'s `data:` images and the file viewer's frame
are), rather than a weaker policy for every page. And markup from outside
that is to be *shown* — a mail's HTML — is never put into a page: it is
sanitised and served as a sandboxed document of its own, which a page frames
("The frame on the chat page" above).
`tests/test_webapp_no_inline_script.py` checks the pages, the components'
markup and every page the gateway builds (its builders rendered, and its
source for the error pages it writes in place), and that every script a page
loads is a file in `webapp/`; `tests/test_web_gateway_csp.py` the header on
every page in each mode.

**No route serves bytes from outside as a script.** `script-src 'self'` is a
boundary only while nothing on the origin answers bytes someone outside chose
under a type a browser runs as a script or a worker (the JavaScript MIME
types; every such answer forbids sniffing, so no other type runs). Thread
attachments, messenger media through `/chats/media`, the life store's results
on `/sparql` and the pairing-code proxy answer a script type as
`application/octet-stream` (`_served_media_type` in the gateway); the
letterbox share serves every file as a download anyway; `/data/` answers JSON
and any other file as a download. `tests/test_web_gateway_no_script_types.py`
asks each of them for every script type a sender might state.

**Other services on the dashboard's host.** The policy reaches what the
gateway answers and nothing else. A service the proxy routes to a path of the
dashboard's host serves its pages on the dashboard's origin, so a script that
runs there can do anything the dashboard can, approving a send included.
Every such service must carry a policy of its own that runs no script from
outside, or be published on a host of its own. Of those in the deployment
example (`docker-compose.override.example.yml`), the updater answers JSON
only, and the egress log viewer (`/egress`) sends a fixed, enforced policy on
every page (`default-src 'none'; style-src 'unsafe-inline'; form-action
'self'; base-uri 'none'; frame-ancestors 'none'`: its pages use no script,
only inline style), forbids sniffing on every answer, and escapes what it
writes into an attribute; `tests/test_egress_log_viewer.py` checks both.

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
state, not the source's: it never archives the thread or the chat, nor marks
the chat read, and the next message opens a fresh item. A chat shows its sender's spheres, as the contact card and the
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
row) is the address book itself: every person, searchable by name,
e-mail address and e-mail domain, each editable in place — their handles on
every channel, the e-mail domains they write from, their spheres, importance
prior, the Focus modes they may interrupt, the VIP flag and the instruction
(with *Copy instruction from…*, for a team). A new contact names the chamber
it is kept in. Over HTTP a person's domains are `domains` on every contact
`GET /contacts` lists; `POST /contacts` takes `domains`, and `POST
/contacts/<id>` takes `domains` (the whole list), `add_domains` and
`remove_domains` — `example.org`, or `*.example.org` for a domain and
everything below it. The address book reads and refuses them: `400` for what
is no domain (an address, a single label, a public suffix), `409` for one
another person files. `GET /contacts?channel=email&handle=<address>` answers
the holder of the address, or — for an address nobody holds — the person
whose domain covers it, marked `exact: false`, `match: "domain"` and
`match_domain` ([contacts.md](contacts.md), "E-mail domains").

Rows open where the item lives: a thread in place (the conversations
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
dashboard, or from the phone on the channels that echo it — not SMS), *Mark
handled*, *Mark as read* or archiving the chat settles it. Undoing a *Mark as
read* reopens the item only where that mark settled it.
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
messenger arrival pushes as before the model, while the list, the digest and
the sweep keep working. It does not apply to mail, which pushed nothing
before the model: a mail pushes only on the model's word (a VIP, a prior or
a permit, an instruction's verdict or its failure rule), and the rail's
answer and the chat's status say it rang only where it did.

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

**Focus is optional, per instance.** The modes suit a personal instance; on
a shared one, or one with a single remit, a mood to follow makes no sense.
`focus.json` `enabled` (on unless set to `false`) is the switch: the
*Focus modes* card under Settings › This instance, `POST /attention/modes
{enabled}`, or `scripts/attention-week.py off` / `on`. Off, one mode is in
force at every moment (`attention.FOCUS_OFF_MODE`: every sphere admitted,
anything time-sensitive rings, active items wait for a digest, passive ones are listed); the schedule
changes nothing, a hand-set mode is refused (409) and a stored one released,
the digests go out at the document's `digest_times` every day, and the
home's header shows no mode and opens no menu, the sheet no Focus rules.
Turning it off or on is a breakpoint like a mode change. The modes, the week,
the holidays and the permits stay as they were, for when it is turned on
again; `GET /attention` says which with `focus_enabled` (and `mode.enabled`).

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

**Each thread's summary is also in the life store**
(`scripts/thread_index.py`): one N-Triples file per thread,
`chambers/_generated/threads/<id>.nt`, subject `urn:retinue:thread:<id>` (the
attention emit's subject too) — `kb:Thread` with
`dcterms:created`/`dcterms:modified`, `kb:kind`, `kb:initiator`, the
`kb:archived`/`kb:muted`/`kb:starred`/`kb:unread` flags, `kb:messageCount`,
and `kb:project` / `kb:companionOf` (the chat's `urn:retinue:chat:<id>`) where
linked. Never the title — a thread opened without one is titled with the
start of its first message, and an agent's title is often a mail's subject —
nor a message text, a preview, an attachment or a shell action: the store is
readable by every agent, and a thread's words stay in its file.
`_save_conv` rewrites the summary when it changed; a delete removes the file,
so the thread's named graph — everything the store knew of it — leaves with
it (the tombstone stays the gateway's own); at startup the gateway writes any
missing summary and removes any whose thread is gone. The thread files remain
the truth.

The full page's **Archived** tab is what this is for. It grew with every
archived thread, and listing it parsed every thread file on every four-second
poll. Now it asks `GET /conversations?archived=1&limit=50`: the order (starred
first, then newest) is a SPARQL `ORDER BY … LIMIT … OFFSET` over the summaries,
and only that page's files are read to build its rows — so a thread the store
has not caught up with yet (deleted or restored seconds ago) is dropped there
without costing the page a slot (it reads on, and `next_offset` is where it
stopped), and one archived seconds ago joins on a later poll. "Load more" at the end
widens the window by another page. The tab polls every 30 s rather than every
4 s; a change made on it refreshes at once. When the store cannot answer, or
knows no archived thread at all (a fresh deployment's summaries still being
indexed), the page is built from the files as before (`source: "files"`).

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
Plumbing routes (`retinue-claude`, wildcards) stay hidden. A **family pin**
— `ANTHROPIC_DEFAULT_<FAMILY>_MODEL`, the variable Claude Code resolves the bare
`opus`/`sonnet`/`haiku`/`fable` aliases through — also retargets that family's
picker entry to the pinned concrete id (label kept), so one setting moves both
the agents and the dashboard to a new release. Where the picker had several
routes of that family, they become this one entry under the first one's
label, since they would all name the same model. A thread that chose any
other id of the family follows the pin. A pin the proxy does not route is
ignored and logged once, and a family with a `retinue_picker: false` route
stays out of the picker despite its pin. The list
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
subagent's model shows only in the turn's `modelUsage`. A resumed session's
envelope sums usage over all its turns, so the gateway stores each turn's
totals with the session entry and labels and prices the next bubble from the
difference — otherwise a model switch mid-thread would be credited to the
earlier model, and every cost after the first would be a running total.

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

A file's name and type are what it was given where it came from — for a mail
attachment, what the sender declared — and they are written into the
download's headers and into the note that hands the file to Ara's turn. So
the stored name is a base name without control characters (`attachment` when
nothing is left) and the stored type is one media type, read strictly and
written back from what was read, or `application/octet-stream`. Both are read
again in the same way where they are used, so metadata stored before this
keeps to it too: a name or a type from outside never becomes a header line of
its own, and a type that does not read as one media type is a download. The
note to Ara's turn names the type by its `type/subtype` alone; the parameters
kept with it go only into the download's `Content-Type`.

A file row has two links: its name opens the file to view — in the
dashboard's file viewer (below) or, for a PDF a frame would not show whole,
as the bare file; either way the file is fetched with `?inline=1`, answered
with `Content-Disposition: inline` for `_INLINE_SAFE_TYPES` — and ↓ saves a
copy (`attachment`); a type the gateway does not serve inline gets the save
link alone. Both attachment URLs end in the file's own name —
`…/attachments/<att-id>/<filename>`, which is also the viewer's `src` — a
segment the route ignores (the header always carries the stored name). It is there for clients that name a
saved file after the URL rather than the header: Android's
`URLUtil.guessFileName` takes a name only from a bare
`attachment; filename="…"` — neither `inline` nor the trailing `filename*`
(RFC 6266, for non-ASCII names) gets through — and the shell, which named its
downloads with it until it read the header itself, saved `<att-id>.pdf`.
Inside the shell the two links stay distinct even though its WebView renders
no PDF: see "Files" under "Inside the Android shell".

The view link opens the dashboard's **file viewer**,
`/viewer.html?src=<attachment path>&name=…&type=…`
(`components/viewer-page.js`): the file in a frame under the navigation row,
with its name and ↓ Save above it. The browser's own PDF or image view carries
no navigation, so in an installed PWA — no browser chrome — a file opened bare
had no way back to the thread but a gesture or a keyboard shortcut. The
viewer's ‹ goes back through history when the dashboard opened it (the
Navigation API's `canGoBack`, else the history length), and home when it was
opened cold. It shows nothing but a thread attachment exactly as a file row
links it — `src` is checked as the browser will request it, so no `..` or
other spelling of a path step leads to another route of the origin — and only
the inline types; its `frame-src 'self'` keeps a link inside a PDF from
loading another site into the frame. The service worker serves the page from
its cache whatever the query, so it opens offline too, ‹ and all.

Images and text open in the viewer everywhere. A PDF does only where a frame
shows the whole document (`framesWholePdf` in `components/base.js`);
elsewhere its row keeps the bare `?inline=1` link and the platform's own way
back:

- in the Android shell, the phone's viewer app (see "Files" under "Inside the
  Android shell");
- in a browser with no PDF viewer of its own (`navigator.pdfViewerEnabled
  === false`), and on Android, where no browser draws a PDF in a frame even
  when it reports a viewer (Chrome's `AndroidHandlePdfInIframe`, on by default
  on Android laptops, shows an "Open" placeholder instead): the browser's own
  PDF view or a download;
- on iOS and iPadOS, WebKit's full PDF view: a frame there shows only a
  picture of the first page (WebKit's `UseImageDocumentForSubframePDF`),
  although `pdfViewerEnabled` is true. In a Safari tab that view has the
  browser's back button; in an app installed to the home screen it still has
  none — drawing the PDF in the page itself (pdf.js) would be the way to give
  it one.

Images the gateway serves inline (`_INLINE_SAFE_TYPES`: PNG, JPEG, GIF, WebP,
AVIF — never SVG, which is script in the dashboard's origin) also **preview in
the bubble**: a lazy-loaded `<img>` at its true aspect ratio, capped in width
and height, linking to the full view (the file viewer above), with the name row and the
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

**Media in a messenger chat** is another store under the same rule. A file a
contact sends stays with the gateway that received it, together with the
content type the sender's client declared, and the chat page loads it through
the gateway's authenticated proxy, `GET /chats/media/<slug>/<media-id>`
(docs/messenger-chats.md). That type comes from outside, as the bytes do, so
the proxy reads it strictly and writes its own headers from it
(`_chat_media_headers`):

- **In place**: the images of `_INLINE_SAFE_TYPES`, which a bubble shows
  through `<img>`; `audio/*` and `video/*`, which the chat's players load
  (an XML type, `…+xml`, excepted); and a PDF or plain text, which a file row
  opens in the browser's own viewer.
- **As a download** (`Content-Disposition: attachment` and the letterbox
  share's sandboxing `Content-Security-Policy`): everything else, so a file
  from outside never becomes a page or a script of the dashboard. An image of
  another type (SVG, TIFF, HEIC) still shows in the bubble, since `<img>` runs
  nothing in it, and is saved when opened by itself; an HTML or XML file, a
  script, an office document or an archive is a file row that saves it. Such
  a file keeps its stated type, because the browser and the Android shell
  take the saved file's extension, and the app that opens it, from the type —
  except a script type, which goes out as `application/octet-stream` (see
  "The Content-Security-Policy").

A stated type that does not read as exactly one media type counts as
`application/octet-stream`. The type sent is written from what was read (lower
case, with its `charset` or `codecs` parameter and no other), never relayed as
it came, and every answer, a relayed error included, carries
`X-Content-Type-Options: nosniff`. The proxy does not know the name the sender
gave the file, so a download is named after its URL and type
(`<media-id>.docx`). The gateways' store keeps the stated type as one line of
printable ASCII (`inbound_store.clean_content_type`), so it is exactly one
header of their own `GET /media/<id>`, whatever was declared.

An attachment's URL is the proxy's or a web link, never anything else. The
references a chat's messages carry are text from outside as well: the
gateways' ledger records and the notify rail (`POST /internal/chats/inbound`)
keep what their callers wrote. The gateway makes a media reference
(`urn:retinue:media:<channel>:<id>`, or the older
`http://<service>:<port>/media/<id>`) into the proxy's URL, passes any other
reference on only when it is an absolute `http(s)` URL with a host
(`news_store.web_link`, the rule news links follow), and leaves the
attachment without a URL otherwise (`_shape_chat_attachments`). The chat page
holds to the same rule on its own: it renders a link, an image, a player or
the lightbox only for a URL of the proxy (`/chats/media/<slug>/<media-id>`),
a link for the proxy's URL or a web link, and the file's name as plain text
for anything else, so a reference from outside never becomes script or a
link of another scheme. A web link is only ever a link, whatever the
attachment's type: an image, a voice note or a video there is a file row to
open, never loaded into the page (the origin's Content-Security-Policy allows
images and media from this origin only). Two further kinds of URL are
rendered, neither of them ever as a link: the bubble of a send on its way
previews the user's staged images from object URLs the page created, and a
`data:image/…`, `data:audio/…` or `data:video/…` URL is taken for the
`<img>`, `<audio>` or `<video>` of its own kind, and for an image's
lightbox, when the attachment's type is of that kind as well. The gateway
passes no `data:` URL on, and the policy allows none on the chat page; only
the reference documents under `data/chats/`, a test corpus, use them.

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

## Chats: unread, read, archived

The Chats page (`chats.html`) sorts every messenger chat into three groups,
**Unread · Read · Archived**, and the gateway says which: every chat in
`GET /chats` carries `list_group`, `unread` and `inbound_seq` (the count of
arrivals the summary includes), and the payload `counts` — chats per group,
for the tabs. (`group` on a chat is the boolean "a group chat", as before.)

- A chat is **unread** while its latest inbound message has neither been
  replied to nor marked read, and **read** once it has — by a reply from the
  dashboard, or from the user's own phone on the channels that echo it
  (Signal, WhatsApp, Telegram; not SMS, where a chat answered on the phone
  stays unread until it is marked), or by **Mark as read**: the button under
  the latest inbound message on the chat page, and a swipe right on the Chats
  page.
- **Opening a chat does not mark it read.** The pages post no read on open,
  on scroll or on a poll; `POST /chats/<id>/read` is the user's explicit word
  and nothing calls it for them.
- **A mark is about what was shown.** Both pages post `{seen}`, the
  `inbound_seq` of the summary they rendered. A message that has landed
  since is one the user has not seen: the gateway marks nothing and answers
  409, and the page shows the message instead (the list is fetched again,
  the chat page polls and says a new message came in).
- A new inbound message moves a read chat back to *Unread* and brings an
  archived one back unless it is muted — by the order it landed in, so also
  when it is stamped before the mark (a delayed delivery, a sender's slow
  clock). A message is one arrival however often its channel delivers it,
  and one a fast clock stamped a little ahead of the mark that read it stays
  read. An agent's own outbound message changes nothing: it is no reply.
- On the page's rows a **swipe right marks as read** (on *Unread*), a
  **partial swipe left reveals the tray** and a **full swipe left (or a
  fling) archives** at once (on *Unread* and *Read*); on the *Archived* tab a
  swipe right restores and a swipe left reveals the tray. The tray is the
  same on every tab and every channel — Star, on *Unread* Mark read, Archive
  and Mute (on *Archived* Restore and Mute or Unmute), Spam and Delete — with
  one exception: **Spam** is for mail threads only (a messenger has no junk
  folder; mute the chat instead). Right-click and the context-menu key open
  the tray too, so a keyboard reaches what a swipe does.
- **Delete** takes two taps (the armed button says what the second does:
  "Delete for good?", "Move mails to Trash?" on mail) and has no Undo. Every
  other action that takes a chat out of view offers **Undo for 15 s**; Spam
  is deferred — nothing is sent until the window ends, and a page hidden or
  left sends it at once (`keepalive`). A failed action brings the row back
  with the server's own reason.
- **Multi-select**: a long press starts it (a tap then toggles a row, Space
  on a focused row, Shift-click a range on a wide screen); a bar offers the
  tab's actions for every selected chat — Spam only when all are mail — with
  one request per chat, one Undo for the batch, failures counted and each
  failed row saying why, and a bulk Delete in two taps. Esc, Cancel, the
  back gesture or a tab switch end it.
- **Spam** (`POST /chats/<id>/spam`, mail only) moves the thread's inbound
  mails to the mailbox's Junk folder (`MAIL_JUNK_FOLDER`), and only then mutes
  and archives the chat. Restore or Unmute bring it back like any muted
  chat; the mails stay in Junk — the mailbox follows the chat, never back
  (docs/mail-chats.md).
- The badge on a row is the number of messages that have arrived since the
  user last dealt with the chat; the *Unread* tab carries the number of
  unread chats.
- *Mark as read* settles the chat's attention item as *Mark handled* does and
  offers **Undo** for a few seconds. The undo
  (`POST /chats/<id>/read {restore, marked}`) puts the read mark back only if
  nothing has moved it since — a reply sent in between stands — and reopens
  the item only if that mark is what settled it.
- A mark, a reply and an archive are passed on to the chat's gateway, where a
  channel with a mailbox behind it follows (the thread's mails seen, or moved
  out of the inbox); the undo and an un-archive are not. See "Writing mail",
  *The mailbox follows the chat*.
- **Both endpoints are same-origin only**, like a send: `POST
  /chats/<id>/read` (the mark and its undo) and `POST /chats/<id>/flags`
  answer `403` to a request another site's page made the browser send
  (`Sec-Fetch-Site` other than `same-origin`/`none`). They are the user's
  word about their chat, and on a mail chat they reach the real mailbox —
  the dashboard's credential is ambient, and a `text/plain` form whose body
  is valid JSON needs no preflight. The dashboard's own fetches are
  same-origin; a caller that sends no such header (a script in the
  container) passes the read mark, as on the other guarded routes. The
  flags, like a send, are also accepted only through the reverse proxy:
  `403` from inside the container where `EDGE_PROXY_PEERS` names the
  proxy; without it, only from loopback and from the address the caller
  dialled — a caller coming in from another of the container's addresses,
  or through a sibling that relays, passes (an agent has `POST
  /internal/chats/<id>/flags`; "The gate for mail", "Who is asking").

The derivation, the endpoint's request and answer shapes, and what the store
being away does to the counts are in
[messenger-chats.md](messenger-chats.md), "Unread · Read · Archived".

## Mail threads as chats

A mail thread is a chat like any other — the same list, page, state document
and companion ([mail-chats.md](mail-chats.md) is the design; this is what the
web gateway serves of it). The channel is `email`, the chat key the thread
root's `Message-ID`, the account the mailbox address:
`email:~<account>:<message-id>`. The mirror reads the mailbox, and since the
writing phase the chat writes to it: replies and new mail leave from here
under the e-mail send policy, and the mailbox follows what the user does to
the chat ("Writing mail" below). An arriving mail is gated like a messenger
message ("The gate for mail" below): it pushes nothing by default, a contact
can say otherwise, and only a VIP's or an instructed sender's buys a model
turn. E-mail triage is retired; nothing else looks at mail.

**Routing.** The mail gateway's service is `mail-gateway` and its channel
`email`, which no reading of the slug says, so a registry entry may state its
`channel` and the web gateway reads that before it reads a slug or a label
(`_gateway_in_channel`, `_slug_channel`); an entry without the field is read
as before. The blobs are `urn:retinue:media:email:<id>` and go through the
media proxy like a messenger's (`/chats/media/mail-gateway/<id>`).

**A name that is a subject.** A mail chat's `name` is its thread's subject
(`(no subject)` when there is none; never the key), so who it is with is kept
beside it: the state document's `participants` and `peer`, which the rail
writes. Nothing that names a *person* writes the name — not a sender's name
on the rail, not the contact card, the autolink or a renamed person, not the
name backfill (which asks the mail gateway's `GET /recent-chats` for what a
state lacks). Everything about a person works on the peer's address instead:
the contact card and the autolink (`_chat_handle` answers
`("email", <peer address>)` for a one-to-one thread and nothing for a
group), and the attention item, whose sender — the key the profile's priors,
spheres and permits are filed under — is the linked contact, else the person
the address book gives the peer's address to — its holder, else whoever's
e-mail domain covers it most closely — else the address. Never the
subject, and never the display name a mail carried: anyone can type the name
of someone the user gave a permit, so that name is only the item's title.

A domain says who a mail is *from*, not whose address it is, so the two are
kept apart everywhere but in the attention item. A party the gateway
resolves (`_mail_party`) carries `name` and `contact` for the **holder** of
the address only, and, for an address nobody holds, `by_domain`:
`{contact, name, domain}` of the person whose domain covers it. The item is
filed under that person (`attention_store.mail_peer_person`) — an
organisation's prior, permits and sphere apply to all its mail, and such a
sender is not screened — while the thread gets no contact card from a domain
(the autolink asks for the exact holder) and the list goes on showing the
name the mail carried.

**A card is about one address.** A thread's people change with every mail,
and its card was filed for the peer it had while it was one-to-one. So the
card speaks for the thread only while that is still who it is with
(`attention_store.chat_card`): not for a **group** — a thread a third address
has joined, which takes nothing but a `Message-ID` to cite — and not where
the address book gives the thread's peer to someone else or to nobody. Such
a thread's entry in `GET /chats` has `contact: null`, and its attention item
takes no sender, sphere or tags from the card: the newcomer is judged as
whoever their own address makes them. The card stays in the state document
(nothing is rewritten on a mail's say-so), and applies again once the thread
is one-to-one with its person. A write follows what is shown: the contact
card's save (`POST /chats/<id>/contact`) acts on the card that speaks for the
thread. A group is refused, as any group chat is; a thread whose peer is no
longer the card's person files that peer's address — under the person who
holds it, else as a new one — and neither rewrites the person on the hidden
card nor moves what the profile learned under their name.

**A mail pushes nothing by default; a contact can say otherwise.** A mail
that just arrived goes through the attention model like any arrival, and the
model is what keeps a mailbox off the phone: an unjudged mail is passive —
listed, never pushed, in no digest — whether the thread is one-to-one or not
(`attention_store.CHANNEL_DEFAULT_IMPORTANCE`). What raises it is the person
it is from: their importance prior (active: held for the digest), a permit
for the mode in force (rings), the VIP flag (rings in every mode), or the
verdict of the turn their instruction buys. "The gate for mail" below has
the flow.

**The home list.** A mail chat is an attention item **while it holds unread
mail the user has not settled** — one rule, whether or not the chat has an
attention block (`attention_store.chat_wants_attention`). Not every mail is
judged on arrival — one of a held group is not, nor is history the mirror is
catching up on, nor an instructed sender's while its turn works — so it is
the unread mail that lists the thread, not a block: unread mail lists it; a
reply or *Mark as read* (nothing unread any more), an archive, or *Mark
handled* settles it; a mail that is gone from the mailbox is not unread, so
the thread leaves by itself; and the next mail brings a settled thread back
— the model opens its block for a mail it judges, and the rail reopens it
for every other mail it counts, listed and never pushed
(`_attention_relist_chat`) — once per mail however often the event is
delivered. A thread somebody put on the list stays until the user settles
it, read or not: one they put back by hand (`pulled`), and one an agent
declared attention on and left open — `attention-set.py chat:<id> …` with a
deadline, an actor, an importance, or just `--reopen` — which the block
records as `declared`. That one is an item like any other until then: its
deadline is swept, and *Later* holds it for the digest instead of taking it
off the list.

**The list.** A mail chat's entry in `GET /chats` carries, beyond every
chat's, `participants` (`[{address, name, contact}]`, resolved through the
address book — the name of the person who holds the address, else the name
the mail carried, else the address, with `by_domain` beside it where a
person's domain covers an address nobody holds; at most six,
`participants_more` counting the rest), `peer`, and in
`last` the sender's name on either direction and `removed`: a head mail that
is gone from the mailbox previews as removed, not as an empty line. The
subject, the sender's name and the removal come with the head message's
record, which the list reads whole anyway — no pattern is added to its
queries. `last.text` is a **preview** on every channel: the first
`CHAT_PREVIEW_CHARS` (300) characters, ending in an ellipsis where it was
cut. The list is every chat in one answer and a mail's text can be its whole
body; the message itself is served by `GET /chats/<id>/messages`.

**A tombstone is not unread.** A mail that is gone from the mailbox keeps
its record and its place in the thread, and counts for nothing: the unread
query leaves out a record that carries `kb:removedAt` (one
`FILTER NOT EXISTS` in `_CHATS_UNREAD_SPARQL`, no further query), and the
chat state's own count of arrivals forgets it (`inbound_gone`). So a chat
whose unread mail was deleted in a client leaves *Unread* and its badge
counts live mail only.

**The messages.** A mail's Message is its **trimmed** text (`kb:text`) and,
beyond every message's: `sender` and `sender_name` on either direction,
`subject`, `recipients`, `cc`, `trim` (`deterministic` · `wanting` · `model`),
`removed` / `removed_at`, and `original` — the address of the full mail,
present exactly when there is one to show. The attachments are the mail's
files, an inline (`cid:`) picture among them; the mail's own parts — its
plain body, its HTML, its header block — are blobs of the same store and are
never listed as attachments. A tombstone (a mail deleted or moved away in a
client) keeps its place — who, when, which mail — and shows no text, no
attachment and no original. A mail thread's page is read like the list: one
lean query for the page's message IRIs, then their records as
`(message, predicate, object)` rows, rather than one more `OPTIONAL` per
field in the messenger query.

**The original.** `GET <original>` answers
`{id, headers: {from, to, cc, date, subject}, header_block, text, html_url, attachments}`:
the headline headers from the record, the whole header block and the whole
plain text read from the gateway that keeps the parts, and `html_url`
(`<original>.html`) when the mail has an HTML part. Both answer only for a
mail of the chat the path names — 404 for any other, and for a tombstone —
and sit behind the dashboard's sign-in like the media proxy. The message
segment of the path is a token the gateway mints; the client follows the
address it was given and never builds one. The HTML is served as a sandboxed
document of its own: see "The frame on the chat page" under "The
Content-Security-Policy". Each text part is read up to
`MAIL_ORIGINAL_MAX_BYTES` (4 MiB).

**The model trim.** A mail whose `trim` is `wanting` — the deterministic
trim left it HTML-only, still long, with a quote marker or a rule held back
for want of evidence (docs/mail-chats.md, "Trimmed bubbles") — gets one turn
on the router tier that picks the lines which are the author's own message.
The chat page asks for it once the bubble has been in view:
`POST /chats/<id>/messages/<token>/trim`, `<token>` being the segment of the
message's `original`, resolved as the Original resolves it. Same-origin, like
the user's other acts on a chat (it spends a turn of the day's budget), and,
like a chat's flags and send, only through the reverse proxy: `403` from
inside the container where `EDGE_PROXY_PEERS` names the proxy — without it
only from loopback and from the address the caller dialled. It answers

- `200 {state: "model" | "deterministic", text}` — settled: from the record,
  or just now;
- `202 {state: "queued" | "running"}` — a turn is queued or under way; asking
  again answers the same and never queues twice;
- `200 {state: "wanting", retry_after}` — nothing was started: the feature is
  off (`MAIL_MODEL_TRIM=0`), the day's budget is spent (until the local
  midnight of the attention schedule's zone), the queue is full, or the mail
  or the whole feature is backing off; the page does not ask again for that
  mail before `retry_after` seconds;
- `410` for a mail that is gone — from the mailbox (a tombstone), or with its
  chat (erased with a deleted chat); `404 {error: "no such mail in this
  chat"}` for a token that names no mail of this chat (and a plain `404` for
  a chat that is no mail thread); `502` when the store cannot say what the
  mail is. The token is resolved — chat, account, erasure, tombstone —
  before anything is answered, also from what this process settled lately.
  Each of these is about one mail: the page ends that mail and keeps
  watching the others; only `405`, `501` or a `404` that is not this
  endpoint's (the route fallback's `{error: "not found"}`, or no JSON) tells
  it the server has no such endpoint.

Nothing else starts a turn: not ingest, not the backfill, not a GET of the
messages, not an agent reading the thread (the companion and the arrival turn
read what is on the record), and not an agent that calls the endpoint from
inside the container where `EDGE_PROXY_PEERS` names the reverse proxy — the
day's budget bounds whatever still gets through. `GET /chats/<id>/messages`
keeps carrying each mail's `trim`; for the seconds until the store has
indexed a record the trim rewrote, the poll and the chat list's preview show
the settled text this process holds, so a bubble that took it does not fall
back — and the mail's
arrival entry in the live overlay, which still carries the text from before,
is forgotten when a model trim settles. What is held names the text the trim
was asked about: a record the store shows with neither that text nor the
settled one was written again by its gateway since (in its own place: another
copy read, a placeholder read at last), and is shown — and asked about — as
the reading it is. A mail the arrival turn is offered only after it was
viewed (the rail was down, a turn failed) carries `trim: "model"` on the
rail, and the turn's quote of it is marked as the companion's note marks it.

*The worker.* One thread runs one turn at a time from a queue of
`MAIL_MODEL_TRIM_QUEUE` (30) mails, oldest first. A turn is counted against
`MAIL_MODEL_TRIM_DAILY_MAX` (200) before it is spawned, in the model trim's
state file beside the conversations (`mail-trim-state.json`, which replaces
`mail-trim-budget.json` and reads its count until it is first written), so a
restart does not hand out the day a second time; a spawn that never reached
the model (below) is given back — the budget counts turns that ran. What is
queued is spoken for: a page asking while the rest of the day is spoken for
is told to ask again in two minutes, and only one asking once it is spent is
told to wait for the local midnight. For each mail the worker asks the chat's
own mail gateway — the account the chat id names, as a send does — for the
mail's source (`POST /messages/trim-source`: the deterministic trim's own
lines, uncapped, with their digest), runs the turn, and hands the judgement
back (`POST /messages/trim-result` with the digest). The gateway validates
the ranges, assembles the bubble from its own copy of the lines and writes the
record (`kb:trim "model"`, or `"deterministic"` with the text unchanged); it
tells the rail nothing — open pages see the change through their poll, and
this process has the new text in the answer. The record's original parts are
never touched; `kb:text` is replaced by design. A `409` whose `state` is still
`wanting` (the source is not what the turn was shown any more) settles
nothing here and counts against nothing but that mail, which waits
`MAIL_MODEL_TRIM_RETRY_SECONDS` before it is asked again; so does a mail the
gateway no longer has (`404`/`410`). A `409` whose `state` is `model` or
`deterministic` is the mail settled meanwhile, and is taken as a `200` is
(below). A placeholder — a mail the gateway could not read yet — answers
`state: "unread"` (from `trim-source`, or in a `409` when the record became
one meanwhile): nothing to choose from and nothing settled, so it is taken
as a stale source is, and the record written in its place once the mail
reads is asked about as the mail's first reading. A gateway that could not say what the
mail is (away, or a part it could not read just now) costs nothing and that
mail waits five minutes.

*A paid judgement is never thrown away.* When the gateway cannot take a
judgement just now — busy (`503`: a pass holds its lock), another 5xx, the hop
timed out or the gateway away — this process holds it (the digest and the
ranges, in memory) and offers it again, first after five seconds and then
twice as long each time up to two minutes, with no new turn and no new
charge, until it is taken, refused (`409`, `404`, `410`) or about fifteen
minutes have passed; meanwhile the mail answers `202 queued`. Offering
it again is safe: the gateway rebuilds the source and compares the digest, so
a judgement about lines that are no longer the mail's is refused, never
applied. A `409` whose `state` says the mail is settled — by this judgement,
when an earlier offer was written and its answer lost on the way — is taken
as a `200` is: the chat list is rebuilt without the arrival's overlay entry
and the mail counts no attempts any more.

*Only a judgement settles a mail — at most `MAIL_MODEL_TRIM_ATTEMPTS` turns
without one.* A judgement is an answer: the ranges (`keep`, which the gateway
may still refuse — then the mail is `deterministic`), an answer that is no
strict JSON, the empty answer included (`failed: "unparseable"`), and a mail
too long to hand over within `PROMPT_MAX_BYTES` (`failed: "too_long"`, without
a turn; the gateway settles one beyond its own bounds itself). **No
judgement** — the turn timed out (`MAIL_MODEL_TRIM_TIMEOUT`, 60 s), exited
non-zero, printed no envelope, or reported an error in it (`is_error`, a
subtype other than `success`) — sends nothing and leaves the record `wanting`.
The mail can steer its turn there (an answer spun out past the timeout), so
it costs the mail an attempt: it waits `MAIL_MODEL_TRIM_RETRY_SECONDS` (3600)
after the first and four times as long after the second (a day after any
further one, where `MAIL_MODEL_TRIM_ATTEMPTS` allows more), and the
`MAIL_MODEL_TRIM_ATTEMPTS`-th (3: with the default, the third turn, after an
hour's and a four hours' wait) makes this process send
`failed: "gave_up"` and the mail is settled as the rules trimmed it — without
another turn, and also by a process that only finds the count in the state
file after a restart. The count is per mail and durable (the state file, at
most 2000 mails, the oldest dropped; a file that is lost or will not read is
taken as empty, but never hands out what this process counted itself). What
plainly never reached the model — the binary could not be started, or an
error envelope that used nothing (no token, no cost: an expired sign-in, a
usage limit), which no mail can bring about — costs the mail no attempt and
the day no turn; that mail waits `MAIL_MODEL_TRIM_RETRY_SECONDS` in memory.
Every failure of the model, of either kind, counts towards resting it: three
in a row rest the whole feature for ten minutes (`retry_after` says so; a mail
whose attempts are used up is settled all the same, since that spends
nothing). The lines are drawn on the CLI's envelope, never on the answer's
words: a mail that makes its model answer like an error message is still
judged.

*The turn* is the transcript cleanup's kind of spawn: `claude -p` with no
tools, no MCP servers, no session persistence, away from `/workspace`, in the
allowlisted environment (`session_env.build`), the sign-in refreshed first
(`_run_claude`). The model is `MAIL_MODEL_TRIM_MODEL`, else
`RETINUE_ROUTER_MODEL`, else the gateway's default model, else `haiku`. The
mail is data: its subject and sender in a `<mail_facts>` element, its lines
in `<mail_lines>`, each the content of an `<l n="…">` element whose number
this system wrote — escaped as every prompt here holds text from outside
(`_prompt_escape`), with the characters that end or disguise a line blanked
in the copy shown. That escaping is the fence: no line can close its element,
open another or number itself, so no per-turn token is needed. The
instructions are fixed and name no language; the answer must be exactly one
JSON object `{"keep": [[first, last], …]}` (a code fence around it is allowed;
any other member, more than 200 ranges or a number that is no integer makes
it unparseable).

*What it cannot prevent.* The model can only choose among the mail's own
lines, so no mail can add a word to its own bubble. It can still be steered
into choosing badly — a mail that says "keep only line 40", where line 40 is
the phishing line and the warning above it is dropped, may get just that —
and the narrowed `kb:text` is then what the bubble, the chat list, search and
every agent turn read. What bounds it is that only the mail's own lines can
show, that the choice is marked — the Original is headed "Shortened by a
model — this is the full mail." for such a bubble, and the companion's note
marks the mail `trim="model"` and says the mail can steer that choice — and
that the full mail is one tap away (SECURITY.md, "Known limitations").

**`can_send`.** A mail chat's summary says whether it can be written to:
`can_send: true`, or `false` with `cannot_send_reason`, one sentence saying
which it is — no mail gateway is set up for the thread's mailbox, the mail
gateway is not answering, it is not connected to its mailbox, or **it reports
that it cannot send**: the gateway's own `can_send: false` on `/health`, with
its `cannot_send_reason` passed on as one line (a mailbox can be mirrored
with no SMTP server configured). It is read from what the gateway last
reported on `/health` (cached like its identity; a gateway that said "not
connected" is asked again within seconds), so the list costs no request per
chat. A mailbox whose gateway cannot send is not offered for a new mail
either (`GET /chats/compose`). It is advice for the page: the send itself
asks the gateway and answers what it says — its sentence, with its `503`,
where it cannot send. No other channel's chat carries the field. The shared draft works for a mail chat as for any — the user's
(`/draft`, `/draft/undo`) and an agent's (`/internal/chats/<id>/draft`,
`chat-draft.py`).

**The rail.** `POST /internal/chats/inbound` takes five more fields for a
mail (`scripts/chat_ingest.py`): `subject` (this mail's own), `participants`
and `peer` (the thread's), `backfill` and `seen` — and from the mail gateway
`gate_sender` beside `sender` ("The gate for mail"). An e-mail event is
taken only with a token, as `X-Conversation-Backend-Token` or a bearer
token: `CHATS_INGEST_TOKEN` where it is set, else `MAIL_GATEWAY_TOKEN`,
compared in constant time; with neither set every e-mail event is refused
(`403`), and so is any rail call a browser marks cross-site
(`Sec-Fetch-Site`). A `GET` of it is answered at once (`405`) — the mail
gateway's look at whether the rail is up — and an event the handler fails
on is answered `500`, never left without an answer. A mail that just arrived is
offered like a messenger message, with its `gate` verdict, `handover` and —
for a VIP's or an instructed sender's — its `files` ("The gate for mail"
below). A **backfilled** mail —
history the mirror is catching up on — is filed and nothing more: no push, no
turn, no attention arrival, no overlay entry, and an archived chat stays
archived. `seen` on it says the mailbox had it marked read, and moves the
chat's read mark up to that mail (forward only) — the one place the
mailbox's `\Seen` is ever honoured; a backfilled mail that was not read
counts as an arrival like any other — once, by its message id, like a live
one: the mirror delivers every event at least once. Two further calls,
authorized as an e-mail event is (the token above, never open), keep the
chats in step with the mailbox. They are a mailbox's operations and are
**refused for any other channel** (`400`): no messenger chat merges or loses
messages, and the rail — open to a messenger event unless
`CHATS_INGEST_TOKEN` is set — must not be a way to fold one correspondent's
chat into another's or to archive one. The gateway retries each on its own,
so either may name a chat that has been folded away since it was queued; it
is then about the chat that one is part of now.

- `POST /internal/chats/merge` `{channel, account, from: [key…], into}` —
  threads a later mail linked are one chat. The absorbed chat's state is
  folded into the survivor's (`ChatStateStore.merge`): the later read mark,
  with what had landed unread in either still unread; starred or muted if
  either was, archived only if both were (or muted); its draft and its
  companion only where the survivor has none — a second companion becomes an
  ordinary conversation that still points at the surviving chat — and its
  attention block where that is the open one. Overlay entries move with it.
  Idempotent; answers `{ok, id, merged}`. The absorbed chat's **id** is
  remembered (`ChatStateStore.note_merged`, until either chat is deleted):
  the store goes on answering with rows under the old key until it has
  reindexed, and the list leaves those out rather than show a stateless
  ghost; and every `/chats/<absorbed id>/…` endpoint — `messages` first of
  all — answers **`410`** with
  `{"error", "id", "merged_into": "<surviving id>", "messages": "<its messages address>"}`
  and writes nothing. Not a redirect: the page has to learn that its chat
  has another id, or its next write goes to the dead one. An id that has a
  state document again (a new mail arrived under it) is a chat again.
- `POST /internal/chats/removed` `{channel, account, chat, message_ids, live}`
  — mails are gone from the mailbox. The overlay lets go of them, they stop
  counting as unread (above; a mail that comes back is announced again and
  counts again), and the list is rebuilt; `live: 0` — no mail of the thread
  left — **archives** the chat, without muting it, and settles its open
  attention item as archiving does. Its tombstones, state and companion
  stay; the next mail brings it back. Answers `{ok, id, archived}`.

**The companion.** A mail thread's note names the thread by its id where it
gives instructions and carries the subject and the participants as data, in
a block beside the mails — a subject is the first thing a stranger writes.
Both are elements of their own, everything in them escaped so nothing a mail
says can close them: `<mail_thread_facts>` holds the subject and the
participants, and `<mail_thread>` the mails, one `<mail>` element each, its
content the mail's text and its attributes `at`, `from` (who a mail sent to
the user says it is from), `by` (who of the user's side sent one — this
system's label, which no mail can write), `attachments`, `removed`, `cut`
and `trim="model"` (also this system's word: a model chose the text from the
mail's own lines, a choice the mail can steer — "The model trim").
It says the texts are the trimmed ones — and, of a mail marked
`trim="model"`, that lines of it may be missing and the full mail is in its
parts — and lists attachments by type under
the rule in "Attachments". Then the draft, as in every chat's note: what the
shared draft holds, that the `secretary` writes the words, and the
`chat-draft.py` command that stages them. Where a messenger chat's note ends
in "you do not send, under any circumstances", a mail thread's says how a
mail leaves it: by the user's press on the draft, or — only when the user
asks for it there — through `scripts/mail-push.py --chat <id> --reply
all|sender --file <path>`, under the mailbox's send policy, the approval link
of a queued send relayed, and no other tool (not `email_client.py send` or
`reply`, not a messenger's push script). Two things a session cannot know
are said outright:

- **Which gateway.** `mail-push.py` posts to the default mailbox's gateway
  (`MAIL_GATEWAY_SEND_URL`), which refuses a thread of another mailbox (`404`,
  "not this gateway's account"). For such a thread the command — the send and
  the `--retract` — carries `--url <that gateway>/send`, taken from the
  gateway registry, never from a mail.
- **Which policy**, with the rule that goes with it (CLAUDE.md, "When to
  send"): `verify` — send when asked, it queues, relay the link; `trust` —
  `--user-approved` only on the permission phrase or a tapped send chip for
  that very text, otherwise it queues; `allow` — the mail goes out at once,
  so send only once the user has seen the exact text and given the phrase or
  tapped a send chip. `EMAIL_SEND_POLICY` is not in a session's environment;
  the note resolves it for the thread's mailbox as the gateway does
  (`email_client.resolve_category`), or takes the gateway's own word where
  its `/health` states `send_policy`. A policy nobody can read is treated as
  `allow`: the strictest rule for the agent.

It says that the quick patterns' words — the last message, the draft, the
composer — mean the thread's mails and its shared draft, and how many sends
of the thread wait for approval, are on the way out, or failed, so a turn
does not send one twice. A send whose outcome is not known — handed to the
mail server, never confirmed — is counted apart from those that failed and
said as that: "it failed" is what gets a mail sent a second time.

`tests/test_chat_mail.py` covers all of the above against real ledger
records.

### The gate for mail

The mail gateway persists a new mail undelivered, gates it, and offers it to
the rail with `handover: true` and
`gate: {forward, vip, instructed, reason}` — `forward` false for a `quieted`
or `ignored` group, `vip` and `instructed` for the sender, by address or by
an e-mail domain ([mail-chats.md](mail-chats.md), "The gate";
[messenger-chats.md](messenger-chats.md), "Inbound flow", is the flow this
adopts). What the web gateway does with the event, by who sent the mail:

| The sender is | Rings | A model turn | The rail answers |
|---|---|---|---|
| nobody in particular | no: passive — listed, in no digest | none | `200 {accepted}` |
| a contact with a prior or a permit | as the attention model decides from them | none | `200 {accepted}` |
| a **VIP** | at once, in every mode | one, in the thread's companion | `202 {accepted, job_url}` |
| **instructed**, not a VIP | when the turn ends, as its verdict decides | one, briefed by the instruction | `202 {accepted, job_url}` |
| VIP and instructed | at once | one, briefed by the instruction | `202 {accepted, job_url}` |
| any of them, in a held group or a muted chat | no | as above — a held group stops no turn | as above |

- **The item is the person's.** The arrival is judged as the sender of that
  very mail — the one the gate took it for (`gate_sender`) — the holder of
  the address, else the person whose domain covers it, else the address
  (above, "A name that is a subject"). The push reads as a mail client's —
  who, then the subject and the text — and names the holder by the address
  book's name; for a sender known by a domain it is the name the mail
  carried with the organisation's beside it. A mail with no gate sender is
  **nobody's**, everywhere its item is shown or judged. One helper decides
  whom a mail chat's item is about from its newest mail's gate sender
  (`_mail_item_peer`), for the arrival the model judges and for the mail
  that only reopens a settled thread (`_attention_relist_chat`: one the
  gate holds, history the mirror catches up on) alike, and its block says
  so (`nobody`), so the item rebuilt for the home list, the sweep or a
  correction is "Unknown sender" with no card, prior or permit — never the
  thread's peer on record, who is whoever last wrote with an address — until
  a mail with a sender is the newest. A gate sender is lower-cased in its
  ASCII letters only, and is nobody's where one of its characters maps to an
  ASCII one, one for one, under NFKC or case folding (KELVIN SIGN, a
  fullwidth letter); an internationalized address's own letters (ß, ı, ü)
  stay.
- **One channel value.** Whether a rail event is a mail is read off the
  chat id it composes, and the event's `channel` must be that id's channel:
  a field such as `email:~<account>`, which would compose a real mail
  thread's id, is refused (`403` without the rail's token, `400` with it),
  so the token check, the chat id and everything after see one channel.
- **The turn** is the messengers' (`_start_chat_arrival_turn`, the verdict
  endpoint, the failure rule), worded for a mail (`_mail_arrival_prompt`),
  with a timeout of its own (`MAIL_INSTRUCTION_TIMEOUT`, below). Everything a mail says of itself — its sender's
  name and address, its subject, its `Message-ID`, its trimmed text, the
  first `MAIL_ARRIVAL_QUOTE_CHARS` (8000) characters of it — stands in one
  quoted block, escaped — at most `MAIL_ARRIVAL_BATCH_BYTES` (48 KiB) for
  all the mails of one turn, header lines included: a batch that would not
  fit is worked in turns. The turn takes the newest mails that fit and is
  told how many older ones wait; those stay on the chat's queue, untouched
  by anything that turn's outcome settles (its archive, its verdict, the
  rings, "told", the turns being over), and get a turn of their own next —
  a turn is for the mails it was shown, and only for those. Which mails those
  are is decided once, by the worker (`_mail_arrival_quotes` over the whole
  batch), and the prompt quotes exactly the quotes it decided: every mail a
  turn takes is quoted in its prompt, and every mail the prompt says gets a
  turn of its own gets one. None of it stands in a
  sentence that tells the turn
  what to do: those name the person by the address book's name alone, and
  the sender's address stands in the one command that needs it (the memory
  recall by `sender:<address>` and `sender:<domain>`) only when it is
  plainly an address — ASCII letters, digits and `._%+-` around one `@`. The
  files the rail handed over are listed by the path they were stored under,
  their bare type and their size ("Attachments"), and the turn is told how
  many the mail carries that it was not handed. A prompt that would still
  be longer than one argument carries (`PROMPT_MAX_BYTES`) sheds data, the
  oldest first and each cut said: a replayed transcript, then the thread's
  oldest mails in the note, one whole `<mail>` at a time — never the
  instruction, the commands or the arrival the turn is for.
- **What a VIP's turn does:** files what the mail changes — a project link
  (a mail is named `mid:<Message-ID>`), a memory, a document for the
  `archivist` — and stages a reply where one is wanted. It sends nothing and
  leaves the thread where it is.
- **What an instructed turn may do**, each only where the instruction asks:
  the same filing; a reply written by the `secretary` and staged in the
  shared draft; a reply *sent* — by `scripts/mail-push.py --chat <id>
  --reply all|sender --file <path>` and nothing else (there is no reply
  token for a mail; `--url` names the thread's own gateway where that is not
  the default one), under the mailbox's send policy: `verify` queues and the
  turn flags the waiting mail; `trust` and `allow` send unseen only where
  the instruction itself carries the permission phrase, and otherwise queue
  (`trust`) or fall back to the draft (`allow`, and a policy nobody can
  read); a calendar event or anything else a send policy gates, under its
  gate; **archiving the thread** with `scripts/chat-flags.py`; and, last,
  the verdict (`scripts/chat-verdict.py`). Nothing deletes or pays.
- **`POST /internal/chats/<id>/flags`** — body `{archived?, muted?,
  starred?}`, token-gated like the internal draft
  (`X-Conversation-Backend-Token`) — is the user's `POST /chats/<id>/flags`
  by another door: one function sets the flags for both
  (`_set_chat_flags`), so muting archives, archiving settles the attention
  item, and the mailbox follows through the same hook and the same durable
  queue of owed calls. `scripts/chat-flags.py --chat <id> --archive |
  --unarchive | --mute | --unmute` is its CLI (`--star` / `--unstar` only on
  the user's word, as for a thread). There is no delete: erasing a chat is
  the user's own act. A merged thread's id answers `410` here as everywhere
  — unless a running turn answers to it (below).
- **Who is asking** is the web gateway's to decide, not the turn's
  cooperation. It hands each arrival turn's session a token of its own
  (`RETINUE_ARRIVAL_TURN`, one of the turn's unguessable job ids), and each
  session of a dashboard thread's turn the user started — by a message
  sent from the dashboard itself (`via_edge`, the rule that decides who may
  type), never an arrival's or a scheduled job's turn, and never one a
  "user" message posted from inside the container started — a proof of that
  (`RETINUE_USER_TURN`, minted per turn and valid while it runs and answers
  the user's own message); both are
  set per spawn by `scripts/session_env.py` and never inherited, so a
  session another one spawns carries neither. `scripts/chat-flags.py` sends
  them as `X-Arrival-Turn` and `X-User-Turn`. In this order:
  1. a call with a turn's token is that turn's (`_turn_by_token`) while it
     runs on the chat named — on it, or on the chat a merge folded it into;
     a token that names no turn running there (a turn asking about another
     chat, one whose turn is over) is refused, `403`, and changes nothing;
  2. a call with a valid user-turn proof has the user's meaning, the mute
     included, even while an arrival turn runs on the chat; a stale or
     invented proof is no proof;
  3. a call with neither, on a mail thread while a mail's turn runs on it,
     is taken as that turn's (below), and the answer's `reason` begins `a
     turn is running on this chat; the call was taken as the turn's` and
     ends `ask again once it is over` — for a scheduled job or another
     session, which `chat-flags.py` tells so;
  4. any other call — no turn running on the chat, a messenger chat — has
     the user's meaning, as before the gate. A messenger chat's turns are as
     they were: a call with the turn's token is the turn's, one without it
     the user's word.

  The user's own `POST /chats/<id>/flags`, which needs no token, is no way
  round this order: like a chat's delete and send it is accepted only
  through the reverse proxy (`_request_from_edge`, `403` otherwise, with
  `chat-flags.py` named as the agent's door) — where `EDGE_PROXY_PEERS`
  names the reverse proxy, which refuses every peer it does not name, a
  caller inside the container included. Without that setting
  (`_classify_request_origin`) the gateway refuses only a loopback peer
  and a peer that is the very address the caller dialled, and takes any
  other for the dashboard: a caller inside the container that comes in
  from another of the container's addresses (the documented deployment
  puts it on two networks), or through a sibling that relays (the
  egress-audit proxy), has the user's meaning — a mail's turn too. Where
  the proxy itself arrives on loopback (host networking,
  `EDGE_PROXY_PEERS` including it) the gateway cannot tell the dashboard
  from a caller inside the container at all. A deployment sets
  `EDGE_PROXY_PEERS` (`docker-compose.override.example.yml`).

  This holds for the turn's own chat while the turn runs, and no further.
  It is a guard against a turn that was talked into something, not a
  sandbox: an arrival turn's session holds the backend token every agent
  session holds, so without its token it can set another chat's flags with
  the user's meaning, and a process it leaves behind can act on its own
  chat once the turn has ended. And while a dashboard turn the user started
  runs, its proof sits in that turn's process environment, which another
  session of the same uid can read — sessions are not sandboxed from one
  another. What keeps a turn to its brief there is the brief, as on the
  messengers.
- **A turn can only archive its own mails.** A mute keeps every later
  message of the chat out of view and unrung, a VIP's included — the user's
  to decide, never a turn's that one message started — and so are taking a
  mute off, bringing an archived chat back, and the star. So `{muted: true}`
  from a turn is taken as the archive it also is (the same archive as
  `{archived: true}`, below); `{muted: false}`, `{archived: false}` and
  `starred` change nothing. The answer says what was refused and then what
  was done, worded from the outcome: `{"muted": false, "reason": "a turn
  cannot mute; archived instead"}` where the archive was made, `"a turn
  cannot mute; newer mail arrived"` (and `"archived": false`) where it was
  not, `"a turn cannot unmute or unarchive"` for a turn asked to undo the
  user's mute — and `chat-flags.py` prints what the answer says, no more.
  The chat's next message brings an archived chat back, as after any
  archive.
- **A turn's archive reaches the mail it was given.** In a mail thread a
  turn's archive is made only if no mail has arrived in the chat since the
  turn's own (`ChatStateStore.archive_through`, one decision under the state
  lock) and none waits on the chat's queue for a turn of its own (an older
  one its batch could not quote, another person's under another brief), and
  the mailbox hook carries exactly the `Message-ID`s the turn worked, named
  at that decision — never a mail the mirror has stored but the rail has not
  counted yet. Where one has arrived — a reply-all that landed while the
  turn filed the invoice — or waits, the chat is **not** archived, that mail
  stays unread on the list and in the inbox, only the mails the turn worked
  leave the inbox, and the answer says so: `{"archived": false, "reason":
  "newer mail arrived"}` (or `"an earlier mail of the thread waits for a
  turn of its own"`). (That hook, if the gateway is away, is not owed for
  later: an archive is owed only while the chat is archived.) What a turn
  asked is remembered on its mails once it completes, made or refused
  (`archive_asked` beside `turns_done` in the chat's state, durable): newer
  mail every piece of which a completed turn worked and asked to archive
  too is no reason to stay (`chat_state.asked_through`). So the chain of
  turns a batch too big to quote whole leaves — the newest mails' turn
  first, refused while the older ones wait, then theirs, refused before by
  "newer mail arrived" — archives the thread with its last turn, and the
  instruction's "then archive the mail" is carried out. A mail that arrived
  since and has had no turn, or whose turn did not ask, still keeps the
  thread on the list; a later turn of a mail that did not ask takes the
  ask off it, and so does the user bringing the thread back while the turn
  runs. An instructed turn's ask is remembered only where the turn is
  trusted with its put-away — it set a verdict, and the timeout had not
  rung for it (`_instructed_settle` decides both) — so a turn that ended
  without a verdict, or whose mails the timeout rang, leaves no ask behind.
  And the user bringing the thread back (`archived: false` on their own
  endpoint, or Ara's call with the user's meaning) forgets every ask made
  until then, in the same write (`set_flags` `forget_asked`): only a turn
  asking after the user's last unarchive can archive the thread over a
  newer mail. That holds for a turn wrapping up as the unarchive lands,
  its ask not yet written, too: the write moves the chat's `asked_epoch`
  on, a turn notes it when it asks, and its ask is written only while that
  note still stands — decided in the write that records the ask
  (`note_turn_done` `epoch`). (A merge that lands in that same moment
  finds no record to carry the note along: the ask is then dropped where
  the merged chat's epoch differs from the one the turn noted — where the
  other half was ever brought back by the user — and kept where neither
  was.) The undo of a
  turn that did not finish forgets nothing.
- **A thread its turn archived rings nothing.** What the turn archived is
  noted as its own. If the turn then ends with a verdict, the ring that
  waited on it is not delivered — whatever importance the verdict gives —
  and the chat's status says `quiet`, "the turn archived the chat", with the
  verdict and its actions. If the turn does not finish (it fails, or sets no
  verdict), or the timeout rings it first, its archive is undone before the
  failure rule rings the mail: a turn that did not finish is not trusted
  with having put a mail away. What the user set meanwhile is theirs: their
  archive or mute during a turn takes the flag off the turn's record,
  stays, and changes nothing about the ring beyond what it does on every
  channel (a muted chat is quiet). (A messenger chat's timeout only rings,
  as it always did: [messenger-chats.md](messenger-chats.md).)
- **The same failure rule, a longer timeout.** A turn that errors, ends
  without a verdict or runs past `MAIL_INSTRUCTION_TIMEOUT` (600 s; the
  messengers' `CHAT_INSTRUCTION_TIMEOUT` stays 60 s) has the mail rung as a
  VIP's. A mail is not a chat message: ten minutes is a ring in time, and a
  turn that files the document a mail carried needs them.
- **A merge while a turn runs.** The mail gateway may fold the thread into
  another while its mail's turn works. Every registry the turns keep by
  chat id — the queue, the running turns, the rings that wait, the handles
  minted per message — moves to the surviving chat (`_rekey_turns`); the
  turn, which calls back with the id its prompt named, has its verdict and
  archive taken for the survivor, and nothing is written under the dead
  id.
- **A mail offered again.** The mail gateway has no triage to fall back on:
  a mail the rail did not accept — the turn could not be started, or failed;
  the answer was lost — stays undelivered and is offered again on a later
  poll. Each mail is counted once, by its `Message-ID`, and whether the user
  has been **told** of it is kept per arrival on the chat's state
  (`untold`): owed in the write that counts it, told once its attention step
  and push have run to the end — or, for an instructed sender's, once the
  ring that waited on its turn went out. A repeated offer of a mail that was
  told un-archives nothing, reopens no settled item and rings nothing; one
  of a mail that never was — the first request died between the count and
  the push, or the ring died with the process, whatever later mail of the
  thread was attended meanwhile — is that first offer, and does what it
  owed (a death right after the push tells twice rather than never). It is
  answered with the handle of the turn that is running or done; the handle
  of a turn that **failed** is handed back for `CHAT_ARRIVAL_RETRY_SECONDS`
  (900) and then gives way to a turn anew, so the work is done once an
  outage is over without a failing turn being run once a poll. Such a turn
  is told the user already knows of the mail (the failure rule rang it), and
  its verdict records; a ring waits on it only where the mail is still
  owed and no read covers it — the case after a restart mid-turn. An owed
  ring whose turn is over (its outcome could not be written) rings at the
  next offer by the failure rule, and where that turn completed its handle
  then answers done: the mail is accepted, and no second turn runs for it.
  A mail gets at most `MAIL_ARRIVAL_TURN_ATTEMPTS` (3) turns: after the last
  one fails it is accepted as it stands — it rang once, by the failure rule
  or on arrival — and the companion holds **one** note saying the turns gave
  up; a failed mail turn posts no apology of its own. Both are kept on the
  chat's state, not only in this process: the count of turns
  (`turn_attempts`) and the mails whose turns are over (`turns_done` — one
  completed, or they gave up), so an offer after a restart — even one in the
  middle of the last attempt — buys the mail no further turn and the
  companion no second note.
- **With `CHAT_ARRIVAL_TURNS` off** a mail is accepted all the same, since
  its gateway has nowhere else to take it, and no turn runs: a VIP's mail
  still rings, an instructed sender's is delivered like anyone's. (A
  messenger gateway is told nothing and forwards to triage, as before.)

`tests/test_chat_mail_gate.py` covers this section.

### Writing mail

[mail-chats.md](mail-chats.md), "Writing", is the contract; this is the web
gateway's side of it. `tests/test_chat_mail_send.py` covers it against a mock
mail gateway that answers as `scripts/mail-gateway.py` does (real-length
`Message-ID`s, `"sent": false` on a 5xx that nothing was sent, a dismissed
keyed entry of unknown outcome kept and answered by id while one that failed
for certain is removed, `repeat`, a failed key tried afresh), and
`tests/test_chat_mail_e2e.py` across the seam with no mock of either side:
the real mail gateway's HTTP handler, over a fake mailbox and SMTP server,
behind the real web gateway.

**A reply** is `POST /chats/<id>/send`, as on every chat, with two more
fields: `reply`, `"all"` or `"sender"` (optional; the gateway's default is
all), and `request_key`, the idempotency key of the press (below). The
gateway is handed `{recipient: <chat key>, reply, message, author: "user",
user_approved: true, request_key}` and builds the mail from its own records
of the thread — this side names no recipient and no subject.

**The recipients a user sees are the recipients the gateway will use.**
`GET /chats/<id>/reply-preview` answers to whom a reply would go right now:

```json
{"all":    {"to": [Recipient], "cc": [Recipient]},
 "sender": {"to": [Recipient], "cc": []},
 "choice": true, "subject": "Re: …"}
Recipient = {"address": "bob@y.org", "name": "Bob" | null,
             "contact": "Bob Meier" | null, "contact_id": "<id>" | null}
```

It is the mail gateway's own answer (`POST <gateway>/reply-preview {chat,
account}` — the function its `/send` addresses a reply with, asked without
sending), passed on; nothing here and nothing in the page computes an
address. `all` and `sender` are the two reply modes and `choice` says whether
they differ. Every recipient has its `address`, spelled as the gateway will
use it; `name` is the display name the thread's mails gave it — text from
outside, one bounded line, to be shown beside the address and never in its
place; `contact` is the name of the person in the address book who holds the
address (`contact_id` their id), null where it is nobody's. An answer that
does not say in full where both modes go is `502`: part of a preview would
show other recipients than the mail gets. `409 {"error"}` where no reply is
possible — the gateway's own sentence for a thread with no mail to answer (a
chat that holds only a queued mail), or the reason no gateway can be asked;
`404` for a chat that is no mail thread. Behind the dashboard's sign-in like
every chat read. A mail's Message carries `reply_to`, the addresses of its
`Reply-To` header (`[]` without one): why a reply may go to somebody the mail
is not from.

**The send press is not the approval here.** On the messengers a send the
policy queues is released by the web gateway in the same request: the press
*is* the approval `verify` exists for. On e-mail it is not
(mail-chats.md, Decisions). The press is passed on as `user_approved`, which
is what `trust` asks for; under `verify` the mail is queued even though the
user typed it, and the web gateway leaves it queued — it never calls
`approve` for a mail. So the endpoint has three answers that are no error:

- **sent** → `200` and the Message, as on every chat: the overlay has it,
  the draft is cleared, the chat is read, its attention item settled. Where
  the mail server took the mail for some recipients and refused others, the
  Message carries `refused`, the addresses it did not reach;
- **queued** → `202 {"pending": Pending}`. The draft is cleared — the words
  are in the queue — and nothing else moves: no reply has been made, so the
  chat is not read and its attention item stands;
- **not known** → `202 {"unconfirmed": true, "request_key", "since",
  "message_id"?, "id", "note"?}` (below).

```json
Pending = {"request_id": "…", "approval_url": "/sends/mail-gateway/<id>",
           "text": "…", "ts": "…", "author": "user" | "agent",
           "to": ["a@x.org"], "cc": [], "subject": "Re: …",
           "message_id": "<the Message-ID the mail will go out under>",
           "request_key": "<the key of the press, where it had one>",
           "status": "pending" | "sending" | "failed",
           "error": "<why it failed>" | null, "outcome_unknown": false,
           "refused": []}
```

**A mail that may have gone out is never reported as not sent.** "Not sent"
puts the words back in front of the user with a button that sends them
again. So the hop to the mail gateway's `/send` (`_mail_send`) waits
`MAIL_SEND_TIMEOUT` (120 s — under `allow`, or `trust` with the press, the
gateway answers only once the SMTP server has taken the mail) and tells its
endings apart by what they prove:

| The hop ended with | Answer to the browser |
|---|---|
| the gateway's `200 sent` / `202 pending_approval` | `200` Message / `202 {pending}` |
| no connection — the request was never written | `502 {error, sent: false, by: "dashboard", request_key}`: nothing was asked |
| the gateway's word on the request itself: `400`, `409`, `413` | that status, `{error, sent: false, by: "gateway", request_key}` with **its sentence** |
| the gateway's `503` that says `"sent": false` (it cannot send, could not queue) | `503 {error, sent: false, by: "gateway", request_key}` with its sentence |
| the gateway took the request and the mail did not go: its `502` (or any other `5xx`) that says `"sent": false` ("send failed: …" — the SMTP server refused the mail, the login failed) | **`424`** `{error, sent: false, by: "gateway", request_key}` with its sentence |
| any other `4xx` — a status the gateway answers before it reads a send (`401`, a route it does not have), or one from whatever stands in front of it | `502 {error, sent: false, by: "dashboard", request_key}`: the request was not made |
| a timeout, or a connection lost, after the request was written | `202 {unconfirmed}` |
| the gateway's `502 {unconfirmed: true}` / `{outcome_unknown: true}` (the SMTP server's answer was never seen), or its `202 sending` for a queued mail that is being sent | `202 {unconfirmed}` with its `message_id` and its sentence as `note` |
| a `5xx` **without** `"sent": false` — an older gateway's "send failed", a proxy's page, a body that is no JSON or was cut short — and any answer nobody planned for (a `500`, a `504`, a body that is no object) | `202 {unconfirmed}` |

A gateway's error answer is read **whole** before any of this is decided
(`_gateway_hop`): its `502 {unconfirmed, outcome_unknown, request_id,
message_id, chat}` carries two `Message-ID`s — a correspondent chooses the
length of one of them — and is longer than any cut made for display. Only a
body that is no JSON at all is cut, and passed on as the error. A body that
ended before its `Content-Length` (the gateway stopped while it wrote its
answer) is no answer: the hop raises, and the send is `unconfirmed` — what
was cut off may have been the very field that said the mail may be out. A
`5xx` is "not sent" only on the gateway's explicit `"sent": false`, because
its `502` is also the answer of a mail handed over and not confirmed: the
two are never told apart by their wording.

**A kept key is dropped only on the mail gateway's own word about that
key** (decision H, [mail-chats.md](mail-chats.md)). Every answer that says
the mail did not go carries `"sent": false` beside its sentence and the
press's `request_key` — this request sent nothing — and says who said so,
`by`:

- `"gateway"` — the mail gateway's own answer about this request, given
  once it had looked up the key: its certain failure (`424`), its
  `400`/`409`/`413` refusals, its `503`s. That is its word on the key too:
  the press is over, and a page drops the key.
- `"dashboard"` — the web gateway answered by itself, the mail gateway not
  having spoken: it was never reached, no gateway serves that mailbox, the
  gateway has not said which mailbox it serves, what was sent does not
  validate, a `403` (not same-origin, not through the proxy), a `410` (a
  merged chat), a status the gateway answers before it reads a send. This
  request was not made; what an earlier request under the same key did is
  as open as it was, and a page that pressed again under a kept key keeps
  it. (Keeping a key is harmless: the mail gateway treats a key it never
  saw, or whose send failed for certain, as a new attempt.)

Every refusal of `POST /chats/<id>/send` for a mail chat and of `POST
/chats/compose` carries `by` — the validation `400`s and the `403`/`409`/
`410`s too; a messenger send's answers are as they were. `424` is the
answer for a mail the gateway took and could not send; `502 {sent: false}`
is "the gateway was never reached" (or did not take the request), `503`
"it cannot send". A `5xx` **without** `"sent": false` proves nothing — a
proxy, a crash, or the web gateway's own `502` for a send the gateway
queued but named no usable request id for (the mail waits in its queue) —
and a page keeps the key for it. Anything whose outcome is open is `202
{unconfirmed}`, never a `4xx` or `5xx`. After a `424`, the chat's copy of
the queue is brought in line at once, so a send a poll had listed as
`sending` while its SMTP session lasted does not stand there after the
answer — for a new mail, in the chat the poll made of it, found by the
press's key (`_settle_failed_new_mail`), which is then forgotten with the
send.

An error answer leaves the draft in the composer. An unconfirmed one clears
it — the words are with the gateway — and marks nothing read: no reply is
known to have been made, and the rail does that when the mail goes out. No
message is made of it either (a mail not known to have gone out is no entry
of the thread). Instead the gateway's queue is asked once for the send — by
the key of the press, or the `Message-ID` the gateway named — and its entry
goes into the chat's pending sends as `sending`, so the chat shows it after
a reload too; `message_id` in the answer is what the page matches its own
bubble to the mail by, `since` the instant from which it may match by the
words where no id is known.

`request_key` is the **idempotency key** of one press of Send: an opaque
token of at most 64 printable characters that the page mints, sent in the
body of `/send` and of `POST /chats/compose` and passed on to the gateway.
The gateway answers a repeat of a key whose mail went out, waits, is being
sent or may have gone out with what became of that send, and sends nothing;
a key whose send failed for certain is a new attempt. So the same press can
be retried safely. A request without one gets a key minted here; the answers
of the hop that are not `sent` or `queued` — "not sent", and `unconfirmed` —
name the key.

**A repeat is not a reply.** An answer the gateway gave from an earlier
entry of the key says so (`"repeat": true`), and the web gateway then only
reconciles: it returns the Message (or the Pending, or `unconfirmed`) and
brings the chat's copy of the queue in line. It advances no read mark,
settles no attention item and tells the mailbox nothing: the rail did all of
that when the mail went out, and what has arrived since has not been
answered by anyone. **A repeat clears the draft it repeats** (decision I):
the shared draft is cleared exactly when it still holds this press's words
(compared trimmed, guarded by the draft's version —
`ChatStateStore.clear_draft(only_text=…)`) and left as it is when it holds
others. Where the answer to the first request was lost, the page put the
words back into the box and autosaved them as the draft; left there, the
next opening, another device or an agent would be handed a mail that went
out, to send a second time. A draft of other words was written since. A gateway built
before it said `repeat` is read by what its answer proves (`_mail_repeated`):
the chat knew that `Message-ID` before this request began (as a mail, or as
a send in its copy of the queue), or the instant the gateway gives for the
mail lies more than `MAIL_REPEAT_SLACK_SECONDS` (2) before it. *Before* is
the point: the rail may announce a direct send, and a page's poll may list
it, while the request that made it still waits for its answer — that is a
reply.

`approval_url` is the path of this dashboard's own approval page, built here
from the gateway's registry slug and the request id. What a gateway states
as its approval URL (absolute, where `SEND_APPROVAL_BASE_URL` is set) is
never passed on, and the chat state keeps a queued send only with a link of
exactly that form (`chat_state.clean_pending`).

**Pending sends in the chat.** `GET /chats/<id>/messages` carries top-level
`pending`: every send of that chat that is not over, whoever made it — the
composer, the companion, an agent with `mail-push.py` — oldest first; `[]`
on every other channel. A chat's entry in `GET /chats` carries the count as
`pending`. **Nothing the user queued or sent leaves the chat without a
word**, so a send is there in one of three states (`status`):

- `pending` — it waits for the user's decision, with its approval link;
- `sending` — allowed on `/sends`, or sent directly and not confirmed yet:
  on its way, the outcome not seen;
- `failed` — it ended without going out for certain. `error` is the
  gateway's reason (it may repeat what a mail server said: one bounded line,
  shown as text) and `outcome_unknown` says the mail may have gone out all
  the same. It keeps its text, so the page can offer it back to the
  composer, and stays until the user dismisses it:
  `POST /chats/<id>/pending/<request id>/dismiss` → `200 {id, dismissed,
  pending}`. Same-origin only; only a send of that very chat (`404`
  otherwise) and only one that failed (`409` for one that waits or is being
  sent); the gateway forgets the entry (`POST
  <gateway>/pending-sends/<id>/dismiss`), and an entry it no longer holds is
  answered as dismissed.

`refused` on an entry are the recipients the mail server would not take. For
a mail that went out all the same they are kept with the chat
(`chat_state.note_refused`, by `Message-ID`) and stand on its Message as
`refused` on every later read: the mail's record lists whom it reached, the
Message also whom it did not. They are learned from the direct send's
answer, from the gateway's entry of a queued send once its mail shows, or
from an outbound rail event that carries `refused`.

The gateway's queue is the truth (`GET <gateway>/pending-sends?chat=<key>`,
which lists the three states); the chat's state keeps a copy for the list
and for when the gateway is away, and the copy follows the queue from three
sides:

- the chat page's poll asks the gateway about its chat and brings the copy in
  line (a gateway that could not answer is left alone for
  `CHAT_PENDING_RETRY_SECONDS`, 15, and the copy stands in);
- the chat list has every mail gateway's whole queue read in the background,
  at most every `CHAT_PENDING_SYNC_SECONDS` (20) — so a send queued past the
  dashboard shows in the list without its chat being opened;
- a decision on `/sends` is noted at once: denied or retracted, the entry
  leaves; **allowed, it stays, as `sending`** — the approval is carried out
  off the approving request, and a mail the SMTP server then refuses must
  not have left its chat a moment earlier. The rail's word that the mail
  went out (an outbound event under the queued send's `Message-ID`) takes
  it out.

An entry the copy holds and the queue no longer lists is asked after once,
by id (`GET <gateway>/pending-sends/<id>`), before it goes: sent, denied,
retracted, dismissed or gone (`404`), it leaves; still being sent, or
failed, it stays as that — so a gateway whose list left it out loses
nothing. A send whose mail is among the
page's messages is never also listed as pending, whatever a queue that lags
says.

**A dismissed entry is over.** The mail gateway removes a send that failed
for certain when it is dismissed — by the user, or by itself when a direct
send failed for certain and its caller has that as the answer — and one a
new attempt under its key replaced: asked by id it answers `404`, which
reads here as *gone*. Only a send of unknown outcome that named its
`request_key` is kept, for as long as the key is remembered (24 h), so that
a repeat is still told the mail may have gone out: not listed, and still
answered by id as `{status: "error", dismissed: true}`. Every reader here
takes either as ended, whatever else the entry says (`_pending_ended`, and
a list that still names a dismissed entry): it leaves the chat's copy, and
a chat that was only that mail is forgotten with it. Read as a failure it
would stand in the chat as a failed bubble the user had already put away,
or for words the composer was just handed back, and return with every
poll.

**An unknown outcome is never worded as "failed".** An entry that ended as
`error` with `outcome_unknown: true` may have gone out, and "it failed" is
what makes a person or an agent send it again. Every reader of an entry
asks (`_send_outcome_unknown`) before it words one: the chat's Pending
(`outcome_unknown`), the chat list (`last.outcome_unknown`), the companion's
note (which counts such sends apart from failed ones), the approval page and
what it polls, and the note into the thread that queued the send (below).

**A new mail that is only queued is listed.** The `Message-ID` that keys a
new thread is minted when the send is accepted, so a queued new mail has its
chat id before any mail exists. Such a chat is in no ledger; its state — the
copy of the queued send — lists it: `last` is that send
(`{direction: "out", text, author, pending: true, status}` — `status` is the
Pending's: `"pending"`, `"sending"` or `"failed"`), `list_group` is
`"read"`, its name is the subject and its participants are who the mail is
to. Allowed, it is a chat of messages. A send of it that failed keeps it
listed (`last.failed: true`, and `last.outcome_unknown` — true where the
mail may have gone out all the same) until the user dismisses it. Denied,
retracted, dismissed or gone from the gateway's queue (a send that failed
for certain, also one a page's poll listed while its SMTP session lasted),
it is no longer listed — **and nothing is kept of it**: the
state document of a chat that was only ever a mail that never went out
(`unsent`, cleared by the first mail of it that exists) is deleted with its
last send, unless a draft or a companion thread is in it
(`_forget_unsent_chat`). No list would show such a chat, so nothing could
delete it, and its subject and recipients would stay for good.

**On `/sends`.** The mail gateway is a registry gateway, so its queue is on
`/sends` with every other: listed by the mail's subject and its recipients,
and the approval card shows *From* (the mailbox), *To*, *Cc*, *Subject*, the
category, the names of attached files and the text — the recipients in full,
since a reply's are built from the thread and nobody typed them. A queue
entry is addressed by the gateway's registry slug whatever it says its own
account is. Only what waits for a decision is listed there: a send that is
being sent or has failed shows in its chat. A decision is in the chat at
once (above), and a mail that was allowed appears as a message. The status
page of a mail the server took for only some recipients says so — a *Not
delivered to* row, and "Sent — but the mail server refused …" in place of
"Sent." (also as `refused` and `note` in the JSON the page polls). The note
a decision leaves in the dashboard thread that queued the send names such a
mail by the addresses it goes to and nothing else — that note is replayed to
Ara's sessions as her own words, and a reply's subject and its recipients'
display names are a stranger's; where recipients were refused it names them
too, and badges the thread.

A send that was allowed and ended with its **outcome unknown** (the entry's
`outcome_unknown`: the mail server was handed the mail and its answer was
never seen) is not shown as a failure anywhere on this path. The status
page draws a question mark in a neutral disc — never the cross, the mark of
a send that did not go — and "Not confirmed — it is not known whether
this went out, so do not send it again unchecked: …" with the gateway's
sentence, where a failure reads "The gateway could not deliver the message:
…". `GET /sends/<gateway>/<id>/status` always states `outcome_unknown`
(`{status, error, outcome_unknown, next?}`), and for such an entry carries
`note`, the whole sentence the page shows in place of its "could not
deliver" line (the status row carries that sentence's lead-in as
`data-note-unknown`, beside the other notes). The
note into the queuing thread says "It was handed over and never confirmed:
it is not known whether it went out. Do not send it again before checking",
never "sending it failed"; the turn context it leaves says the same — "NOT
a failed send … must not be sent again unless the user, having checked,
asks for that" — with the gateway's sentence as one bounded line, and the
thread is badged and woken as for a failure, since the user has not seen
the end of it.

**A new mail.** `GET /chats/compose` answers what the ＋ of the Chats page
can offer: `{"channels": [{"channel": "email", "label": "E-mail",
"accounts": ["me@example.org"]}]}` — a channel for each whose gateway takes a
new conversation (today e-mail), with the mailboxes whose gateway is up; no
such gateway, no entry. `POST /chats/compose`
`{channel: "email", account?, to: [...], cc?: [...], subject, text,
request_key?}` hands the mail to that mailbox's gateway (`account` is needed
only where several are mirrored) under the same policy as a reply, and
answers `200 {id, message}` or `202 {id, pending}` with the new chat's id —
or, where the outcome is not known, `202 {unconfirmed: true, request_key,
id?, message_id?, …}` exactly as a reply does (the chat's id is the
gateway's word, or found in its queue by the press's key; without either the
chat appears when the mail does). It is the
user's own act and gated like a send: same-origin only, and only through the
reverse proxy. Recipients are passed on as the strings that were typed — the
gateway parses them and its `400` for one that is no address is passed back
— each one line; the subject is collapsed to one line before it leaves.

**The mailbox follows the chat.** After the chat's state has changed, its
gateway is told, each call `{chat, account}` — and, for a mail thread,
`message_ids`: which mails the call is about (below):

| In the chat | Call |
|---|---|
| *Mark as read* that leaves nothing unread; a reply, from here or from anywhere the rail sees | `POST <gateway>/chats/read` |
| `archived: true` (muting archives) | `POST <gateway>/chats/archive` |
| the undo of a mark, un-archive, a star | nothing |

The hooks are generic: the call goes to the chat's own gateway on every
channel, and a gateway that answers `404` or `501` has no mailbox — no error,
and it is not asked again for `MAILBOX_NONE_SECONDS` (600). **A mail
gateway's `404` is not that**: it has a mailbox, and its `404` is its word
about one request ("not this gateway's account"). It switches nothing off —
the gateway is asked anew which mailbox it serves, and that call stays owed.
A mail chat id that names no mailbox is put to no gateway at all.

**A call about a mail thread names its mails.** `message_ids` are the
`Message-ID`s of the mails the chat held when the user acted: the store's (up
to five pages of the thread) and what the rail announced a moment ago,
without the arrivals that landed after the action — after what the read
covers, or after the archive. The gateway serves the call when it gets to it
(after the pass over the mailbox that may be running), and by then the
thread can hold a mail that has arrived since, which the chat counts as
unread or which has just brought an archived chat back; "the whole thread"
at that later moment would be marked seen, or moved, with it. How far the
action reaches is read while it is the newest word about the chat; an owed
call keeps its list, and where the store cannot name the thread's mails the
call is owed rather than made without one. A messenger's call carries no
list.

They never block and never fail the user's action: the call runs on a thread
of its own after the state is written. The chat's state is the truth; a call
that could not be made — the gateway failed or was away, or no gateway was
known for the thread's mailbox just then — is logged and **owed**. Owed
calls are kept in a small durable queue beside the chats' state
(`<CHAT_STATE_DIR>/mailbox/owed.json`, `chat_state.OwedCalls`), so a restart
of the web gateway forgets none, and are made again by a timer
(`MAILBOX_RETRY_SECONDS`, 120; first soon after a start) and after every
hook call that goes through — the user could not repeat the action
themselves: a read chat offers no *Mark as read*, an archived one no
*Archive*. A round leaves a gateway that fails again alone until the next
one; a single call's refusal holds nothing up. An owed call is dropped, not
made, once it no longer says what the chat says (the user un-archived, undid
the mark, or new mail has landed since), and given up after
`MAILBOX_OWED_KEEP_SECONDS` (a week). A *Mark as read* that leaves mail
unread (a `{ts}` mark a newer mail outran) calls nothing: the chat is not
read.

**Delete** is no hook. `POST /chats/<id>/delete` asks the thread's own
gateway — the one that serves its mailbox, and no other mail gateway — with
the erase capability (`CHAT_ERASE_TOKEN`), and that gateway moves the
thread's mails to the Trash before it erases its mirror. Only when it
answers that all of it is done (`errors: 0`) does the web gateway erase its
own state, the copy of the queued sends and the companion; otherwise the
answer is `502` and the chat is as it was. The report carries `trashed`.
A gateway that erased nothing says why in a sentence of its own (`error`
beside `errors`: the mailbox has no Trash folder and which variable names
one; mails moved since the mirror last looked, try again in a minute), and
that sentence is the reason in the `502` — "the chat's messages could not
be completely erased (`<gateway>`: `<its sentence>`)" — in place of a count
of files: the remedy is in it. It is a gateway's text and may repeat a mail
server's, so it is passed on as one line of at most
`CHAT_DELETE_REASON_MAX` (300) characters, control characters removed, in a
JSON string the page shows as text.

## Archived, muted, starred, deleted

Archived and unread is a contradiction: the thread claims
to want attention while being invisible. So when an agent files something new
into an archived thread (`POST /internal/conversations/<id>/messages`, the
`--thread` path of `conversation-push.py`), the gateway **un-archives it** — the
message would otherwise be lost, which is exactly what happened before this
existed. The opt-out is the separate **`muted`** flag: a muted thread stays
where it is, however much arrives. These are the chats' flags, with the
chats' words:

- **Archive** — the thread bar's button and the Threads page's swipe right
  (`POST /conversations/<id>/archive`, or `…/flags` `{archived: true}`): out of
  the list until something new is filed into it. `muted` is untouched.
- **Mute** — the tray's (`…/flags` `{archived: true, muted: true}`): out of the
  list, and it stays out. On the server `muted` stays independent of
  `archived` — an agent may mute a thread the user keeps in the list — so the
  tray sends both.
- **Unmute** — the next message brings the thread back; until then it stays
  archived.
- **Restore** — the swipe right on the Archived tab, the tray's, and the
  thread bar's *Unarchive* alike: back in the list, and `muted` cleared, so
  the ways back in never disagree.
- **Star** (`starred`) — the user's mark: the thread sorts above the rest of
  whichever list it is in (Active, Archived, Edits, Cowork, the project
  page's), each group still newest first. It is independent of everything
  else — archive, mute, restore and a wake all leave it — and it is no
  attention signal: no importance, no delivery, nothing on the home. Chats
  carry the same flag (`POST /chats/<id>/flags`).
- **Delete** — the Archived tab's tray, two taps, no Undo
  (`POST /conversations/<id>/delete`): the thread's messages, attachments,
  attention state and every Claude session it used are erased; a turn still
  running cannot write any of it back. Its idempotency-key bindings are the
  one thing kept (they hold nothing but the key's hash and the thread id):
  they now point at the tombstone below, which is how a redelivered open
  under the same key is told the thread was deleted. A chat's companion,
  erased with its chat, loses its bindings instead. It is the
  user's own act, like a chat delete — accepted only from the dashboard
  through the reverse proxy (403 otherwise), never for a chat's companion
  (409, it goes with its chat), and with no agent path at all: a manipulated
  turn must not be able to erase the record of what it did, and the cowork
  threads are the audit trail of what outside sessions asked. What the thread
  caused elsewhere stays: sent messages, sends still queued on `/sends` (whose
  decision note then has nowhere to go), calendar events, memories, filed
  documents.

**A deleted thread stays deleted.** The gateway keeps a tombstone per deleted
thread under `CONVERSATIONS_DIR/.deleted/` — its id and the deletion time,
nothing else — and every later read, append, flag write and keyed open of it
answers **410 Gone**: an agent's `--thread <id>` writes nothing, and a
redelivered open under the key that created it opens nothing in its place.
`conversation-push.py` reports a 410 as the user's decision (exit 0), not as a
failure; the monitors' notifier and the Ask-Ara connector's audit (which opens
a fresh thread for what is asked next) treat it the same way, and triage
resolves the message instead of proposing it again. Reminders into a deleted
incident thread stay quiet, but news it did not carry is not swallowed: a
sign-in warning the user deleted that turns into an outage opens a thread of
its own. Without the tombstone,
"gone" would read as "start over" and bring back exactly what was deleted.

Agents set the flags through `POST /internal/conversations/<id>/flags`:

```bash
# archive for good — what to run when the user says "archive this conversation"
python3 /workspace/scripts/conversation-push.py --thread <id> --archive --mute
# also: --unarchive, --unmute, --star/--unstar (only on the user's word);
# flags are their own call, never mixed with a message. There is no --delete.
```

A **chat's** flags have the same door for agents, `POST
/internal/chats/<id>/flags`, with the semantics of the user's own endpoint —
muting archives, an archive settles the attention item, and a mail thread's
mails move out of the inbox ("The gate for mail"):

```bash
# what a turn runs that a standing instruction told to archive the mail,
# and what to run when the user says "archive this chat" (add --mute for good)
python3 /workspace/scripts/chat-flags.py --chat <chat id> --archive
# also: --unarchive, --mute, --unmute, --star/--unstar (only on the user's
# word). There is no --delete.
```

The behavioural rules this implies for Ara — Archive-click vs "archive this",
and a deleted thread being the user's answer — are in `CLAUDE.md`, since they
apply on ordinary turns.

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
settings page — inside the Android app, on the app's own, one size for every
endpoint (see "The settings page"). Design and acceptance criteria: issue #282.

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

### The dictation vocabulary

The words an ASR model mangles are the proper nouns — people, practices,
medications — and they are in the **life store** already, so that is where they
are fetched from: one lean `VALUES`-bounded query for
`vcard:Individual`/`schema:Person` names, then organisations, then drugs and
therapies. Asking the store rather than one chamber's files is the point — a
doctor in a care-provider list or a drug in a medication plan is as visible as a
contact entry. The result is cached for five minutes, capped at 200 names
(`DICTATION_NAME_LIMIT`), and **fail-open**: an unreachable store costs the
hints, not the dictation.

The query lives in `scripts/dictation_vocabulary.py` — a stdlib-only module that
both processes needing the vocabulary import, each keeping its own cache:

- the **STT service** turns the names into **Whisper hotwords** and biases every
  decode with them, so the correction happens *before* the mishearing rather
  than after it. It reads the store itself (`QLEVER_LIFE_URL`, same `agents`
  network) instead of having the list pushed at it with each clip: the
  vocabulary belongs to the deployment, not to one request, and this way an
  inbound voice note gets the same bias as dashboard dictation — the messenger
  gateways do not have to know the feature exists. `STT_HOTWORDS=0` turns it
  off. Hotwords share Whisper's 224-token prompt window, so the list is capped
  (`DICTATION_HOTWORD_CHARS`) and each rank gets a share of that budget
  (`HOTWORD_SHARES`) — otherwise a store full of people would crowd the
  medications out entirely. Only names' *words* are used (runs of three or more
  letters in any script, two for Han, Kana and Hangul, where two characters are
  a whole name; combining vowel signs as in Devanagari or Thai belong to the
  word), since Whisper matches hotwords word-wise.
- the **web gateway** hands the whole names to the cleanup pass below as
  **spelling hints**.

Hotwords bias, they don't constrain: a name the store knows in one spelling can
still be heard as another name it also knows.

Two decode settings in the service serve the same end. `vad_filter` (Silero,
`STT_VAD_FILTER=0` to disable) drops non-speech — silence is what makes Whisper
invent whole sentences, and decoding less audio is also faster; if the VAD model
is unusable at runtime the service logs it once, disables it for the process and
decodes on. `condition_on_previous_text=False` stops the repetition loops a
window conditioned on the previous window's text can fall into. Which knobs the
installed faster-whisper actually supports is probed from
`MODEL.transcribe`'s signature — an older wheel biases through `initial_prompt`
instead of `hotwords` — and the startup line reports what it resolved to.

### The cleanup pass

Dashboard voice input adds a **cleanup pass** on top: the raw transcript is run
through a small model (`TRANSCRIPT_CLEANUP_MODEL`; unset it falls back to
`RETINUE_CLAUDE_MODEL` — so a non-Anthropic deployment cleans up on its own
backend — with `haiku` as the last resort) with the
thread so far and the dictation vocabulary above as context, so what lands in the
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
An answer of Ara's to the loop's own turn that ends in a **call directive**
(`[[call: <number> | <name>]]`, `.claude/skills/dashboard-composing` §7)
places the call where the mic would open, when the page runs in a shell that
can (`placeCall`, below): once the answer has been read, the composer shows
the name and the number to be dialled with a **Cancel** button for 5 s while
the mic is open as for any take, its no-speech timeout stretched past the
call's wait (a mic that fails to open leaves the call to go ahead). Where the
mic would not open because the user is writing, no call is placed either: the
loop stops as it does there, and the directive stays a link. Speech (the
detector's `speech`, so in any language) cancels the call, and the take goes
on to Ara as the next turn, whatever ends it, carrying `cancelled_call: {number, name}`, from which the gateway writes a
fixed agent-only note (`_cancelled_call_context`): the call was not made;
unless the words mean to stop it, place it again as a second attempt with
`no-voice-cancel`. A cancelling take whose transcript is empty is no turn:
the call goes ahead at once, with Cancel by touch only. A cancelling take
that would carry text or files from the box goes to review instead, like any
take, and the call stays cancelled. A cancelling take the service worker
sends because the page was left or frozen meanwhile (webapp README, "Unsent
input") carries the call there all the same; one the worker could not send
waits in the box with its note, and the call stays cancelled; so does one
the page could not send (another turn still on the wire, a model pin
refused): it waits in the box, and no later turn carries its call. Sent
more than 90 s after its transcript came (a page opened later that takes
the take over), or after it was recorded where the worker restarted it, it
carries no call: its words go to Ara alone, and no call is placed again
that nobody is waiting for. Cancel itself
(by touch) cancels for good.
Speech cancels only where the detector can hear the stream; a take sent by
touch (➤) while the call waits cancels it all the same, and where the page
cannot hear the stream (plain timers, above) the take that listened is not
dropped when the call is due but sent (to review with text or files in the
box) as a cancelling take, so its transcript decides. A call that would go
ahead after an empty transcript while the page is hidden is dropped.
Nobody speaking, the take is dropped, the mic released (up to 2 s), the number
handed to the shell, and the voice flag let go once the shell has answered
(10 s at most). A call the shell refuses, or does not answer in that time,
leaves a note in the composer that the call to the number could not be
placed; a call placed, or the dialer opened with the number, needs none. The
flag `| no-voice-cancel` as a third part (or in the name's place, with no
name) keeps the mic
shut for those 5 s — for a loud room or an emergency — but only on a second
attempt: unless an earlier answer of Ara's in the thread carried a directive
for the same number (`calledBefore`), the flag is ignored. Only a directive
the bubble shows as a call link is a call: `callOf` renders the reply as the
bubble does and takes the last link. One quoted as code (a span, whose
backticks pair within a line, or a ``` or ~~~ fence, closed or not), split
across lines, or cut apart by a table's cells is shown as text, never placed.
Leaving the thread, hiding the page (the mic open or not) or
stopping the loop drops the call. Anywhere else — a typed thread, an older reply, a message pushed
into the thread (an agent's included), Ara's reply to a chat message that
arrived in a companion thread meanwhile (stored with `arrival: true`, never the
loop's answer nor an earlier attempt), a browser — the directive is only a tap-to-dial `tel:` link (its `#`
written `%23`, so a service code dials whole)
(`webapp/components/markdown.js`, `callOf`; `tests/test_webapp_call_directive.py`).
The presentation lint never delivers a result whose call directives differ
from the original's, read as the bubble reads them (JavaScript's whitespace
included), nor one that changes a line holding a directive, or a fence or
table-separator line, in a message with one (`docs/model-routing.md`, phase 4).
Entry points: the toggle, `/#new?voice=1` (the composer in the mode — its
first take opens the thread, which continues in it) and a wake (`/?wake=1`
or the shell's `wake` event), which opens the thread used in the mode within
the last 15 minutes, else a new one. Both flags leave the address once read,
and only inside the shell do they open the mic by themselves; elsewhere (a
link in a message, a reload) the conversation opens in the mode with a
**Start talking** button. The detector listens for energy, not words, so it
is language-agnostic.
Ending it: the toggle, or **by voice** — the user tells Ara to switch the
mode off (in any language; Ara reads the intent, the page reads no words),
and her turn runs `conversation-push.py --end-continuous` (the thread is the
turn's own, `RETINUE_THREAD_ID`). The gateway stamps the thread document
`continuous_off` (ISO time); each device showing the thread in the mode
leaves it on its next read, as if the toggle had been tapped (the mic closes,
the shell's voice flag is released, and a call directive the answer ends in,
or a call waiting to be placed, is not placed). The stamp acts once per device and only
on a mode switched on before it (one minute of clock skew allowed), so a
toggle the user presses afterwards stays on.

Language handling (constrain detection to the languages the user speaks, and
re-decode when a guess falls outside that set — carrying the same hotwords and
settings into the second pass) lives entirely in the service.
`STT_SUPPORTED_LANGUAGES` states the set outright; left unset it is the **union**
of what the messenger channels declare (the `*_SUPPORTED_LANGUAGES` settings of
Signal, WhatsApp and Telegram), because this one model transcribes for all of
them and no single channel's setting speaks for the deployment. An optional
`STT_TOKEN` gates the endpoint (defence-in-depth; the service is not published
to the host). Downloaded model weights persist in the `stt-models` volume.

## Several instances: name and colour

Every deployment serves the same baked manifest and icons, so two installed
PWAs (a main and an admin instance, say) look identical on the home screen and
in the task switcher. Two optional settings brand an instance; a single
deployment needs neither (`scripts/instance_branding.py`):

- **Name** — a short label (`Admin`, at most 40 characters, one line). The
  manifest's `short_name` becomes the label, its `name` and every page
  `<title>` `Retinue (Admin)`; push notifications without a thread title fall
  back to it, the Ask-Ara connector answers as `Ara (Admin)` unless
  `ARA_MCP_IDENTITY` says otherwise, and the Android shell lists the instance
  under it: the shell reads each endpoint's manifest `short_name` and shows
  that in its endpoint menu — there is no label of the shell's own (see
  "Inside the Android shell").
- **Accent colour** — `#rrggbb` (or `#rgb`), for the icons and the dashboard
  alike. The stock icons (blue ring on near-black) are recoloured, and the
  dashboard's accent with them: the `--accent` definition in `styles.css` plus
  the `rgba(110, 168, 254, …)` tints the components' shadow styles hard-code —
  send buttons, links, focus rings, hovers. Mode, sphere and status colours
  (`--high`, `--ok`, `--outbound`) are left alone; they carry meaning. The send
  glyph stays dark, so choose a light or medium tone. An invalid value is
  ignored.

Two sources, dashboard over deployment, as for the agent models:
**Settings › Instance** (`GET`/`POST /instance`, server-side and shared by
every device) writes `RETINUE_INSTANCE_FILE` (default
`/root/.retinue/instance.json`, the persistent volume); the variables
`RETINUE_INSTANCE_NAME` and `RETINUE_INSTANCE_COLOR` are the deployment
defaults, in force wherever the card holds nothing (its *Stock colour* button
and an emptied name field hand back to them). The card shows the default as
the name field's placeholder.

Both are applied when the gateway serves the file — the baked webapp is never
edited — and both are folded into the service worker's shell hash, so a change
reaches installed clients like any shell edit, and a change on the Settings
page reaches the very next response.

**The colour is in the icon URLs too.** An installed app keeps the icon it
fetched for a URL: desktop Chrome, since 143 ("predictable app updating"),
refreshes an installed PWA's icons only when the manifest names other icon
URLs, and the Windows taskbar, the Start menu and the macOS dock show the
shortcut icons Chrome made from those at install. So with a colour in force
every icon reference the shell makes carries it as a version —
`/icons/icon-192.png?v=cb6cfe` in the manifest, in every page's `rel="icon"`
and `apple-touch-icon` link, and in the service worker's precache list and
notification icon. The gateway ignores the query and serves the colour in
force. A new colour means new URLs, which the browser takes as a new icon the
next time it checks the manifest (on a launch of the app; Chrome may ask the
user to confirm a changed icon). The stock colour keeps the plain URLs.

An already-installed PWA may keep its old home-screen label and icon until the
browser has checked the manifest; reinstalling applies it at once. A pinned
taskbar entry that still shows the old icon after Chrome's update is Windows'
icon cache; unpinning and pinning it again refreshes it. The Android shell is
not held up by any of that: it fetches the manifest itself.

## The settings page

`settings.html` (the gear in the navigation row) has two parts in a browser:

- **This browser** — what is kept per device and browser: Web Push
  notifications, the voice timeouts, the layout, the shelf size, and the
  dashboard version with a manual update check.
- **This instance** — what is the same on every device: the instance's name
  and colour (see above), whether it follows focus modes (see "The home"),
  the agent models, the system pages
  (`components/instance-settings.js`, `<retinue-instance-settings>`).

Inside the Android app there is one settings page for the user, the app's own
(the app's README, "First run and settings"), in three parts: *this app* (its
endpoints, voice and shelf, home screen, split screen, wake phrase, launcher
bar, updates), *this app with the endpoint shown* (its health data; its
notifications, once the app receives them), and *this instance*. The app
serves that page itself, so it opens with no server at all, on first run and
when the dashboard cannot load. Only the instance part comes from the server,
as **`/settings-instance.html`**: the instance section on its own, served with
`Content-Security-Policy: frame-ancestors 'self'
https://appassets.androidplatform.net` — the address at which the app serves
its page (reserved by Google for exactly this; nothing on the internet answers
it) — so no other site can frame it. Framed, it tells the app's page how tall
it is (`postMessage({type: 'retinue-instance-settings', state: 'ready',
height})`, addressed to that origin only), and its links leave the frame
(`<base target="_top">`); the app opens them in the dashboard. A shell that
has a page of its own says so (`RetinueShell.settingsPage === true`,
RetinueShell/4): the gear then opens that page instead of `settings.html`, and
`settings.html`, reached anyway, shows a card leading there in place of the
browser part (Web Push does not reach a WebView anyway). A shell without one
(RetinueShell/3) keeps the Phone shell card at the top of `settings.html`, as
before.

**Per-device preferences** (`components/device-store.js`). The voice
timeouts, the shelf size, the column layout and the list or card views belong
to the device, not to the instance. In a browser they live in `localStorage`,
per origin. Inside the app (RetinueShell/4) the app holds them, so one phone
has one set for every endpoint it shows: `deviceGet(key)` reads the app's
value (`RetinueShell.deviceSettings()`), `deviceSet(key, value)` writes it
(`setDeviceSetting`), and a value the app does not have yet is taken over from
the page's own `localStorage` once. A change made elsewhere — on the app's
settings page, or by another tab in a browser — reaches an open page through
`onDeviceChange` (in the app, the `device-settings` event naming the keys
that changed). Only the keys the store lists go to the app, and the app takes
only those, bounded in size, so one instance cannot pass data to another
through them; a new per-device preference is added to both lists
(`DEVICE_KEYS`/`DEVICE_PREFIXES` here, `DeviceWebSettings` in the app).

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
| User agent | ` RetinueShell/6 (Android)` appended (` RetinueShell/5` before `placeCall`, ` RetinueShell/4` before the keyboard members, ` RetinueShell/3` before the app's own settings page and the per-device preferences, `/2` before the endpoint members, `/1` before the Health Connect members; the page feature-detects every member rather than parsing the version). |
| `window.RetinueShell` | Installed at document start, configured origin only: `platform`, `version`, `setVoiceActive(bool)` and `openSettings(section?)`, which opens the app's settings: from RetinueShell/4 its own page (the gear calls it when `settingsPage` is true), before that the native screens behind Settings → Phone shell (shown only when the object exists; the card is the first on the page there). `setVoiceActive(true)` means *a voice conversation is engaged: keep the screen on and pause the wake phrase*. The page asserts it before it asks for the mic (the wake detector must release it first) and keeps it asserted for as long as a continuous conversation is going — through transcription, Ara's answer and its reading, not only while the mic is open — and sends `false` once the loop stops (mode off, no speech, leaving the thread, an error). Wake detection being paused for the whole conversation is intended. |
| Endpoints (RetinueShell/3) | The shell holds a list of Retinue servers (address, client certificate, sign-in, health switch) and shows one; the app's settings page lists, edits and switches them (RetinueShell/3: the Settings page's Phone shell card lists them in a menu and switches between them). `endpoints()` → a JSON string `[{"id": str, "label": str, "current": bool}]` — the label is the instance's own name, the manifest `short_name` the shell fetched with that endpoint's certificate or password (the host while it is unknown; the host appended when two names collide); no URLs, the other servers being none of the current page's business. `selectEndpoint(id)` asks the shell to show that server (it loads that dashboard's start page, so the page is left). `openSettings("endpoints")` opens the app's settings at the endpoint list, or at the editor straight away with one endpoint. The page may ask; it can never write an address, certificate or password — whatever origin the shell is pointed at receives its bridge, microphone, certificate and password, so that stays in the app. An `endpoints` event (`detail.type`) says the list changed while a page is open; the card re-renders. The shell's one base URL of earlier builds is the first endpoint after an update. |
| Health Connect (RetinueShell/2) | Further members on `window.RetinueShell`: `healthConnectAvailable()` → `"true"`/`"false"`; `healthSync()` — a deliberate sync now, returns at once (a deliberate sync always runs, whatever the interval); `healthSyncStatus()` → a JSON string `{"available": bool, "permissions": "granted"\|"partial"\|"none", "last_sync_at": ISO\|null, "last_result": "ok"\|"error"\|"skipped"\|null, "last_error": str\|null, "last_count": int\|null, "min_interval_minutes": int, "auto_on_resume": bool, "enabled": bool}` (`enabled`, RetinueShell/3: the current endpoint's own health switch, set in the shell's editor — off, nothing is read or sent for that server, a sync asked for is `skipped` with reason `disabled`, and the card shows one line and *Edit endpoint* instead of the controls; an older shell omits the field and uploads for its one server); `setHealthSyncInterval(minutes)`; `setHealthAutoSync(bool)`. The shell reads the phone's Health Connect store through its Changes API and uploads what it returns, unaltered, as one JSON file per delivery by **WebDAV `PUT` to `/inbox/<name>`** — the letterbox share below, with the WebView's own client certificate and cookies — where a chamber that declares the file type in its `accepts` takes it (docs/scheduling.md); the framework knows nothing about the data. On resume the shell syncs on its own at most every `min_interval_minutes` (default 5) while `auto_on_resume` holds. From RetinueShell/4 the app's settings page has these controls, beside the endpoint's switch, and the dashboard shows none; with RetinueShell/3, Settings → Phone shell shows, only where `healthConnectAvailable()` is true, "Update health data now", the last sync (from `healthSyncStatus()` and the events), the auto-sync toggle and the interval. Every member is feature-detected (`webapp/components/shell.js`): an older shell simply shows none of it. |
| Settings page and per-device preferences (RetinueShell/4) | `settingsPage: true`: the app has a settings page of its own (see "The settings page"), and the gear opens it. `deviceSettings()` → a JSON string `{key: value}` of the per-device preferences the app holds for every endpoint; `setDeviceSetting(key, value)` writes one (`null` removes it; a key the app does not take is ignored). `components/device-store.js` is the only caller. |
| Keyboard (RetinueShell/5) | The shell can be paired with another device — a VR headset, a TV, a computer — as a **Bluetooth keyboard** (Android's `BluetoothHidDevice`), and type into it; pairing, the device's keyboard layout and a live keyboard are the app's own screen. `keyboardStatus()` → a JSON string `{"available": bool, "enabled": bool, "connected": bool, "host": {"name"}\|null, "layout": str}` (the device's name only — its Bluetooth address stays in the app). `keyboardType(text, optsJson)` types `text` (opts `{"id", "enter"}`) — a line break as **Shift+Enter** (a new line in chat apps and text areas, but a submit in a terminal or a one-line field), a tab as Tab; `keyboardKey(name, optsJson)` (opts `{"id"}`) presses one of `enter`, `tab`, `backspace`, `escape`, `up`, `down`, `left`, `right`, `home`, `end`, `delete`, `space`; `keyboardSecret(optsJson)` (opts `{"id", "label", "enter"}`) opens a **native** field in the shell, labelled and naming the device it types into, that the user's password manager fills by autofill (or the user types) — the shell types it and overwrites its own copy, and the secret never reaches the page, the server or Ara; `keyboardOpen()` opens the app's keyboard screen (refused as `not-enabled`, as typing is, while the app's *may type* switch for this server is off — that switch is on the app's settings page); `keyboardCancel()` stops what is being typed (its outcome: `error` `cancelled`) and closes an open secret dialog (`secret-cancelled`) — but never interrupts a secret being typed, which only the phone's own Stop can. Before anything that can act on the device — a text with Enter, a line break or a tab, an empty text or one with whitespace at either end (Python's set), and any key — the shell asks on the phone itself, in a confirmation it draws; plain one-line text without Enter and without whitespace at its edges it types at once in its default (one-line) mode — after the user pressed Stop on the phone's strip, and on a device set to ask every time, it asks about that too. With the app's *may type* switch for this server off the answer is `not-enabled`, at once. Only a confirmation the user declines or does not answer ends late: as `error` `cancelled`, the same as `keyboardCancel()`, up to 25 s (a text) or 12 s (a key) after the call. The shell takes typing, keys and the secret dialog only while it is in front — resumed, focused and unlocked (otherwise `error` `not-in-front`), and with no secret dialog open (otherwise `busy`; one dialog at a time) — and types into the device the user connected, never another one that connects by itself; typing it started runs to its end unless stopped, the phone kept awake meanwhile. An `id` is echoed in the request's events when it is 1–64 of `A–Z a–z 0–9 . _ : -`, and dropped otherwise. A call with invalid arguments throws; every other request with an id gets exactly one final event (a secret dialog nobody answers closes itself after ten minutes). Outcomes come as `keyboard` events (below). Callers: `components/shell-keyboard.js` through `shell.js`; what drives them is a reply's shell actions (see "Shell actions"). |
| Shell → page | `window.dispatchEvent(new CustomEvent('retinue-shell', {detail: {type}}))`, `type` `wake`, `home`, `health-sync`, `endpoints`, `device-settings` (with `keys`, the per-device preferences that changed), `keyboard` (RetinueShell/5: `state` `typed` with `count` (a key press too), `error` with `error` — `not-enabled`, `not-connected`, `not-in-front`, `too-long`, `unsupported-char`, `busy`, `cancelled`, `failed` —, `secret-typed`, `secret-cancelled`, or `status` when the connection changed; `id` names the action where there is one) or `call` (what became of a `placeCall`, see Phone calls below). A `health-sync` event carries `state`: `started`, `done` (with `count`), `error` (with `error`) or `skipped` (with `reason`: `interval`, `no-permission`, `unavailable`, `no-backend-support`, `disabled`); it, `endpoints`, `device-settings`, `keyboard` and `call` are information only — no page navigates on them. The home answers in place: `wake` opens continuous conversation (the thread used in the mode in the last 15 minutes, else a new one), `home` unwinds every open thread or composer back to the list at `/`, closes a sheet and scrolls the list to its top. Other dashboard pages answer by loading `/?wake=1` or `/`, always: unsent input on the page (a draft, an unsaved project edit, a dictation or send under way) never holds either back — it is kept on the device and waits in its field; a dictation is finished by the service worker, which puts a review dictation into its field and posts one the user had sent into its thread (webapp README, "Unsent input"). |
| Pages without the web app's code | The gateway's own pages (`/sends`, `/claude-auth`) do not load `components/shell.js`, so nothing on them answers `wake` or `home`. The shell handles that itself: when the page in front is not one of the dashboard's, a Home press loads `/` and a wake loads `/?wake=1` instead of dispatching the event. |
| `/sync-health` | The link target for "update your health data" in threads: `https://<dashboard origin>/sync-health#sync` (the origin is `CONVERSATION_BASE_URL`'s). Same origin, so the shell opens it in place. A static page — a GET never changes server state; the fragment is the instruction, and the server never sees it. In the shell with `#sync` it calls `healthSync()`, says "Health data update triggered", and after a moment goes back (`history.back()`, or `/` when opened cold); a `done` arriving first is shown before it leaves, an `error` or `skipped` stays on screen with *Update now* and *Back*. The fragment is dropped from the address once read, so a reload starts nothing. In the shell without `#sync`: a short explanation, the last sync and *Update now*. In a browser on Android: the Ara app is needed, and an **Open in Ara app** link — an intent link the page builds from `location`, never from configuration: `intent://open?url=<percent-encoded https://<origin>/sync-health#sync>#Intent;scheme=retinue-shell;package=os.retinue.ara;S.browser_fallback_url=<percent-encoded https://<origin>/sync-health>;end`. The app opens the URL only when its origin is exactly the one it is configured for, and otherwise says so and connects nowhere (no Android App Links, no Play services). On a desktop: the explanation only. The page loads `shell.js`, so `wake` and `home` work on it, and it is in the service worker's shell cache like every page. |
| Cold wake | With no page loaded the shell loads `/?wake=1`; the home treats it as a wake and drops the flag from the address. A wake should reach the page once it is resumed; one that arrives while the page is hidden still waits (up to 60 s) for it to come to the front before the mic opens. |
| Speech output | A `speechSynthesis` / `SpeechSynthesisUtterance` stand-in over the native TTS when the WebView has none; `webapp/components/speech.js` is unchanged. |
| Phone calls (RetinueShell/6) | `placeCall(number)` on `window.RetinueShell`: the shell calls the number without a tap (`ACTION_CALL`, on speakerphone — the user may not be holding the phone) when *Call directly by voice* is on for the endpoint (a switch per endpoint, in the app's settings under *This app with <endpoint>* → *Phone calls*, off for a new endpoint), Android's phone-call permission is granted, and a voice conversation is engaged (`setVoiceActive(true)`) — never outside one, so no page can dial behind the user's back. The shell normalises the number (separators, a leading `tel:` and the `(0)` of `+44 (0)20` dropped); it must then be an optional `+` followed by digits, `*` and `#`, with 3 to 20 digits and at most 32 characters. It answers with a `retinue-shell` event `{type: "call", state, reason?, number?}`: `placed` (with `number`); `dialer` — the dialer opened with the number instead (with `number`), for an emergency number (Android lets no app call those directly; `reason: "emergency"`), a service code — any number with `*` or `#`, which always opens the dialer (`service-code`) —, with the switch off (`disabled`) or without the permission (`no-permission`); or `refused` (`invalid-number`, `not-in-voice`, `no-telephony`, or `failed` with `number`). An empty string is answered `refused` / `invalid-number`; `placeCall()` and `placeCall(null)` send nothing. The web side (`shell.js`: `canPlaceCall`, `placeCall`, `onCall`) uses it only for the call directive of the continuous loop's own answer (above), and lets the voice flag go on any answer, whatever its state. |
| Files | A file the WebView cannot show (a PDF, anything served as `attachment`) reaches the shell's download listener, which follows the `Content-Disposition` as a browser does. `inline` — a PDF row's name link (`?inline=1`; images and text open in the dashboard's file viewer, see "Attachments") — means *view*: the shell opens it in the phone's app for the type from a private copy, so no copy lands in Downloads (none installed: it saves it there and says so). Anything else — the ↓ link — is saved to Downloads. The name is the header's (`filename*`, then `filename`); the attachment URL ends in it too (see "Attachments"). |

### Shell actions: typing on a paired device

A reply of Ara's may carry **shell actions**: text for the device the Android
shell types on as a Bluetooth keyboard (RetinueShell/5, above). It is how
Retinue reaches a device that cannot run it — a VR headset, a TV, someone
else's computer: the user says "type the text above" in a thread, and the
phone types it there.

- **Agent side.** Inside a dashboard-thread turn, `scripts/shell-type.py`
  attaches an action to the reply being written — a text (`--enter` to press
  Enter after it), `--key <name>`, or `--secret "<label>"` — by appending one
  ASCII JSON line to the turn's manifest, `RETINUE_REPLY_SHELL_ACTIONS_FILE`.
  A text goes in a file Ara writes with her Write tool, exactly as it is to
  be typed, so no shell ever parses it (the one trailing line break a file
  usually ends with is dropped; `-` reads stdin the same way, for another
  program's output):

  ```sh
  scripts/shell-type.py [--sensitive] [--enter] --file /tmp/type-<thread id>.txt
  ```

  On the command line every quoting can be ended by the text itself: inside
  `"…"` bash expands `$`, backticks and `\` (`Pa$$w0rd!` typed with the
  shell's PID in it, `$(…)` run in the container), an apostrophe ends `'…'`,
  and a line `EOF` ends a `<<'EOF'` heredoc early, after which the text's
  remaining lines run as commands with the agent's tokens — a passage from an
  e-mail or a page can be written to do just that. So the docs offer only
  `--file`, and `--sensitive` refuses a text on the command line (exit 2,
  nothing written, the text not echoed): it would be in the process list and
  the transcript, so a sensitive text comes from `--file` or `-` (stdin).
  With `--sensitive` the CLI deletes the file when it ends, also
  when it refuses the call (outside a turn, a bad combination of flags, a
  file that is no text), so the code does not stay in the container.
  It is the sibling of `conversation-push.py --reply-attach`
  (RETINUE_REPLY_ATTACHMENTS_FILE) and follows the same rules: a fresh
  manifest per spawn, only the kept run's is read, so an escalated junior's
  actions go with her discarded reply; outside a thread turn the CLI refuses.
  Only a turn the **user** started **from the dashboard** gets the manifest
  (`_conv_turn_may_type`): its message came through the reverse proxy
  (`via_edge`, set by the dashboard's own endpoints, never for a request
  another site's page made the browser send, `Sec-Fetch-Site` other than
  `same-origin` or `none`: `POST /conversations`, `…/<id>/messages` and the
  outcome call below answer it 403, as the sign-in endpoints do; a sent
  dictation the service worker posts for a page that was left is the
  dashboard's own, `same-origin`, so it counts as the user's message), so
  neither a chat
  arrival — someone else's words — nor a "user" message an in-container
  agent posted can make the phone type; the gateway drops any action such a
  turn produced anyway. It is decided again once the turn holds the session
  lock, when its prompt is built from the thread as it is then: a turn that
  waited behind another, and whose prompt would end with a "user" message
  posted from inside the container meanwhile, gets no manifest either (it
  still answers the dashboard message, without typing). `scripts/shell_actions.py` validates in both places
  (kinds, a 32-hex `id`, text ≤ 4000 characters without control characters,
  line or paragraph separators or bidi controls, a known key, a one-line
  label ≤ 80; at most 10 per reply, which the CLI enforces as it writes).
- **Stored** on the reply as `shell_actions`:
  `{kind: "keyboard-type", id, text, enter, sensitive}`, `{kind:
  "keyboard-key", id, key}`, `{kind: "keyboard-secret", id, label, enter}`,
  each with `auto` — whether the page may run it without a tap, decided by
  the gateway: only in a plain chat the user opened (`_conv_allows_auto_run`;
  never in a companion thread or a thread an agent opened, whose turns carry
  other people's words), only plain one-line text
  (`shell_actions.runs_by_itself`: no Enter, line break or tab, no key, no
  sensitive text, no password-manager field, not only whitespace nor
  starting or ending with it), only a text that already stands verbatim, as
  a whole line (edge whitespace aside), in the thread as it was when the turn
  started, up to the message it answers — one of the user's messages sent
  from the dashboard (`via_edge`) or an earlier reply of Ara's
  (`_conv_text_was_shown`), so nothing Ara read meanwhile, nor a message the
  turn itself posted, types by itself — and only a reply's first action, so
  no text is put together from pieces, and none is typed ahead of an earlier
  action that waits for a tap (an Enter confirmed later would run a line the
  user never saw; the page, too, runs no later one by itself) — and none
  at all when the user wrote again while the turn ran: if any user message
  stands after the one the reply answers (the newest it covered, else the
  one it was started for) by the time the reply is stored, every action
  waits for a tap, since that message ("no, don't type that") was never read
  by the reply; `_conv_add_message` checks it under the lock it appends
  with, so none can land in between. What plain
  characters do is the device's affair: a letter can be an app's shortcut
  there, which is why a piece of a line does not count — and, once the page reports
  it, `outcome` (`{state: typed | error | secret-typed | secret-cancelled,
  error?, at}`, `POST /conversations/<id>/shell-actions/<action id>/outcome`,
  through the reverse proxy only, never cross-site). An action once typed stays typed: a later
  Retype that does not type (a confirmation declined, say) is kept beside it
  as `last_attempt` and told to Ara as *typed (a later retype: …)*, since
  what the first one did on the device stands (`shell_actions.record_outcome`).
  Transcripts shown to later turns note each action with its outcome
  (`shell_actions.describe`); a resumed session is told the outcomes of its
  own last reply's actions. An `error` `cancelled` is told as *stopped, or
  declined or not confirmed in time on the phone*: the page's Stop and the
  phone's confirmation (Keyboard, above) end the same way, so it never
  reads as the user's refusal.
- **Passwords, PINs and codes** can be typed two ways:
  - `shell-type.py --sensitive --file <path>` (a file holding the code, which
    the CLI deletes, also when it refuses the call) — one the user
    dictated, or one Ara fetched (an SMS code). The text reaches the
    phone, but the dashboard shows it only as `••••••` (no reveal) and types
    it only on the user's tap, a replayed transcript never carries it
    (`shell_actions.describe`), and it is **deleted from the stored reply** as soon as the page reports
    it typed (the `outcome` call above) and in any case after
    `SENSITIVE_TTL_SECONDS` (two minutes): every read of a thread
    (`_load_conv`) purges an expired text before anything sees it, a timer
    per due text rewrites the file, a startup sweep
    (`_conv_sensitive_sweep`) does both for whatever a restart left, and the
    page drops its own copy at the same mark. A purged action keeps `kind`,
    `id`, `enter`, `sensitive` and gets `purged: true`, `text: null`; the row
    says *Text deleted* and nothing can retype it. A thread title derived
    from one of the user's messages becomes *Typing on a paired device*, so
    the thread list and pushes do not carry it. A reply that is only shell
    actions (no text, no file) is pushed as *Ready to type on your device*,
    never with what it types.
  - The turn's Claude session holds the text verbatim (the user's message,
    Ara's own command), so it is **never resumed**: `send_message` decides
    so under the thread's session lock, before any queued turn can resume it
    — whether the run succeeded or failed, and for a sensitive action in an
    escalated junior's discarded run too — and drops the binding with every
    session id the thread ever had, erasing their transcripts
    (`_record_turn_session`, `previous_session_ids`). The next turn starts a
    fresh session, shown the thread as stored: Ara's command and the typed
    text are masked there, but **the user's own message still reads as they
    wrote it until it is redacted.** A spawn that raises still drops it, and
    a restart that cut a turn short leaves nothing resumable either: at
    startup, a leftover shell-action manifest (named with its session key's
    tag) that holds a sensitive text drops that key's sessions
    (`_sweep_stale_shell_manifests`). A turn queued behind another builds its
    prompt only once it holds the session lock, for the user message it was
    started for — and is skipped when a turn before it of the same kind
    (one that may type, or one that may not) already answered it; an arrival
    answers no user message, so a dashboard message that landed meanwhile
    keeps its own turn, manifest and all. A turn whose message another
    turn's reply already follows is shown the whole thread. Ara never repeats such a text in her
    reply. When the user dictated it, she offers to **redact** that message:
    `conversation-push.py --thread <id> --redact-message <message id>` (every
    user message shows its `[message id: …]` to Ara — one stored before
    messages had ids gets one derived from its time and role; `POST
    /internal/conversations/<id>/redact`, token-gated) replaces its text with
    *[deleted by request]* for good — only after the user approves, which
    stays the prompt's rule; the endpoint itself takes a redaction only from
    inside a turn of that thread the user started from the dashboard after
    that message (409 otherwise). Redaction also deletes the message's
    attachment files, a thread title derived from it, the session-log
    entries of that message — entries name their thread and message, and one
    a turn running at that moment writes later is logged redacted; entries
    written before entries named their message are not reached — and every
    Claude session the thread has had since this version, the latest 100 (a
    turn running at that moment has its session refused once). The session
    log never records a prompt as a question: a message without text is
    logged as what it carried (`[attachment: …]`). The one-shot model calls that see the user's
    words (dictation cleanup, the presentation lint) keep no transcript
    (`--no-session-persistence`). What no redaction reaches: the model
    provider processed the text, and push notifications already delivered.
  - `--secret "<label>"` — the convenient path for a password the user keeps
    in a password manager. It carries a label only; the shell asks for the
    secret in its own native field (autofill), naming the device it types
    into, types it and overwrites its own copy. Ara and the server learn only
    `secret-typed` or `secret-cancelled`.
- **Page side** (`components/shell-keyboard.js`, rendered by
  `conversation.js` under the bubble). Outside the shell the row only says the
  Ara app is needed. Inside it, each action gets a button — *Type N
  characters on <device>*, *Type ••••••*, *Retype*, *Password from manager:
  <label>*, each saying *+ Enter* (×N) for every Enter it presses, line
  breaks included — above it, for a text, the **text itself** as it will be
  typed (line breaks marked ⏎, tabs ⇥), so a tap confirms what the user
  sees; and the outcome line from the `keyboard` events (*Typed on
  Quest 3 ✓*, *Not typed: no device connected* with *Open keyboard*, *Not
  typed: typing is off in the Ara app (for this server, or the keyboard
  itself)* with *Open settings* — the app's settings page holds the server's
  switch and the way to the keyboard screen, which the app refuses while
  that switch is off); after a reload the row shows the stored outcome. An
  action the app never answers is given up on after 30 s and 0.1 s a
  character (a text) or 15 s (a key): the row says *No answer from the Ara
  app — check the device before you retype*, the next action starts, and
  nothing is reported, as nobody knows what the device took — an answer that
  comes later still reaches the row and the gateway. *Stop* on a row that
  still says it is typing an action the page no longer runs settles it the
  same way. While the phone asks before
  an Enter, a line break, a tab, a key, an empty text or a text with
  whitespace at either end (Python's set; `phoneConfirms`, which follows
  the app's `onlyWrites` rule), the row says
  *Waiting for confirmation on the phone…* instead of *Typing…*; a
  `cancelled` reads *Not typed: stopped, or not confirmed on the phone*.
  The phone may also ask about a plain one-line text, an automatic run
  included (after the Stop on its strip, or on a device set to ask every
  time; the row, which cannot tell, says *Typing…*); its `cancelled` up to
  25 s later is an ordinary end — reported, worded as above, offered for
  *Retype*, never run by itself again.
  Actions run **one after
  another** — the next starts on the previous one's final event, the app
  refusing a second one while it types — and *Stop* ends the one running (a
  text being typed, or an open password dialog — never a password being
  typed) and drops the rest. A confirmation on the phone declined or not
  answered in time (`cancelled`) drops the rest too, as do `not-enabled`,
  `not-in-front` and `not-connected`, and a password dialog closed on the
  phone without a password (`secret-cancelled`: Cancel, back, or its own
  close after ten minutes): what waited was meant to follow the action that
  did not run (a text after a Tab is for the field the Tab moves to, a Tab
  after a password for the field after it), so it is reported `cancelled`
  rather than typed into whatever has focus. The outcomes are reported to the gateway from the module,
  not the thread view, so leaving the thread mid-text loses none. A
  `keyboard-type` action runs **by itself once**: only when the gateway
  marked it `auto`, only when its message arrives live in the thread open
  and visible on this device (not on opening the thread, a reload, a history
  read, or coming back to a page that was hidden — what arrived meanwhile
  waits for a tap), only within two minutes of the message's time, and only
  if this device has not run its `id` before (a bounded list in localStorage
  — only the app's one WebView can type at all). Everything else waits for
  the user's tap, and the page checks the rule of `runs_by_itself` again
  (`needsTap`). Retype is
  always a tap, and impossible once a sensitive text is deleted.

`manifest.webmanifest` declares `launch_handler: {client_mode:
"focus-existing"}`, so a second launch of the installed PWA brings its open
window to the front as it is — an open thread, a draft or a recording under
way are kept; `navigate-existing` would load `/` over them. Known gap: Web
Push does not reach a page inside an Android WebView, so notifications still
need the PWA installed in Chrome alongside.

## The letterbox share (`/inbox/`)

The built-in letterbox (`/workspace/chambers/inbox`, docs/scheduling.md,
"The built-in letterbox") is shared over **WebDAV** at `/inbox/` on the
dashboard origin, by the gateway itself (`scripts/webdav_share.py`): OPTIONS,
PROPFIND (depth 0 and 1), GET/HEAD, PUT (with a `Content-Length` or chunked;
`LETTERBOX_MAX_UPLOAD`, default 1 GiB), DELETE, MKCOL and MOVE — class 1, no
locks. That is what the phone shell's Health Connect upload uses, and it is
enough for `curl -T file https://<origin>/inbox/`, scanners and the WebDAV
clients of file managers and sync apps.

**Auth is the dashboard's, unchanged**: the router's forward-auth to `/auth`
(`scripts/gateway_auth.py`) — the client certificate, or a basic-auth user,
host-scoped by `GATEWAY_BASIC_AUTH_SCOPES`. Scoping works per host, so a
deployment that wants to hand a sync app or scanner a credential that opens the
share and nothing else gives the share a host name of its own (a Traefik router
with `Host(…) && PathPrefix(`/inbox`)`) and scopes that user to it — commented
example in `docker-compose.override.example.yml`. Nothing here adds a login.

The share refuses hidden names (any segment starting with a dot — so its
`.inbox.json` is out of reach, and no dot-manifest can be planted) and the
names the framework reads (`letterbox.DENIED_NAMES`: `CLAUDE.md`, `AGENTS.md`,
`requirements.txt`, `accepted-requesters.txt`, …; docs/scheduling.md,
"Reserved names"), `..` and symlinks — all 403 — and answers every GET as a
sandboxed download
(`application/octet-stream`, `attachment`, `nosniff`), so an uploaded HTML file
never runs on the dashboard's origin. An upload is written to a hidden sibling
and renamed into place when complete, so the intake watcher never sees half a
file. Every request gets an answer: a malformed header is a 400, a full
letterbox volume a 507, a read-only one a 403. Changes to the share are Tier 3 like the rest of the gateway's serving
logic.
