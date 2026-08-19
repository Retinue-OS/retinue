---
name: dashboard-composing
description: >
  Linking and reply-chip principles for any text shown in the Retinue dashboard
  — a conversation reply as Ara, a thread opened or appended via
  conversation-push.py (triage proposals included), and curated card content.
  ALWAYS use before composing such text when it asks the user to choose between
  options (every option gets a click-to-fill chip), refers to an e-mail without
  showing it in full (add a details chip), mentions a GitHub pull request or issue
  (the label itself links to it), or contains any URL, absolute or relative
  (never shown bare — always a labeled Markdown link).
---

# Composing for the dashboard: links and reply chips

The dashboard renders everything through one shared Markdown renderer
(`webapp/components/markdown.js`). Two inline affordances carry these
principles:

- **Links** — `[label](url)`: navigate. Standard Markdown; opens in a new tab.
- **Reply chips** — `[[chip: Label | prefill text]]`: a click-to-fill button
  styled as an inline link. Clicking one drops the *prefill text* into the
  composer for the user to review and send themselves — it never auto-sends.
  Use chips to offer replies without the user typing.

The dashboard is used on a phone: every tap saved matters, and long raw URLs
wrap over several lines and are unreadable when a thread is skimmed or read
aloud. Hence the four principles below.

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

## 4. Never show a URL as text — a relative one least of all

A URL is a **link target, never display text**. The renderer auto-links bare
URLs, but the full address then appears (and gets read aloud) as text — so
don't rely on that. Always write `[label](url)` with a short human label:

- `[the invoice PDF](https://…)`, not `https://gateway.example.com/conversations/42ec…/attachments/9f31…`
- `[Zimmerberg Trophy](https://…)`, not the registration portal's raw URL
- Applies everywhere the dashboard renders Markdown: conversation messages,
  pushed threads, project pages, news notes.

A **root-relative URL** — `/gateways`, `/sends`, `/news.html` — is the worse
case, not the milder one: it is still a URL, only missing its host, and the
renderer does not auto-link it. So it arrives as inert text the user cannot
tap and has to translate into a navigation step. Never name a dashboard page
by its bare address; link it by name, resolving the host from the deployment's
own setting (`CONVERSATION_BASE_URL`, or the `SEND_APPROVAL_BASE_URL` chain in
scripts) rather than guessing it:

- `Open [the gateways page](https://…/gateways)`, not `Open /gateways`
- `approve it on [the sends page](https://…/sends)`, not `see /sends`

This is about **URLs**, not about anything that starts with a slash. A
filesystem path is not a URL and is not covered: when the user asks where a
config file, script or chamber lives, `/workspace/scripts/gateway-monitor.py`
is the answer, written as text (in backticks) — there is nothing to link it to.

The rule binds **generated** text too. Anything a script posts into a
conversation — `gateway-monitor.py`, `recurring-projects.py`, any
`conversation-push.py` caller — is dashboard text under it, so a bare address
leaking into such a notice is fixed in the script that composes it, not by
hand afterwards.

## Chip mechanics and limits

- Syntax: `[[chip: Label | prefill text]]` — whitespace around each part is
  trimmed; matching is case-insensitive on `chip:`.
- The **label** may contain neither `|` nor `]`; the **prefill** must be a
  single line and may not contain `]]`. Keep labels to a word or two.
- Chips render as inline links (deliberately not as buttons): clicking only
  fills the composer. There is no chip that acts immediately.
- Chips are functional **in conversation bubbles only** — the click-to-fill
  handler lives in the conversations component. Don't emit chips into project
  pages or other non-conversation surfaces; use plain prose or links there.
- Chips are dashboard-only markup. Text that also goes out on other channels
  (Signal, WhatsApp, Telegram, e-mail) must not contain chip tokens — recipients
  there see them as literal brackets.
