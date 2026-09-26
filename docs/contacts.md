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
(Tier 1), and indexed by the life store like every chamber file. Each entry in
the deployment's `chambers.json` may declare where its address book is kept:

```json
{"chambers": [
  {"name": "private", "url_env": "PRIVATE_CHAMBER_URL", "contacts": "people"},
  {"name": "work",    "url_env": "WORK_CHAMBER_URL"},
  {"name": "health",  "url_env": "HEALTH_CHAMBER_URL", "contacts": false}
]}
```

- `contacts` is a directory relative to the chamber root. When it is left out,
  the directory is `contacts/`, so `work` above keeps its people in
  `work/contacts/`.
- `"contacts": false` means the chamber holds no contacts.
- A path that leads out of the chamber is ignored, and so is a chamber that is
  not mounted.
- Without a readable manifest, every mounted chamber is a location at the
  default path.
- **The order is the preference order.** The first location is the one the
  dashboard pre-selects and the one older cards are migrated into.

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
| `sms` | `vcard:hasTelephone <tel:…>` (E.164 only) | — |
| `signal` | `foaf:OnlineAccount`, homepage `https://signal.org/` | for a phone handle: `tel:`, and `sgnl://signal.me/#p/<E.164>` |
| `whatsapp` | `foaf:OnlineAccount`, homepage `https://www.whatsapp.com/` | for a phone handle: `tel:`, and `https://wa.me/<digits>` |
| `telegram` | `foaf:OnlineAccount`, homepage `https://telegram.org/` | for a username: `https://t.me/<name>` |
| anything else (`matrix`, `threema`, …) | `foaf:OnlineAccount` | for a phone handle: `tel:` |

**Normalization.** Phone numbers lose punctuation, and a leading `00` becomes
`+`. A national number without a country code (`079 …`) is refused rather than
guessed. Handles that are not phone numbers (a WhatsApp LID, a Telegram user id)
are kept exactly as the channel spells them.

**Uniqueness.** An account or an e-mail address belongs to at most one person
across all chambers: `add` answers 409 with the owner's name. A telephone can be
shared (a family landline).

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
into the person once, at start, where the person leaves the field unset.

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
python3 /workspace/scripts/contacts.py delete <id>
```

- `--json` gives machine-readable output.
- `find --handle` also lists people who have the same number on another channel,
  marked `match: "phone"`. That is a suggestion to confirm, not an identity.
- Writes are committed and pushed in the owning chamber. Pass `--no-commit` or
  set `CONTACTS_COMMIT=0` to skip that.
- Always `find` before `add`: a second person for someone the book already
  knows splits their channels again.

### The dashboard

The **Contacts page** (`contacts.html`, the *Contacts* button in the dock at the
foot of the home) is the address book: every person, searchable by name and
e-mail address, each edited in place — handles on every channel, spheres,
importance prior, the modes they may interrupt, VIP. *+ New contact* names the
chamber first.

The **contact card** on a chat's attention sheet (`docs/dashboard.md`) files
the chat's peer:

- **A new person** is created in the chamber picked under *kept in*. The first
  location is pre-selected.
- **Someone the book already has** is offered as a one-tap *This is …*. That
  covers the owner of this handle, and anyone with the same number on another
  channel (Mara filed from Signal, now writing on WhatsApp). Linking adds this
  chat's account to that person. If another person held the handle, it moves.
- **A card already filed** updates its person. A change to a person (name,
  spheres) is copied to every chat whose card points there, and what the
  attention profile learned under the old name moves with it.
- **VIP** is a switch on the card, and it is the person's.
- **Removing the card** unlinks the handle. The person stays: they are more
  than this chat. Erasing a chat does not erase the person either.

The chat document keeps a copy of the card (`contact`: name, sphere, tags,
`person`, `chamber`, `path`). The person is the source of truth.

The HTTP API behind it, for the dashboard and anything on the edge:

- `GET /contacts` returns `{locations, default, contacts}`. Add `?q=` to search
  names and e-mail addresses, or `?channel=&handle=` to look up one handle on
  any channel.
- `POST /contacts` with `{chamber, name, handles: [{channel, handle}], sphere?,
  tags?, importance?, permits?, vip?}` creates a person and answers 201.
- `POST /contacts/<id>` with `{name?, sphere?, tags?, importance?, permits?:
  [mode id], vip?, add?: [handle], remove?: [handle]}` changes one; an unknown
  mode answers 400.

### Queries

Contacts are chamber files, so the life store holds them like any other data:

```sparql
PREFIX vcard: <http://www.w3.org/2006/vcard/ns#>
PREFIX foaf:  <http://xmlns.com/foaf/0.1/>
PREFIX kb:    <https://w3id.org/retinue/kb#>

# Every handle of everyone called Mara, whatever the channel.
SELECT ?person ?name ?channel ?handle WHERE {
  ?person a vcard:Individual ; vcard:fn ?name .
  FILTER (CONTAINS(LCASE(?name), "mara"))
  { ?person foaf:account ?a . ?a kb:channel ?channel ; foaf:accountName ?handle }
  UNION { ?person vcard:hasEmail ?m . BIND ("email" AS ?channel) BIND (STR(?m) AS ?handle) }
}

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
no `person`) as a person in the **default chamber**, the first contact location.
If a person already has the handle, the card is linked to them instead. Once no
card is left unfiled, it removes the old generated file
(`CONTACTS_EMIT_PATH`, `_generated/contacts/dashboard.ttl`). Cards with the same
name are not merged automatically; the card offers the match instead.

The migration is idempotent. A deployment whose manifest declares no contact
location keeps its old cards, and the old file, until it declares one.

## Not yet

- **Two persons with the same name share attention facts**, because the
  profile underneath is keyed on the display name; the first in the address
  book wins the overlay.
- **Contacts outside the address-book directories** (a person described in some
  other file of a chamber) are in the life store but not in the card's
  suggestions. A SPARQL-backed lookup could cover them.
- **E-mail and SMS threads have no contact card** of their own; their people
  are edited on the Contacts page.
