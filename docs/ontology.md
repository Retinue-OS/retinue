# Ontology defaults

The life store holds every mounted chamber's triples in one place. That is only
useful if the same kind of fact is modelled the same way wherever it comes from:
a step count from one chamber and a meter reading from another should answer to
the same query shape. This document fixes the **system-wide defaults**. They are
modelling standards, not statements about any subject area, which is why they
live in the framework.

The Archivist has no domain knowledge by construction; these defaults are what
it falls back on. A chamber may **extend** them with domain vocabularies in its
`.retinue/archivist/extraction.md` (clinical codes, financial identifiers, …).
Replacing a default for a kind of data listed here needs a reason, recorded in
that same guide.

## Defaults

| Kind of data | Vocabulary | Namespace |
|---|---|---|
| Measurements, readings, any time series | **SOSA** | `http://www.w3.org/ns/sosa/` |
| Units of measure | **UCUM** codes, as the unit string (`"mmol/L"`) | — (codes, not IRIs) |
| Dates and times | XSD datatypes (`xsd:date`, `xsd:dateTime`) | `http://www.w3.org/2001/XMLSchema#` |
| People and organisations as contacts | **vCard** | `http://www.w3.org/2006/vcard/ns#` |
| A person's account on a messaging service (Signal, WhatsApp, Telegram, …) | **FOAF** `foaf:OnlineAccount`, `foaf:account`, `foaf:accountName` | `http://xmlns.com/foaf/0.1/` |
| Roles, events, places, general things | **schema.org** | `http://schema.org/` |
| Documents and their metadata (title, date, creator, format) | **Dublin Core Terms** | `http://purl.org/dc/terms/` |
| Labels and simple classification | RDFS / **SKOS** | `http://www.w3.org/2000/01/rdf-schema#`, `http://www.w3.org/2004/02/skos/core#` |
| Where a fact came from | **PROV-O** | `http://www.w3.org/ns/prov#` |
| Retinue's own concepts (projects, memories, data-quality flags) | `kb:` | `https://w3id.org/retinue/kb#` |
| **Fallback** — nothing above fits | schema.org | `http://schema.org/` |

Mint a term of your own only when none of these (nor a chamber's declared domain
vocabulary) has one, and put it in a namespace the chamber owns — never in `kb:`,
which is the framework's.

## Observations: the SOSA shape

Every reading is one `sosa:Observation` with at least:

| Predicate | Value |
|---|---|
| `rdf:type` | `sosa:Observation` |
| `sosa:observedProperty` | a property IRI — the chamber's guide defines its scheme |
| `sosa:hasSimpleResult` | the value, typed (`xsd:decimal`, `xsd:integer`, …) |
| `sosa:resultTime` | `xsd:dateTime` |
| `sosa:madeBySensor` | a sensor IRI, when the source identifies the device |

Keep the value and unit exactly as the source reports them; do not convert or
round. A period later found to be defective is annotated, never deleted — see
the `<stem>.quality.nt` convention in `.claude/agents/archivist.md`.

## Identifiers

- Prefer an identifier the source already carries (a serial number, an invoice
  number, a standard code) over a synthetic one, so re-ingesting the same file
  yields the same IRIs.
- Where a chamber mints IRIs, its guide states the scheme (`urn:<chamber-domain>:…`
  or an `https://` namespace it controls); the same thing gets the same IRI in
  every file.
- Do not write graph IRIs into files; the store derives them from the file path
  (`docs/triple-stores.md`).
