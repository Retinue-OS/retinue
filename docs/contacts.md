# Contacts: people behind handles — proposal

*Status: proposal, not implemented. Touches `scripts/web-gateway.py` and the
webapp's contact card, so the implementation is Tier 3.*

## The problem

A contact card saved in the dashboard (`POST /chats/<id>/contact`) is stored on
the chat's state document and emitted into the life store at
`CONTACTS_EMIT_PATH` (`chambers/_generated/contacts/dashboard.ttl`) as:

```turtle
<urn:retinue:contact:signal%3A%2B41791234567>
    a vcard:Individual ;
    vcard:fn "Mara Keller" ;
    kb:channel "signal" ; kb:handle "+41791234567" ; kb:chat "signal:+41791234567" ;
    vcard:hasTelephone <tel:+41791234567> ;
    kb:sphere sphere:friends ;
    kb:addedAt "2026-09-05T16:40:00+00:00"^^xsd:dateTime .
```

The subject is minted from `(channel, handle)`, so what the store calls a
`vcard:Individual` is actually **a messenger account**. That causes three
problems:

1. **One person, several individuals.** Mara on Signal and Mara on WhatsApp are
   two unrelated `vcard:Individual`s.
2. **No link to what the chambers know.** A chamber may already describe Mara
   (an `.nt` address book, Markdown frontmatter through a chamber converter).
   The dashboard card creates a second, disconnected Mara instead of pointing
   at that one.
3. **Ad-hoc terms where standard ones exist.** `kb:handle` and `kb:addedAt`
   duplicate terms from FOAF and Dublin Core. `docs/ontology.md` says to mint
   only when none of the defaults fits.

## The model

Two resources instead of one: the **person** and their **accounts**.

| Thing | Class | Vocabulary | Why |
|---|---|---|---|
| The person | `vcard:Individual` | vCard | Already the default for contacts (`docs/ontology.md`) |
| A handle on a messaging service | `foaf:OnlineAccount` | FOAF | The established term for "an account on a service"; SIOC's `sioc:UserAccount` is a subclass of it |
| Person → account | `foaf:account` | FOAF | — |
| Account's handle | `foaf:accountName` | FOAF | Replaces `kb:handle` |
| Account's service | `foaf:accountServiceHomepage` | FOAF | Where the service has one (not SMS) |
| Account ↔ messages | `kb:channel`, `kb:chat` | `kb:` (existing) | The same literals the ledger records carry (`scripts/inbound_store.py`), so account-to-message is a plain join |
| Phone number | `vcard:hasTelephone <tel:…>` | vCard, RFC 3966 | Unchanged; now on the person, where address books put it |
| Messenger address | `vcard:hasInstantMessage <…>` | vCard (IMPP) | Lets vCard-only consumers see the channel |
| Name | `vcard:fn` | vCard | Unchanged |
| When the card was made | `dcterms:created` | Dublin Core Terms | Replaces `kb:addedAt` |
| Spheres | `kb:sphere`, `kb:tag` → `urn:retinue:sphere:<word>` | `kb:` (existing) | Retinue's own attention concept, same IRIs as `_generated/attention/` |
| A retired dashboard person | `dcterms:isReplacedBy` | Dublin Core Terms | See **Linking and merging** |

The only new IRI schemes are `urn:retinue:person:` and `urn:retinue:account:`,
both framework-owned. No new `kb:` terms are needed.

### The same card, remodelled

```turtle
@prefix vcard:   <http://www.w3.org/2006/vcard/ns#> .
@prefix foaf:    <http://xmlns.com/foaf/0.1/> .
@prefix dcterms: <http://purl.org/dc/terms/> .
@prefix kb:      <https://w3id.org/retinue/kb#> .
@prefix sphere:  <urn:retinue:sphere:> .
@prefix xsd:     <http://www.w3.org/2001/XMLSchema#> .

<urn:retinue:person:5f0c9a2e-8d1b-4e7a-9c3f-2b6d1e4a7c90>
    a vcard:Individual ;
    vcard:fn "Mara Keller" ;
    vcard:hasTelephone <tel:+41791234567> ;
    vcard:hasInstantMessage <sgnl://signal.me/#p/+41791234567> ,
                            <https://wa.me/41791234567> ;
    kb:sphere sphere:friends ;
    foaf:account <urn:retinue:account:signal%3A%2B41791234567> ,
                 <urn:retinue:account:whatsapp%3A%2B41791234567> ;
    dcterms:created "2026-09-05T16:40:00+00:00"^^xsd:dateTime .

<urn:retinue:account:signal%3A%2B41791234567>
    a foaf:OnlineAccount ;
    foaf:accountServiceHomepage <https://signal.org/> ;
    foaf:accountName "+41791234567" ;
    kb:channel "signal" ;
    kb:chat "signal:+41791234567" .

<urn:retinue:account:whatsapp%3A%2B41791234567>
    a foaf:OnlineAccount ;
    foaf:accountServiceHomepage <https://www.whatsapp.com/> ;
    foaf:accountName "+41791234567" ;
    kb:channel "whatsapp" ;
    kb:chat "whatsapp:+41791234567" .
```

**Account IRIs** are derived deterministically from `(channel, handle)`, as the
current contact IRIs are. This follows the ontology's rule to prefer the
source's own identifier: the same handle always gets the same IRI, in every
emit.

**Person IRIs** cannot be derived from a handle, because a person has several
handles and must survive gaining or losing one. The gateway mints
`urn:retinue:person:<uuid4>` once and stores it on the card (see below).

**Service homepages and IM URIs, per channel:**

| Channel | `foaf:accountServiceHomepage` | `vcard:hasInstantMessage` |
|---|---|---|
| signal | `https://signal.org/` | `sgnl://signal.me/#p/<E.164>` (phone handles only) |
| whatsapp | `https://www.whatsapp.com/` | `https://wa.me/<E.164 without +>` |
| telegram | `https://telegram.org/` | `https://t.me/<username>` when a username is known; otherwise none (a numeric user id is no public address) |
| sms | — (no service) | — (`vcard:hasTelephone` already says it) |

A handle is written as `<tel:…>` only when it is E.164 (the existing
`_PHONE_RE`). A LID-only WhatsApp sender or a Telegram user id gets an account
but no telephone.

## Linking and merging

The card gains one optional field, `person`: the IRI of the person this handle
belongs to. On the chat's state document:

```json
"contact": {"name": "Mara Keller", "sphere": "friends", "tags": [],
            "at": "2026-09-05T16:40:00+00:00",
            "person": "urn:retinue:person:5f0c9a2e-…"}
```

Saving the card resolves `person` in one of three ways.

1. **A chamber already knows them.** The card offers matches from the life
   store (see **Finding the person**). If the user picks one, `person` becomes
   that chamber's IRI, for example `<https://example.org/people#mara>`. The
   emit then writes only what the dashboard itself knows about that IRI:

   ```turtle
   <https://example.org/people#mara>
       foaf:account <urn:retinue:account:signal%3A%2B41791234567> ;
       kb:sphere sphere:friends .
   ```

   It writes **no `vcard:fn` or telephone** for a person a chamber describes.
   The chamber is the authority, and a second name would just be a
   conflicting value. The card shows the chamber's name. Writing triples about
   another file's subject is ordinary here, because the store unions all graphs
   and every triple keeps its provenance in its file's named graph.

2. **Another chat's card already names them.** Picking an existing dashboard
   person (Mara on WhatsApp, when the new chat is Mara on Signal) reuses that
   `person` IRI. Both accounts then hang off one individual.

3. **Nobody yet.** The gateway mints a fresh `urn:retinue:person:` IRI, and
   the card writes it in full, as in the example above.

**Merging** is repointing. If a dashboard-minted person later turns out to be a
chamber's person, every card carrying the minted IRI is rewritten to the
chamber IRI. Because the emit regenerates the whole file from the chat
documents, the minted individual simply disappears from it. Memories or
threads may already cite the old IRI, so the emit keeps one tombstone per
retired IRI (kept in a small `retired` map next to the chat state):

```turtle
<urn:retinue:person:5f0c9a2e-…> dcterms:isReplacedBy <https://example.org/people#mara> .
```

**Why not `owl:sameAs`.** Identity would then be symmetric and transitive: one
wrong tap fuses two people's whole descriptions for any consumer that honours
it. QLever does no OWL reasoning, so every query would also have to follow
`sameAs` chains by hand. Repointing the card is exact, undoable (unlink =
point back at a fresh IRI) and needs nothing from the query side.
`dcterms:isReplacedBy` states only "use that one instead", which is exactly
what happened. Whether chambers use `owl:sameAs` among themselves is theirs to
decide and outside this proposal.

**Unlinking** (an empty name, as today) removes the card. The account stays
unclaimed and the sender goes back to screening.

## Finding the person

When a screened sender's card opens, the gateway asks the life store for people
whose telephone matches the handle. Chambers model phones in more than one way,
so the query accepts the common shapes:

```sparql
PREFIX vcard:  <http://www.w3.org/2006/vcard/ns#>
PREFIX foaf:   <http://xmlns.com/foaf/0.1/>
PREFIX schema: <http://schema.org/>

SELECT DISTINCT ?person ?name WHERE {
  VALUES ?tel { <tel:+41791234567> }
  {   ?person vcard:hasTelephone ?tel }
  UNION { ?person vcard:hasTelephone/vcard:hasValue ?tel }   # vCard 4 node form
  UNION { ?person foaf:phone ?tel }
  UNION { ?person schema:telephone ?lit
          FILTER (REPLACE(STR(?lit), "[^0-9+]", "") = "+41791234567") }
  OPTIONAL { ?person vcard:fn|foaf:name|schema:name ?name }
}
LIMIT 5
```

A single hit is offered as a chip on the card (*Is this Mara Keller?*). The
name field also gets a type-ahead over people's `vcard:fn` / `foaf:name` /
`schema:name`, so the user can link by name when the number is new. Both
suggestions only propose; nothing links without the user's tap. The
comparison is exact on E.164 (`tel:` URIs carry no spaces, RFC 3966).
Normalising non-E.164 numbers in chamber data stays the chamber's job.

## What else changes

- **`docs/ontology.md`** gains one row: *Accounts on messaging services →
  FOAF `foaf:OnlineAccount`, `foaf:accountName`,
  `foaf:accountServiceHomepage`*.
- **`_contact_names`** (transcript cleanup) keeps working unchanged: it still
  matches `vcard:fn` in the emitted file. It could additionally read the names
  of linked chamber people from the store; that is optional.
- **Contact lookup for sends** (`messaging-contact-lookup`, and the SMS path
  that already falls back to "the chambers' contacts") gets one query shape
  from a name to every handle a person has: `?p vcard:fn ?n ; foaf:account ?a .
  ?a kb:channel ?c ; foaf:accountName ?h`.
- **History of a person across channels** becomes one join: `?p foaf:account
  ?a . ?a kb:chat ?k . ?m kb:chat ?k ; kb:text ?t`.

## Migration

- The emitted file is regenerated wholesale, so the old
  `urn:retinue:contact:` subjects disappear on the first emit after the
  change. No query in the tree reads `kb:handle`, `kb:addedAt` or the old IRI
  scheme. The only consumers are `tests/test_attention_api.py`'s string
  assertions, which move to the new shape.
- Existing cards get a minted `person` lazily, on the first emit that meets
  them, and the IRI is written back to the chat document so it stays stable
  from then on. Cards with the same name are **not** merged automatically;
  after the migration the card offers the match, like any other suggestion.
- The chat document's other fields and the `POST /chats/<id>/contact` request
  shape stay as they are; `person` is an added, optional field.

## Out of scope, noted for later

- **Attention priors keyed on the display name.** `_attention_rename_sender`
  moves priors and permits when a name changes. Keying them on the person IRI
  would make them follow the person across channels and survive renames. This
  is a natural next step, but it is a change to the attention store and gets
  its own PR.
- **E-mail senders.** The same model fits: `vcard:hasEmail <mailto:…>` on the
  person, with no account needed. It would let triage's sender whitelist and
  the messenger VIP flag name the same individual.
- **Name clash with `kb:account`.** The ledger's `kb:account` names the
  *user's own* gateway account, which is a different thing from
  `foaf:account` (a person's account). The two never share a subject, but
  documentation should say so wherever both appear.
