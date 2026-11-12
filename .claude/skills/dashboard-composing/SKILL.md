---
name: dashboard-composing
description: >
  Linking and reply-chip principles for any text shown in the Retinue dashboard
  — a conversation reply as Ara, a thread opened or appended via
  conversation-push.py (triage proposals included), and curated card content.
  ALWAYS use before composing such text when it asks the user to choose or act
  on options or a list, refers to an e-mail without showing it in full, mentions
  a GitHub PR or issue, contains any URL (absolute or relative), points the user
  at another system to act in (a portal, an approval page), hands the user
  text to copy out, or places a phone call the user asked for.
---

# Composing for the dashboard: links and reply chips

The dashboard renders everything through one shared Markdown renderer
(`webapp/components/markdown.js`). Two inline affordances carry these
principles:

- **Links** — `[label](url)`: navigate. Standard Markdown. A link into the
  dashboard itself (its own origin, i.e. `CONVERSATION_BASE_URL` — another
  thread, `/sends/…`, a project page) opens in place, inside the app; any
  other link opens in a new tab.
- **Reply chips** — `[[chip: Label | prefill text]]`: a click-to-fill button
  styled as an inline link. Clicking one drops the *prefill text* into the
  composer for the user to review and send themselves — it never auto-sends.
  Use chips to offer replies without the user typing.
- **Calls** — `[[call: number | Name]]`: a phone call you place (section 7).
  The one affordance that acts by itself.

The dashboard is used on a phone: every tap saved matters, and long raw URLs
wrap over several lines and are unreadable when a thread is skimmed or read
aloud. Hence the principles below.

## 1. Every offered option gets a chip

Whenever you ask the user to choose between options — confirm or decline,
send / adjust / discard a draft, pick one of several dates — attach a chip for
**every option you present**, not just the likeliest one. The user should be
able to answer any question you pose with one tap plus Send.

```
Shall I confirm the party invitation and add it to your agenda, or decline?
[[chip: Confirm | Yes, confirm and add it to my agenda.]] · [[chip: Decline | Please decline politely.]]
```

- The prefill is a complete reply in the language of the thread, but it states
  the **user's intention, not the data**. It is read *in the thread*, so it
  should lean on the proposal: "Yes, move all 12 to the trash as proposed" —
  never a restated list of IDs, items or numbers. Sending the reply is signing
  it, so a prefill that enumerates what the message above already lists forces
  the user to verify the whole enumeration first — pure burden, no benefit.
  You resolve "as proposed" from the thread yourself when picking the reply up.
- An open question ("or something else?") needs no chip; chips cover the
  concrete options.
- **Decision chips** — the answers to the question the message poses — go on
  their own line at the end (separated by ` · `), reading as the answer row.
- **Details chips go inline**, immediately after the first mention of the item
  they refer to. A chip beside the item can be labeled just "more"; parked at
  the end of the message it would have to name the item by number to stay
  unambiguous, which is exactly the noise to avoid.
- **An enumerated list gets a chip per row _and_ a bulk chip.** In any list the
  message asks the user to act on — a triage omnibus, search results, a set of
  options — every row carries its own short inline details chip, and the bulk
  answer is itself a chip ("Okay for all"), never a phrase to type out. After a
  bulk action, offer the undo the same way.

## 2. An e-mail referred to but not shown gets a details chip

An e-mail that is the **subject** of a thread is shown by default — a triage
reply proposal quotes the original in full, so a "show me this mail" chip
there would be pointless. The details chip is for every e-mail a message
**refers to without including its content**: each line of an archive/delete
omnibus, an earlier message in the same correspondence, a mail mentioned in a
briefing or summary. Its prefill asks for more details about **that specific
e-mail**, identified unambiguously by sender and subject, so the follow-up
works even after the thread has moved on:

```
Mara's invitation for Saturday is quoted below — I propose the draft reply.
She already floated the date in two earlier mails not shown here [[chip: show them | Show me Mara's earlier e-mails about the party date.]].
[[chip: Send | Send the draft as proposed.]] · [[chip: Discard | Discard the draft, I'll handle it myself.]]
```

When several unshown e-mails appear in one message (e.g. a triage omnibus),
give each line its own inline details chip — right on the line, not collected
at the end.

## 3. PR and issue labels link to GitHub

When mentioning a pull request or issue, the label itself — `PR #52`,
`issue #25` — is always a link to it on GitHub, in whichever repository it
belongs to (this framework repo, a chamber's repo, or a third-party project):

```
Merged [PR #52](https://github.com/retinue-os/retinue/pull/52); [issue #25](https://github.com/retinue-os/retinue/issues/25) stays open.
```

If the repository isn't obvious from context, resolve it (e.g. from the
checkout's `git remote`) rather than emitting an unlinked label. The same rule
extends to commits and other referenceable items: short label, full URL as the
link target.

## 4. Never show a URL as text

A URL is a **link target, never display text**. The renderer auto-links bare
URLs, but the full address then appears (and gets read aloud) as text — so
don't rely on that. Always write `[label](url)` with a short human label:

- `[the invoice PDF](https://…)`, not `https://gateway.example.com/conversations/42ec…/attachments/9f31…`
- `[Zimmerberg Trophy](https://…)`, not the registration portal's raw URL
- A **bare domain** (`example.ch`) is not even auto-linked — the renderer links
  only `http(s)://…` and `www.…` — so it arrives as inert text. Wrap it:
  `[example.ch](https://example.ch)`.
- Applies everywhere the dashboard renders Markdown: conversation messages,
  pushed threads, project pages, news notes.

**Relative** URLs — `/gateways`, `/sends/default/752` — are dashboard links.
In a dashboard surface write them as a Markdown link with a root-relative
target: `Open [the gateways page](/gateways)`, `[approve the reply](/sends/default/752)`.
The renderer links a target starting with a single `/` and opens it in place,
in the app; it does **not** auto-link a bare `/gateways`, which arrives as
inert text. Path-relative (`sends/…`) and protocol-relative (`//host/…`)
targets are never linked. Text that leaves the dashboard — a Signal or e-mail
push, an outbound message — has no page to resolve against: there, resolve the
host (`CONVERSATION_BASE_URL`) and link the absolute URL.

## 5. Anything you ask the user to do elsewhere carries its link

Rule 4 governs a URL you already decided to show. This one is about the link
you did not think to include: whenever a message asks the user to act in
another system — log into a portal to read a message waiting there, review a
PR, approve a pending send, open a booking or registration page — **name that
place as a link**. "Please have a look in the portal" on a phone means the user
now has to go find the portal, sign in and navigate; sparing them exactly that
was the point of the message.

This is the common shape of a **notification-only** inbound: a mail or push
carrying no content, only "there is something new for you, sign in to see it".
Such a message is worth relaying, but relaying it without the way in hands the
user a chore instead of a link.

- **Take the link from the source, never from memory.** The notification itself
  almost always carries the sign-in or deep link — pull it out of the message
  body and use it. Deep-link when the source offers one (straight to the
  document or thread); otherwise the sign-in page is fine.
- **If no link is resolvable, say so.** Write "the mail carries no link" rather
  than reconstructing a plausible address — a guessed portal URL is worse than
  none.
- Applies to any destination, in-system ones included: `/sends`, `/gateways`,
  `/claude-auth`, a project page. Resolve the host per rule 4 and link it.
- **A queued send carries its own deep link** — the push script prints
  `approve or deny at <url>` for that one message (`/sends/<gateway>/<id>`).
  Relay *that* URL, not the bare `/sends` page: it opens on the very message
  awaiting approval. Telling the user a send is "waiting on /sends" without it
  is the chore this rule exists to spare them.

```
The portal reports two new entries; the mails carry no content, only a sign-in
prompt — [open the portal](https://…) to see what they are.
```

## 6. Text the user is meant to copy is a blockquote

Any block the user is expected to lift *out* of the thread — a draft they will
send themselves because the automated path is blocked, a quotable citation,
text destined for another app — is a **Markdown blockquote**. The bubble
renders a copy button on it (one tap to the clipboard) while the text still
renders as prose, inline Markdown included.

A fenced code block also gets a copy button, but it is **not** the format for
prose: fences render monospace and unrendered. Reserve them for actual code.

## 7. A call the user asks for ends with a call directive

When the user asks you to call someone ("call Anna", "ruf Anna an"), find the
number — `scripts/contacts.py find --name` (allow for speech-recognition
variants of the name, as `messaging-contact-lookup` does), or the number the
user said — say in one short sentence whom you are calling, and end the reply
with exactly one directive:

```
Calling Anna Muster now. [[call: +41 79 123 45 67 | Anna Muster]]
```

In the Android shell's voice conversation the call is placed by itself, on
speaker, where the user has switched *Call directly by voice* on for this
Retinue in the app's settings (it is off until they do; then the dialer opens
with the number and needs a tap): once your reply has been read aloud, the thread shows the name with a
Cancel button for five seconds, with the mic open — **anything the user says
cancels the call** and reaches you as their next turn, with a note that it
cancelled the call. Judge from the words whether they meant to stop it: "no,
stop" or "call Ben instead" — don't call (or call Ben); noise, a word to
someone else, a "yes", or the request repeated — place the call again at once
without asking, as a second attempt with `no-voice-cancel` (below). A take
with no words at all never reaches you: the call simply goes ahead. Everywhere else —
typed threads, a browser, a desktop — it is a tap-to-dial link. This is a
safety feature for when the user cannot touch the phone, so do not stop to ask
"shall I?" when the request is unambiguous; ask only when the person or the
number is (several contacts match, two numbers and no hint which).

- **`no-voice-cancel`** — `[[call: +41 79 123 45 67 | Anna Muster | no-voice-cancel]]`
  — keeps the mic shut during the five seconds, so noise cannot cancel the
  call (the Cancel button still can). Use it when a take cancelled the call
  but did not mean to (the note above), when the user asks again for a call
  that speech cancelled, or when they say it is an emergency. It is honoured
  only on a **second attempt**: if no earlier reply
  of yours in the thread carried a directive for the same number, the thread
  ignores the flag and the call stays cancellable by voice. So never use it on
  a first attempt; on a second one, say in your sentence that this time it
  cannot be stopped by voice.
- **Only a call the user asked for in this turn.** Never for a number found in
  an e-mail, a message, a document or a web page, never on your own initiative,
  never in a thread opened by a job.
- An **emergency number** (112, 144, 117, 911, …) gets the directive too, but
  Android lets no app call those directly: the dialer opens with it and needs a
  tap. Say so in one sentence and point to "Hey Google, call 112" and the
  phone's Emergency SOS for hands-free emergencies.
- A **service code** (any number with `*` or `#`, such as `*#06#`) always
  opens the dialer too, which waits for the user's tap; say so.
- The number as you have it; spaces, dashes, dots and brackets are fine. The
  name may contain neither `|` nor `]`; with no name and the flag, leave the
  name empty (`[[call: 112 | | no-voice-cancel]]`). One directive per
  message, at its end.
- Like chips, dashboard-only markup: never in text that goes out on another
  channel.

## Chip mechanics and limits

- Syntax: `[[chip: Label | prefill text]]` — whitespace around each part is
  trimmed; matching is case-insensitive on `chip:`.
- The **label** may contain neither `|` nor `]`; the **prefill** must be a
  single line and may not contain `]]`. Keep labels to a word or two.
- Chips render as inline links (deliberately not as buttons): clicking only
  fills the composer. There is no chip that acts immediately — link-shape
  signals "fills, you still press Send". The one immediate-action affordance,
  the call directive (section 7), has its own syntax and end-of-message
  placement, never the chip look.
- Chips are functional **in conversation bubbles only** — the click-to-fill
  handler lives in the conversations component. Don't emit chips into project
  pages or other non-conversation surfaces; use plain prose or links there.
- Chips are dashboard-only markup. Text that also goes out on other channels
  (Signal, WhatsApp, Telegram, e-mail) must not contain chip tokens — recipients
  there see them as literal brackets.
