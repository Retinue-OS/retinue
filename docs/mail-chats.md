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

**Status.** Built in five phases, each shippable alone. All of them are
built: nothing of the design is outstanding.

| Phase | What ships | State |
|---|---|---|
| **Three groups** | Unread · Read · Archived for every channel; no read-on-open; *Mark as read* | built |
| **1 — read-only mirror** | the mail gateway, mail chats rendered, the deterministic trim, the original view | built |
| **2 — writing** | reply and compose under the send policy; the mailbox follows the chat | built |
| **3 — the gate** | VIP / instructed turns for mail; triage and the whitelist retire; the migration | built |
| **4 — the model trim** | one router-tier turn per mail the deterministic trim could not settle | built |

Phase 1's deployment is in place: the `mail-gateway` compose service and
image, its `messenger-email` ledger and `mail-data` state volumes, the
registry entry (`email`, enrolled by `MAIL_GATEWAY_BASE_URL`), the monitor's
wording for a mailbox, and the e-mail policy file on the ledger volume. The
mirror is opt-in: it runs once the operator sets `MAIL_GATEWAY_TOKEN`. How to
run it: *Operator notes* at the end.

Phase 2 ends the read-only mirror and adds to that deployment: the gateway
names `EMAIL_SEND_POLICY`, `SEND_APPROVAL_BASE_URL` / `SEND_APPROVAL_SLUG`,
`CHAT_ERASE_TOKEN` and `MAIL_SENT_GRACE_SECONDS` (`MAIL_TRASH_FOLDER` is read
now), the `retinue` service already carries the send URL, the base URL and the
erase token and now also names the tunables the retinue-side code reads
(`MAIL_SEND_TIMEOUT`, `MAIL_GATEWAY_TIMEOUT`, `CHAT_PENDING_*`,
`MAILBOX_NONE_SECONDS`), and `scripts/mail-push.py` is the agents' way to send
a mail it holds as a chat.

Phase 3 makes the mail gateway mail's delivery gate (*The gate*, below): a
mail that arrives is persisted undelivered, gated and offered to the chats
rail, and the gateway files news mail itself. E-mail triage, its whitelist,
the omnibus, the status store and the already-answered check are retired;
what triage knew lives on contacts (*The migration*). The gateway gains the
news rail (`NEWS_INGEST_URL`, `NEWS_INGEST_TOKEN`) and `MAIL_NEWS_FOLDER`,
`MAIL_NEWS_EXCERPT_CHARS`, `MAIL_RAIL_FILE_BYTES` and `MAIL_RAIL_JOB_TIMEOUT`.
Phase 4 adds the model trim (*The model trim*, below): two endpoints on the
mail gateway (`POST /messages/trim-source`, `/messages/trim-result`), which
reads nothing new, and the turn the web gateway runs on a bubble's first
view, under the `MAIL_MODEL_TRIM*` tunables of the `retinue` service.

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

`References` is the sender's to write, so what one mail can do with it is
bounded. "Oldest" goes by when the mailbox server received a mail, never by
its `Date` header: a backdated mail cannot make itself the root of someone
else's thread. A mail may cite 100 ids (the first and the nearest parents) and
merge at most four chats; one that cites more merges none, joins its nearest
known parent, and the refusal is logged.

```python
def normalize_id(raw) -> str                 # "<a@b>" → "a@b"; "" for junk
def cited_ids(references, in_reply_to, message_id="") -> list[str]
def base_subject(subject) -> str             # strips Re:/Fwd:/Fw:/AW:/WG:/… repeatedly
class ThreadIndex:                           # sqlite3, one file per account
    def resolve(self, message_id, cited, date=None, subject=None) -> Resolution
    #   Resolution(chat, merged, new, repeat, unmerged) — records M and the
    #   cited ids; `merged` lists the keys absorbed into `chat`, `repeat` a mail
    #   seen before, `unmerged` the chats a mail cited too many of to merge
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
  than the user's own — the account's and every other address of the user's
  that may appear on its mail (the aliases on the mailbox *and* the user's
  other mirrored accounts, `MAIL_OWN_ADDRESSES`); else a thread that reaches
  two of the user's accounts would list the user as a correspondent in each,
  turn into a group and make the user its peer. Resolved through the address book
  (`ContactBook.find("email", address)`); the display name the mail carried
  otherwise, else the address.
- **Mark**: an `@` channel mark.
- **Group**: more than one participant makes a group chat — sender labels on
  inbound bubbles, reply-all by default.
- The account is shown where names repeat, as on every channel.

A mail chat's *name* is its subject, but the attention model and the contact
card are about a **person**. So the chat state keeps both: `name` (the subject)
and `peer` (`{address, name}` of the latest inbound sender who is not the
user, or the first recipient for a thread the user started). For a chat that is not a group the
contact card and the autolink work on `("email", peer.address)`; the attention
item is about the person and never the subject — its sender is the contact who
owns the address, else the address itself. Not the display name the mail
carried: anyone can type a name, and keying on it would let a stranger inherit
a known person's priors, permits and spheres.

### What never becomes a chat

- A mail from a **`news`** sender or carrying a `news` **`List-Id`**
  (`triage_policy.email_gate_decision`) starts no chat. The news rail takes
  it — run by the gateway (*The gate*, "News"). (It joins a chat only under
  the rule in *Decisions*, or when its sender is a VIP or instructed: the
  two axes of the gate are independent.) Where no news rail is configured
  (`NEWS_INGEST_URL` emptied) the flag does nothing and such mail is mirrored
  like any other: with no feed to take it, it would be shown nowhere.
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
| Token | `MAIL_GATEWAY_TOKEN` — required: without it the gateway mirrors and serves nothing; the erase capability is `CHAT_ERASE_TOKEN` (`X-Chat-Erase-Token` on `POST /chats/delete`), which no session holds |
| Registry | a built-in channel: `MAIL_GATEWAY_BASE_URL` enrols it |

The registry (`messenger_gateways.py`) learns that a channel's name and its
gateway's name can differ: each entry carries its `channel`, and the web
gateway reads that before it falls back to reading a slug.

`/health` answers `{configured, connected, mode: "inbox", account, error,
blocked, needs_repair: false, build, backfill: {done, pending, days},
waiting: {rail, since, news}}` —
`backfill.days` is the window fixed at the first run; `waiting` says how many
mirrored mails the chats rail has not taken yet (and since when the oldest of
them has been in the mailbox) and how many news mails wait for the feed or for
their move out of the INBOX. `blocked` names what
holds the mirror when that is not the mailbox: `"policy"` (below) or
`"mirror"` — it stopped on a fault of its own, and `error` says on what.
An unconfigured gateway (`configured: false`) idles and is not an outage; so
does one not opted in — no `MAIL_GATEWAY_TOKEN`, or the default account where
`MESSENGER_BUILTIN_CHANNELS` is set without `email` — and `error` says which.
The mirror starts only once the e-mail policy file is in place, since its
first backfill decides at ingest which mail is news. While it waits `blocked`
is `"policy"`: after ten minutes the gateway reports `connected: false` —
nothing is being mirrored — but as a matter of the deployment's setup, with
`error` saying where to look, and the connection monitor words it so, not as
a mailbox that refused the login.

### The mirror

**The mailbox is the source of what exists; the mirror follows it.** The
gateway polls (`MAIL_POLL_SECONDS`, 60) the mirrored folders — **INBOX**,
**Sent** and the **archive folder** (`MAIL_ARCHIVE_FOLDER`, default
`Archive`; unset, the server's own `\Archive` or `\All` folder is used where
it marks one, and a name set explicitly always wins over the server's
SPECIAL-USE marks) — and keeps a small index of where each mail lives
(`folder, UIDVALIDITY, UID → Message-ID`).

- **A new mail in INBOX** becomes a `kb:InboundMessage`; **a new mail in Sent**
  a `kb:OutboundMessage`. A mail the gateway sent itself is already on record
  under its `Message-ID` and is not written twice; any other mail in Sent is
  the **own-device echo** (`author: device`) unless an outbound note names its
  author (see *Writing*). A mail with a copy in both folders is the user's
  own, whatever address it is `From` — `MAIL_OWN_ADDRESSES` need not list it.
  The one exception carries its own evidence: a Sent copy that was sent on as
  it stood (`Resent-` fields: a redirect) under someone else's `From` is
  still that person's mail.
- **The archive folder is only followed, never read for new mail**: a mail
  that moves there from INBOX is still that mail. One case is read: a mail
  that passed through INBOX or Sent between two polls — a filter, or a quick
  hand in a client — shows as a UID that was handed out and is gone, and what
  the archive gained since is then mirrored like any arrival.
- **A mail that is in none of the mirrored folders any more** — deleted or
  moved away in a client — is removed from the mirror on that poll and leaves
  a **tombstone**: the record keeps its identity (chat, sender, time,
  `Message-ID`), loses its text, parts and attachments, and gains
  `kb:removedAt`. Only an answer counts: a mail is gone once every mirrored
  folder has *said* it does not hold it. A folder the server will not open, a
  refused search, a dropped connection tombstone nothing; the pass is repeated.
  An archive folder that keeps refusing (a broken mailbox on the server) holds
  back only that — finding a mail again, calling one gone: INBOX and Sent are
  still mirrored, new mail and the backfill, and `/health.error` carries the
  server's words meanwhile.
- **A client's `\Seen` changes are ignored.**
- **Backfill**: on first run, INBOX and Sent for `MAIL_BACKFILL_DAYS` (90),
  merged and ingested **oldest first**, so parents arrive before replies.
  Backfilled mail is told to the rail as `backfill: true`: it pushes
  nothing, starts no turn, and carries `seen` so the read mark can start from
  the mailbox. It is never gated: written `delivered: true`, with no
  handover offered, and not filed to the news feed.

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

**What one mail may ask for is bounded**, since anyone can send one. An
attachment larger than `MAIL_MAX_PART_BYTES` is not stored; a small text
attachment named `<name>.not-mirrored.txt` stands in for it and says what it
was. A mail larger than `MAIL_MAX_MAIL_BYTES` keeps its text parts and one
such note for all its attachments. Beyond 200 attachments one note names the
rest. `kb:text` holds at most 256,000 characters (cut with a mark, trim state
`wanting`; the whole text stays in the body part). Header fields, address
lists and ids have their own caps, and a mail is decoded only under real
character sets — never under a codec the sender names. The text of a mail is
always kept. The work of parsing is bounded too, not only what is built: the
parser tests every line against the boundary of every multipart around it, so
a mail may nest 200 levels at most and ask for no more than 50 million such
tests (lines × levels; under six seconds on Python 3.12, three on 3.13) —
beyond either it is not parsed to the end.

**A mail that cannot be read is still there.** One mail's failure never stops
the folder, or the backfill: a mail the gateway cannot read is mirrored as a
placeholder that says so (with its sender, its time and its header block,
unchanged in the mailbox), at once. It is read again at start and every hour
(less often each time it changes nothing), and takes its placeholder's place
when it reads — that mail, and no other: the placeholder's record keeps the
header block of the copy it was made from, and only a mail with that very
header block replaces it, wherever it lies by then. A mail that merely
arrives under a placeholder's `Message-ID` is one more place of the mail on
record, as a second copy of any mail is; ids are their sender's to choose.

**Whose fault a failure is, is decided by what failed.** *Reading* a mail —
the parse, the walk of its parts, the trim — fails on the mail's account when
the same reader still reads a known-good mail the gateway carries with it
(the canary): whatever was raised, one of the bounds above or a recursion or
memory error among them, the mail is a placeholder and the mirror as healthy
as it was. *Writing* — the ledger, the indexes, the blobs — never fails on a
mail's account. A reader that cannot read the canary either, and any failure
in writing, is a fault of the mirror itself, which must not turn the mailbox
into placeholders one mail at a time with `/health` still `connected`: the
pass ends there. No mail is set aside or marked over it, and no folder's mark
moves past a mail that has neither a record nor a place — so the next pass
reads it again, after a restart too, and if it has left for the archive by
then it is read there, as a mail that came and went. `/health` reports
`connected: false` with `blocked: "mirror"` meanwhile, and `error` says on
what — naming the mail, where the pass stopped at one.

A record's time is the earlier of the mail's `Date` and the server's arrival
time, never the future, so a forged date cannot pin a mail to the head of its
chat; the arrival time itself is kept as `kb:arrivedAt`. A mail with no usable
`Message-ID` gets a stable one derived from its headers.

### The record

The ledger (`scripts/inbound_store.py`) learns the predicates a mail needs.
All are optional, round-trip through every rewrite, and mean nothing on other
channels:

| Predicate | Value |
|---|---|
| `kb:subject` | the mail's own subject, decoded |
| `kb:senderName` | the display name on `From` |
| `kb:gateSender` | inbound: the address the gate takes the mail to be from (*The gate*, "Who the sender is") — absent when it has none |
| `kb:recipient` (multi) | `To` addresses |
| `kb:cc` (multi) | `Cc` addresses |
| `kb:inReplyTo` | the parent's id |
| `kb:references` | the `References` ids, space-separated, in header order (then any further `In-Reply-To` id) |
| `kb:arrivedAt` | when the mailbox server received the mail (IMAP `INTERNALDATE`); kept on a tombstone |
| `kb:listId` | the normalised `List-Id` |
| `kb:bodyPart`, `kb:htmlPart`, `kb:headerPart` | media IRIs (see above) |
| `kb:contentId` (on an attachment IRI) | the part's `Content-ID` |
| `kb:trim` | `deterministic`, `wanting` (a model trim is due when the bubble is first viewed) or `model` (the text is a model's choice of the trim's lines) |
| `kb:removedAt` | when the mail was found gone from the mailbox |

`kb:text` is the **trimmed** text — what the bubble, the list preview, the
companion and a SPARQL reader all want. `kb:sender` is the `From` address
(inbound; else the `Sender` header's, else `unknown`) — what the chat shows
the mail as from, and nothing the gate goes by: that is `kb:gateSender`
alone. `kb:messageId` is the `Message-ID` without brackets.

New store operations: `update_message` learns `chat` and `trim`;
`rekey_chat(store, chat, account, into)` re-keys a whole chat for a merge;
`tombstone_message(path, removed_at)` strips a record to its identity (and the
threading ids, so an index rebuilt from the ledger still knows what the gone
mail cited) and unlinks its blobs; `delete_chat` also unlinks the part blobs;
`iter_records(store)` reads the ledger back for an index rebuild. A triple the
parser does not know is kept verbatim through every rewrite. A caller that is
the only writer of its blobs and knows its chat's records says so
(`tombstone_message(own_blobs=True)`, `rekey_chat(paths=…)`) and is spared a
scan of the whole ledger; the defaults keep the scan, which is what the
messenger gateways sharing this module rely on.

## Trimmed bubbles, the original one tap away

A mail is shown like a message: **its own new text**.

### The deterministic trim (`scripts/mail_trim.py`)

Pure, stdlib-only, language-agnostic by mechanism (structure and markers, never
a word list in one language where a structural test will do; where words are
needed — "wrote", "From:", "Sent from my" — the lists are multilingual by
design and extended as data).

```python
def trim(plain, html, subject=None, *, is_reply=None, bulk=None) -> TrimResult
#   TrimResult(text, state: "deterministic" | "wanting", removed: dict)
def html_to_text(html) -> str
def sanitize_html(html, cid_map=None) -> str   # a whole document, for the Original view
def is_tracking_pixel(width=None, height=None, attrs=None) -> bool
```

**The rule above every other: never cut what the author wrote.** So the trim
cuts on evidence, and where it sees a shape but lacks the evidence it leaves
the text in and says so:

- **Quoted history** written with `>` goes, with its attribution line.
  History that carries no `>` — Outlook's `-----Original Message-----` and its
  `From:/Sent:/To:/Subject:` block, `#divRplyFwdMsg`, an attribution above
  bare text — goes only from a mail that **cites a parent** (`is_reply`) and
  whose subject says it is a reply. Under a forward prefix the same history
  *is* the content and is kept. A `From/Date/Subject` block in a mail that is
  no reply is a notification or a memo, and stays.
- **Signatures**: the single standard `-- ` delimiter near the end, and a
  phone line that holds names only ("Sent from my …" with nothing said).
- **Footers and disclaimers**: an unsubscribe block or a list footer only in
  mail that says itself it is **bulk** (`bulk`: a `List-Id`, a
  `List-Unsubscribe`, a bulk `Precedence`) — "unsubscribe me please" from a
  person is their message; a legal notice only where the mail ends on one
  that is whole.
- **Tracking pixels.** Inline images that belong to the text stay, as media.

The gateway passes both facts. Where a word table decides (attribution verbs,
forward prefixes), a language the table lacks fails safe: the text stays.

The state is `wanting` when the mail is HTML-only, when what is left is still
long (more than `MAIL_TRIM_WANTING_CHARS`), when a quote marker survived, or
when a rule saw its shape and held back for want of evidence. An empty result
falls back to the untrimmed text: a bubble is never blank because the trim was
greedy.

**What one mail may cost is bounded**, like everything else it controls — the
mirror has one thread for all of them. The bounds are on the work, not on how
long a mail may be: a short reply above megabytes of thread, a log sent in
answer, a digest are ordinary mail and are trimmed to their own text. The
trim runs over a text a fixed number of times (four passes a reading, four
readings a mail). Lines it finds to be quoted history it skips, at next to no
cost; every other line it reads closely, and one reading reads at most 50,000
of them and 4,000,000 characters, counted once per pass — all four passes for
12,500 lines of the author's own, the two an ordinary mail takes for 25,000.
A mail that would take more is not trimmed and is recorded as it came,
`wanting`, as is one of more than 200,000 lines or 5,000,000 characters,
which is not read at all. Of an HTML part the first 1,000,000 characters of
markup are parsed, not counting inline `data:` images: beside a plain part a
longer one is not consulted, and where it is the mail's only text the bubble
ends where the reading did, on a line `[…]` — *Original* shows all of it.
Within those bounds the cost is linear in the size of the mail, for hostile
mail too.

### The model trim (phase 4)

Where the state is `wanting`, **one turn on the router-tier model** chooses
which of the lines the deterministic trim kept are the author's own message.
It runs **on first view** — when the bubble first comes into view on the chat
page, which asks for it — never on ingest, never on backfill and never from
an agent's read of the thread (the companion and the arrival turn read what
is on the record). Its result is cached on the record (`kb:text` replaced,
`kb:trim "model"`), so it runs at most once per mail.

**The model chooses lines; it never writes text.** The mail is text from
outside: the turn has no tools and no session, its prompt hands the mail over
as escaped data, and the mail is shown as numbered lines. The answer is the
line ranges to keep, and the bubble's new text is assembled by the mail
gateway from its own copy of those lines — so nothing a mail says can add a
word to its own bubble, by construction rather than by a check after the
fact. The record's parts are never changed.

**Only a judgement settles a mail.** A choice (valid or not), an answer that
is no choice at all (not one JSON object of ranges), and a mail too long to
hand over settle the record — `kb:trim "deterministic"` where no valid
choice came, the text as the rules left it — so it is not asked again. A
failure that says nothing about the mail — the model could not be started,
timed out, sign-in expired, a usage limit, the mail gateway away — changes
nothing on the record: the mail stays `wanting` and is tried again later. A
dead model must not settle every mail the user scrolls past.

#### The two halves (`scripts/mail_trim.py`)

```python
def model_source(plain, html, subject=None, *, is_reply=None, bulk=None) -> ModelSource | None
#   ModelSource(lines: list[str], digest: str)
def apply_keep(lines, keep) -> str | None
```

`model_source` is **`trim()` called with the same arguments** — one code path
and every one of its cost bounds — and its text split into lines, before the
record's cap (`MAX_RECORD_TEXT_CHARS`) is applied: a choice can only narrow
what the rules kept, never bring back history they cut. Lines end where a
reader sees a line end (`str.splitlines`: a form feed, U+2028 and their kin
as well as `"\n"`), so a mail cannot write something that reads as a line of
ours with a number of its own choosing. It is None when no line has text or
when there is more than may be handed over — more than
`MODEL_SOURCE_MAX_LINES` (1,500) lines or `MODEL_SOURCE_MAX_CHARS` (60,000)
characters, tested on the text's size before it is split. `digest` is the
SHA-256 (hex) of the lines joined by `"\n"`, UTF-8: the same parts always
give the same lines and the same digest.

`apply_keep` takes `keep = [[first, last], …]`, 1-based inclusive line
numbers, and is a choice only if it is a list of at most
`MODEL_KEEP_MAX_RANGES` (200) pairs of ints (`bool` and `float` are not),
each `1 <= first <= last <= len(lines)`, strictly ascending and not
overlapping. The kept lines are joined with one blank line where a gap was
cut (ranges that touch are one stretch), runs of blank lines collapsed and
none at either end. None for anything that is no choice, and for a choice
that keeps no line with text. Both are pure and never raise; `apply_keep` is
linear in the ranges plus the lines kept and refuses a list longer than 200
on its length, before one pair is looked at.

#### The mail gateway's endpoints

The gateway stays the single writer of the ledger and never trusts its caller
with text. Both endpoints take the bearer `MAIL_GATEWAY_TOKEN`, the mail as
the chat endpoints name it — `chat` (the chat key), `account` (the gateway's
own, else `404`) and `message_id` — and answer `404` for an unknown mail, or
one the call does not name by its chat, and `410` for a mail removed from the
mailbox. The chat may be the mail's chat on record, or a chat merged into
that one since (a chat key is its root's id, and the thread index knows which
chat that id is in now): a call made before a merge lands on the same record
after it.

- **`POST /messages/trim-source`** → `200 {"state": "wanting", "lines",
  "digest", "subject", "sender", "is_reply", "bulk"}`, built with
  `model_source` from the record's own parts and **the evidence the ingest
  gave `trim()`**: the subject on record, `is_reply` from `kb:inReplyTo` /
  `kb:references` (the record keeps every id the mail cited, so one is there
  exactly when the mail cited one), and `bulk` read again from the header
  part (`List-Id`, `List-Unsubscribe`, a bulk `Precedence` — the record
  carries only the normalised `List-Id`). So the lines are the record's own
  text, uncapped. `200 {"state": "deterministic" | "model"}` for a mail
  already settled; `200 {"state": "deterministic", "settled": "too_long"}`
  when `model_source` answers None — the gateway settles the record then and
  there (text unchanged), since asking again would only find it as long.
  Otherwise read-only, and without the mirror's lock. A part the record
  references that cannot be read answers `503` and settles nothing.
- **`POST /messages/trim-result`** with `digest` and exactly one of `keep`
  (any value: whether it is a choice is `apply_keep`'s to say) or `failed`
  (a short reason, logged, never stored). The gateway rebuilds the source and
  refuses `409 {"error": "stale", "state"}` when the mail is no longer
  `wanting` or the digest is not its source's — a choice is about the lines
  it was made from and no others. Then, under the mirror's lock (`503` when a
  pass holds it past `EXCLUSIVE_SECONDS`) and with the record read again (a
  record written anew from other parts meanwhile is stale too): a valid
  choice becomes `kb:text`, capped as every `kb:text` is, with `kb:trim
  "model"`; an invalid one, and `failed`, settle `deterministic` with the
  text unchanged. `200 {"state", "text"}` — the record's text as it now
  stands. The write is `inbound_store.update_message(text=, trim=)`, read
  back to confirm (a write that did not happen is `503`, never a `200`); it
  touches nothing but the text and the trim, and nothing on a tombstone.

Nothing is told on the rail. A mail's text changing on its record is how a
better reading reaches the chat — the life store indexes the rewrite within
seconds and the chat page's poll re-renders a message whose `text` or `trim`
changed — and the one caller, the web gateway, has the new text in the
answer.

#### The turn and the page

The web gateway runs the turn: `POST /chats/<id>/messages/<token>/trim`
(same-origin, the dashboard's auth, mail chats only; `<token>` is the
message's `original` segment) answers `200 {state, text}` for a mail settled,
before or just now; `202 {"state": "queued" | "running"}` while a turn is
queued or in flight (asking again never queues twice); and `200 {"state":
"wanting", "retry_after"}` when nothing was started — the feature off, the
day's budget spent, the queue full, the model in backoff. One worker thread
runs one turn at a time from a bounded queue (`MAIL_MODEL_TRIM_QUEUE`, 30).
The model is `MAIL_MODEL_TRIM_MODEL`, else `RETINUE_ROUTER_MODEL`, else the
transcript cleanup's fallback, spawned with that cleanup's discipline: no
tools, no session persistence, the allowlisted environment, the sign-in
refresh, a timeout (`MAIL_MODEL_TRIM_TIMEOUT`, 60 s). `MAIL_MODEL_TRIM`
(`1`) switches it off with `0`/`false`/`no`; `MAIL_MODEL_TRIM_DAILY_MAX`
(200) caps the turns of a local day, counted across restarts. The prompt is
fixed instructions, language-agnostic, then the subject, the sender and the
numbered lines as escaped data in an element they cannot close, within the
prompt size every turn of the web gateway keeps to; it asks for the ranges of
the lines that are the author's own message — no quoted history, signature,
footer, disclaimer, boilerplate, navigation or tracking residue — as one JSON
object `{"keep": [[first, last], …]}`. An answer parsed as that is a
judgement and goes to `trim-result` as `keep`; one that does not parse is a
judgement too and goes as `failed: "unparseable"`. A spawn error, a non-zero
exit, a timeout, an auth or usage-limit failure and the gateway unreachable
send nothing: the mail backs off (`MAIL_MODEL_TRIM_RETRY_SECONDS`, 3600, in
memory), and three such failures in a row pause the feature for ten
minutes.

The chat page asks for a `wanting` bubble's trim once it has been at least
half in view for a moment — not while the list is scrolled through — once per
mail per page life unless an answer's `retry_after` has passed. While a turn
is queued or running the bubble says so in a quiet line under its text; the
settled text arrives through the POST's answer and through the ordinary poll,
and the reader's place is kept when a bubble above the viewport shrinks. A
bubble whose `trim` is `model` says so where the user looks for the full
mail: the Original panel is headed "Shortened by a model — this is the full
mail." Messenger chats never ask.

### The original

Every mail bubble offers **Original**. A tap shows the full mail in place:

- the headers (From, To, Cc, Date, Subject, and the full block behind a
  disclosure);
- the HTML part, in a sandboxed frame, or the full plain text when there is
  none;
- every attachment.

`GET <original>` — the message's `original` field,
`/chats/<id>/messages/<token>/original`, where `<token>` is a segment the
gateway mints and the client never builds (never the raw `Message-ID`) —
answers the headers, the full
text and the attachment list; `…/original.html` serves the HTML part as a
document of its own under a policy stricter than the dashboard's
(`sandbox allow-popups allow-popups-to-escape-sandbox; default-src 'none';
img-src data:; style-src 'unsafe-inline'`), sanitised first (no script, no
event handlers, no forms, no frames, no remote loads) with `cid:` images
inlined as `data:` URLs and links opening in a new tab. Remote images never
load, so a tracking pixel never fires. The chat page's own policy gains
`frame-src 'self'` for that one frame.

**The record is never changed** by viewing, trimming or replying: the original
parts are immutable, and the mailbox copy's content is never changed (a reply
sets only `\Seen` on the thread, see "The mailbox follows the chat, never the
reverse").

## Writing (phase 2)

- **Reply.** The composer sends `POST /chats/<id>/send` as on every chat. For
  a mail chat the body may carry `reply: "all" | "sender"` (default `all` in a
  group, and the only meaning in a 1:1). The gateway builds the mail from its
  own records of the chat, never from the caller: `From` the thread's account
  (with `EMAIL_FROM_NAME`), `Subject` `Re: <base subject>` (no second `Re:`),
  `In-Reply-To` the id of the latest live mail of the thread that is not the
  user's own (else the latest mail), `References` that mail's chain plus its
  id. Reply-all: `To` is that mail's sender — its `Reply-To` when it had one,
  which the record now keeps — and `Cc` everyone on its `To`/`Cc` except the
  account's own addresses and the `To`; *sender only*: the `To` alone. If the
  latest mail is the user's own, a reply-all goes to its recipients again.
- **The recipients the user sees are the recipients the gateway uses.** The
  browser never recomputes a reply's addressing. `POST /reply-preview
  {chat, account}` on the mail gateway (token-gated like `/send`, read-only,
  never sends) answers `{all, sender, choice, subject}`: `all` and `sender` are
  each `{to: [{address, name}], cc: [{address, name}]}` — exactly what `/send`
  would address for that mode right now, by the same function — `choice` says
  whether the two differ, `subject` is the `Re: …`; `400`/`409` as `/send`
  answers for a chat it cannot reply in. The web gateway serves it at `GET
  /chats/<id>/reply-preview` (dashboard auth, `contact` names resolved from the
  address book, `409 {error}` where no reply is possible — a chat that holds
  only a queued mail), and every mail message carries `reply_to`. The composer
  draws its To/Cc line and the send button's label from that answer, fetched on
  load, after every poll that changed the thread and again at the press; a
  send is refused client-side if the preview is missing or older than the
  thread on screen. Each recipient shows its **address**, and one that is not
  the `From` of the mail answered says why (its `Reply-To`). *Reply all* /
  *Sender only* is offered exactly when `choice` is true, and the mode sent is
  the one on screen at the press.
- **Under the send policy.** The gateway resolves `EMAIL_SEND_POLICY` for its
  own address. `verify` queues; `trust` sends when the request carries
  `user_approved` (the web gateway sets it for the user's own press), else
  queues; `allow` sends. `author: "user"` grants nothing by itself. A queued
  send answers `202 pending_approval` with `request_id` and `approval_url`
  (`/sends/<slug>/<id>`), is listed on `/sends` through the gateway's
  `/pending-sends`, and **shows in the chat as a pending bubble with its
  approval link** until it is sent, rejected or retracted — whoever queued it:
  the composer, the companion or `mail-push.py`. The web gateway does not
  approve a queued mail on the user's behalf (the messengers keep their
  queue-then-approve behaviour). A `400` answers no recipient, an unknown chat
  or an address that does not parse; a `409` a chat with no live mail.
- **A mail that may have gone out is never reported as not sent.** `/send`
  answers as soon as the SMTP server has taken the mail; the Sent copy is filed
  afterwards by the mirror loop. A `/send` may carry a `request_key` (an opaque
  token of at most 64 characters, minted per press): a repeat with a key seen
  within 24 hours answers what became of the first, with the same `message_id`,
  and sends nothing — unless the first failed for certain (the next point). The web gateway's hop waits `MAIL_SEND_TIMEOUT` (120 s) and
  passes the browser's `request_key`; a timeout, a connection dropped after the
  request was written and the gateway's `502 {unconfirmed: true}` all answer the
  browser `202 {unconfirmed: true, message_id (when known), request_key}`, never
  a 5xx that reads as "not sent". The chat page keeps the bubble as *Sending —
  not confirmed yet*, never puts the words back into the composer, and
  reconciles the bubble by message id when the mail shows. One press of Send
  (reply or compose) has one `request_key`, reused while the outcome of that
  press is not known.
- **A send that certainly failed ends its press; only an unknown outcome keeps
  its key.** Three layers, each right on its own:
  - *The mail gateway.* A send failed **for certain** when nothing left the
    gateway: the SMTP server refused the mail, or the send ended before the
    hand-over. (The mark `data` is written to the entry before any of the
    message is on the wire, so an entry without it was never handed over.) Its
    entry is `status: "error", outcome_unknown: false`, and `/send` answers
    `502 {error: "send failed: …", "sent": false}`. A `/send` under the
    `request_key` of such an entry is a **new attempt**: the old entry is
    removed, and the mail is composed, put to the policy and sent afresh — a
    new `message_id`, and under `verify` a new approval. A key whose entry is
    `pending`, `sending`, sent, or `error` with `outcome_unknown: true` is
    answered from the entry and sends nothing, for as long as the key is
    remembered, dismissed or not. Across a restart the entry is first what
    the start makes of it (the table below): a direct send a stop caught
    before the hand-over ends as a certain failure there, and its repeat is a
    new attempt. An `error` entry that does not say `outcome_unknown` either
    way is read as unknown.
    `"sent": false` is also on the gateway's `503`s (not configured, no SMTP
    server, the send could not be written down) — which come only after the
    key was looked up, so it is never said over a mail that went out — and a
    fault nobody planned for is answered from what stands under the key, else
    as `unconfirmed`; never as "failed" by guesswork.
  - *The web gateway.* Its certain "nothing was sent" for a mail — the
    gateway's definite failure — answers the browser `424 {error: <sentence>,
    "sent": false, request_key}`. `502` stays "the gateway was never reached"
    and `503` "cannot send / could not queue"; both carry `"sent": false` when
    the web gateway knows nothing was handed over (it never wrote the request).
    Anything whose outcome is open is `202 {unconfirmed: true, …}`, never a
    4xx or 5xx. A 5xx from the mail gateway is a failure **only on its
    explicit `"sent": false`**: a `502`/`503` without it, a body cut short of
    its `Content-Length`, and a body that is no JSON are all *unconfirmed*
    (`202`), never `424`.
  - *Who said it.* Every "not sent" answer to the browser names its speaker:
    `"by": "gateway"` where it hands on the mail gateway's own word about this
    request (its certain failure, the `424`; its refusals, each a 4xx; its
    `503`s), `"by": "dashboard"` where the web gateway answers
    without the mail gateway having spoken (never reached, no gateway serves
    that mailbox, validation, `403`/`410`). Only the first is a word about the
    key; the second is about this request alone.
  - *The webapp* (the chat page and the compose sheet). **A kept key is
    dropped only on the mail gateway's own word about that key**: an answer
    with `"by": "gateway"` and `"sent": false` or a 4xx, a change of the
    words, or the press's mail showing up. Any other "not sent" answer keeps
    the key with the words. Keeping a key is harmless — the mail gateway takes
    a key it never saw, or one whose send failed for certain, for a new
    attempt — while dropping one whose earlier outcome is unknown can send the
    mail twice. What the user is told depends on the press: a first press (a
    fresh key) answered `"sent": false` or a 4xx reads *Not sent:
    <sentence>*, the words in the box and the next press a new one; a press
    made *again* under a kept key and answered by the dashboard reads that the
    gateway could not be asked just now, and that it is still not known
    whether the first attempt went out. An answer with no `by` (an older
    server) is read as the gateway's for a first press and as the dashboard's
    for an again press. A 5xx without `"sent": false` (a proxy, a crash) is
    unknown and keeps the key.

  What a start finds under a key, and what the same request made again then
  does (`tests/test_mail_gateway_send.py` walks a stop after every write):

  | The entry on disk | At a start | A repeat of the request |
  |---|---|---|
  | none (the stop came before it stood) | — | the first send: carried out |
  | `pending` | stays | `202`, the same pending send; sends nothing |
  | `sending`, no mark, approved | the approval is carried out, once | `202 sending` or `200 sent`; sends nothing |
  | `sending`, no mark, direct | ends as `error`, `outcome_unknown: false` ("stopped before this mail was handed to the mail server; it was not sent") and is listed as failed — one that named no `request_key` is dropped | a **new attempt**: nothing had left |
  | `sending`, mark `data` | Sent is asked: found, it went out; not found, `error` with `outcome_unknown: true` | until Sent has said, `502 unconfirmed` (a queued send: `202 sending`); then as the row it became — never sent again |
  | sent (`approved`) | record and Sent copy finished if owed | `200 sent`, the same `message_id` |
  | `error`, `outcome_unknown: true` | stays (listed until dismissed) | `502 {unconfirmed, outcome_unknown}`; never sent again |
  | `error`, `outcome_unknown: false` | stays (listed until dismissed) | a **new attempt** |
  | `rejected` / `retracted` | stays | `409`; sends nothing |
- **A repeat is not a reply.** When a `/send` under a known key is answered
  from an earlier entry, the gateway says so: `"repeat": true` in the answer
  (the `200` sent, the `202` pending, the `502` unconfirmed, the `409` of a
  send that was rejected or retracted). The
  mail went out — or was queued — *before*; this request did none of it. The
  web gateway then only reconciles: it returns the Message or the Pending and
  drops its pending copy, and does nothing a reply does — no read mark
  advanced, no attention item settled, no mailbox read hook. The rail did all
  of that when the mail went out. **A repeat clears the draft it repeats, and
  no other**: the chat's shared draft is cleared exactly when it still holds
  this press's own words, and a draft written since stays — on every answer
  to a press made before, a new attempt under its kept key included. (A
  press whose answer never reached the page puts its words back in the box,
  and the box saves them as the shared draft; the repeat that finds the mail
  sent takes them out again, or every device and every session would get a
  sent mail back as the draft, and one more press would send it twice.)
  `mail-push.py` prints that the answer was a repeat. A new attempt after a certain failure is no repeat.
- **A failed or partly refused send leaves a trace.** `GET /pending-sends`
  (and `?chat=`) lists entries with status `pending`, `sending` and `error`;
  an `error` entry carries a sentence `error` and `outcome_unknown` (the hand-over
  may have happened), and stays listed until `POST /pending-sends/<id>/dismiss`.
  Nothing re-sends an `error` entry by itself; only a new request under the key
  of one that failed for certain does, as a new attempt. A direct send whose
  caller was still on the line has the failure as its answer and leaves no
  entry; one whose caller had gone, or that a restart caught before the
  hand-over, is listed as failed. Addresses the SMTP server refused while taking the
  rest are on the entry and in the `200` answer as `refused: [addresses]`, and the
  record's recipients are those the server took. In the chat the pending entry
  gains `status` (`pending`, `sending`, `failed`), `error`, `outcome_unknown` and
  `refused`; a sent message carries `refused`; `POST
  /chats/<id>/pending/<request id>/dismiss` (same-origin) clears a failed one.
  A failed bubble says so with its reason, offers *Copy text back to the
  composer* and *Dismiss*, says the mail may have gone out where
  `outcome_unknown`, and names refused recipients.
- **A dismissed entry is over, and `outcome_unknown` is read by every
  reader.** Dismissing removes an entry that failed for certain. One of
  unknown outcome that named a `request_key` is kept for the 24 hours its key
  is remembered — so that a repeat is still told the mail may have gone out —
  and is no longer listed; `GET /pending-sends/<id>` then answers `{status:
  "error", dismissed: true, …}`, which every reader treats as ended, not as a
  failed bubble. And an `error` with `outcome_unknown: true` is never worded
  as "failed": the chat, the `/sends` approval page and its status JSON, the
  note written back into the thread that queued the send, and `mail-push.py
  --retract` (`scripts/pending_retract.py`) all say that it is not known
  whether the mail went out, and that it must not be sent again.
- **What a send writes.** SMTP through `email_client`'s helpers, a Sent copy
  (filed by the mirror loop) unless `SMTP_SAVE_SENT=false`, and the outbound record (`kb:OutboundMessage`,
  the given `author`, the trimmed text the message itself, `kb:bodyPart`) at
  once, with the rail told, so the bubble does not wait for a poll. The
  `Message-ID` is minted when the request is accepted — before it is queued —
  so a new mail's chat key is known at once. The Sent-folder pickup then finds
  the same `Message-ID` and writes nothing; and a record the gateway wrote for
  its own send is not "unlocated" until its Sent copy has had
  `MAIL_SENT_GRACE_SECONDS` (600) to appear — with `SMTP_SAVE_SENT=false` and
  no copy appearing it stays a live record, the mail having been sent. The
  gateway never writes policy: sending to someone does not make them known to
  the gate.
- **New mail.** ＋ on the Chats page offers *E-mail* (what
  `GET /chats/compose` lists: channels whose gateway takes a new
  conversation): recipients, Cc, subject, text. `POST /chats/compose` hands it
  to the gateway's `/send` without a `recipient`, which mints the `Message-ID`
  that becomes the new chat's key; a new mail that exists only as a queued send
  is already listed on the Chats page.
- **Agents** send a mail they hold as a chat through the thin
  `scripts/mail-push.py` (the sibling of `sms-push.py`: `--to`/`--cc`/`--subject`,
  or `--chat <id>` with `--reply all|sender`, `--attach`, `--user-approved`,
  `--thread`, `--retract`), under the same policy. Each run names its request
  with a key of its own; a run that got no answer says "NOT KNOWN whether this
  mail was sent" and prints the key, and `--request-key <key>` with the same
  arguments asks about that send again — answered as a repeat where the mail
  went out or waits, and carried out as a new attempt where nothing had left.
  (`--reply-to <token>` is
  there for a gateway prompt that carries a token; no reply token exists for
  mail — the gateway forwards no mail to a session — and agent-facing texts
  do not point at it.) A mail reaches an agent as a chat, and the turn the
  gate starts for a VIP or an instructed sender answers with `mail-push.py
  --chat <chat id>` (with `--url` for a thread on another account's gateway).
  `email_client.py send/reply` keeps
  working as before and keeps its own queue (an IMAP draft, approved on
  `/sends` under `/sends/<account>/<id>`); after a successful send, direct or
  approved, it tells the gateway whose mail it was (`POST /outbound-note`,
  best-effort, only where `MAIL_GATEWAY_BASE_URL` is set and only for the
  account the gateway serves), so the bubble says *Ara* rather than *mail
  client*. Its mail is picked up from the Sent folder.
- **The companion** works as on any chat: it reads the trimmed thread and the
  attachments, stages drafts (`chat-draft.py`), sends only through
  `mail-push.py` under the policy, forks; the quick-pattern chips apply. The
  draft endpoints work for mail chats, and a mail chat's `can_send` is true
  unless its gateway is unknown, not connected, or reports that it cannot send
  (no SMTP server configured).

### The mailbox follows the chat, never the reverse

| In the chat | In the mailbox |
|---|---|
| **Mark as read**, or a reply | `\Seen` on every mail of the thread in INBOX or the archive folder |
| **Archive** | the thread's INBOX mails move to the archive folder (created if absent); *unarchive does not move them back*, and a restore calls nothing |
| an instructed turn files a document and archives | the same move |
| **Delete** (two taps, the erase capability, same-origin only) | every mail of the thread, in every mirrored folder, moves to the account's **Trash** (`MAIL_TRASH_FOLDER`), **then** the mirror is erased — nothing is expunged |

The web gateway calls the chat's gateway for each (`POST /chats/read`,
`POST /chats/archive`, `POST /chats/delete`, all `{chat, account}`; the account
must be the gateway's own, else 404, and an unknown chat answers zero). The
first two run after the chat state changed, best-effort: a 404 or 501 means the
channel has no mailbox and is not an error, any other failure is logged and
retried on the next such action — the chat is the truth, the hook idempotent —
and the mirror follows an archive by its own index, with no tombstone and no
rail event. The two hooks carry the `message_ids` of the mails the action
covers (a read mark: the chat's inbound mails up to the mark; an archive: the
inbound mails present when it was archived) and the gateway marks or moves
those mails only, so a mail that arrived after the user's action is never
touched; it is read or archived by a later action of its own.

`POST /chats/<id>/read` and `POST /chats/<id>/flags` write to the real mailbox
now, so they are the user's own act like send, compose and delete:
**same-origin only**. Delete is the exception: it needs `X-Chat-Erase-Token`
(`CHAT_ERASE_TOKEN`; 503 while unset, 403 on a mismatch), and the gateway must
succeed before the web gateway erases its own state — if any move fails, or a
mail of the thread cannot be placed, nothing is erased and the answer reports
`errors > 0`, with the reason as one sentence in `error`. The answer is
`delete_chat`'s shape plus `trashed`; a mail that turns up again later in a
mirrored folder (restored from the Trash) starts the chat anew.

**One message in two folders.** A mailbox whose folders are labels (Gmail: the
archive is `\All`) shows one message in INBOX and in the archive at once, and
the mirror gives such a mail both places. It is still one mail. *Read* flags
it in each place (the count is of places). *Archive* takes it out of the INBOX
and keeps the place it already has in the archive — no second copy. *Delete*
moves it to the Trash from the first place, which empties the second by the
gateway's own act: a mail that is no longer where the index had it is not an
error by itself. Once every place was tried it is looked for in every
mirrored folder within the same call — found, it goes to the Trash from
there; in none, it is gone, and the delete completes in that one call. Only a
folder that cannot say leaves the mail open (`errors > 0`, nothing erased,
the call made again finishes).

IMAP writes are `UID STORE` and `UID MOVE`, with `UID COPY` + `\Deleted` +
`UID EXPUNGE` of exactly those UIDs as the fallback where `UIDPLUS` allows
(else the `\Deleted` copy is left flagged and reported) — never a whole-folder
`EXPUNGE`.

**Folders.** The gateway never invents a **Trash**: it is the folder the server
marks `\Trash`, else the one `MAIL_TRASH_FOLDER` names, and with neither,
Delete is refused with `errors > 0` and nothing is erased. The archive folder
is created when it is missing, and a folder the gateway creates is
**subscribed**, so a mail client lists it. A name set explicitly
(`MAIL_ARCHIVE_FOLDER`, `MAIL_TRASH_FOLDER`) wins over the server's SPECIAL-USE
marks; unset, the server's own `\Archive` (else `\All`) / `\Trash` folder is
used, else the default name.

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
   wildcard. The two axes are independent: no group flag — `news` included —
   stops a VIP's or an instructed sender's mail.
3. **Notify the chat rail.** **A mail pushes nothing by default; a contact can
   say otherwise** — VIP rings on arrival, an instruction's verdict decides,
   and a person's importance prior and permits are honoured because the
   attention item's sender is the person.
4. **A model turn only for a VIP or an instructed sender**, in the chat's
   companion, briefed by the instruction, ending in a verdict, with the
   messengers' failure-rings guarantee — and a timeout of mail's own,
   `MAIL_INSTRUCTION_TIMEOUT` (600 seconds, read by the web gateway), where a
   messenger message's ring waits `CHAT_INSTRUCTION_TIMEOUT` (60): a mail is
   not a chat message, ten minutes is a ring in time, and a turn that files a
   document needs them.

**Who the sender is.** Every header of a mail is its sender's to write, the
`From` address included; the one barrier before it is the mailbox provider's
check of the `From` domain (DMARC), and that check is only sure to have looked
at a mail with exactly one `From` header holding exactly one address. So that
is the only form the gate takes a sender from: the **gate sender**
(`kb:gateSender`, `gate_sender` on the rail) is that one address, as the
gateway's own address grammar reads it, lowercased by its ASCII letters alone
and refused outright where a character of it folds into ASCII on some reading
(a Kelvin sign, a fullwidth letter). A mail with no `From`, an empty one, a
group, two addresses, two `From` headers, or an address the grammar rejects
has **no gate sender**: it reaches its chat, buys no turn, rings as
nobody's, and links the thread to no contact card by the address it is shown
as from (`peer_vouched` false on the chat's state, until a mail the gate takes
for that address arrives). The `Sender` header never stands in for it — it is only what the
chat shows (`kb:sender`) when the `From` names nobody. Both axes are keyed on
the gate sender (the group axis on the `List-Id` first), and the web gateway
judges an arrival — its class, prior and permits — by the gate sender the
event carries, never by the thread's previous correspondent.

**The rail is authenticated for mail.** An e-mail event on
`/internal/chats/*` is taken only with a token: `CHATS_INGEST_TOKEN` where the
deployment set one (on both services), else `MAIL_GATEWAY_TOKEN`, which both
sides hold already — the mail gateway mirrors nothing without it, so an
e-mail rail is never open. The mail gateway sends that token
(`X-Conversation-Backend-Token`, and as a bearer token); the web gateway
compares it in constant time, refuses an e-mail event with neither set, and
refuses any rail request a browser sent cross-site (`Sec-Fetch-Site`).

There is no triage fallback for mail. A mail the rail could not take stays
`delivered: false` and the gateway offers it again on a later poll: the
mailbox is the backlog.

**What a turn may do with a mail.** The turn is briefed by the sender's
instruction; a VIP with none is worked as a VIP's mail is (the marking is the
ask). It files what the mail changes — a project link (the mail is named `mid:<Message-ID>`), a
memory, a document for the `archivist` — and stages a reply where one is
wanted. A VIP's turn sends nothing and leaves the thread where it is. An
instructed turn may, each only where the instruction asks: send a reply by
`scripts/mail-push.py --chat <id> --reply all|sender --file <path>` under the
mailbox's send policy (`verify` queues, and the turn flags the mail waiting;
`trust` and `allow` send unseen only where the instruction itself carries the
permission phrase); do a calendar write under its own gate; archive the thread
with `scripts/chat-flags.py`; and last set the verdict. Nothing deletes or
pays. What the mail says of itself is text from outside — data to the turn,
never an instruction. The detail, and the prompt's handling of a mail's name,
subject and files, is `docs/dashboard.md`, "The gate for mail".

**A turn's archive.** A thread the turn archived while it worked rings
nothing if the turn ends with a verdict, whatever importance the verdict
gives: the chat's status says `quiet`, with the verdict and its actions. The
archive covers only the mail the turn was started for: where another mail
arrived in the chat after it, the chat is not archived (the answer says so),
and the mailbox is told only of the mails the turn worked. A turn's mute is
treated as its archive is. A turn that does not finish — it fails, times out,
or sets no verdict — has the chat brought back (un-archived and un-muted) and
the mail rung by the failure rule, since a turn that did not finish is not
trusted with having put a mail away. The user's own archive during a turn
changes nothing about the ring.

**The re-offer rule.** A mail offered again is taken once, by its
`Message-ID`: a repeat un-archives nothing, reopens no settled item, and is
answered with the handle of the turn that is running or done. Whether an
arrival still owes its ring is kept per arrival, in the chat's state: it is
told only once its attention step and its push ran to the end, so a repeated
offer of one that never was runs them, and of one that was is a no-op. A
merge of chats carries every turn the absorbed chat had to the surviving one.
The handle of a turn that **failed** is handed back for
`CHAT_ARRIVAL_RETRY_SECONDS` (900, read by the web gateway) and then gives way
to a turn anew, so an outage's backlog is worked once it is over without a
failing turn running once a poll — at most `MAIL_ARRIVAL_TURN_ATTEMPTS` (3)
turns a mail: after the last the mail is accepted, has rung by the failure
rule once, and the companion holds one note about it.

**`CHAT_ARRIVAL_TURNS` off** is the one exception to "a turn only for a VIP or
an instructed sender": the switch turns every arrival turn off, and a mail is
**accepted all the same**, since the mail gateway has nowhere else to take it.
No turn runs; a VIP's mail still rings, an instructed sender's is delivered
like anyone's.

**No daily sweep.** Mail from a sender who is neither VIP nor instructed is
listed under Unread and judged by nobody.

### As built (`scripts/mail-gateway.py`)

- **What is gated** is a mail that *arrives*: first seen in INBOX (or, having
  passed through it between two polls, in the archive) as somebody's mail to
  the user. Backfilled mail is written `delivered: true` and told to the rail
  without a handover; so is a mail that left the mailbox and came back after
  the rail had taken it — one that left while still owed keeps that on its
  tombstone and is offered when it comes back — and the user's own mail is
  never owed. With no chats rail configured every mail is written delivered:
  nobody is there to take it.
- **The verdict** is asked when the offer is made, not when the mail arrived,
  so a mail that waited is judged by the policy as it stands: `gate =
  {"forward": not (quieted or ignored), "vip", "instructed", "reason"}`. The
  group axis is `email_gate_decision` on the group flags; the sender axis is
  `gate_decision("email", gate_sender, group)` on the policy file the address
  book projects (`<ledger>/policy/policy.nt`), exact address first, then a
  domain wildcard — on the record's `kb:gateSender` alone (*Who the sender
  is*; a record without one is from nobody). The event carries `gate_sender`
  beside `sender`. The two axes are independent: a quieted list does not stop
  a VIP's turn. **The group flags unreadable** reads as no flags (fail open);
  **the sender policy unreadable** reads as nobody special (fail closed, for
  turns only) and is logged — a mail must not buy a model turn because a file
  could not be read. The gate is never switched off for mail.
- **Files** — the mail's attachments, in the `POST /message` shape — ride
  along only for a VIP or an instructed sender, within the rail's limits
  (`MAIL_RAIL_FILE_BYTES` a file; 20 files and 50 MiB an event). Of a file's
  name only a short plain extension travels, on a name of the gateway's own;
  its type goes as the one clean line the ledger keeps.
- **The answer.** `{"accepted": true}` → `mark_delivered`. `accepted` with a
  `job_url` → `job_delivery.confirm_delivery`, and delivered only when the job
  reports done. The `job_url` must be a path on the rail's own host, checked
  as it is parsed (printable ASCII; no scheme, no host, no `//`) and the
  joined URL checked again for the rail's scheme and host; anything else is
  no acceptance. Anything else — no acceptance, a refusal, no answer, a job
  that failed, expired or did not finish within `MAIL_RAIL_JOB_TIMEOUT` —
  leaves the record `delivered: false`. Nothing is forwarded anywhere else.
- **One queue, in order.** The first offer of a mail has its place in the
  gateway's durable outbox among the other rail events (merges, removals, the
  user's own mail), so the web gateway hears things in the order they
  happened — a reply from a mail client reads the chat up to it, and must not
  be heard before the mail it answers. While the web gateway does not answer
  at all — the connection refused, a 401/403/404, or no answer to an offer
  and none either to a GET that takes nothing — everything waits behind that
  point, and tries back off (30 seconds, doubling to 15 minutes). **One mail
  never holds back another**: an answer that is about one mail — a 408, 429
  or 5xx for it, or no answer for it while the rail answers otherwise — backs
  off that mail alone, which leaves the outbox and waits its own time as a
  mail not taken does.
- **Offered again.** A mail that was answered for and not taken leaves the
  outbox and is offered again on later polls: after one minute, then twice as
  long each time up to an hour, at most 20 offers a poll, the oldest first,
  and never while the job of a turn working it is still awaited — such a mail
  is passed over and takes none of the 20. **The ledger's `delivered` flag is
  the queue**: the gateway's index of it is rebuilt from the records at every
  start, so the wait survives a restart and a lost state volume. An offer is
  built from the record as it stands then — under the chat the mail is in
  *now* if its thread was merged meanwhile; a record that cannot be read just
  then waits its time and is offered again — and a mail that is gone is
  offered to nobody (its tombstone keeps the flag it had, for the day the
  mail comes back). An offer needs the ledger, not the mailbox: while the
  mailbox does not answer, what has arrived is offered all the same (not
  while the policy file is awaited). Offers are at-least-once; the web
  gateway takes a repeat once, by its message id, and hands back the job it
  started for it.
- **News.** A mail of a `news` group is filed by the gateway
  (`news_ingest.forward_news(channel="email", source_id="email:<address>",
  title=<subject>, …)`): the sender as the source, the subject and an excerpt
  of the trimmed text (`MAIL_NEWS_EXCERPT_CHARS`), and the link the mail
  declares for itself — its `Archived-At` alone, http(s) only. Not
  `List-Archive`, which names the list's archive and is the same on every
  issue (the feed keys an item on its link: every issue after the first
  would be dropped), and never a link picked out of the body; a mail without
  one is filed with no link. Where the group is also `ignored` and the feed
  took the item, the mail is marked `\Seen` and moved to `MAIL_NEWS_FOLDER`
  (unset: the archive folder, by the gateway's own folder rules; set and
  empty: left in the INBOX) through the adapter's write half, and the mirror
  follows its own move. Only a mail that is in no chat is moved — a news mail
  that joined a chat is filed, shown there, and left to the chat. **A VIP's
  or an instructed sender's mail in a news group** is such a mail: filed,
  and also written, offered and worked in its chat like any of theirs, and
  never moved. A feed that refuses leaves the mail untouched and is asked
  again; the move never comes before the filing. A news mail the reader could
  not read is neither filed nor a chat; it is read again with the
  placeholders (hourly, less often each time it still fails) and filed once
  it reads. Backfilled news mail is neither filed nor moved. With
  `NEWS_INGEST_URL` emptied there is no feed to take the mail, so the `news`
  flag does nothing: such mail is **mirrored as a chat** like any other,
  filed nowhere and moved nowhere. `MAIL_NEWS_FOLDER` and
  `MAIL_NEWS_EXCERPT_CHARS` were the retired triage gate's
  `TRIAGE_NEWS_FOLDER` and `TRIAGE_NEWS_EXCERPT_CHARS`: an old name is still
  read where the new one is not set (an empty value included), and the start
  says so.
- **`/health`** counts what waits (`waiting`), and says so in `error` while
  the web gateway has not answered three times running and mail waits.

### What retires

The omnibus, the per-mail triage threads, the status store, the frequent and
daily triage runs, the already-answered check, the e-mail whitelist (addresses,
wildcards, auto-add on send, the Sent-folder derivation, the contacts
projection into it). What stays: the news rail (run by the gateway now), the
group flags, `email_client.py`, and triage itself for a messenger message the
chat rail could not take. Triage retires whether or not the mail gateway runs:
a deployment without `MAIL_GATEWAY_TOKEN` has no e-mail handling at all after
this upgrade, and the migration waits for the token (*The migration*).

### The migration (`scripts/migrate-triage.py`)

Run once, by the entrypoint, in three steps. It is idempotent and resumable:
a marker file (`.migrated-to-contacts.json` in the triage state directory,
`TRIAGE_STATE_DIR`, `/root/.retinue/triage` by default) journals every
outcome as it happens — and what it is about to write *before* it writes it —
so a run killed at any point is finished by the next one, reporting what it
did as done, and a second run changes nothing. **Nothing is deleted**:
contacts are created or extended, memories are superseded, the old status
store stays where it is. `--dry-run` writes nothing, spawns no model and
prints what a run would do. Running it by hand: `docs/contributing.md`, "The
triage migration".

**It moves nothing while no mail is handled.** Triage retires with this
upgrade whether or not the mail gateway runs, and the gateway is opt-in: with
`MAIL_GATEWAY_TOKEN` unset **e-mail is handled by nobody** — no chat, no ring,
no turn. Both steps then wait, writing nothing but the marker (an instruction
that buys a turn per mail means nothing without the gate, and the steps need
the mirror); the report says so first, the stub `triage-gate.py` a stale job
still runs says so in its log line, and the boot run opens **one** dashboard
thread of its own (importance 4, key `migrate-triage-mail-off`, fixed wording)
on a deployment that had e-mail triage — opened again only after the gateway
was switched on and off again. Once the token is set the steps also wait for
the mirror's **first backfill** (the gateway's `/health`, `backfill.done`):
names and the senders only the mirror has seen come from it. That wait is
bounded — after `MIGRATE_TRIAGE_MIRROR_WAIT` (6 h by default) the migration
goes ahead and the report says what it may have cost (a contact named after
its address, a sender reported as unknown); a gateway that reports itself not
configured is not waited for.

**Only what was triage's moves.** The first time this code runs — the
entrypoint's `--step snapshot`, before any daemon — it fixes a **cutoff** in
the marker, which never moves. Only `sender:` memories recorded before it are
candidates: one recorded later is not triage's (the gate's own turns store
such facts, out of mail) and is reported as such, never shown to the model,
never carried by `--no-model`, never superseded.

**Step `whitelist`** — deterministic.

- The legacy triples (`triageWhitelistAddress`, `triageWhitelistWildcard`)
  are **snapshotted first**, into `.legacy-email-whitelist.json` beside the
  marker: the policy file they live in is written by code that no longer
  knows them, and its next write drops them. The entrypoint takes that
  snapshot right after it places the policy file, before any daemon exists;
  the snapshot is add-only, and the step works from it. A policy file that is
  there and **cannot be read** is not "no whitelist": the entrypoint's call
  fails loudly, and both steps stay pending, saying why, until it reads.
- **An address a person already holds** goes to that person: with no
  instruction they get the generic one (*Decisions*, 4), with one they keep
  theirs.
- **An address nobody holds** becomes a contact in the address book's default
  chamber, named from the display name most of its mirrored mails carried
  (`kb:senderName`, reduced to letters, digits and a little punctuation — it
  is text from a mail), else the address's local part; with the generic
  instruction. A new contact that shares its name with an existing one is
  reported, never merged: a name a mail carried does not prove a person.
- **A wildcard** becomes a contact for the domain (`kb:emailDomain`), named
  after the domain; `*@d` and `*@*.d` are one contact with both spellings. A
  domain somebody already files goes to them.
- **Left alone, and reported:** the user's own addresses (`EMAIL_USER*` and
  `MAIL_OWN_ADDRESSES` where the process holds them, the accounts the mirror
  mirrors, and every address a mirrored mail of the user's went out under),
  an entry that is no address, a wildcard over a public suffix, and the old
  VIP projection (`…email-whitelist:contacts`: people already).
- One projection into the gate's policy files and one commit per chamber, at
  the end. Where **no chamber keeps contacts** nothing is written and the
  step stays pending.

**What a briefed contact costs** — the report and the thread say it. Every
contact the migration briefs buys a model turn for each mail from then on,
and the mail's ring waits for that turn's verdict, up to
`MAIL_INSTRUCTION_TIMEOUT` (600 s by default). An instruction is the
**person's, on every channel**: a briefed person's Signal, WhatsApp, Telegram
and SMS messages buy a turn too, their ring waiting up to
`CHAT_INSTRUCTION_TIMEOUT` — and an SMS sender id, or a mail from a briefed
domain, can be forged (`SECURITY.md`). So the report counts the contacts
briefed, names those of them who also hold a messenger handle or a phone
(with the channels), counts the filing senders (a filing turn can outlast the
timeout; its mail then rings as a VIP's would), and gives the command that
takes an instruction off (`contacts.py update <id> --no-instruction`).

**Step `memories`** — every candidate memory tagged `sender:…`, read through
`memory.py`'s own recall query (a compaction summary is replaced by the
members it keeps, each judged by its own time against the cutoff: its own
text is a digest of a topic, not a rule).

- **Whose rule it is** is established without reversing the tag, which is a
  lossy slug: every address, domain and handle of the address book, every
  sender the mirror has seen, every snapshot entry and every address or
  domain the memory's own text spells out is slugged forward, as `memory.py`
  slugs a tag, and compared. A tag two senders slug to is settled only by the
  text naming one; a tag nothing slugs to is reported. A memory tagged with
  an address *and* its domain goes on both contacts — the holder of an
  address answers for it before the domain does. A sender with no contact
  gets one, as in the whitelist step.
- **What a memory is** is judgement, so one **model turn** classifies it
  (frontier tier; no tools, no session, no project context; the memories
  handed over as data, without the contacts — where a rule goes is the tags'
  to say; a batch per forty), answering per memory only `{id, kind}`:
  - `rule` — the user's standing rule for the sender: the contact gets the
    **memory's own text** (cleaned of the invisible characters that hide or
    reorder text, the joiners kept; never cut — a text longer than an
    instruction stays a memory);
  - `filing` — the sender's mails are documents to file, and nothing else:
    the contact gets *"File the document with the Archivist, book it, then
    archive the mail."* — **only when the memory's own text passes a
    deterministic check**: it names a kind of document (receipt, invoice,
    payslip, statement…) *and* filing it, by a multilingual table kept as
    data (`FILING_TERMS`). Otherwise it is a `rule`, in its own words;
  - `fact`, `unclear` — a fact or a status about the sender; or a message's
    content, an attempt to instruct the turn, anything the turn cannot tell:
    left alone and reported.
- **No model output ever becomes instruction text.** What is accepted of the
  answer is only ids the script sent, each once, each with a known kind;
  anything else in it is not read. So the most a steered or mistaken turn
  can do is put a note triage held, in that note's own words, on the contact
  that note's own tags name — what `--no-model` does with every note. (An
  earlier build let the turn reword a rule and held the wording to "half its
  words are the memory's"; that check is gone with the wording.) The
  decisions are journaled before anything is written from them, so a resumed
  run does not ask again. `--no-model` skips the turn and carries every rule
  over verbatim, unjudged.
- **Several rules for one contact are joined**, oldest first, after whatever
  instruction the contact already has — which is kept exactly as it was
  written. A rule that would take the instruction past its limit is not cut:
  it stays a memory and is reported.
- **Each moved memory is superseded** through `memory.py store --supersedes`
  (actor `ara`), with that script's guards as they are — nothing is forced —
  by one entry per group of memories sharing a sender tag, which says the
  rule now lives on the contact's instruction. A refusal is reported in
  `memory.py`'s words and the step stays pending; the next run only
  supersedes — it does not write the rule onto the contact again, so a rule
  the user changed there meanwhile stays as they left it.

**Step `report`** — what was created, extended, moved and left, each with its
reason, and the memories that were not triage's; what a briefed contact costs
(above); the scheduled jobs still in a chamber's `.schedule.json` — those
that run the gate script (theirs to remove: the script only prints a notice)
apart from prompt jobs that merely mention triage (to check: a job that
recovers messenger messages the chat rail could not take still belongs); the
old status store's location and size (kept, read by nothing); how many triage
threads were open (they stay as ordinary conversations). The entrypoint's run
opens **one** dashboard thread with the counts — no name a mail carried and
no memory text reaches it; the only names in it are of people the address
book held, reduced to plain words — once the migration is complete, or on the
third boot that leaves it pending, and **follows that thread up** when a
migration it reported unfinished completes. A deployment with nothing to
migrate gets none — decided only once the steps are complete: a boot that
could not read the store settles nothing. While the mail gateway is off the
report's thread waits; the thread of its own says what there is to say.

After the migration **no filing rule lives outside a contact**.

## Out of scope

Threads with Ara as chats; a people view across channels; clustering; a daily
sweep; changes to the attention model beyond the mail default above.

## Operator notes

The running side of this document is `docs/messaging.md`, "E-mail (the mail
gateway)": configuration, the backfill, what `/gateways` shows, named
accounts. The points an operator meets first:

- **One thing to set for the default account: `MAIL_GATEWAY_TOKEN`.** The
  gateway reads the mailbox variables `.env` already holds for
  `email_client.py`, and the base compose runs it in every deployment — but it
  mirrors and serves nothing until the token is set (both services read it).
  So an upgrade that changes nothing in `.env` mirrors nothing; setting the
  token is the decision to mirror. After the upgrade that retires e-mail
  triage nothing else handles mail: set the token as part of it.
  `MAIL_BACKFILL_DAYS=0` starts the mirror with no history.
- **A pinned channel set must name `email`.** A deployment that sets
  `MESSENGER_BUILTIN_CHANNELS` adds `email` to it, in `.env` (a value set only
  on the `retinue` service does not reach the gateway); without it the
  default account's gateway idles as not configured.
- **`MAIL_OWN_ADDRESSES` lists every address of the user's** that may appear
  on the mailbox's mail — aliases and the user's other mirrored accounts —
  one value for every mail gateway.
- **The credentials are in two containers**: the mail gateway's and, as
  before, the `retinue` container's, whose `email_client.py` backend still
  uses them (`SECURITY.md`).
- **The gate needs nothing set.** Compose wires the chats rail and the news
  rail, and the rail takes a mail event only with a token: `CHATS_INGEST_TOKEN`
  where `.env` sets one (compose then passes it to both services), else the
  `MAIL_GATEWAY_TOKEN` both already hold; with neither it refuses every mail,
  and a request a browser marks cross-site is refused whatever it carries.
  Who is a VIP and who is instructed comes from the address book
  (`contacts.py`, the Contacts page), which projects it into the ledger
  volume's `policy/policy.nt`. `MAIL_NEWS_FOLDER` says where consumed news
  mail goes (unset: the archive folder; set and empty: nowhere).
  `/health.waiting` on the gateway says how many mails the chat surface has
  not taken yet.
- **Writing needs the policy and the erase token on the gateway**:
  `EMAIL_SEND_POLICY` (the same `.env` value `email_client.py` reads; unset,
  every send queues), `SEND_APPROVAL_BASE_URL` for the approval link and
  `CHAT_ERASE_TOKEN` (unset, deleting a mail chat is refused). Both are
  named on the service in compose; `MAIL_TRASH_FOLDER` and
  `MAIL_SENT_GRACE_SECONDS` tune where Delete moves mail and how long a sent
  mail's Sent copy is waited for. On the `retinue` service compose names the
  web gateway's and `mail-push.py`'s own tunables (`MAIL_SEND_TIMEOUT`,
  `MAIL_GATEWAY_TIMEOUT`, `CHAT_PENDING_TIMEOUT`, `CHAT_PENDING_RETRY_SECONDS`,
  `CHAT_PENDING_SYNC_SECONDS`, `MAILBOX_NONE_SECONDS`, and for the gate
  `CHAT_ARRIVAL_RETRY_SECONDS`; table in
  `docs/messaging.md`), since that service loads no `env_file`.
- **`mail-data` holds data, not only an index**: besides the thread and mailbox
  indexes it keeps the mails waiting for approval with their attachments and
  the journal of sends in flight. Recreating the volume drops those.
- **The e-mail policy file moves** to
  `chambers/_generated/messenger/email/policy/email-policy.nt`, on the
  gateway's ledger volume; the retinue side moves the old
  `_generated/triage/email-whitelist.nt` there first thing at start. The
  mirror waits for the file before its first backfill — with no limit unless
  `MAIL_POLICY_WAIT_SECONDS` is a positive number. An old file that differs
  from the new one is left in place and reported, never deleted.
- **A named account** is a second gateway service with `MAIL_ACCOUNT_NAME`,
  enrolled in `MESSENGER_GATEWAYS` with `"channel": "email"`
  (`docker-compose.override.example.yml`).
- **Not running it at all**: give the service a profile nobody enables in the
  override (`profiles: ["off"]`) and leave `email` out of
  `MESSENGER_BUILTIN_CHANNELS`, as for any built-in channel never run, so the
  monitor does not report the absent container as an outage. Only then does
  the mailbox password stay out of a second container: compose hands it to
  the gateway whether or not it mirrors.
- **The model trim needs nothing set.** It is on by default and spends at
  most `MAIL_MODEL_TRIM_DAILY_MAX` router-tier turns a day, one per mail the
  rules left `wanting` that the user actually looked at; `MAIL_MODEL_TRIM=0`
  on the `retinue` service switches it off, and every bubble then keeps the
  deterministic text. The mail gateway reads none of its tunables.
