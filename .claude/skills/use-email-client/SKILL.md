---
name: use-email-client
description: >
  Reference for working with the provider-independent email client
  (scripts/email_client.py). Use this skill whenever reading, replying to,
  forwarding, or managing email — regardless of which agent or account is
  active. Covers command syntax, reply threading, the send policy, and where
  the mail gateway's mirror (mail threads as chats) takes over.
---

# Using the Email Client (`email_client.py`)

The script `/workspace/scripts/email_client.py` is the unified IMAP+SMTP client
for all agents. It returns JSON and has no external dependencies.

Account names and connection details come from environment variables. Each
account is configured in the system-wide `.env` file using an upper-cased suffix
and selected with `--account NAME`:

```bash
# Use the named "ari" account (EMAIL_USER_ARI, EMAIL_PASS_ARI, …)
python3 /workspace/scripts/email_client.py --account ari list --folder INBOX
```

Credentials defined this way are covered by the credential isolation described
below. Avoid `--env-file` / `EMAIL_ENV_FILE`: a file loaded that way is readable
by the agent and bypasses the isolation, giving it direct SMTP/IMAP access and
allowing it to circumvent the send-control policy.

## Mirrored threads: send with `mail-push.py`

Where the **mail gateway** mirrors the mailbox (docs/messaging.md, "E-mail (the
mail gateway)"), each mail thread is a chat. **A mail you reach as a chat** —
you hold its chat id (a mail chat's companion thread names it, and the command
to send with) — is answered, and a new mail from the account the gateway
serves is written, with `scripts/mail-push.py`, the sibling of the messenger
push CLIs, not with this client:

```bash
python3 /workspace/scripts/mail-push.py --chat <chat id or key> --reply all "..."   # or --reply sender
python3 /workspace/scripts/mail-push.py --to a@b.ch --subject "..." --attach /tmp/x.pdf "..."
python3 /workspace/scripts/mail-push.py --retract <id>
```

(`--reply-to <token>` also exists, for a gateway prompt that carries a token;
nothing issues one for mail yet, so do not look for one.) It goes through the
same `EMAIL_SEND_POLICY` (below) but a different queue: a `verify` send waits
in the gateway's own pending store, shows in the chat as a pending bubble and
is taken back with `mail-push.py --retract`, whereas a `send`/`reply` of this
client waits as an IMAP draft and is taken back with `retract --request-id`.
Both are approved on `/sends`.

Do not guess a chat id from a UID: holding only a UID, answer with
`email_client.py reply --uid <UID>` (below).

Use this client for everything else too — reading, searching, flags, moving,
drafts, attachments of a mail you have a UID for, and an account the gateway
does not serve. A mail sent with it from a mirrored account still lands in its
chat once it is in the Sent folder. Neither send makes the recipient known to
the gate: whose mail is worked on arrival is decided by a contact's VIP flag or
instruction alone (`contacts.py`, docs/contacts.md).

---

## Command reference

```bash
# List folder contents
python3 /workspace/scripts/email_client.py list --folder INBOX --limit 20

# Search (flags are combinable)
python3 /workspace/scripts/email_client.py search --folder INBOX --unseen
python3 /workspace/scripts/email_client.py search --folder INBOX --from user@example.com --subject "Subject"

# Read a message (does NOT automatically mark it as read)
python3 /workspace/scripts/email_client.py read --uid <UID>

# Read: `body` is a rendered plain-text version; an HTML-only sender's
# <a href> targets that aren't already implied by their visible text are
# folded in as `label <url>` right there in the text, and the same targets
# are always listed separately in the `links` array — so a link-only call to
# action (a "Rechnungskopie einsehen" button with no other clue to the URL)
# never gets lost in rendering, and losing one would show up as an empty
# `links` array. Add --html for the text/html part verbatim (null if the
# message has none) or --raw for the original MIME source, base64-encoded,
# when even that isn't enough:
python3 /workspace/scripts/email_client.py read --uid <UID> --html
python3 /workspace/scripts/email_client.py read --uid <UID> --raw

# Mark as read
python3 /workspace/scripts/email_client.py flag --uid <UID> --read

# Download an attachment. Without --out the file is saved in the working
# directory under the base name of the attachment's own name (a path the sender
# wrote into it is dropped), and an existing file is never overwritten — the
# command fails instead, so pass --out.
python3 /workspace/scripts/email_client.py fetch-attachment --uid <UID> --part 1 --out /tmp/file.pdf

# Move a message
python3 /workspace/scripts/email_client.py move --uid <UID> --from INBOX --to "Archive/Folder"

# Reply to a message — threading headers are derived from the source (preferred; see below)
python3 /workspace/scripts/email_client.py reply --uid <UID> --body "..."

# Send a raw message (no source to thread against; set threading by hand if it's a reply)
python3 /workspace/scripts/email_client.py send \
  --to <addr> --subject "Re: <original subject>" --body "..." \
  --in-reply-to "<message_id>" --references "<message_id>"

# Forward with original attachments — goes through the same send-control gate as
# send/reply (verify/trust/allow, below), and renders an HTML-only source body as
# plain text before quoting it.
python3 /workspace/scripts/email_client.py forward --uid <UID> --to <addr> --prepend "FYI ..."

# Save as draft (without sending)
python3 /workspace/scripts/email_client.py draft \
  --to <addr> --subject "Re: <original subject>" --body "..." \
  --in-reply-to "<message_id>" --references "<message_id>"

# Use a named account (suffix-based env variables, e.g. EMAIL_USER_ARI)
python3 /workspace/scripts/email_client.py --account ari list --folder INBOX
```

---

## Send control: sender-address categories (verify / trust / allow)

Outgoing mail is **not** sent on the model's judgement alone. Every `send` is
governed by the **control category of its sender address**, configured in the
`EMAIL_SEND_POLICY` environment variable (a JSON array of
`{address, category[, account]}`). The active account's address determines the
category:

| Category | Behaviour of `send` |
|---|---|
| `allow` | Sends directly, no confirmation (e.g. an agent persona's own mailbox). |
| `trust` | Sends directly **only** when you pass `--user-approved` (you assert the user approved this exact send). Without that flag it falls back to the `verify` flow. |
| `verify` | **Never** sends directly. Saves the message as a pending draft and returns an `approval_url`; the user approves or denies it on the web gateway. |

`reply` and `forward` route through the same choke point as `send`
(`_dispatch_message`), so the table above governs all three, not just `send`.

Addresses not listed fall back to a `"*"` wildcard entry, or — absent that — to
`verify` (the fail-safe default).

```bash
# trust address: assert the user approved this send
python3 /workspace/scripts/email_client.py send \
  --to a@b.ch --subject "Re: ..." --body "..." --user-approved

# verify / trust-without-flag: returns {"pending": true, "approval_url": "...", "request_id": "..."}
python3 /workspace/scripts/email_client.py send --to a@b.ch --subject Hallo --body "..."
#   → share the approval_url with the user (e.g. via scripts/signal-push.py)

# List the pending send requests (analogous to the web list page)
python3 /workspace/scripts/email_client.py pending

# Retract a pending request you created (deletes the draft, nothing is sent)
python3 /workspace/scripts/email_client.py retract --request-id <ID>
```

`--user-approved` is **only** honoured for `trust` addresses. It is ignored for
`allow` (already direct) and is insufficient for `verify` (always web approval).
Only set it when the user has actually approved the concrete message — never to
bypass review.

The user approves/denies pending sends on the gateway: `GET /sends` lists them,
and each request page (`/sends/<account>/<request-id>`) has **Allow / Deny /
Skip** buttons. Approval sends the draft and removes it from Drafts (it lands in
Sent); Deny deletes it; Skip moves to the next request, leaving this one pending.
Approval happens on the web gateway's `/sends` page — there is no CLI
`approve` command. Don't work around that to approve your own pending send;
the point of `verify` is that the user takes this step.

Because a pending draft is parked in the provider's IMAP Drafts folder, it makes
a round trip through that store — and some providers stamp their own headers on
it there. Approval therefore drops the headers named in `SEND_STRIP_HEADERS`
(default `X-ZohoMail-Sender`) before submitting; the result reports them in
`stripped_headers`. This is why an approved send and a direct send now produce
byte-identical messages. If a **verify** send bounces where an **allow** send of
the same text goes through, suspect an injected header and compare the two raw
messages before looking anywhere else.

### Credential isolation

In remote-control mode you run with **no mailbox credentials**: `email_client.py`
transparently proxies every command to the web gateway, which holds the
credentials and runs the real IMAP/SMTP. (This is gated by `EMAIL_BACKEND_TOKEN`,
which the entrypoint auto-generates when not supplied, so the isolation is always
on.) You don't need to do anything differently — all commands above work
unchanged — but you cannot read `EMAIL_PASS*` or talk to SMTP/IMAP directly to
bypass the send-control policy. This holds in every session alike — dashboard
turns, scheduled jobs, triage, Ask-Ara answers — because each is spawned with
the allowlisted environment of `scripts/session_env.py`, which carries the
backend token and URL but never a mailbox credential. The one exception is a
mailbox loaded via `--env-file`, whose credentials sit on disk and are
therefore not isolated.

---

## Replies: use `reply --uid`, don't hand-set threading headers

**To answer a message you have a UID for, use `reply` — not `send`.** `reply`
fetches the source message and derives everything a correctly-threaded reply
needs from it: `In-Reply-To` (the source Message-ID), `References` (the source's
own chain plus its Message-ID), the recipient (source `Reply-To`/`From`), and the
subject (`Re: …`). This removes the one manual step that has been silently
skipped in the past — a reply sent without `In-Reply-To` breaks the thread on the
recipient's side **and** in the mirror: threading follows the headers and
nothing else, so such a reply starts a chat of its own instead of joining its
thread, and the mail it answers stays unread there.

```bash
# The whole reply, threaded automatically:
python3 /workspace/scripts/email_client.py reply --uid 1234 --body "Hi, ..."

# Override recipient/subject/attachments if needed; otherwise they come from the source:
python3 /workspace/scripts/email_client.py reply --uid 1234 --body "..." \
  --to someone-else@example.com --subject "Re: something" --attach /tmp/x.pdf
```

`reply` goes through the **same send-control policy** as `send` (verify / trust /
allow) — a `verify` sender still returns an `approval_url`, `--user-approved`
still only counts for `trust`. On a direct send it returns `"sent_uid"` (the
Sent-folder UID) and `"message_id"` of the outgoing mail, so the caller can
record them and later verify the reply really went out.

**Only hand-build a reply with `send --in-reply-to`/`--references`** when there
is no source UID to reply to — e.g. threading onto a message you know the
Message-ID of but don't have in a folder. In that case pass all previous IDs in
`--references` with the new `message_id` last.

---

## Contacts: who a sender is, and a person's address

E-mail addresses are handles in the channel-independent address book
(`scripts/contacts.py`, docs/contacts.md): one person per file, stored in a
chamber, with every channel that reaches them. Use it both ways:

```bash
# Who wrote this? (the sender's address, any case)
python3 /workspace/scripts/contacts.py find --email mara@example.org
# What is Mara's address? (names and addresses are both searched)
python3 /workspace/scripts/contacts.py find --name mara
# File a new correspondent — a contact always names the chamber it is kept in:
python3 /workspace/scripts/contacts.py add --chamber <chamber> --name "Mara Keller" --email mara@example.org
# Add an address to someone the book already knows (e.g. from a Signal chat):
python3 /workspace/scripts/contacts.py update <id> --add-email mara@work.example
```

Pick the chamber from `contacts.py locations` and the context (whose sphere the
person belongs to); ask the user when that is not clear. An address belongs to
one person only, so `add` refuses one that is already filed; `find` first.

---

## Read state: the chat's, not the mailbox flag's

For an account the **mail gateway mirrors**, whether a mail has been dealt
with is the chat's business: a reply — from the chat, from `mail-push.py`,
from this client or from the user's own mail program — moves the thread to
*Read*, as does the user's **Mark as read**. The mailbox follows the chat,
never the reverse (docs/mail-chats.md, "The mailbox follows the chat"): the
mirror ignores a `\Seen` set from outside, so `flag --read` on a mirrored
account records nothing the system reads. Do not use it as bookkeeping there,
and do not read "unread in the mailbox" as "not handled" — nothing keeps a
status per mail any more (e-mail triage and its status store are retired;
docs/mail-chats.md).

**An account the gateway does not serve** — an agent persona's own mailbox,
say — has no chat and no mirror. An agent that works such a mailbox by its
unread flag marks a message read **before** it sends the reply, not after:

```bash
python3 /workspace/scripts/email_client.py read --uid <UID>
python3 /workspace/scripts/email_client.py flag --uid <UID> --read     # now, before sending
python3 /workspace/scripts/email_client.py reply --uid <UID> --body "..."
```

If the session dies between the two steps, marked-but-not-sent is noticed (no
reply in Sent) and repaired by hand; sent-but-not-marked is answered a second
time by the next run.

---

## Configuration variables

| Variable | Meaning | Default |
|---|---|---|
| `EMAIL_USER` | Login address | — |
| `EMAIL_PASS` | App password (never the regular account password) | — |
| `IMAP_HOST` | e.g. `imap.gmail.com` / `imap.zoho.eu` | — |
| `IMAP_PORT` | Implicit TLS | `993` |
| `SMTP_HOST` | e.g. `smtp.gmail.com` / `smtpout-mail.zoho.eu` | — |
| `SMTP_PORT` | STARTTLS | `587` |
| `SENT_FOLDER` | IMAP folder for sent copies | `Sent` |
| `DRAFTS_FOLDER` | IMAP folder for drafts (also the pending-send store) | `Drafts` |
| `SMTP_SAVE_SENT` | Append a copy after sending | `true` (for Gmail: `false`, Gmail saves automatically) |
| `EMAIL_SEND_POLICY` | JSON array mapping sender addresses to `verify`/`trust`/`allow` | unset → all addresses `verify` |
| `SEND_APPROVAL_BASE_URL` | Public base URL for approval links (falls back to `CONVERSATION_BASE_URL`) | — |

For named accounts: suffix versions, e.g. `EMAIL_USER_ARI`, `EMAIL_PASS_ARI`.
A named account defines its own identity in full: the client never falls back
to the unsuffixed variables for it, and a missing required one is an error.
