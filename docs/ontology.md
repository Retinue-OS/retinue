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

Every `sparql` block in this document is a complete query. An agent POSTs it to
the life store as it stands. On the dashboard, `/sparql` renders this file with
a **Run** button on each one: the query becomes an editor in place, and its
results appear right below it.

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

## The graph layout

The store keeps every file's triples in a **named graph of its own**, named
after the file's path relative to the chambers root:

```
<file:health/observations/cgm/glucose_2026-05-21.nt>
```

The first path segment is therefore the chamber (`health` above), and the rest
is the path inside it. `_generated/` is not a
chamber but the framework's own output: the agent roster, memories, the
conversation models. Files a converter reads (Markdown frontmatter, CSV, …) get
their graph the same way. The default graph is the union of all of them: a
pattern outside `GRAPH` matches every file, and `GRAPH ?g` adds which file
said it. The mechanics are in `docs/triple-stores.md`.

```sparql
# What each chamber holds: how many files and triples, largest first.
SELECT ?chamber (COUNT(?g) AS ?files) (SUM(?n) AS ?triples) WHERE {
  { SELECT ?g (COUNT(*) AS ?n) WHERE { GRAPH ?g { ?s ?p ?o } } GROUP BY ?g }
  BIND (STRBEFORE(STRAFTER(STR(?g), "file:"), "/") AS ?chamber)
} GROUP BY ?chamber ORDER BY DESC(?triples)
```

```sparql
# What there is: every class with instances in the store, most common first.
SELECT ?class (COUNT(DISTINCT ?s) AS ?instances) WHERE {
  ?s a ?class .
} GROUP BY ?class ORDER BY DESC(?instances) LIMIT 100
```

## Example queries

There is one query for each kind of data in the defaults table. Each
query declares its own prefixes, so it runs exactly as written. The queries
are generic on purpose: a query that knows a chamber's property IRIs belongs in
that chamber's guide. `tests/test_doc_sparql.py` keeps the queries
self-contained and fails when a namespace in the table has no example.

### Measurements — SOSA

```sparql
PREFIX sosa: <http://www.w3.org/ns/sosa/>

# Everything that is measured: each observed property, how many readings
# there are of it, and the span they cover.
SELECT ?property (COUNT(?o) AS ?readings) (MIN(?t) AS ?first) (MAX(?t) AS ?last) WHERE {
  ?o a sosa:Observation ;
     sosa:observedProperty ?property ;
     sosa:resultTime ?t .
} GROUP BY ?property ORDER BY DESC(?readings)
```

### Dates and times — XSD

```sparql
PREFIX sosa: <http://www.w3.org/ns/sosa/>
PREFIX xsd:  <http://www.w3.org/2001/XMLSchema#>
PREFIX kb:   <https://w3id.org/retinue/kb#>

# Readings since a date (edit it), newest first, exactly as the source
# reported them. Any observation a later analysis flagged is left out
# (the <stem>.quality.nt convention).
SELECT ?time ?property ?value ?sensor WHERE {
  ?o a sosa:Observation ;
     sosa:observedProperty ?property ;
     sosa:hasSimpleResult ?value ;
     sosa:resultTime ?time .
  OPTIONAL { ?o sosa:madeBySensor ?sensor }
  FILTER (?time >= "2026-01-01T00:00:00"^^xsd:dateTime)
  FILTER NOT EXISTS { ?o kb:dataQuality ?quality }
} ORDER BY DESC(?time) LIMIT 100
```

### Contacts — vCard

```sparql
PREFIX vcard: <http://www.w3.org/2006/vcard/ns#>

# The address book: every person, with their e-mail addresses and phone numbers.
SELECT ?person ?name
       (GROUP_CONCAT(DISTINCT STRAFTER(STR(?mail), "mailto:"); SEPARATOR=", ") AS ?emails)
       (GROUP_CONCAT(DISTINCT STRAFTER(STR(?tel), "tel:"); SEPARATOR=", ") AS ?phones) WHERE {
  ?person a vcard:Individual ; vcard:fn ?name .
  OPTIONAL { ?person vcard:hasEmail ?mail }
  OPTIONAL { ?person vcard:hasTelephone ?tel }
} GROUP BY ?person ?name ORDER BY ?name
```

### Messaging accounts — FOAF

```sparql
PREFIX vcard: <http://www.w3.org/2006/vcard/ns#>
PREFIX foaf:  <http://xmlns.com/foaf/0.1/>
PREFIX kb:    <https://w3id.org/retinue/kb#>

# Every messenger account, the person it belongs to, and how many messages the
# ledger holds from it. An account joins its messages on (channel, handle).
SELECT ?name ?channel ?handle (COUNT(?message) AS ?messages) WHERE {
  ?person vcard:fn ?name ; foaf:account ?account .
  ?account kb:channel ?channel ; foaf:accountName ?handle .
  OPTIONAL { ?message kb:channel ?channel ; kb:sender ?handle }
} GROUP BY ?name ?channel ?handle ORDER BY DESC(?messages)
```

### Events, places and everything else — schema.org

```sparql
PREFIX schema: <http://schema.org/>

# Events in date order, with where they take place.
SELECT ?event ?name ?start ?place WHERE {
  ?event a schema:Event ; schema:startDate ?start .
  OPTIONAL { ?event schema:name ?name }
  OPTIONAL { ?event schema:location/schema:name ?place }
} ORDER BY ?start LIMIT 100
```

### Documents — Dublin Core

```sparql
PREFIX dcterms: <http://purl.org/dc/terms/>

# Documents with their metadata, newest first, and the file that describes
# each one.
SELECT ?document ?title ?date ?creator ?format ?file WHERE {
  GRAPH ?file { ?document dcterms:title ?title }
  OPTIONAL { ?document dcterms:date ?date }
  OPTIONAL { ?document dcterms:creator ?creator }
  OPTIONAL { ?document dcterms:format ?format }
} ORDER BY DESC(?date) LIMIT 100
```

### Labels — RDFS and SKOS

```sparql
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>

# Find anything by name: every label, preferred or alternative, that contains
# the search text (edit it).
SELECT ?thing ?label ?kind WHERE {
  { ?thing rdfs:label ?label . BIND ("rdfs:label" AS ?kind) }
  UNION { ?thing skos:prefLabel ?label . BIND ("skos:prefLabel" AS ?kind) }
  UNION { ?thing skos:altLabel ?label . BIND ("skos:altLabel" AS ?kind) }
  FILTER (CONTAINS(LCASE(STR(?label)), "sensor"))
} LIMIT 100
```

### Provenance — PROV-O

```sparql
PREFIX prov: <http://www.w3.org/ns/prov#>

# Where facts came from, where a file says so: the source each entity was
# derived from, and the activity that produced it. (Which *file* holds a
# triple needs no PROV: that is its graph.)
SELECT ?entity ?source ?activity ?ended WHERE {
  ?entity prov:wasDerivedFrom ?source .
  OPTIONAL {
    ?entity prov:wasGeneratedBy ?activity .
    OPTIONAL { ?activity prov:endedAtTime ?ended }
  }
} LIMIT 100
```

### Retinue's own — `kb:`

```sparql
PREFIX kb: <https://w3id.org/retinue/kb#>

# Running projects and who has the next move, each field read from the
# project's own file (the dashboard's projects card asks the same question).
SELECT ?title ?actor ?next ?file WHERE {
  GRAPH ?file {
    ?project a kb:Project .
    OPTIONAL { ?project kb:title ?title }
    OPTIONAL { ?project kb:currentActor ?actor }
    OPTIONAL { ?project kb:currentNextAction ?next }
    OPTIONAL { ?project kb:paused ?paused }
    OPTIONAL { ?project kb:status ?status }
  }
  FILTER (!BOUND(?paused) || ?paused = false)
  FILTER (!BOUND(?status) || ?status != "done")
} ORDER BY ?actor ?title
```

```sparql
PREFIX kb: <https://w3id.org/retinue/kb#>

# The session log: the latest memories, who stored them, and the newer entry
# that corrected, superseded or questioned each one, if any.
SELECT ?when ?actor ?content ?challengedBy WHERE {
  ?memory a kb:Memory ;
          kb:content ?content ;
          kb:recordedAt ?when ;
          kb:actor ?actor .
  OPTIONAL { ?memory kb:correctedBy|kb:supersededBy|kb:questionedBy ?challengedBy }
} ORDER BY DESC(?when) LIMIT 50
```
