# Mail threads as chats

E-mail gets the surface the messengers have: a mail thread is a **chat** on the
Chats page and the chat page, with the companion pane beside it. A **mail
gateway** mirrors the mailbox into the same ledger the messenger gateways
write, e-mail triage and its whitelist retire in favour of instructions on
contacts, and the Chats page sorts every channel's chats into **Unread · Read ·
Archived**.

This document is the contract for that build. Read `docs/messenger-chats.md`
first: a mail chat is a chat in every sense that document defines, and only
what differs is stated here.

**Status.** Built in five phases, each shippable alone:

| Phase | What ships |
|---|---|
| **Three groups** | Unread · Read · Archived for every channel; no read-on-open; *Mark as read* |
| **1 — read-only mirror** | the mail gateway, mail chats rendered, the deterministic trim, the original view |
| **2 — writing** | reply and compose under the send policy; the mailbox follows the chat |
| **3 — the gate** | VIP / instructed turns for mail; triage and the whitelist retire; the migration |
| **4 — the model trim** | one router-tier turn per mail the deterministic trim could not settle |

## Decisions

The brief left four things open. What was decided, and why:

1. **One thread on two of the user's accounts stays two chats.** A chat is
   `<channel>:<account>:<key>` on every channel, the ledger and the send route
   are per account, and a reply must leave from the address the mail reached.
   Where two rows carry the same name the account is shown beside it, as it is
   for a messenger peer on two accounts.
2. **A chat whose last mail was deleted in a client is archived, not erased.**
   Its bubbles are tombstones, its companion thread and its state stay, and it
   leaves the Unread and Read groups: nothing in it can be read or answered any
   more, so it must not ask for attention, and erasing it would destroy the
   companion's record of what was done about it. A later mail in the same
   thread brings it back like any archived chat. Deleting it for good is the
   user's own two-tap delete.
3. **The unread count badge stays.** The group says *whether* a chat wants
   attention, the badge *how much* has arrived since the user last dealt with
   it — one new mail and twelve read differently. Rows in *Unread* carry the
   count; the *Unread* tab carries the number of unread chats.
4. **The generic instruction** given to every migrated whitelist address is

   > See whether this belongs to a project and link it; extract the facts
   > worth keeping; determine importance and urgency.

   **"Link it to a project" belongs in it.** Linking is what triage's Phase 3
   did for every substantive mail, the companion turn has the same means (a
   line in the project's log and a link triple naming the message by
   `mid:<message-id>`), and leaving it out would silently drop the one piece of
   filing triage did for every correspondent.

Further decisions the build had to take:

- **Backfill starts from the mailbox's `\Seen`.** A mail that was already read
  in a client before the mirror first saw it starts read; without that, a
  mailbox of ninety days would arrive as ninety days of unread chats. From
  then on the mailbox follows the chat and a client's `\Seen` is ignored.
- **A send from the composer goes through the send policy too.** On the
  messengers the user's send press is the approval. On e-mail a send under
  `verify` queues even when the user typed it, and the chat shows it pending
  with its approval link; under `trust` and `allow` the user's press sends.
- **A `news` mail that continues a mirrored thread joins it.** A `news` sender
  or list never *starts* a chat. A list mail whose `References` reach a chat
  that already exists — normally because the user wrote in that thread — is
  filed to the feed *and* shown in that chat, since it is the answer to the
  user's own post.
- **Mark as read offers Undo; it does not un-see the mailbox.** The swipe is
  easy to do by accident. Undo restores the previous read mark in the chat
  state; `\Seen` stays set.
- **The bubble text is derived, the original is immutable.** `kb:text` holds
  the trimmed reading text; the mail's own parts — headers, plain body, HTML
  body, attachments — are stored once as blobs and never rewritten. A better
  trim replaces `kb:text` and nothing else.

## A thread is a chat

### Identity

The channel is `email`. The chat key is the **thread root's `Message-ID`**,
without angle brackets, so a mail chat is

```
email:~<account address>:<root message-id>
```

(`chat_state.make_chat_id`). The account is the mailbox address the gateway
serves.

### Threading (`scripts/mail_threads.py`)

Pure, stdlib-only, no I/O beyond the index it is handed. Threading follows the
headers and nothing else — **no subject-only threading**.

For a mail with id `M`, its `References` list `R1 … Rn` (in header order) and
`In-Reply-To` `P`, the *cited ids* are `R1 … Rn, P` with duplicates dropped and
order kept. The index maps every id ever seen or cited, per account, to a chat
key.

1. Look up `M` and every cited id in the index.
2. **None known** — the mail starts a chat. Its key is the *declared root*: the
   first cited id when there is one, else `M`. A reply whose parent was never
   mirrored therefore starts a chat keyed by the root it names, and the parent
   — arriving later, out of order or by backfill — lands in that chat with no
   merge at all.
3. **One chat known** — the mail joins it.
4. **Several chats known** — they are one thread: all merge into the chat
   whose earliest mail is oldest (ties: the smaller key). This is the case of a
   client that sent only `In-Reply-To`, or cut `References` short, so two
   chats were started before the mail that links them arrived.
5. `M` and every cited id are then mapped to the surviving key.

```python
def normalize_id(raw) -> str                 # "<a@b>" → "a@b"; "" for junk
def cited_ids(references, in_reply_to, message_id="") -> list[str]
def base_subject(subject) -> str             # strips Re:/Fwd:/Fw:/AW:/WG:/… repeatedly
class ThreadIndex:                           # sqlite3, one file per account
    def resolve(self, message_id, cited, date=None, subject=None) -> Resolution
    #   Resolution(chat, merged, new, repeat) — records M and the cited ids;
    #   `merged` lists the keys absorbed into `chat`, `repeat` a mail seen before
    def chat_of(self, message_id) -> str | None
    def members(self, chat) -> list[str]     # the chat's mails, oldest first
    def subject_of(self, chat) -> str        # base subject of its earliest mail
    def dates(self, chat) -> tuple           # (earliest, latest)
    def forget_chat(self, chat) -> None      # after a delete
    def rebuild(self, records) -> dict       # from the ledger; {ledger key: survivor}
```

A **merge** re-keys the absorbed chat: the gateway rewrites `kb:chat` on its
records and tells the web gateway (`POST /internal/chats/merge`), which folds
the absorbed chat's state into the survivor — read mark, flags, draft when the
survivor has none, and the companion thread when the survivor has none (else
the absorbed companion becomes an ordinary conversation linked to the
surviving chat, so nothing said in it is lost).

### What a chat shows

- **Name**: the *first* subject of the thread with `Re:`/`Fwd:`/`AW:`/`WG:` and
  their kin stripped (`base_subject`); "(no subject)" when empty.
- **Participants**: every address on `From`/`To`/`Cc` across the thread other
  than the account's own, resolved through the address book
  (`ContactBook.find("email", address)`); the display name the mail carried
  otherwise, else the address.
- **Mark**: an `@` channel mark.
- **Group**: more than one participant makes a group chat — sender labels on
  inbound bubbles, reply-all by default.
- The account is shown where names repeat, as on every channel.

A mail chat's *name* is its subject, but the attention model and the contact
card are about a **person**. So the chat state keeps both: `name` (the subject)
and `peer` (`{address, name}` of the latest inbound sender, or the first
recipient for a thread the user started). For a chat that is not a group the
contact card and the autolink work on `("email", peer.address)`; the attention
item's sender is the person's name, so priors, permits and spheres follow the
person and never the subject.

### What never becomes a chat

- A mail from a **`news`** sender or carrying a `news` **`List-Id`**
  (`triage_policy.email_gate_decision`) starts no chat. The news rail takes it
  as before. (It joins a chat only under the rule in *Decisions*.)
- Pending-send drafts, and anything outside the mirrored folders.

`ignored` and `quieted` keep their meaning: the chat shows the mail silently,
and neither stops a VIP's or an instructed sender's turn.

The user's own sent mail is in the chat, whichever client sent it.

## The mail gateway (`scripts/mail-gateway.py`)

A sibling of the messenger gateways (`scripts/sms-gateway.py` is the model),
one account per container, and the **single writer of the mail ledger**. It
holds the mailbox credentials in its own container — the same variables
`email_client.py` reads (`EMAIL_USER`, `EMAIL_PASS`, `IMAP_HOST`, `IMAP_PORT`,
`SMTP_HOST`, `SMTP_PORT`, `SENT_FOLDER`, `EMAIL_FROM_NAME`, `SMTP_SAVE_SENT`,
`EMAIL_SEND_POLICY`), with `MAIL_ACCOUNT_NAME` selecting a named account's
suffixed set — and reuses `email_client.py` as a library for IMAP, MIME and
SMTP. `email_client.py` itself, the CLI and its backend proxy stay as they
are.

| | |
|---|---|
| Compose service / slug | `mail-gateway` (port 8096) |
| Channel | `email` |
| Ledger volume | `messenger-email` → `/inbound` (gateway, RW), `chambers/_generated/messenger/email` (retinue, RW), qlever-life (RO) |
| State volume | `mail-data` → `/root/.local/share/mail` (thread index, mailbox index, pending sends) |
| Token | `MAIL_GATEWAY_TOKEN`; erase capability `CHAT_ERASE_TOKEN` |
| Registry | a built-in channel: `MAIL_GATEWAY_BASE_URL` enrols it |

The registry (`messenger_gateways.py`) learns that a channel's name and its
gateway's name can differ: each entry carries its `channel`, and the web
gateway reads that before it falls back to reading a slug.

`/health` answers `{configured, connected, mode: "inbox", account, error,
needs_repair: false, build, backfill: {done, pending}}`. An unconfigured
gateway (`configured: false`) idles and is not an outage.

### The mirror

**The mailbox is the source of what exists; the mirror follows it.** The
gateway polls (`MAIL_POLL_SECONDS`, 60) the mirrored folders — **INBOX**,
**Sent** and the **archive folder** (`MAIL_ARCHIVE_FOLDER`, default
`Archive`) — and keeps a small index of where each mail lives
(`folder, UIDVALIDITY, UID → Message-ID`).

- **A new mail in INBOX** becomes a `kb:InboundMessage`; **a new mail in Sent**
  a `kb:OutboundMessage`. A mail the gateway sent itself is already on record
  under its `Message-ID` and is not written twice; any other mail in Sent is
  the **own-device echo** (`author: device`) unless an outbound note names its
  author (see *Writing*).
- **The archive folder is only followed, never read for new mail**: a mail
  that moves there from INBOX is still that mail.
- **A mail that is in none of the mirrored folders any more** — deleted or
  moved away in a client — is removed from the mirror on that poll and leaves
  a **tombstone**: the record keeps its identity (chat, sender, time,
  `Message-ID`), loses its text, parts and attachments, and gains
  `kb:removedAt`.
- **A client's `\Seen` changes are ignored.**
- **Backfill**: on first run, INBOX and Sent for `MAIL_BACKFILL_DAYS` (90),
  merged and ingested **oldest first**, so parents arrive before replies.
  Backfilled mail is offered to the rail as `backfill: true`: it pushes
  nothing, starts no turn, and carries `seen` so the read mark can start from
  the mailbox.

Nothing is reduced to text. Every MIME part is kept:

| Part | Kept as |
|---|---|
| the plain text | blob, `kb:bodyPart` |
| the HTML | blob (`text/html`), `kb:htmlPart` |
| the header block | blob, `kb:headerPart` |
| attachments, and inline (`cid:`) images | blobs, `kb:attachment`, with the content type the sender declared, the file name, and `kb:contentId` for an inline one |

Blobs live in the ledger's media store and are served by the gateway's
token-gated `GET /media/<id>`, proxied by the web gateway — exactly as a
messenger gateway keeps a photo. Tracking pixels are not attachments: a remote
image is never fetched, and an inline image of two pixels or fewer a side is
dropped.

### The record

The ledger (`scripts/inbound_store.py`) learns the predicates a mail needs.
All are optional, round-trip through every rewrite, and mean nothing on other
channels:

| Predicate | Value |
|---|---|
| `kb:subject` | the mail's own subject, decoded |
| `kb:senderName` | the display name on `From` |
| `kb:recipient` (multi) | `To` addresses |
| `kb:cc` (multi) | `Cc` addresses |
| `kb:inReplyTo` | the parent's id |
| `kb:references` | the `References` ids, space-separated, in header order |
| `kb:listId` | the normalised `List-Id` |
| `kb:bodyPart`, `kb:htmlPart`, `kb:headerPart` | media IRIs (see above) |
| `kb:contentId` (on an attachment IRI) | the part's `Content-ID` |
| `kb:trim` | `deterministic`, `wanting` (a model trim is due on first view) or `model` |
| `kb:removedAt` | when the mail was found gone from the mailbox |

`kb:text` is the **trimmed** text — what the bubble, the list preview, the
companion and a SPARQL reader all want. `kb:sender` is the `From` address
(inbound) and `kb:messageId` the `Message-ID` without brackets.

New store operations: `update_message` learns `chat` and `trim`;
`rekey_chat(store, chat, account, into)` re-keys a whole chat for a merge;
`tombstone_message(path, removed_at)` strips a record to its identity (and the
threading ids, so an index rebuilt from the ledger still knows what the gone
mail cited) and unlinks its blobs; `delete_chat` also unlinks the part blobs;
`iter_records(store)` reads the ledger back for an index rebuild. A triple the
parser does not know is kept verbatim through every rewrite.

## Trimmed bubbles, the original one tap away

A mail is shown like a message: **its own new text**.

### The deterministic trim (`scripts/mail_trim.py`)

Pure, stdlib-only, language-agnostic by mechanism (structure and markers, never
a word list in one language where a structural test will do; where words are
needed — "wrote", "From:", "Sent from my" — the lists are multilingual by
design and extended as data).

```python
def trim(plain, html, subject=None) -> TrimResult
#   TrimResult(text, state: "deterministic" | "wanting", removed: dict)
def html_to_text(html) -> str
def sanitize_html(html, cid_map=None) -> str   # a whole document, for the Original view
def is_tracking_pixel(width=None, height=None, attrs=None) -> bool
```

The subject matters for one case: a forward that carries no marker of its own
(Outlook) looks exactly like a reply, and only its `Fwd:` prefix says the
history below *is* the content.

It removes, in this order: quoted history (`>` blocks and their attribution
line, "On … wrote:" in its common forms, Outlook's `-----Original Message-----`
and `From:/Sent:/To:/Subject:` header blocks, `gmail_quote` / `blockquote` /
`#divRplyFwdMsg` in HTML), signatures (the `-- ` delimiter, "Sent from my …"),
footers and disclaimers (confidentiality notices, unsubscribe blocks),
and tracking pixels. Inline images that belong to the text stay, as media.

The state is `wanting` when the mail is HTML-only, or when what is left is
still long and looks unsettled (more than `MAIL_TRIM_WANTING_CHARS` after the
trim, or a quote marker survived). An empty result falls back to the untrimmed
text: a bubble is never blank because the trim was greedy.

### The model trim (phase 4)

Where the state is `wanting`, **one turn on the router-tier model**
(`RETINUE_ROUTER_MODEL`) returns the content as plain text. It runs **on first
view** — when the chat page first asks for that message — never on ingest and
never on backfill, and its result is cached on the record (`kb:text`
replaced, `kb:trim "model"`), so it runs once per mail.

The mail is text from outside. The turn has no tools and no session; its
prompt hands the mail over as data. Its answer is accepted only if it is a
*reduction* of the mail — every line of it occurs in the mail's own plain
text, whitespace aside — so nothing a mail says can add words to its own
bubble. An answer that fails the check, an error or a timeout leaves the
deterministic text in place and marks the record `deterministic`, so it is
not retried on every view.

### The original

Every mail bubble offers **Original**. A tap shows the full mail in place:

- the headers (From, To, Cc, Date, Subject, and the full block behind a
  disclosure);
- the HTML part, in a sandboxed frame, or the full plain text when there is
  none;
- every attachment.

`GET /chats/<id>/messages/<message id>/original` answers the headers, the full
text and the attachment list; `…/original.html` serves the HTML part as a
document of its own under a policy stricter than the dashboard's
(`sandbox allow-popups allow-popups-to-escape-sandbox; default-src 'none';
img-src data:; style-src 'unsafe-inline'`), sanitised first (no script, no
event handlers, no forms, no frames, no remote loads) with `cid:` images
inlined as `data:` URLs and links opening in a new tab. Remote images never
load, so a tracking pixel never fires. The chat page's own policy gains
`frame-src 'self'` for that one frame.

**The record is never changed** by viewing, trimming or replying: the original
parts are immutable, and the mailbox copy is untouched.

## Writing (phase 2)

- **Reply.** The composer sends `POST /chats/<id>/send` as on every chat. For
  a mail chat the body may carry `reply: "all" | "sender"` (default `all` in a
  group, and the only meaning in a 1:1). The gateway builds the mail from its
  own records: `From` the thread's account, `To`/`Cc` from the latest inbound
  mail (reply-all: its sender plus everyone on `To`/`Cc` but the account
  itself), `Subject` `Re: <base subject>`, `In-Reply-To` the latest mail's id,
  `References` its chain plus that id.
- **Under the send policy.** The gateway resolves `EMAIL_SEND_POLICY` for its
  own address. `verify` queues; `trust` sends when the request is the user's
  own press (or carries `user_approved`); `allow` sends. A queued send answers
  `202 pending_approval` with `request_id` and `approval_url`, is listed on
  `/sends` through the gateway's `/pending-sends`, and **shows in the chat as a
  pending bubble with its approval link** until it is sent, rejected or
  retracted. The web gateway does not approve a queued mail on the user's
  behalf.
- **New mail.** ＋ on the Chats page offers *E-mail*: recipients, subject,
  text. `POST /chats/compose` hands it to the gateway, which mints the
  `Message-ID` that becomes the new chat's key.
- **Agents** send through the thin `scripts/mail-push.py` (the sibling of
  `sms-push.py`), under the same policy. `email_client.py send/reply` keeps
  working as before; a mail it sends is picked up from the Sent folder, and
  the backend tells the gateway its author (`POST /outbound-note`) so the
  bubble says *Ara* rather than *mail client*.
- **The companion** works as on any chat: it reads the trimmed thread and the
  attachments, stages drafts (`chat-draft.py`), sends only through
  `mail-push.py` under the policy, forks; the quick-pattern chips apply.

### The mailbox follows the chat, never the reverse

| In the chat | In the mailbox |
|---|---|
| **Mark as read**, or a reply | `\Seen` on the thread's mails |
| **Archive** | the thread's INBOX mails move to the archive folder; *unarchive does not move them back* |
| an instructed turn files a document and archives | the same move |
| **Delete** (two taps, the erase capability, same-origin only) | the thread's mails move to the account's **Trash**, then the mirror is erased — nothing is expunged |

The web gateway calls the chat's gateway for each (`POST /chats/read`,
`POST /chats/archive`, `POST /chats/delete`, all `{chat, account}`). A gateway
that does not know the first two answers 404 and nothing is owed: the hooks
are for channels with a mailbox behind them.

## Unread · Read · Archived, for every channel

- **Unread** — the latest inbound message has neither been replied to nor
  marked read.
- **Read** — it has: by a reply from anywhere (the dashboard, the phone, a
  mail client), or by the **Mark as read** button under the latest inbound
  message.
- **Archived** — as before.

**Opening a chat does not mark it read.** A new inbound message moves a read
chat back to Unread.

The read mark is the chat state's `last_read`, now set only by *Mark as read*
and by a reply. Since a reply can reach the ledger without passing the rail (a
backfilled Sent mail), the list derives the mark from both: a chat's unread
count is the number of inbound messages later than
`max(last_read, the chat's latest outbound message)`. The group is that count
(`unread > 0`) and the `archived` flag; `GET /chats` states it as `list_group`
on every chat so no client re-derives it (`group` stays what it was: the
boolean that says a chat is a group chat).

On the Chats page: three tabs, the *Unread* tab with the number of unread
chats. **Swipe right marks as read, swipe left archives**; the tray (Star,
Mute, and on Archived: Restore, Delete) opens by long press, right-click or
the context-menu key. On the *Archived* tab swipe right restores and swipe
left opens the tray, as before.

## The gate (phase 3)

The e-mail path of the delivery gate becomes the messengers' inbound flow
(`docs/messenger-chats.md`, *Inbound flow*):

1. **Persist** — the record, `delivered: false`.
2. **Gate** — the group axis as today (`news` / `quieted` / `ignored`, on the
   `List-Id` or the sender address), the sender axis as **VIP / instructed /
   everyone else**. `contacts.sync_policy` projects the e-mail addresses of
   VIP and instructed people into the `email` channel's policy file exactly as
   it projects messenger handles, and a person's `kb:emailDomain` as a
   wildcard.
3. **Notify the chat rail.** **A mail pushes nothing by default; a contact can
   say otherwise** — VIP rings on arrival, an instruction's verdict decides,
   and a person's importance prior and permits are honoured because the
   attention item's sender is the person.
4. **A model turn only for a VIP or an instructed sender**, in the chat's
   companion, briefed by the instruction, ending in a verdict, with the
   messengers' timeout and failure-rings guarantee.

There is no triage fallback for mail. A mail the rail could not take stays
`delivered: false` and the gateway offers it again on its next poll: the
mailbox is the backlog.

**No daily sweep.** Mail from a sender who is neither VIP nor instructed is
listed under Unread and judged by nobody.

### What retires

The omnibus, the per-mail triage threads, the status store, the frequent and
daily triage runs, the already-answered check, the e-mail whitelist (addresses,
wildcards, auto-add on send, the Sent-folder derivation, the contacts
projection into it). What stays: the news rail (run by the gateway now), the
group flags, `email_client.py`, and triage itself for a messenger message the
chat rail could not take.

### The migration (`scripts/migrate-triage.py`)

Run once (idempotent; a marker records completion), by the entrypoint.

- **Every whitelisted address becomes a contact**, or an address on the person
  who already has it, with the generic instruction. A person who already
  carries an instruction keeps it. A wildcard (`*@domain`) becomes a contact
  for the domain (`kb:emailDomain`).
- **Receipt and filing rules move too.** A sender whose mails triage filed as
  receipts, payslips, statements or invoices gets *"File the document with the
  Archivist, book it, then archive the mail."*
- **`sender:` memories are read once**, written as instruction text on the
  contact they name, and superseded by a memory that says where the rule now
  lives. A memory whose text reads as a message's content rather than the
  user's instruction is left alone and reported.
- **No filing rule lives outside a contact** afterwards.

The scheduled triage jobs live in chambers' `.schedule.json`; the migration
reports any it finds so the operator can remove them, and the gate scripts
they call answer with a notice instead of running.

## Out of scope

Threads with Ara as chats; a people view across channels; clustering; a daily
sweep; changes to the attention model beyond the mail default above.
