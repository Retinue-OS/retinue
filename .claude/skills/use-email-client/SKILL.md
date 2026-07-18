---
name: use-email-client
description: >
  Reference for working with the provider-independent email client
  (scripts/email_client.py). Use this skill whenever reading, replying to,
  forwarding, or managing email — regardless of which agent or account is
  active. Covers command syntax, reply threading, and the universal best
  practice of marking messages read before sending a reply.
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

# Mark as read
python3 /workspace/scripts/email_client.py flag --uid <UID> --read

# Download an attachment
python3 /workspace/scripts/email_client.py fetch-attachment --uid <UID> --part 1 --out /tmp/file.pdf

# Move a message
python3 /workspace/scripts/email_client.py move --uid <UID> --from INBOX --to "Archive/Folder"

# Send a reply (with correct threading — see below)
python3 /workspace/scripts/email_client.py send \
  --to <addr> --subject "Re: <original subject>" --body "..." \
  --in-reply-to "<message_id>" --references "<message_id>"

# Forward with original attachments
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
| `allow` | Sends directly, no confirmation (e.g. Ari's own mailbox). |
| `trust` | Sends directly **only** when you pass `--user-approved` (you assert the user approved this exact send). Without that flag it falls back to the `verify` flow. |
| `verify` | **Never** sends directly. Saves the message as a pending draft and returns an `approval_url`; the user approves or denies it on the web gateway. |

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
Approval is **web-only** — there is no CLI `approve` command, so you cannot
approve a pending send yourself.

### Credential isolation

In remote-control mode you run with **no mailbox credentials**: `email_client.py`
transparently proxies every command to the web gateway, which holds the
credentials and runs the real IMAP/SMTP. (This is gated by `EMAIL_BACKEND_TOKEN`,
which the entrypoint auto-generates when not supplied, so the isolation is always
on.) You don't need to do anything differently — all commands above work
unchanged — but you cannot read `EMAIL_PASS*` or talk to SMTP/IMAP directly to
bypass the send-control policy. The one exception is a mailbox loaded via
`--env-file`, whose credentials sit on disk and are therefore not isolated.

---

## Replies: setting threading headers correctly

When replying, `--in-reply-to` and `--references` **must** be set to the
`message_id` of the original message. The `message_id` is included in the JSON
output of `read`. Without these headers the reply will not appear in the correct
thread on the recipient's side.

```bash
# 1. Read the message and note the message_id
python3 /workspace/scripts/email_client.py read --uid 1234
# → in the JSON: "message_id": "<abc123@mail.example.com>"

# 2. Send the reply with threading headers
python3 /workspace/scripts/email_client.py send \
  --to sender@example.com \
  --subject "Re: Original subject" \
  --body "Hi, ..." \
  --in-reply-to "<abc123@mail.example.com>" \
  --references "<abc123@mail.example.com>"
```

When replying to an existing thread where `References` already contains multiple
message IDs, include all previous IDs in `--references` with the new `message_id`
last — the client handles the correct formatting.

---

## Best practice: mark as read before sending a reply

**Always call `flag --read` before sending the reply** — not after.

Reason: if the session crashes between sending and marking, the message will
still appear unread on the next run and will be answered again (duplicate reply).
The reverse — marked but not sent — is a much smaller problem: on the next run
the message is already marked read, but there is no reply in the Sent folder;
this is noticeable and can be handled manually.

```bash
# Correct order:
# 1. Read
python3 /workspace/scripts/email_client.py read --uid <UID>
# 2. Mark as read — NOW, before sending
python3 /workspace/scripts/email_client.py flag --uid <UID> --read
# 3. Send the reply
python3 /workspace/scripts/email_client.py send --to ... --in-reply-to ...
```

This order applies to all agents, regardless of whether they send directly,
save drafts, or do something else.

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

For named accounts: suffix versions, e.g. `EMAIL_USER_ARI`, `EMAIL_PASS_ARI` —
the client falls back to the version without suffix when the suffixed one is missing.
