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
a mail it holds as a chat. The gate
(phase 3) and the model trim (phase 4) are still to come: until then no mail
pushes or starts a turn.

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
  (`triage_policy.email_gate_decision`) starts no chat. The news rail takes it
  as before — until the gate phase that is e-mail triage's filing, which runs
  beside the mirror unchanged; from then on the gateway's. (It joins a chat
  only under the rule in *Decisions*.)
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
placeholders: {standing, made, why}, lost, can_send, cannot_send_reason,
rewritten_message_ids}` — `backfill.days` is the window fixed at the first
run; `can_send` is false, with the reason, where no SMTP server is set, and
`rewritten_message_ids` counts the gateway's own sends its provider was seen
to file under a `Message-ID` of its own (see *Writing*). `blocked` names what holds the mirror when that is not the
mailbox: `"policy"` (below) or `"mirror"` — it stopped on a fault of its own,
and `error` says on what. `placeholders` counts the mail that could not be
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
header block replaces it, wherever it lies by then. A mail that merely
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
That one mail is read as any mail is read the first time where it lies —
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
mail that came in too — its From is all that is left to say so; and an
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
(inbound) and `kb:messageId` the `Message-ID` without brackets.

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
  gateway never writes policy: `email_client`'s whitelist-on-send is disabled
  inside it.
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
  there for a gateway prompt that carries a token; nothing issues one for mail
  in this phase, and agent-facing texts do not point at it.) A mail that
  e-mail triage hands an agent — a UID, no chat id — is answered with
  `email_client.py reply --uid`, as the `triage` skill still says, because
  triage's status store records the `sent_uid` and the draft queue that `reply`
  returns; that holds until triage retires in the gate phase. `email_client.py send/reply` keeps
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

## Operator notes

The running side of this document is `docs/messaging.md`, "E-mail (the mail
gateway)": configuration, the backfill, what `/gateways` shows, named
accounts. The points an operator meets first:

- **One thing to set for the default account: `MAIL_GATEWAY_TOKEN`.** The
  gateway reads the mailbox variables `.env` already holds for
  `email_client.py`, and the base compose runs it in every deployment — but it
  mirrors and serves nothing until the token is set (both services read it).
  So an upgrade that changes nothing in `.env` mirrors nothing; setting the
  token is the decision to mirror. `MAIL_BACKFILL_DAYS=0` starts the mirror
  with no history.
- **A pinned channel set must name `email`.** A deployment that sets
  `MESSENGER_BUILTIN_CHANNELS` adds `email` to it, in `.env` (a value set only
  on the `retinue` service does not reach the gateway); without it the
  default account's gateway idles as not configured.
- **`MAIL_OWN_ADDRESSES` lists every address of the user's** that may appear
  on the mailbox's mail — aliases and the user's other mirrored accounts —
  one value for every mail gateway.
- **The credentials are in two containers**: the mail gateway's and, as
  before, the `retinue` container's, whose `email_client.py` backend and
  e-mail triage still use them (`SECURITY.md`).
- **Writing needs the policy and the erase token on the gateway**:
  `EMAIL_SEND_POLICY` (the same `.env` value `email_client.py` reads; unset,
  every send queues), `SEND_APPROVAL_BASE_URL` for the approval link and
  `CHAT_ERASE_TOKEN` (unset, deleting a mail chat is refused). Both are
  named on the service in compose; `MAIL_TRASH_FOLDER` and
  `MAIL_SENT_GRACE_SECONDS` tune where Delete moves mail and how long a sent
  mail's Sent copy is waited for. On the `retinue` service compose names the
  web gateway's and `mail-push.py`'s own tunables (`MAIL_SEND_TIMEOUT`,
  `MAIL_GATEWAY_TIMEOUT`, `CHAT_PENDING_TIMEOUT`, `CHAT_PENDING_RETRY_SECONDS`,
  `CHAT_PENDING_SYNC_SECONDS`, `MAILBOX_NONE_SECONDS`; table in
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
- **Not in this phase**: the gate, the gateway's own news filing and the
  retirement of e-mail triage (phase 3), the model trim (phase 4). Until
  phase 3 no mail pushes or starts a turn, whatever a contact's prior or VIP
  flag says.
