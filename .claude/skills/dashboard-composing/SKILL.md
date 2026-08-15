---
name: dashboard-composing
description: >
  Linking and reply-chip principles for any text shown in the Retinue dashboard
  — a conversation reply as Ara, a thread opened or appended via
  conversation-push.py (triage proposals included), and curated card content.
  ALWAYS use before composing such text when it asks the user to choose between
  options (every option gets a click-to-fill chip), mentions an e-mail (add a
  chip to ask for details about it), mentions a GitHub pull request or issue
  (the label itself links to it), or contains any URL (never shown bare — always
  a labeled Markdown link).
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

- The prefill is a **complete, self-contained reply** in the language of the
  thread — it must still make sense as the user's next message after they may
  have edited it.
- An open question ("or something else?") needs no chip; chips cover the
  concrete options.
- Place chips on their own line at the end of the message (separated by ` · `),
  so they read as the answer row, not as part of the prose.

## 2. Mentioning an e-mail? Offer a details chip

Whenever a message mentions an e-mail (a triage proposal, a briefing item, a
thread summary), include a chip whose prefill asks for more details about
**that specific e-mail** — identified unambiguously by sender and subject, so
the follow-up works even after the thread has moved on:

```
Mara asked about Saturday ("Re: Party") — I propose the draft below.
[[chip: Send | Send the draft as proposed.]] · [[chip: Details | Show me the full e-mail from Mara, subject "Re: Party".]]
```

When several e-mails appear (e.g. a triage omnibus), give each line its own
details chip.

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

## 4. Never show a full URL

A URL is a **link target, never display text**. The renderer auto-links bare
URLs, but the full address then appears (and gets read aloud) as text — so
don't rely on that. Always write `[label](url)` with a short human label:

- `[the invoice PDF](https://…)`, not `https://gateway.example.com/conversations/42ec…/attachments/9f31…`
- `[Zimmerberg Trophy](https://…)`, not the registration portal's raw URL
- Applies everywhere the dashboard renders Markdown: conversation messages,
  pushed threads, project pages, news notes.

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
