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
  itself have written for the person's previous state and keeps every other
  line byte for byte, comments included. Deleting a person removes their
  managed triples; if anything else is left, the file stays.

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
| `email` | `vcard:hasEmail <mailto:…>` (lowercased) | — |
| `sms` | a number: `vcard:hasTelephone <tel:…>` (E.164); a sender id or short code: `foaf:OnlineAccount` | — |
| `signal` | `foaf:OnlineAccount`, homepage `https://signal.org/` | for a phone handle: `tel:`, and `sgnl://signal.me/#p/<E.164>` |
| `whatsapp` | `foaf:OnlineAccount`, homepage `https://www.whatsapp.com/` | for a phone handle: `tel:`, and `https://wa.me/<digits>` |
| `telegram` | `foaf:OnlineAccount`, homepage `https://telegram.org/` | for a username: `https://t.me/<name>` |
| anything else (`matrix`, `threema`, …) | `foaf:OnlineAccount` | for a phone handle: `tel:` |

**Normalization.** Phone numbers lose punctuation, and a leading `00` becomes
`+`. A national number without a country code (`079 …`) is refused rather than
guessed. Handles that are not phone numbers (a WhatsApp LID, a Telegram user id)
are kept exactly as the channel spells them.

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
counts as an account. A telephone can be shared (a family landline).

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
arrive (docs/triage-delivery-gate.md). It is set on the person, never per
handle: `contacts.sync_policy` projects every handle of every VIP person into
the delivery gate's policy files — messenger accounts as VIP handles on their
channel (telephones for SMS), e-mail addresses into the whitelist, so the
frequent run handles their mail. The projection sits under a subject of its
own and is replaced wholesale on every contact write and on the gateway's
tick, so an edit made outside the gateway (the CLI, a hand edit) reaches the
gate within seconds; VIPs set by hand for an unfiled handle
(`triage_policy.py vip-add`) and the Sent-derived whitelist are left alone.

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
(`kb:instruction`; at most 2000 characters). When a message arrives from a
person with an instruction:

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
  `CHAT_INSTRUCTION_TIMEOUT` (60 seconds by default) has the message rung as
  a VIP's would be — the reason line says the turn did not finish, not that
  the sender is a VIP. An instruction can delay a ring by at most that long
  and never prevent one; a draft that finishes later still lands. So does a
  message whose turn could not be started at all. Besides a muted chat, only
  a message the user has already read or answered while the turn worked
  stays quiet: a ring would tell them nothing new, and would reopen the item
  their reply settled.
- **No sending.** A reply is only ever staged in the chat's draft; the user's
  send press is the only way a chat reply leaves in their name. That is how
  the chat surface works for every agent (docs/messenger-chats.md): the send
  endpoint takes only the user's own request.
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
  where a sender is easily spoofed (SMS).

**How it reaches the gate.** `contacts.sync_policy` projects every messenger
handle of every person with an instruction into the delivery gate's policy
files as the **`instructed`** class (`triageInstructedHandle`, beside the
VIPs, under the same `…:contacts` subject), so the gateway knows before any
model turn that this rail applies; the words stay with the person, where the
web gateway reads them (`contacts.instructed_person`). A handle can carry both
classes. E-mail addresses are not projected: e-mail is triaged on its own
rail, and an instruction there is not built yet.

**Where the user sees it.** The chat page says, above the composer, that a
message is being worked by the instruction (and a draft may be on its way),
then what the turn did, its verdict and anything it flagged; the attention
sheet shows the same next to the importance, and the reason line says why the
message rang or was held.

**Cost.** One model turn per message from an instructed person, trivial ones
included — fine for a few people, and it adds up with many. For a person who
is not a VIP, one turn's delay before a message rings, bounded by the timeout.

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
python3 /workspace/scripts/contacts.py update <id> --vip --importance 4 --permit focused   # --no-vip, --importance '', --no-permits
python3 /workspace/scripts/contacts.py update <id> --instruction "Draft a reply to every message. Ring me only if a visit moves to today or tomorrow."
python3 /workspace/scripts/contacts.py update <id> <id> <id> --instruction-from <id>   # a team, alike; --no-instruction removes it
python3 /workspace/scripts/contacts.py delete <id>
```

- `--json` gives machine-readable output.
- `find --handle` also lists people who have the same number on another channel,
  marked `match: "phone"`. That is a suggestion to confirm, not an identity.
- Writes are committed and pushed in the owning chamber's repository.
- Always `find` before `add`: a second person for someone the book already
  knows splits their channels again.
- `update` takes several ids to give a team the same facts — the same
  instruction, say. A name or a handle belongs to one person, so those
  changes name exactly one. A shared, named instruction can come later, if
  copies drift.

### The dashboard

The **Contacts page** (`contacts.html`, the *Contacts* tab of the dashboard's
navigation row) is the address book: every person, searchable by name and
e-mail address, each edited in place — handles on every channel, spheres,
importance prior, the modes they may interrupt, VIP, the instruction (with
*Copy instruction from…* to give a team the same one). *+ New contact* names
the chamber first.

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
  attention profile learned under the old name moves with it.
- **VIP** is a switch on the card, and it is the person's. So is the
  **instruction**, a text field beside it, with *Copy from…* for an
  instruction another person already carries.
- **Removing the card** unlinks the handle. The person stays: they are more
  than this chat. Erasing a chat does not erase the person either.

The chat document keeps a copy of the card (`contact`: name, sphere, tags,
`person`, `chamber`, `path`). The person is the source of truth.

The HTTP API behind it, for the dashboard and anything on the edge:

- `GET /contacts` returns `{locations, default, contacts}`. Add `?q=` to search
  names and e-mail addresses, or `?channel=&handle=` to look up one handle on
  any channel.
- `POST /contacts` with `{chamber, name, handles: [{channel, handle}], sphere?,
  tags?, importance?, permits?, vip?, instruction?}` creates a person and
  answers 201.
- `POST /contacts/<id>` with `{name?, sphere?, tags?, importance?, permits?:
  [mode id], vip?, instruction?, add?: [handle], remove?: [handle]}` changes
  one; an unknown mode answers 400. An absent `instruction` leaves it; `""` or
  null removes it. `POST /chats/<id>/contact` takes `instruction` the same way.
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
