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

**Status.** Built in five phases, each shippable alone. The three groups and
phases 1–3 are built; only the model trim (phase 4) is outstanding.

| Phase | What ships | State |
|---|---|---|
| **Three groups** | Unread · Read · Archived for every channel; no read-on-open; *Mark as read* | built |
| **1 — read-only mirror** | the mail gateway, mail chats rendered, the deterministic trim, the original view | built |
| **2 — writing** | reply and compose under the send policy; the mailbox follows the chat | built |
| **3 — the gate** | VIP / instructed turns for mail; triage and the whitelist retire; the migration | built |
| **4 — the model trim** | one router-tier turn per mail the deterministic trim could not settle | outstanding |

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
The model trim (phase 4) is the one thing still to come.

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
placeholders: {standing, made, why}, lost, waiting: {rail, since, news,
failing, failing_since}, can_send, cannot_send_reason,
rewritten_message_ids}` — `backfill.days` is the window fixed at the first
run; `can_send` is false, with the reason, where no SMTP server is set, and
`rewritten_message_ids` counts the gateway's own sends its provider was seen
to file under a `Message-ID` of its own (see *Writing*). `waiting` says how
many mirrored mails the chats rail has not taken yet (and since when the
oldest of them has been in the mailbox), how many news mails wait for the
feed or for their move out of the INBOX, and how many rail events the web
gateway keeps failing on (*The gate*, "One queue, in order"). `blocked` names
what holds the mirror when that is not the mailbox: `"policy"` (below) or
`"mirror"` — it stopped on a fault of its own, and `error` says on what.
`placeholders` counts the mail that could not be
read (below): how many placeholders stand, how many were `made` since this
start, and `why`, by reason — a mail that cannot be read leaves the mirror
`connected`, so a run of them shows here, whatever the cause, and the
connection monitor reads it: when five or more stand beyond those it last
told the user of, it says so in a thread of its own — a notice, not an
outage. `lost` counts the mails whose record is gone from the ledger and
that are still looked for in the mailbox (below); the monitor counts them
with the placeholders.
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
beyond either it is not parsed to the end. The parameters of all its parts'
`Content-Type` and `Content-Disposition` fields have a sum of their own
(semicolons × length, a thousand million), since the parser splits them at a
cost that grows with both.

**And so is the time one mail may take**, which is the bound for what the
others did not foresee: each of them closes one way a mail made the standard
library's mail code spend the square of something its sender chose, and each
was found when some hundred kilobytes had held the mirror's one thread for a
minute. Reading a mail has a deadline — `MAIL_READ_SECONDS` (30), and one
more for every megabyte fetched; an ordinary mail reads in a millisecond, the
heaviest honest one in a second or two. The gateway reads its mail in **a
process of its own** (the same script, started with `--reader`, given none of
the gateway's secrets): the reader looks at the clock wherever its own code
runs, and where a single call into the library does not come back, the
process is ended and replaced. Either way the mail is a placeholder — it took
too long to read. (The slowest mail the bounds let through is one of
thousands of parts under encoded file names, at about a second a megabyte:
that is what the second per megabyte is for.) The header block a placeholder
is made from, and the one a mail is found again by, are read the same way —
with one difference, because of what is decided on them. A header block
whose reading does not come back is read once more, after the reader has
shown on the canaries that it works; if it still does not come back, that is
**no answer**: the mail looked for is left as it is, nothing is remembered,
and no mail is called gone on it. (Where the folders are followed, or old
mail read, a placeholder must be made all the same, so the folder can go
on: it is made without the block, under an id of the block's bytes, by which
it is found again with no reading at all, and which gives way in one step
once the mail reads under its own. The mail of a lost record — below — has
no folder to keep going: its id is simply looked for again later.)

**A mail that cannot be read is still there.** One mail's failure never stops
the folder, or the backfill: a mail the gateway cannot read is mirrored as a
placeholder that says so (with its sender, its time and its header block,
unchanged in the mailbox), at once. It is read again at start and every hour
(less often each time it changes nothing; a round of such readings has two
minutes, and what it does not get to waits for the next — a mail that took
too long to read takes that long again), and takes its placeholder's place
when it reads — that mail, and no other: the placeholder's record keeps the
header block of the copy it was made from, and only a mail with that very
header block replaces it, wherever it lies by then (and is owed the rail
what the placeholder was: *The gate*, "As built"). A mail that merely
arrives under a placeholder's `Message-ID` replaces nothing, whether it can
be read or not; ids are their sender's to choose. While the placeholder
stands it is set apart as another mail's: the placeholder is never read
again from it, and is not kept by it — when the placeholder's own copy has
left the mailbox the placeholder leaves a tombstone like any mail, and what
still lies there under its id is then mirrored as the mail it is. Once the
placeholder has given way to its mail, the other is one more place of the
mail on record, as a second copy of any mail is. Which is which is read off
the mails, every time they are looked for: a folder may hold several mails
under one id, all of them are looked at, and the placeholder's own copy may
be the older one — after a move to the archive, or a folder renumbered.

A mail whose record is written again — a placeholder that reads, a mail on
record from its INBOX copy whose Sent copy shows, a tombstone whose mail is
back — gets its new record in the old one's place, in one step, and only
once that is written: until then, should writing fail for an hour or the
process be stopped, the old record stands as it was, and its mail is followed
like any (and tombstoned like any, if it leaves). A placeholder gives way only
to the mail its record keeps the header block of; a UID is taken to be the
mail the index has there only while its folder's `UIDVALIDITY` is the one it
was recorded under. (A record whose file is gone when it is to be written
again is written as the first time, and the index is matched with the
ledger again.) A record of the user's own that is written again keeps who
wrote the mail, and any record written again — whichever way, one that
came in included — every id its provider gave the mail (`kb:sentAs`): the
gateway's own send is on record with its author from the start, and stays
so when its Sent copy comes back from the Trash; a restart knows the same
ids, and a follow-up cites the one the recipients have. An id its provider
gave a send names that send only on the send's own evidence — every
recipient has it, and every answer cites it. In Sent, a copy under it is
the send's as the gateway's own send is matched there (From the account, to
the people it went to, dated when it went, naming no other send): in a
renumbered Sent, moved out and back, a second copy, it is one more place of
the record under the id the gateway minted, and where that record is a
tombstone, the record is written again in its own place, under the minted
id, by the user; never a second record under the provider's id. (A
tombstone keeps no addresses, nor does a placeholder, whose were never
read: its copy is told by the rest.) Outside Sent, nothing a recipient
holds is evidence that a mail is the user's send — not the gateway's
`X-Retinue-Message-ID`, not From, To, Cc or Date, alone or together: the
mail that went out carries them all, and whoever got it can write them on
a mail of their own. A mail there under the provider's id is
the send's only by the very header block its live record keeps (the copy
the record was written from, moved), and then only one more place of it;
it never revives the send's tombstone, and never rewrites the record —
unless that record is a placeholder made from this very copy, which then
takes its place as it would under any id, as what the record already is. Any
other mail under that id — a stranger's, a recipient's forgery of the
send's header block — is a mail of its own that came in, recorded under its
own id, and no place of the send's: the send stays what it was, a tombstone
the user's until the send's own Sent copy comes back. Met before the send's
Sent copy, it is that still, and the Sent copy, matched to the send, is the
send's place beside it. So on a provider that rewrites ids, the INBOX copy
of a mail the user sent to themselves (a Cc to their own address) is a mail
of its own under the provider's id, come in From the user's own address,
beside the send — the chat shows it once as sent and once as received, as
any mail client does.

A mail whose record is gone from the ledger (restored from an older backup,
a file lost) is known no more: its row and its places go, and what the row
said — the id, the sender, the direction, the chat, when the mail arrived —
is kept as hints for finding the mail, never as knowledge of it (the
`lost` list; nothing in it is a record, a place or a claim). The rest of
the work looks for each such id by its `Message-ID` in Sent, INBOX and the
archive (and, for a mail of the gateway's own whose provider filed it under
an id of its own, by that one too — the ids stay known while it is looked
for) — every mail under it in every folder, so a move or a renumbered
folder makes no difference — at most `LOST_BATCH` ids a pass, the one
tried least recently first, before the backfill and never in the way of
new mail. Of what is found, ONE mail is read as the record's: for a row
that said the mail was the user's, a Sent copy, whenever the server
received it (a client may append its Sent copy late); then the copy the
server received when the row said the mail arrived, an archive copy first;
then the rest. For any other row, the copy From the sender the row named
(the one received when the row said, else the first). A From is never
evidence by itself: whoever was sent a mail knows its `Message-ID` and can
write any From. What is: a copy in Sent (only the user's client or the
gateway puts one there), or a copy received when the row said (a move
keeps the arrival time; a look-alike delivered a second or more later does
not have it). A match whose header block does not come back counts as From
nobody and comes last; it holds up nothing where the first is known to be
the record's — a Sent copy, or one From the row's sender received when the
row said (in the archive, for a row that said the mail was the user's).
That one mail is read as old mail — told to the rail, never offered: it was
heard of before — and as any mail is read the first time where it lies —
what a Sent copy holds is the user's, what is in the archive the user's when
it is from one of the user's own addresses and left Sent unseen — with one
difference: in the archive, From that very sender and received when the row
said, where the row said the mail was the user's, it is the user's again,
whatever address that is — and found under an id its provider gave a send of
the gateway's own, the record again under the id the gateway minted, with
the provider's as `kb:sentAs` and the author noted for it. Never in INBOX,
nor in the archive on its From alone: a look-alike From the user's own
address stays a mail that came in — in INBOX, archived, or in an "all mail"
archive — and a second copy where the Sent copy is there. The others found
are read as further copies of any
mail are (one more place, another mail's, or a placeholder). Found in no
folder, with every folder sure of it, the id is dropped. Where that cannot
be told yet — a folder that would not answer, a header block that did not
come back — the id stays, is looked for again less and less often, and holds
back none behind it; `/health` counts it. An id that cannot be searched for
(no `Message-ID`, or one the search cannot spell) is found by reading
headers, and the copy received when the row said is read without waiting for
that reading to cover the whole window. After `LOST_TRIES` (8) tries in
vain, what was found is read as mail nothing is known of, a log line says
so, and the id is looked for no more — but where every match is gone between
the search and the reading, or one is and what is left is not known to be the
record's, it is looked for so once more, an hour on.

What this leaves, plainly: while the archive does not answer, every lost id
waits (`/health` says the archive does not answer, and counts them); an id
whose mail is in no folder, or no more where the search found it, leaves
the list with a log line and no `removed` event — there is no record to
tombstone — so a mail moved under both of the last looks can be left
unmirrored, and a look-alike under its id read in its place;
a ledger restored from an older backup over a live one can leave two
record files for one id (a record written as the first time takes a name
of its own, and the backup brings back the one it had before); a mail to
oneself whose Sent copy is gone comes back as a mail that came in —
nothing but its From says the user wrote it; on a server that stamps a
moved mail with a new arrival time, an archived sent mail comes back as a
mail that came in too — its From is all that is left to say so; so does
an archived mail the gateway sent whose Sent copy the provider filed
(`SMTP_SAVE_SENT=false`): the record's arrival time is the gateway's clock
at the send, the copy's the provider's at the filing, and they agree only
by chance (found under an id the provider gave it, it also comes back under
that id, not the one the gateway minted); and an
arrival time has whole seconds, so a look-alike under the id, From the same
address, delivered in the same second, cannot be told from the record's own
mail: archived, with the Sent copy gone, it comes back as the user's, and
where it and the record's mail lie in different folders, the folder
decides which is read as the record — for a mail that came in, a
look-alike in INBOX before the record's mail archived.

The bookkeeping behind this keeps four rules, written out above `class
Mirror` in the gateway: an answer that was not obtained is never an answer; a
record is replaced or buried only on evidence about its own mail; a record
stands until what replaces it does; and a fault delays only the work that
failed.

**Whose fault a failure is, is decided by what failed.** *Reading* a mail —
the parse, the walk of its parts, the trim — fails on the mail's account when
the same reader still reads the known-good mails the gateway carries with it
(the canaries, one for every path a mail takes through the reader: a plain
reply beside its HTML, HTML alone, an inline image and a nested mail):
whatever was raised, one of the bounds above or a recursion or memory error
among them, the mail is a placeholder and the mirror as healthy as it was.
Time is judged the same way: after a mail ran out of it, the canaries are read
against the clock too, with a tenth of `MAIL_READ_SECONDS` for their few
milliseconds — on a machine slow enough to time out ordinary mail they time
out first, and that is no mail's fault. *Writing* — the ledger, the indexes,
the blobs — never fails on a mail's account. A reader that cannot read the
canaries either, or not in their time, and any failure in writing, is a fault
of the mirror itself, which must not turn the mailbox into placeholders one
mail at a time with `/health` still `connected`: the pass ends there. No mail
is set aside or marked over it, and no folder's mark moves past a mail that
has neither a record nor a place — so the next pass reads it again, after a
restart too, and if it has left for the archive by then it is read there, as a
mail that came and went. A mail whose record is to be written again and
cannot be keeps the record it has meanwhile (see above). `/health` reports
`connected: false` with `blocked: "mirror"`
meanwhile, and `error` says on what — naming the mail, where the pass stopped
at one. A fault delays only the work it was in. Following the folders — new
mail, vanished mail — is done first and by itself, every poll interval; when
that is what fails, it is tried again after one interval, then twice as long
each time, up to an hour (every try fetches the mail in hand whole again).
When the rest fails — finding mail again, reading mail again, the backfill —
it is the rest that waits so, and new mail goes on being mirrored: `error`
says that too.

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
| `kb:gateSender` | inbound: the address the gate takes the mail to be from (*The gate*, "Who the sender is") — absent when it has none, a header block cut at its bound included, and on a placeholder (*The gate*, "As built") |
| `kb:recipient` (multi) | `To` addresses |
| `kb:cc` (multi) | `Cc` addresses |
| `kb:inReplyTo` | the parent's id |
| `kb:references` | the `References` ids, space-separated, in header order (then any further `In-Reply-To` id) |
| `kb:arrivedAt` | when the mailbox server received the mail (IMAP `INTERNALDATE`); kept on a tombstone |
| `kb:listId` | the normalised `List-Id` |
| `kb:bodyPart`, `kb:htmlPart`, `kb:headerPart` | media IRIs (see above) |
| `kb:contentId` (on an attachment IRI) | the part's `Content-ID` |
| `kb:trim` | `deterministic`, `wanting` (a model trim is due on first view) or `model` |
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
markup are parsed, not counting inline `data:` images (but one that a
browser does not read as all in one attribute value — folded into lines, in
a value in quotes that close — and any after a CDATA section, which mail
almost never has): beside a plain part a longer
one is not consulted, and where it is the mail's only text the bubble ends
where the reading did, on a line `[…]` — *Original* shows all of it.
Within those bounds the cost is linear in the size of the mail, for hostile
mail too.

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
  The user's own is a mail on record as theirs; one that came in `From` an
  address of theirs (an INBOX copy of their own mail, or anyone's mail
  claiming that `From`) has no say in the addressing — its `To` and `Cc` are
  its sender's to write. Where only such mails are left (the user wrote from
  another address of theirs, or "send as" from a phone, with this mailbox in
  copy and the Sent copy in no followed folder), the newest of them is
  followed up as the user's own is: to its `To` and `Cc` but the account's
  own addresses, and to that address only where nobody else is left — the
  same exposure as any mail's reply-all, shown in the preview before
  anything is sent. So a forgery `From` the user steers no reply beside a
  real mail; in a thread made of it alone, the reply goes where a mail
  `From` its writer would. The cost: beside real mails, a mail the user
  wrote from another address of theirs adds nobody to a reply (Ann writes,
  the user answers her and Bob from work: a reply-all goes to Ann and her
  `Cc`, not Bob).
  An older record does not keep `Reply-To`: its header block says it, and is
  read in the reader process — only where the block has a `Reply-To` line, so
  a reply to a mail that named none waits for nothing, and once for both
  modes. The wait that remains: a reply to such an older record whose mail
  has a `Reply-To`, while the mirror reads a heavy mail, waits for the reader
  — up to that reading's deadline; past the web gateway's preview budget the
  preview fails and the send is refused client-side, and the press made again
  once the reading is done finds it answered.
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
  no copy appearing it stays a live record, the mail having been sent. A
  provider that files the copy under a `Message-ID` of its own has it matched
  to the send (by the gateway's `X-Retinue-Message-ID` header with the
  account, the recipients and the date, or where it kept none by the mail
  itself), and the provider's id is kept as `kb:sentAs`; a copy the reader
  cannot read is matched by that header with the account and the date — no
  placeholder of its own in a chat of its own. The
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
**same-origin only**. The flags, like a send, are also accepted only through
the reverse proxy; an agent sets them through `POST
/internal/chats/<id>/flags` (`chat-flags.py`). Delete is the exception: it needs `X-Chat-Erase-Token`
(`CHAT_ERASE_TOKEN`; 503 while unset, 403 on a mismatch), and the gateway must
succeed before the web gateway erases its own state — if any move fails, or a
mail of the thread cannot be placed, nothing is erased and the answer reports
`errors > 0`, with the reason as one sentence in `error`. A mail of the thread
whose record the ledger lost (still looked for, above) is one of them — of
the thread it is in now: a merge takes it along — it is looked for under
every id it is known by and moved to the Trash with the rest (a copy the
server keeps flagged `\Deleted` is kept as any other is, below), and while a
folder cannot say where it is, nothing is erased. The answer is
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
`EXPUNGE`. Such a leftover is no place of the mail for a later read or
archive, and keeps it from being called gone; a delete asks after it, and
one a client has undeleted since is the mail's place again, moved to the
Trash with the rest. It is never the mail's only place for good: where the
server named no new UID and the look in the archive could not tell, the
mail settles and is looked for again until its copy there is found (a
leftover no longer flagged `\Deleted` is its place again). And a move is
noted before the server is asked, so a stop between the server's move and
the index following it is put right at the next start: the source UID gone
or flagged `\Deleted` is no place any more, and the copy in the archive is
found. And where a mail is looked for in
a folder — after a move whose new UID the server did not name, or by a
delete — every mail there under its id is looked at, as the mirror looks:
the mail of a placeholder is only the copy its record keeps the header block
of, and another mail under the same id is neither moved nor flagged; once
the placeholder is erased with its chat, that other mail is mirrored as the
mail it is.

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
and refused outright where a character of it stands in for an ASCII one on its
own — its compatibility form is ASCII, or its lowercase or case fold is one
ASCII character (a Kelvin sign, a long s, a fullwidth letter, a ligature of
ASCII letters). A letter whose case mapping merely holds ASCII letters beside
others, or several of them (`ß`, a dotless `ı`, a dotted `İ`), is a letter of
an internationalised address like any other, and stays. A mail with no
`From`, an empty one, a group, two addresses, two `From` headers, or an
address the grammar rejects has **no gate sender** — and so has a mail whose
header block was cut at its bound (256 KiB) or fetched short of its end
(`MAIL_MAX_MAIL_BYTES`), since a second `From` past the cut is never seen and
"exactly one" cannot be told: it reaches its chat, buys no turn, rings as
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
arrived in the chat after it, or an earlier one waits for a turn of its own,
the chat is not archived (the answer says so), and the mailbox is told only of
the mails the turn worked. What the turn asked is remembered on its mails once
it completes, so a newer mail is no reason to stay in view when a turn that
asked for the archive too has worked it: a batch too big to quote whole is
worked newest first, and the turn for its older mails, the last of the chain,
archives the thread when every mail after its own was worked by a turn that
asked. A mail that arrived since and has had no turn, or whose turn did not
ask, still keeps the thread on the list — and so does one whose turn asked
but is not trusted with having put it away (it ended without a verdict, or
the timeout rang the mail first: below), whose ask is not remembered. The
user bringing the thread back forgets every ask made until then: only a turn
asking after the user's last unarchive can archive the thread over a newer
mail.

A turn cannot mute: a mute it asks for is taken as that same archive, and the
answer says so (`a turn cannot mute; archived instead`) — muting stays the
user's. Archiving is all a turn can do with its chat's flags: its unmute,
unarchive and star change nothing, and the answer says so. Who is asking is
decided by the web gateway, not by the turn's cooperation:

- a call carrying the token the turn's session was handed
  (`RETINUE_ARRIVAL_TURN`, which `scripts/chat-flags.py` sends) is the turn's
  where that turn runs on the chat named, and is refused (403) anywhere else;
- a call carrying the proof a dashboard thread's turn is handed — a turn the
  user started by a message sent from the dashboard itself, not one a message
  posted from inside the container started (`RETINUE_USER_TURN`, sent too;
  valid while that turn runs) — has the user's meaning, a mute included, even
  while a turn runs on the thread;
- a call carrying neither, on a mail thread while a mail's turn runs on it,
  is taken as that turn's, and the answer says why (`a turn is running on
  this chat; …`), so that a scheduled job or another session asks again once
  the turn is over;
- any other call — no turn running, a messenger chat — has the user's
  meaning, as before the gate.

The user's own endpoint, `POST /chats/<id>/flags`, needs no token and is no
way round that rule: like a chat's delete and send it is accepted only
through the reverse proxy, and refuses a caller on loopback or on the
container's own address — or, where `EDGE_PROXY_PEERS` names the proxy, any
peer it does not name. Only where the proxy itself arrives on loopback (host
networking, `EDGE_PROXY_PEERS` including it) can the web gateway not tell the
dashboard from a caller inside the container, and such a call is the user's.

That rule holds for the turn's **own** chat **while the turn runs**, and no
further. It is a guard against a turn that was talked into something, not a
sandbox: an arrival turn's session holds the backend token every agent
session holds, so without its token it can set **another** chat's flags with
the user's meaning, and a process it leaves behind can do so on its own chat
once the turn has ended. And while a dashboard turn the user started runs,
its proof sits in that turn's process environment, which another session of
the same uid can read: sessions are not sandboxed from one another. What
keeps a turn to its brief there is the brief, as on the messengers.

A turn that does not finish — it fails, runs past `MAIL_INSTRUCTION_TIMEOUT`,
or sets no verdict — has the chat brought back (un-archived) and the mail rung
by the failure rule, since a turn that did not finish is not trusted with
having put a mail away. The user's own archive during a turn changes nothing
about the ring.

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
  nobody is there to take it. **A record written again** (*The mirror*: a
  placeholder that reads, under its own id or under the one of its bytes it
  was on record by) is owed what the record it replaces was owed, and no
  more: offered, with its gate sender, if the chat had not taken the
  placeholder yet; told once, as old mail, if it had — never offered twice.
  **A placeholder is from nobody** (no `kb:gateSender`, `gate_sender` empty on
  the rail, whatever its header block names): it reaches its chat and buys no
  turn — a turn would work a mail it cannot read, and its done would deliver
  the mail. So a VIP's mail that arrived unreadable is worked once it reads
  only if its placeholder was still owed then. **The mail of a lost record**
  (one the ledger lost, found again by its `Message-ID`) is told as old mail,
  never offered: it was heard of before. A copy of the user's own send under
  the id its provider gave it is that send, told under the id the gateway
  minted; a stranger's mail under that id is a mail of its own, offered like
  any that arrives.
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
  at all — the connection refused, a 401/403/404, or no answer to an event
  and none either to a GET that takes nothing — everything waits behind that
  point, and tries back off (30 seconds, doubling to 15 minutes). **One mail
  never holds back another**, for every rail event — an offer, the user's own
  mail, a held or backfilled mail, a mail that came back: an answer that is
  about one event — a 408, 429 or 5xx for it, or no answer for it while the
  rail answers otherwise — backs off that event alone. It keeps its place in
  the outbox and waits its own time (one minute, doubling to an hour), and
  only its own chat's later events wait behind it — so the order within a
  chat holds — while every other chat's go on. **A held event steps aside**:
  it holds its chat back only while the failure is young, for three of its
  own attempts (`RAIL_HOLD_ATTEMPTS`). Then a first offer leaves the outbox
  for the re-offers below, and a told event keeps retrying on its own clock
  without holding anything — and the chat's later events go on, so one mail
  the web gateway always fails on cannot stall its thread (a VIP's reply in
  it is offered, rings and is worked). The order within a chat is kept for a
  young failure, not for one that persists. So a mail that stepped aside may
  be offered after the user's answer to it from a mail client has been
  heard: it is offered late, as an arrival — its turn's prompt shows the
  user's answer, and it may ring once. That is the accepted cost of not
  stalling the thread. **A late event says what is true when it is sent**:
  a told event that stepped aside is checked against the mirror's index
  when it goes, not sent as it was queued — a removal names only the mails
  that are still gone, with the chat's `live` count as it is then, and is
  dropped when none is; a told mail that is gone by then (or erased with
  its chat) is not told at all; one that is there is told as its record
  says then, under the chat it is in then. A young held event keeps what
  it was queued with, since the hold keeps its chat's order. **A removal
  waits for the merge of its chat**: while a merge queued before it that
  joins its chat (on either side) is still queued, whatever that merge's
  attempts, a removal is not sent — in the web gateway the mail may still be under the chat the
  merge folds away, with the arrival it brings along — and nothing else
  waits with it: its chat's later events go on, so it is marked as
  overtaken and, once the merge is taken, sent as a late event is. A merge
  queued after the removal does not hold it: the removal names its chat as
  the web gateway still has it, and goes first, in its order. The wait
  covers a merge that names the removal's own chat, not a chain of them: a
  merge of X into Y that stepped aside, then one of Y into Z, and a removal
  naming Z goes once the second is taken, while the web gateway still holds
  the mail under X. So **a merge taken late re-announces what was removed
  under it**: when a merge that stepped aside is finally taken, the same
  transaction that takes it off the outbox queues a removal, built as a late
  one is (the ids still gone, `live` as then), for the chat each chat it
  absorbed is in now, naming every removed mail of that chat but those a
  removal still queued names (that one goes after the merge and says it) —
  the index does not remember which chat a mail was in before a merge, so
  the chat's other removed mails go along, and a removal of a mail the web
  gateway already holds as gone changes nothing. It is queued behind
  everything, so it keeps its chat's order and waits for a merge of its
  chat still queued. A first
  offer's place names the chat it was queued in as well as the one its mail
  is in now, so a merge meanwhile does not take it out of its old chat's
  hold. This holds for a merge or a removal too: a 408, 429 or 5xx for one
  is a failure about it;
  only a 404 — an older web gateway that does not know the endpoint — makes
  it wait without holding anything back. **A pass
  is bounded**: a call that gets no answer in time costs `MAIL_RAIL_TIMEOUT`,
  and after three of them in a row the rail counts as not answering for the
  rest of the pass and its backoff, so a web gateway that is up and too slow
  for every event costs a pass three timeouts, not one per owed mail.
- **Offered again.** A mail that was answered for and not taken leaves the
  outbox (its first offer has been made) and is offered again on later polls:
  after one minute, then twice as
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
  the web gateway has not answered three times running and mail waits — and
  while it answers and takes no mail: when every offer and event of a pass
  (three at least) was answered with a failure, or with none, the notice says
  "the chats rail answers but takes no mail" until a mail is taken. Events
  that have stepped aside are counted as well: `waiting.failing` counts the
  rail events (offers among them) the web gateway failed on three times or
  more, and `waiting.failing_since` says when the oldest first failed. The
  notice for them is worded from whether the rail has taken any event since
  the **newest** of them began to fail (the gateway keeps when it last took
  one, across a restart): if it has, "N rail event(s) keep failing while
  others are taken — the oldest for …" is about those mails; if it has not —
  a quiet mailbox or a short outage can fail on every call without a pass of
  three, and an older failure (a mail that always fails) says nothing of
  that — it says "N rail event(s) keep failing and the rail has taken
  nothing since the newest of them began to fail, … ago", which points at
  the web gateway, not at one mail (the log names them either way). An
  index from before the gateway kept its last take knows neither: it says
  "N rail event(s) keep failing — the oldest for …" until the first take
  (or a pass that takes nothing, which has its notice above). When a
  mail is taken again, what waited over the fault — each wait grown up to an
  hour meanwhile — is offered and told again at once.

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
entrypoint's `--step snapshot`, before any daemon — it fixes a **cutoff**,
which never moves. Only `sender:` memories recorded before it are
candidates: one recorded later is not triage's (the gate's own turns store
such facts, out of mail) and is reported as such, never shown to the model,
never carried by `--no-model`, never superseded. The cutoff is kept in three
places — the marker, a one-line file of its own beside it
(`.migration-cutoff`) and the snapshot — so that a marker that is lost,
restored from elsewhere or moved aside (as the advice for an unreadable one
says) does not move it: a run adopts the **earliest** of them and writes it
back where it is missing. Only when none of the three holds one is "now"
taken. One of them that is there and cannot be read stops the run, and the
log says how to recover that very file (`docs/contributing.md`, "Exit
status").

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
**Briefed by this migration** are only the people whose **first**
instruction it set, as its journal knows it — never as a contact's text
happens to read: the whitelist step created or instructed them, or the
memories step wrote where no instruction stood (it reads the contact and
journals that *before* the write, so a run killed in between still knows).
A contact the step is about to create is journaled with the creation time
it then gives it, to the microsecond: the entry stands only for that
contact, so where a run was killed before the create and the user filed the
person by hand meanwhile, "before" is read from the contact they made.
One of them whose instruction the user has added words to since is reported
as **briefed and edited since** — still briefed, so their messages buy a
turn because of the migration, but kept apart from the people the thread's
chip takes the instruction off, since that would take the user's words too.
**Extended** are the people a sender rule was appended to whose instruction
stood before the migration: they bought a turn per message before, and the
report gives the way to take the rule back off — editing the instruction —
and never `--no-instruction`, which would take the user's own words with it.
A person who kept their own instruction and whose memory it already held is
none of these: nothing was written to them.

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
  - `filing` — the sender's mails are documents to file: the contact gets
    the **memory's own text** too, **first**, and only when that text passes
    a deterministic check — it names a kind of document (receipt, invoice,
    payslip, statement…) *and* filing it, by a multilingual table kept as
    data (`FILING_TERMS`) — one fixed line **after** it, worded as a default
    that yields: *"Unless the note above says otherwise: file the document
    with the Archivist, book it, then archive the mail."* The check cannot
    tell a negation, a condition or a second request from a plain filing
    note in every language, so the line never comes first and never says
    more than "unless the note says otherwise": the user's words stand above
    it, and win. A person gets the line once, as the instruction's **last
    paragraph**, and it is known by its opening: any paragraph that begins
    *"Unless the note above says otherwise:"* is the person's line, however
    the user edited the rest of it and wherever they moved it. A later note
    for them — a second filing note, a rule — goes in front of that
    paragraph, in its own words alone, and the line is never added a second
    time (nor does what the user deleted from it come back); what the user
    put below a line they moved up stays outside its "note above", as they
    left it. Where the words and the line together would pass the limit of
    the instruction they go on, the words go on alone — a
    filing note whose own words fit is never left a memory. A `filing` the
    text does not bear out is a `rule`, in its own words;
  - `fact`, `unclear` — a fact or a status about the sender; or a message's
    content, an attempt to instruct the turn, anything the turn cannot tell:
    left alone and reported.
- **No model output ever becomes instruction text.** What is accepted of the
  answer is only ids the script sent, each once, each with a known kind;
  anything else in it is not read. So the most a steered or mistaken turn
  can do is put a note triage held, in that note's own words, on the contact
  that note's own tags name — what `--no-model` does with every note — and,
  after a note that names filing its sender's documents, add the one
  yielding line; it can never put words in front of the user's, or in
  their place. (An
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
- **Still to come**: the model trim (phase 4). Phases 1–3 are built.
