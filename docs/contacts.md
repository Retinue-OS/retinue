# Contacts — people, not handles

*Read this before changing how contacts are stored, looked up or linked, or
before writing contact data by hand. The code is `scripts/contacts.py` (library
and CLI); the dashboard's contact card is one client of it
(`POST /chats/<id>/contact` in `scripts/web-gateway.py`).*

A contact is a **person**. Whatever channels reach them hang off that one
person: e-mail addresses, phone numbers, and their Signal, WhatsApp and
Telegram accounts. So "who is +41 79 …?", "what is Mara's e-mail?" and "every
chat with Mara" are questions about the same resource. Nothing about a contact
is specific to a channel. A handle is just a `(channel, handle)` pair, and
e-mail is one channel among the others.

## Where contacts live

**Every contact belongs to exactly one chamber and is stored inside it.** It is
versioned with that chamber's data, committed like any other operational file
(Tier 1), and indexed by the life store like every chamber file. A chamber's
entry in the deployment's `chambers.json` may declare where its address book is
kept:

```json
{"chambers": [
  {"name": "private", "url_env": "PRIVATE_CHAMBER_URL", "contacts": "people"},
  {"name": "work",    "url_env": "WORK_CHAMBER_URL"},
  {"name": "health",  "url_env": "HEALTH_CHAMBER_URL", "contacts": false}
]}
```

- `contacts` is a directory relative to the chamber root. Where nothing is
  declared — no `contacts` key, or no entry for the chamber at all — it is
  `contacts/`, so `work` above keeps its people in `work/contacts/`: keeping
  contacts is the default, for every chamber.
- `"contacts": false` opts a chamber out; `health` above keeps none.
- A declared path must stay inside its chamber; one that leads out of it is
  refused (logged) and the default is used.
- `_generated/` is the framework's own derived output, in no chamber's
  repository, and never holds contacts.
- **The default chamber** — pre-selected in the dashboard's chamber dropdown,
  and where older cards are migrated — is **the chamber the last contact was
  created in** (the newest `dcterms:created`), so the pick carries over from
  one contact to the next, across devices and from the CLI. While nobody has
  been created, it is the first chamber: the manifest's order, then any
  chamber it does not list, by name.

**Creating a contact always names its chamber.** The CLI requires `--chamber`,
`POST /contacts` requires `chamber`, and the contact card sends the chamber the
user picked. Which chamber a person belongs in is a judgement about the user's
life (a client belongs in the work chamber, a neighbour in the private one), so
no agent guesses it silently. When it is unclear, ask.

## The file

Each person is one file, `<chamber>/<contacts>/<slug>-<id8>.nt`, for example
`private/people/mara-keller-5f0c9a2e.nt`. The name comes from the person's name
when they are created and does not change when they are renamed, so git history
stays readable.

The format is **N-Triples**, which gives two properties at once:

- the gateway reads and rewrites the file without an RDF library on the serving
  path;
- a person or agent may add triples of their own (an address, a birthday, an
  organisation, a note). A rewrite removes only the triples this module would
  itself have written for the person's previous state, and the lines a read
  *took* into the person in a spelling of their own (below), and keeps every
  other line byte for byte, comments included. Deleting a person removes their
  managed triples; if anything else is left, the file stays.

**Values written by hand, twice or in another spelling.** A read takes a value
it understands into the person, whatever spelling a hand edit or a merged
chamber sync left: every domain, address and telephone (each once, in today's
spelling), the VIP flag (`"true"`/`"1"`, any case; any one says VIP), the
highest importance of the literals that hold one, and **every** instruction
literal that says anything, joined by a blank line — so no instruction hides
behind another, and the one the user is shown is all the gate reads. A rewrite
removes exactly what the read took, and only on these terms:

- a line the read did not take stays exactly as written: a domain that is
  none, a VIP flag or importance in a spelling this module does not read, an
  empty instruction;
- a rewrite that leaves the instruction as it was leaves its literals as the
  user wrote them (two stay two); one that changes it — the user edited the
  text they were shown, or the migration added a rule to it — writes the new
  text as one literal;
- a second importance below the one read stays while the importance is left
  as it was; a rewrite that sets the importance replaces every literal it was
  read from, so the value the user removed or lowered does not come back.

A person described by hand in the address-book directory (`foaf:Person` or
`schema:Person`, with a name, `vcard:hasEmail` or `vcard:hasTelephone`) is a
contact too. It is found by those handles and can be updated like any other.
The Turtle files next to it are indexed by the life store and their names feed
the transcript repair, but they are not managed.

### Vocabulary

Everything is an established vocabulary, following the ontology defaults
(`docs/ontology.md`). The only framework-owned identifiers are two IRI schemes
and the `kb:` terms the rest of Retinue already uses.

| What | Term | Vocabulary |
|---|---|---|
| The person | `a vcard:Individual` | vCard |
| Name | `vcard:fn` | vCard |
| E-mail address | `vcard:hasEmail <mailto:…>` | vCard (RFC 6350) |
| E-mail domain they write from | `kb:emailDomain "example.org"` / `"*.example.org"` | `kb:` (see *E-mail domains*) |
| Telephone | `vcard:hasTelephone <tel:+…>` | vCard, RFC 3966 |
| Messenger address | `vcard:hasInstantMessage <…>` | vCard (IMPP) |
| Person → account | `foaf:account` | FOAF |
| An account on a messaging service | `a foaf:OnlineAccount` | FOAF (SIOC's `sioc:UserAccount` is a subclass) |
| The account's handle | `foaf:accountName` | FOAF |
| The service | `foaf:accountServiceHomepage` | FOAF |
| The channel, as the message ledger spells it | `kb:channel` | `kb:`, as on every ledger record |
| Spheres | `kb:sphere` / `kb:tag` → `urn:retinue:sphere:<word>` | `kb:`, the attention model's IRIs |
| Importance prior (0–5) | `kb:importance "4"^^xsd:decimal` | `kb:`, as on attention items |
| Focus modes they may interrupt | `kb:permit <urn:retinue:mode:<id>>` | `kb:` |
| VIP | `kb:vip "true"^^xsd:boolean` | `kb:` |
| Standing instruction for their messages | `kb:instruction "…"` (free text) | `kb:` |
| When the person was filed | `dcterms:created` | Dublin Core Terms |

**Identifiers.**
- A person gets `urn:retinue:person:<uuid4>`, minted once. A person has many
  handles and must outlive any one of them, so the IRI cannot be derived from a
  handle.
- An account gets `urn:retinue:account:<channel>:<handle>` (percent-encoded).
  It is deterministic, following the ontology's rule that the same thing gets
  the same IRI everywhere.
- The CLI and HTTP API name a person by the UUID. A hand-written person with
  some other IRI is named by that IRI, percent-encoded.

### Handles, by channel

| Channel | Stored as | Also derived |
|---|---|---|
| `email` | `vcard:hasEmail <mailto:…>` (lowercased; `*@…` is no address — a whole domain is filed as a domain, below) | — |
| `sms` | a number: `vcard:hasTelephone <tel:…>` (E.164); a sender id or short code: `foaf:OnlineAccount` | — |
| `signal` | `foaf:OnlineAccount`, homepage `https://signal.org/` | for a phone handle: `tel:`, and `sgnl://signal.me/#p/<E.164>` |
| `whatsapp` | `foaf:OnlineAccount`, homepage `https://www.whatsapp.com/` | for a phone handle: `tel:`, and `https://wa.me/<digits>` |
| `telegram` | `foaf:OnlineAccount`, homepage `https://telegram.org/`; a username or phone is filed with the user id next to it | for a username: `https://t.me/<name>` |
| anything else (`matrix`, `threema`, …) | `foaf:OnlineAccount` | for a phone handle: `tel:` |

**Normalization.** Phone numbers lose punctuation, and a leading `00` becomes
`+`. A national number without a country code (`079 …`) is refused rather than
guessed. Handles that are not phone numbers (a WhatsApp LID, a Telegram user id)
are kept exactly as the channel spells them.
A Telegram username is filed as `@` plus its lowercase form, however it was
typed. A handle filed before a rule like this one existed keeps its old
spelling. A lookup, a removal and the uniqueness check compare it in today's
spelling, and filing the handle again rewrites it that way.

**Telegram ids.** Telegram delivers a message under the sender's numeric user
id only — never the username or phone number. The delivery gate, the chat card
and the instruction all compare that id, so a person filed as `@mara_k` alone
would never match a message of theirs. `add`, `update --add-handle` and the
dashboard's contact API therefore ask the Telegram gateway (`GET /resolve?q=`)
for the id of every username or phone they file, and file it next to the
handle; a name Telegram does not know, or an unreachable gateway, is refused
rather than filed unreachable. Telegram finds a phone number only among the
account's own contacts; a number it cannot find is refused like an unknown name.
A Telegram id typed directly needs no lookup.
Contacts filed before this rule get their ids with `contacts.py telegram-ids`.
It looks every name up before it writes anything, so an unreachable gateway
changes no contact. What it cannot do — a name Telegram no longer knows, a
handle that is no username or number, an id another person already holds — it
reports, and exits 2; every other contact is written all the same.
Removing a username does not remove its id: remove both.

**SMS senders that are no number.** Banks, couriers and carriers send from an
alphanumeric sender id (`HBL`, `DHL`) or a short code (`4636`). Such a sender
is filed as an account on the `sms` channel (`--handle sms:HBL`), in the shape
the SMS gateway delivers it: up to 16 letters, digits, spaces and `._&-`, with
at least one letter, or a short code of 3–6 digits. It gives the person no
telephone, since nothing can be sent back to it, and it is found and matched
case-insensitively, as the delivery gate matches it. So a sender like this can
carry spheres, permits, VIP and an instruction like any person. Keep in mind
that a sender id is trivially spoofed: anyone can send an SMS that says it is
from `HBL`.

**Uniqueness.** An account or an e-mail address belongs to at most one person
across all chambers: `add` answers 409 with the owner's name. An SMS sender id
counts as an account. A telephone can be shared (a family landline). An
e-mail domain belongs to one person too (below).

### E-mail domains

An organisation, a shop that sends receipts, a team behind many mailboxes
writes from a **domain**, not from one address. A person can carry the domains
they write from (`kb:emailDomain`, any number; the record key and the JSON
field are `domains`):

| Filed as | Covers |
|---|---|
| `example.org` | every address `@example.org` — and no subdomain |
| `*.example.org` | every address at `example.org` **or** at any domain below it |

The second reading — the wildcard includes its own apex — is the one the
delivery gate gives the same wildcard (`triage_policy._domain_matches_wildcard`),
so the address book and the gate can never disagree about who is covered.

A domain is **not a handle**: nothing can be sent to it, it is not in a
person's `handles`, and no chat is linked by it. It answers one question —
*who is this mail from?* — when no person holds the sender's address:

- **`ContactBook.find("email", address)`** answers the person who holds the
  exact address; else the person with the **most specific** domain covering
  it: the address's own domain filed plainly (`example.org`) before any
  wildcard, and among wildcards the longest (`*.eu.example.org` before
  `*.example.org`). A hit that came by a domain is a copy of the record marked
  `match: "domain"`, with `match_domain` naming the domain. So a person filed
  with their own address inside an organisation's domain is always themselves,
  and the organisation is everyone else there.
- **`find(…, exact=True)`** leaves the fallback out. That is the question a
  writer asks before it files, links or moves the address itself — a domain
  that covers an address does not own it — and it is what the uniqueness
  check, the contact card and the chat autolink use.
- `suggest` (the card's *This is …*, `find --email` on the CLI) offers the
  person behind the domain when nobody holds the address, as a guess to
  confirm (`exact: false`, `match: "domain"`).

**What is accepted.** A bare domain or `*.domain`, lowercased; the gate's
spelling of the same two (`*@example.org`, `*@*.example.org`) and a leading
`@` are read as what they mean. Refused: a wildcard anywhere else
(`ex*ample.org`, `*.*.example.org`), an address, an IP address, a single
label (`com`) and the shape of a public suffix (`co.uk`, `com.au`) — a domain
says "everyone there", so it has to be somebody's. The public suffix list is
not consulted (the module is stdlib-only): the rule is the shape, a generic
second-level label under a two-letter country code, which also refuses the
rare real domain of that shape; file its addresses one by one. A domain is
compared as written, like an address: an internationalised domain is filed in
the spelling its mail carries (normally the `xn--` form). A value written into
a file by hand that is no domain by these rules is not matched on, and stays
in the file. One that is a domain in another spelling (`*@acme.org`,
`ACME.org`) is matched as that domain and rewritten in the filed spelling on
the person's next write — so removing the domain removes that line too, as it
does for an address in capitals or a spaced telephone number written by
hand.

**Uniqueness.** One spelling of a domain belongs to at most one person, across
all chambers — claiming it twice answers 409 with the owner's name, like a
handle. `example.org` and `*.example.org` are two filings and may be two
people's; the lookup ranks them. An address inside somebody's domain can
always be filed on a person of its own.

**Think before filing a wide one.** The class a domain carries — VIP, an
instruction — applies to every sender under it, and the `From` address of a
mail is what its sender says it is. A VIP on a freemail domain makes a VIP of
everyone who has an account there; file those people by address.

**Whom the gate takes for a mail's sender.** Only the `From` header, and only
when the mail carries exactly one `From` header holding exactly one address the
mail gateway's own grammar accepts — that address, lowercased in its ASCII
letters only, is the mail's *gate sender* (`kb:gateSender` beside
`kb:sender`), and the one thing an address or a domain here is compared with.
The `Sender` header, a second `From`, or a `From` that lists several addresses
is shown, never matched: with no gate sender nobody is special — the mail
reaches its chat, buys no model turn, and rings as nobody's mail would. The
web gateway judges every arrival by its own gate sender (its class, the
person's prior and permits), never by whoever wrote last in the thread.

### Example

```turtle
# private/people/mara-keller-5f0c9a2e.nt, shown as Turtle for reading
<urn:retinue:person:5f0c9a2e-…>
    a vcard:Individual ;
    vcard:fn "Mara Keller" ;
    vcard:hasEmail <mailto:mara@example.org> ;
    vcard:hasTelephone <tel:+41791234567> ;
    vcard:hasInstantMessage <sgnl://signal.me/#p/+41791234567> ;
    foaf:account <urn:retinue:account:signal%3A%2B41791234567> ;
    kb:sphere sphere:friends ;
    dcterms:created "2026-09-05T16:40:00+00:00"^^xsd:dateTime .

<urn:retinue:account:signal%3A%2B41791234567>
    a foaf:OnlineAccount ;
    foaf:accountServiceHomepage <https://signal.org/> ;
    foaf:accountName "+41791234567" ;
    kb:channel "signal" .
```

## What a person carries beyond their handles

**The attention model's facts** (docs/attention-model.md) are the person's:
the sphere they belong to and further ones, the importance prior for a message
of theirs, and the Focus modes they may interrupt. The attention profile keys
these on the sender's display name, which every chat linked to a person shares;
the gateway's attention store lays each person over the profile on every load
and, on every save, writes back into the person what changed for them — a −/+
on the sheet, a permit, the contact card. So they follow the person across
channels, survive a rename, and live in the chamber with the rest of the
contact. What the profile knew about a person before they were filed is copied
into the person where the person leaves the field unset — the first time the
gateway sees them, at start or when they are filed while it runs (the CLI, a
hand edit) — before the overlay takes over.

**The VIP flag** says a model works this person's messages the moment they
arrive (docs/triage-delivery-gate.md; for mail, docs/mail-chats.md, "The
gate"). It is set on the person, never per handle: `contacts.sync_policy`
projects every handle of every VIP person into the delivery gate's policy
files as VIP handles on their channel — messenger accounts, telephones for
SMS, and on the `email` channel their addresses and their domains (*The
projection*, below). The projection sits under a subject of its own and is
replaced wholesale on every contact write and on the gateway's tick, so an
edit made outside the gateway (the CLI, a hand edit) reaches the gate within
seconds; VIPs set by hand for an unfiled handle (`triage_policy.py vip-add`)
are left alone.

### The instruction

VIP is one switch for "this person matters", and it is coarse: every message
rings at once, in every mode, and a model works it with no brief beyond "deal
with it". Two kinds of sender need something finer — someone whose few
time-critical messages (a shift moved, an appointment changed at short notice)
come mixed in with many that are not, and someone whose messages always want
the same handling (a care provider: draft a reply, put a moved visit in the
calendar, ring only when it applies soon). Both need a **brief the model
applies to each message**, and that brief, not the arrival, should decide how
loudly the message rings.

That is the **instruction**: free text the user writes on the person
(`kb:instruction`; at most 2000 characters). It is the **person's, on every
channel**: an instruction written for their mail also briefs a turn for each
of their Signal, WhatsApp, Telegram and SMS messages, whose ring then waits for
that turn's verdict. When a message arrives from a person with an instruction:

1. **One model turn works it, with the instruction as its brief** — the same
   rail VIP uses (a turn in the chat's companion thread, the chat's recent
   history in the prompt), with the instruction added and the sender's
   memories (`sender:<handle>`) pointed at. For a person who is also a VIP it
   is that same single turn, not a second one.
2. **The turn does what the instruction asks, within limits the system
   holds:** it stages a reply in the chat's shared draft (written by the
   Secretary, never sent), passes what the system knows (agenda, facts) to the
   Secretary for it, adds a calendar event, flags something to the user.
3. **The turn's verdict sets attention through the attention model.** It ends
   with `scripts/chat-verdict.py --chat <id> --importance <0–5> [--due …]
   [--action …] [--flag …]`. Importance and deadline are all it sets; whether
   the message then rings now, waits for the digest or is only listed is the
   attention model's decision in the Focus mode in force, as for any message
   (docs/attention-model.md). There is no delivery path of the instruction's
   own.
4. **Not a VIP: the work first, the ring second.** The arrival rings nothing;
   the ring is decided once the turn has finished, so a draft the instruction
   asked for is already in the composer when the user opens the chat.
5. **A VIP: the ring first, the work alongside.** VIP stays exactly what it
   is — the message rings the moment it arrives, in every mode — and the
   instructed turn starts at the same time. Its draft lands when it is ready
   (the chat shows that one is on its way); its verdict and flags are shown
   on the chat, and ring nothing a second time.

| | VIP only | Instruction only | VIP + instruction |
|---|---|---|---|
| A model works every message | yes | yes, by the instruction | yes, by the instruction (one turn) |
| Rings | always, at once | as the verdict decides, once the work is done | always, at once |
| The verdict decides | — | importance, hence ring / digest / list | nothing about delivery: it records |
| Delay before the ring | none | one turn, bounded by the timeout | none; the draft may follow the ring |

A user who would rather be rung only once the draft is ready sets the
instruction without VIP and has it ask for a high importance.

**What the system holds, whatever the instruction says:**

- **Failure rings.** A turn that errors, ends without a verdict, or runs past
  its timeout has the message rung as a VIP's would be — the reason line says
  the turn did not finish, not that the sender is a VIP. The timeout is
  `CHAT_INSTRUCTION_TIMEOUT` (60 seconds by default) for a messenger message
  and `MAIL_INSTRUCTION_TIMEOUT` (600 seconds by default) for a mail: a mail
  is not a chat message, ten minutes is a ring in time, and a turn that files
  a document needs them. An instruction can delay a ring by at most that long
  and never prevent one; a draft that finishes later still lands. So does a
  message whose turn could not be started at all. A mail whose turn keeps
  failing is given at most three attempts (`MAIL_ARRIVAL_TURN_ATTEMPTS`): it
  rings by this rule once, not once per attempt, and after the last it is
  delivered, with one note about it in the companion. The timeout rings; it
  does not stop the turn. A turn that files a document (the `archivist` takes
  longer than a reply does) can outlast it: the message rings as a VIP's when
  the timeout passes, the turn goes on, what it files and stages still lands,
  and its verdict then only records. On a
  messenger, an instruction that files documents wants a deployment that
  raises `CHAT_INSTRUCTION_TIMEOUT`, or its filed messages ring. Besides a chat
  the user muted, only a message the user has marked read or answered while
  the turn worked stays quiet (opening the chat is neither, and a mark taken
  back before the ring is due does not count): a ring would tell them nothing
  new, and would reopen the item their reply or mark settled. See *Unread ·
  Read · Archived* in docs/messenger-chats.md.
- **A turn cannot silence the mail it was given.** An instructed mail turn
  may archive its thread (`scripts/chat-flags.py`) where the instruction
  asks. It cannot mute it: a mute asked by a running arrival turn is taken as
  an archive of the turn's own mail (the answer says `a turn cannot mute;
  archived instead`). Muting stays the user's — their own flags, or an agent
  acting on their word outside an arrival turn. The archive a mail turn sets
  is recorded as the turn's own: a turn that ends with a verdict keeps it, and
  the mail rings nothing; a turn that fails, ends without a verdict or times
  out has it undone — the thread un-archived — before the failure ring. That
  undo is for mail only: a messenger turn's archive stands as it always did
  (docs/messenger-chats.md). And a turn's archive covers only what the turn
  was started for: if another mail arrived in the thread after the turn's
  own, the thread is not archived (the answer says `newer mail arrived`), and
  the mailbox is touched only for the mail the turn worked.
- **No sending on a messenger.** A reply is only ever staged in the chat's
  draft; the user's send press is the only way a chat reply leaves in their
  name. That is how the chat surface works for every agent
  (docs/messenger-chats.md): the send endpoint takes only the user's own
  request. **A mail thread is the exception, and only where the instruction
  says a reply is to be sent:** the turn may send with `scripts/mail-push.py
  --chat <id> --reply all|sender --file <path>` (the thread's own gateway
  and nothing else), under the mailbox's send policy — `verify` queues the
  mail and the turn flags it for the user's approval; `trust` and `allow` send
  unseen only where the instruction itself carries the permission phrase, and
  otherwise queue (`trust`) or fall back to the draft (`allow`). A mail's
  sender is not authenticated (SECURITY.md): instruct a sender, a domain above
  all, only where a forged mail from them would do little harm.
- **Gated actions keep their gates.** A calendar write, or anything else a
  send policy gates, goes through `allow`/`trust`/`verify` as always, at the
  gateway: under `verify` nothing the turn asks for happens without the
  user's tap on `/sends`. Under `trust`, an instruction that says in so many
  words that the user approves an action ("you have my approval to add visits
  to the calendar") is that approval, and the turn passes `--user-approved`
  for actions inside what it names — nothing beyond. Nothing that deletes or
  pays. Be clear about which half the system holds: the policy is the
  gateway's, but *whether an action lies inside what the instruction
  approved* is the turn's judgement, held by its prompt. `--user-approved`
  is an assertion the gateway cannot check, from this turn as from any other
  agent session — the convention-plus-audit footing of every agent send
  (docs/messenger-chats.md, "Trust model"). So the turn names every action it
  took in its verdict, which the chat and the attention sheet show; and an
  identity whose actions must never go out on a model's reading of an
  instruction stays on `verify`.
- **Sensitive requests are flagged, not answered.** Credentials, codes,
  financial or identity details, health information beyond what the
  relationship needs: kept out of the draft and flagged. An instruction can
  narrow this, never widen it.
- **The sender's text is data.** The instruction is the user's; the message
  is the correspondent's, and "mark this urgent" or "add this to the
  calendar" in it is content, never an instruction to the turn. But what the
  sender writes does decide how loud the message is — that is the point — so
  instructions suit senders the user trusts, and want thought on a channel
  where a sender is easily spoofed (SMS; e-mail, whose `From` address is the
  sender's own claim — and a domain covers everyone who can claim it).

**How it reaches the gate.** `contacts.sync_policy` projects every handle of
every person with an instruction into the delivery gate's policy files as the
**`instructed`** class (`triageInstructedHandle`, beside the VIPs, under the
same `…:contacts` subject; *The projection* below), so the gateway knows
before any model turn that this rail applies; the words stay with the person,
where the web gateway reads them (`contacts.instructed_person`). A handle can
carry both classes.

**The instruction reaches e-mail.** A person's e-mail addresses and domains
are projected like their messenger handles, so a mail from an instructed
sender buys the same turn — in the mail chat's companion, briefed by the
instruction, ending in a verdict — and a mail from a sender who is neither
VIP nor instructed buys none and pushes nothing by default; only that
person's importance prior and the modes they may interrupt can raise it to
the digest or a ring (docs/mail-chats.md, "The gate", which also says what
such a turn may do with a mail). This is where
what e-mail triage used to know lives now: a standing rule about a sender is
an instruction on their contact, and a rule about everything an organisation
sends is an instruction on a contact that carries its domain. Being the
person's, an instruction put on someone for their mail reaches their messenger
messages too: the one-time migration of what triage knew
(`scripts/migrate-triage.py`, docs/mail-chats.md, "The migration") briefs
people the book already holds, and its report names those of them who also
have a messenger handle. An instruction is taken off with
`contacts.py update <id> --no-instruction`.

*Whose* instruction applies to a mail follows the lookup of *E-mail domains*:
`instructed_person(book, "email", address)` walks from the holder of the
address to the domains that cover it, most specific first, and answers the
first person who carries an instruction. So a person's own brief beats their
organisation's, a nearer domain's beats a wider one's — and an organisation's
brief stands for a member the book knows by name but gives no brief of their
own. That is exactly the set of senders the gate calls `instructed`, so a
mail the gate sent down this rail always finds the brief it was sent for.

**Where the user sees it.** The chat page says, above the composer, that a
message is being worked by the instruction (and a draft may be on its way),
then what the turn did, its verdict and anything it flagged; the attention
sheet shows the same next to the importance, and the reason line says why the
message rang or was held.

**Cost.** One model turn per message from an instructed person, on every
channel, trivial ones included — fine for a few people, and it adds up with
many; an instruction on a domain buys one for every mail from every address
under it. For a person who is not a VIP, one turn's delay before a message
rings, bounded by the timeout: a minute for a messenger message, ten for a
mail, by default.

### The projection

`contacts.policy_projection(book)` and `contacts.instruction_projection(book)`
each answer one table, `{channel: handles}` — the gate handles of every VIP
person, and of every person who carries an instruction:

| Of the person | Channel | Handle, as the gate compares it |
|---|---|---|
| a messenger account | its channel | the account's handle, in today's spelling |
| a telephone (also one an account implies) | `sms` | the number |
| an e-mail address | `email` | the address |
| an e-mail domain | `email` | `*@example.org` for `example.org`, `*@*.example.org` for `*.example.org` |

`contacts.sync_policy(book)` hands both to `triage_policy.sync_contacts`,
which writes them into each channel's policy file
(`_generated/messenger/<channel>/policy/policy.nt`) under the subject
`urn:retinue:triage:<channel>:contacts`, as `kb:triageVipHandle` and
`kb:triageInstructedHandle`. **E-mail is a channel like the others here**: its
file is `_generated/messenger/email/policy/policy.nt`, on the mail gateway's
ledger volume, in the messenger shape — beside the file of the e-mail group
flags (`email-policy.nt`), which the sync does not write. A channel no gateway
reads (a person's Matrix account) gets no file.

The gate (`triage_policy.gate_decision(channel, sender, …)`) compares a sender
with those handles exactly, on every channel. On `email`, and only there, a
handle spelled as a wildcard also covers every address under its domain: a
sender is VIP, or instructed, if an exact address **or** a wildcard of that
class matches. On a messenger a `*@…` string is a handle like any other, which
nobody has. If the `email` policy file cannot be read, nobody is VIP or
instructed — a mail still reaches its chat, and must not buy a model turn
because a file was unreadable (reason `policy-unreadable`, for the gateway's
log).

So for a mail the classes are a **union** along the lookup's chain: a class
set on a domain holds for every address under it, a person filed there by
their own address included. Filing someone inside a domain without a class
does not take the domain's class away from them.

The e-mail **whitelist** is no part of this any more. VIP addresses used to be
projected into it so that e-mail triage worked their mail on its frequent
run; mail is gated like a messenger message now, and the old projection's
triples in `email-policy.nt` are no longer read and drop out on that file's
next write. `triage_policy.py check-email <address> [--list-id …]` says where
a mail lands: its group and the group's flags, and the sender's class —
`vip`, `instructed`, `vip+instructed` or `neither`.

## Using it

### Agents: the CLI

```bash
python3 /workspace/scripts/contacts.py locations                 # where each chamber keeps contacts
python3 /workspace/scripts/contacts.py find --email mara@example.org
python3 /workspace/scripts/contacts.py find --handle signal:+41791234567
python3 /workspace/scripts/contacts.py find --phone "+41 79 123 45 67"
python3 /workspace/scripts/contacts.py find --name mara           # names and e-mail addresses
python3 /workspace/scripts/contacts.py add --chamber private --name "Mara Keller" \
    --email mara@example.org --handle whatsapp:+41791234567 --sphere friends
python3 /workspace/scripts/contacts.py update <id> --add-email mara@work.example --remove-handle signal:+41…
python3 /workspace/scripts/contacts.py add --chamber work --name "ACME GmbH" --add-domain acme.example \
    --instruction "File the document with the Archivist, book it, then archive the mail."
python3 /workspace/scripts/contacts.py update <id> --add-domain '*.acme.example' --remove-domain acme.example
python3 /workspace/scripts/contacts.py update <id> --vip --importance 4 --permit focused   # --no-vip, --importance '', --no-permits
python3 /workspace/scripts/contacts.py update <id> --instruction "Draft a reply to every message. Ring me only if a visit moves to today or tomorrow."
python3 /workspace/scripts/contacts.py update <id> <id> <id> --instruction-from <id>   # a team, alike; --no-instruction removes it
python3 /workspace/scripts/contacts.py delete <id>
```

- `--json` gives machine-readable output.
- `find --handle` also lists people who have the same number on another channel,
  marked `match: "phone"`. That is a suggestion to confirm, not an identity.
- `find --email` falls back, for an address nobody holds, to the person whose
  domain covers it most specifically, marked `match: "domain"` with
  `match_domain`: that says who the mail is from, not whose address it is.
  `find --name` searches domains too.
- `--add-domain` / `--remove-domain` take `example.org` or `'*.example.org'`
  (quote the `*`); `add` also takes `--domain`. See *E-mail domains* for what
  is refused.
- Writes are committed and pushed in the owning chamber's repository.
- Always `find` before `add`: a second person for someone the book already
  knows splits their channels again.
- `update` takes several ids to give a team the same facts — the same
  instruction, say. A name, a handle or a domain belongs to one person, so
  those changes name exactly one. A shared, named instruction can come later, if
  copies drift.

### The dashboard

The **Contacts page** (`contacts.html`, the *Contacts* tab of the dashboard's
navigation row) is the address book: every person, searchable by name and
e-mail address, each edited in place — handles on every channel, e-mail
domains, spheres, importance prior, the modes they may interrupt, VIP, the
instruction (with *Copy instruction from…* to give a team the same one).
*+ New contact* names the chamber first.

The **contact card** on a chat's attention sheet (`docs/dashboard.md`) files
the chat's peer:

- **A new person** is created in the chamber picked in the *kept in*
  dropdown; the default chamber (where the last contact was created) is
  pre-selected.
- **Someone the book already has** is offered as a one-tap *This is …*. That
  covers the owner of this handle, and anyone with the same number on another
  channel (Mara filed from Signal, now writing on WhatsApp). Linking adds this
  chat's account to that person. If another person held the handle, it moves.
- **A card already filed** updates its person. A change to a person (name,
  spheres) is copied to every chat whose card points there, and what the
  attention profile learned under the old name moves with it. A mail thread
  whose peer the book no longer gives to the card's person shows no card
  (`docs/dashboard.md`, "A card is about one address"), and is filed like a
  chat without one: the person on the hidden card is not touched.
- **VIP** is a switch on the card, and it is the person's. So is the
  **instruction**, a text field beside it, with *Copy from…* for an
  instruction another person already carries.
- **A handle the book already gives to someone** needs no card at all: the
  chat is linked to its owner by itself — on its next arrival or outbound
  message, at startup, after any write to a person, and every
  `CHAT_AUTOLINK_SECONDS` (300) for persons filed outside the gateway (the
  CLI, a hand edit). Only an exact owner counts; the same number on another
  channel stays a suggestion, and so does a domain that covers a mail
  thread's peer. A chat that already has a card is never touched.
- **Removing the card** unlinks the handle. The person stays: they are more
  than this chat. Erasing a chat does not erase the person either.

The chat document keeps a copy of the card (`contact`: name, sphere, tags,
`person`, `chamber`, `path`). The person is the source of truth.

The HTTP API behind it, for the dashboard and anything on the edge:

- `GET /contacts` returns `{locations, default, contacts}`; each contact
  carries its `domains` beside its `handles`. Add `?q=` to search names,
  e-mail addresses and domains, or `?channel=&handle=` to look up one handle
  on any channel (for an e-mail address nobody holds: the person whose domain
  covers it, `exact: false`). A suggestion found by a domain — here and in the
  contact card's suggestions — says so: `match: "domain"` with `match_domain`,
  the domain that covers the address. It tells who the mail is from, never
  whose address it is; a client that offers it as *This is …* must show that.
  The people of a mail chat the gateway resolves (the chat summary's
  `participants` and `peer`) carry the same fact as `by_domain`:
  `{contact, name, domain}` of the person whose domain covers an address nobody
  holds, beside the address's own `name` and `contact` (null for such an
  address) — see docs/dashboard.md, "A name that is a subject".
- `POST /contacts` with `{chamber, name, handles: [{channel, handle}], sphere?,
  tags?, importance?, permits?, vip?, instruction?, domains?: [domain]}`
  creates a person and answers 201.
- `POST /contacts/<id>` with `{name?, sphere?, tags?, importance?, permits?:
  [mode id], vip?, instruction?, add?: [handle], remove?: [handle], domains?:
  [domain], add_domains?: [domain], remove_domains?: [domain]}` changes
  one; an unknown mode answers 400. An absent `instruction` leaves it; `""` or
  null removes it. `POST /chats/<id>/contact` takes `instruction` the same way.
  `domains` replaces the person's domains, `add_domains` / `remove_domains`
  change them (`ContactBook.update`'s arguments of the same names); a domain
  that is refused answers 400, one somebody else has 409.
- `POST /internal/chats/<id>/verdict` with `{importance, due?, actions?,
  flags?}` is the instructed turn's verdict (token-gated; `chat-verdict.py`
  is the client). It is taken only while such a turn runs for the chat.

### Queries

Contacts are chamber files, so the life store holds them like any other data:

```sparql
PREFIX vcard:  <http://www.w3.org/2006/vcard/ns#>
PREFIX foaf:   <http://xmlns.com/foaf/0.1/>
PREFIX schema: <http://schema.org/>
PREFIX kb:     <https://w3id.org/retinue/kb#>

# Every handle of everyone called Mara, whatever the channel — filed by this
# module (vcard:Individual) or by hand (foaf:Person, schema:Person).
SELECT ?person ?name ?channel ?handle WHERE {
  VALUES ?class { vcard:Individual foaf:Person schema:Person }
  ?person a ?class ; vcard:fn|foaf:name|schema:name ?name .
  FILTER (CONTAINS(LCASE(?name), "mara"))
  { ?person foaf:account ?a . ?a kb:channel ?channel ; foaf:accountName ?handle }
  UNION { ?person vcard:hasEmail ?m . BIND ("email" AS ?channel) BIND (STR(?m) AS ?handle) }
}
```

```sparql
PREFIX foaf:  <http://xmlns.com/foaf/0.1/>
PREFIX kb:    <https://w3id.org/retinue/kb#>

# Everything a person wrote, on every messenger: accounts join the ledger on
# (kb:channel, kb:sender).
SELECT ?channel ?text WHERE {
  <urn:retinue:person:…> foaf:account ?a .
  ?a kb:channel ?channel ; foaf:accountName ?h .
  ?m kb:channel ?channel ; kb:sender ?h ; kb:text ?text .
}
```

## Why this shape

- **Two resources, not one.** Earlier versions minted a `vcard:Individual` per
  `(channel, handle)`. That made one person several unrelated individuals,
  showed the same person under different names in different chats, and gave
  their e-mail address nowhere to live.
- **In a chamber, not in `_generated/`.** A contact the user makes is data, not
  derived output. It has to be versioned, extendable by hand and by the
  Archivist, and kept with the part of life it belongs to. The earlier address
  book was one file regenerated from chat state; a hand edit was overwritten,
  and a lost volume lost the people.
- **No `owl:sameAs`.** Saying that two handles are one person means pointing
  both at the same person resource. `owl:sameAs` would make identity symmetric
  and transitive for every consumer that honours it, where one wrong tap fuses
  two people. QLever does no reasoning, so every query would also have to
  follow the links by hand. Moving a handle from one person to another is exact
  and undoable.
- **FOAF for accounts.** vCard has no class for "an account on a service";
  `foaf:OnlineAccount` is the established one, and SIOC builds on it. E-mail
  and SMS stay vCard (`hasEmail`, `hasTelephone`), because an address and a
  number are properties of the person, not accounts.

## Migration

At startup, the gateway files every older card (a name on a chat document, with
no `person`) as a person in the **default chamber** (the chamber the last
contact was created in, else the first contact location).
If a person already has the handle, the card is linked to them instead. Once no
card is left unfiled, it removes the old generated file
(`CONTACTS_EMIT_PATH`, `_generated/contacts/dashboard.ttl`). Cards with the same
name are not merged automatically; the card offers the match instead.

The migration is idempotent. A deployment in which no chamber keeps contacts —
every one opts out, or none is mounted — keeps its old cards, and the old
file, until one does.

## Not yet

- **Two persons with the same name share attention facts**, because the
  profile underneath is keyed on the display name; the first in the address
  book wins the overlay.
- **Contacts outside the address-book directories** (a person described in some
  other file of a chamber) are in the life store but not in the card's
  suggestions. A SPARQL-backed lookup could cover them.
- **E-mail and SMS threads have no contact card** of their own; their people
  are edited on the Contacts page.
