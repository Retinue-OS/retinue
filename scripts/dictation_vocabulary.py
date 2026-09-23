#!/usr/bin/env python3
"""The deployment's dictation vocabulary, read from the life store.

The proper nouns a dictation contains — people, organisations, medications —
are exactly the words an ASR model mangles, and they are all in the life store
already: every chamber's RDF is indexed there, so asking the store reaches a
doctor in a care-provider list as readily as a contact entry (a regex over one
chamber's `contacts/*.ttl` saw neither).

One query, two shapes, one module — because two processes need it:

  * the **STT service** biases the decode with the names' words (Whisper
    hotwords), for every client it serves: dashboard dictation and inbound
    voice notes alike;
  * the **web gateway** hands the whole names to the transcript-repair model as
    spelling hints.

Each process imports this module and keeps its own cache; the store is a
sibling service on the same network, so neither needs the other to fetch it.

Lean by construction (`docs/triple-stores.md`): the classes and name predicates
are bounded by VALUES instead of matched by a wildcard pattern, which is what
keeps QLever's planning cheap over ~100 graphs. The ranks order the vocabulary
by how much a mishearing costs, so the caps below cut the least useful first.

Configuration (environment)
  QLEVER_LIFE_URL / SPARQL_ENDPOINT_LIFE   Life-store endpoint.
  DICTATION_NAME_LIMIT                     Whole names kept (default 200).
  DICTATION_HOTWORD_CHARS                  Hotword budget in characters
                                           (default 600) — hotwords share
                                           Whisper's 224-token prompt window,
                                           so an unbounded list would crowd out
                                           the audio's own context.
"""
import os
import re
import threading
import time
import urllib.parse
import urllib.request

LIFE_URL = (os.environ.get("QLEVER_LIFE_URL")
            or os.environ.get("SPARQL_ENDPOINT_LIFE")
            or "http://qlever-life:7001").rstrip("/")

NAME_LIMIT = int(os.environ.get("DICTATION_NAME_LIMIT", "200"))
HOTWORD_CHARS = int(os.environ.get("DICTATION_HOTWORD_CHARS", "600"))
QUERY_TIMEOUT = float(os.environ.get("DICTATION_QUERY_TIMEOUT", "5"))
CACHE_TTL = 300.0        # the store is cheap, but not per request
CACHE_TTL_EMPTY = 30.0   # a store that was unreachable deserves a retry soon

# Share of the hotword budget per rank. Ranks are spent in order, and an unused
# share carries forward — but no rank may eat the whole window, or in a store
# full of people a medication name would never reach the decoder at all.
HOTWORD_SHARES = {1: 0.55, 2: 0.2, 3: 0.25}

NAME_QUERY = """
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
SELECT DISTINCT ?rank ?name WHERE {
  VALUES (?cls ?rank) {
    (<http://www.w3.org/2006/vcard/ns#Individual> 1)
    (<http://schema.org/Person> 1)
    (<https://schema.org/Person> 1)
    (<http://schema.org/Organization> 2)
    (<https://schema.org/Organization> 2)
    (<http://schema.org/MedicalOrganization> 2)
    (<http://schema.org/MedicalClinicOrHospital> 2)
    (<http://schema.org/Drug> 3)
    (<http://schema.org/MedicalTherapy> 3)
  }
  VALUES ?np {
    <http://www.w3.org/2006/vcard/ns#fn>
    <http://www.w3.org/2006/vcard/ns#family-name>
    <http://www.w3.org/2006/vcard/ns#organization-name>
    <http://schema.org/name>
    <https://schema.org/name>
    <http://schema.org/legalName>
    <https://w3id.org/retinue/kb#name>
    <https://w3id.org/retinue/kb#fullName>
  }
  ?s rdf:type ?cls ; ?np ?name .
  FILTER(isLiteral(?name))
  FILTER(STRLEN(STR(?name)) > 2 && STRLEN(STR(?name)) < 60)
  # Some vocabularies use a name predicate for a whole descriptive phrase; a
  # label with that many words is not a proper noun anyone dictates.
  FILTER(STRLEN(STR(?name)) - STRLEN(REPLACE(STR(?name), " ", "")) < 5)
}
ORDER BY ?rank ?name
"""

# Runs of at least three letters, in any script: a language-agnostic way to take
# the dictatable words out of a name and leave numbers and honorifics ("Dr.")
# behind. Whisper matches hotwords as words, so word pieces are the right unit.
_WORD_RE = re.compile(r"[^\W\d_]{3,}", re.UNICODE)

_cache: tuple[float, list[str], str] | None = None
_lock = threading.Lock()


def _bindings(query: str) -> list[dict]:
    """POST a SPARQL query to the life store and return its result bindings."""
    import json
    data = urllib.parse.urlencode({"query": query}).encode("utf-8")
    req = urllib.request.Request(
        LIFE_URL,
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/sparql-results+json",
        },
    )
    with urllib.request.urlopen(req, timeout=QUERY_TIMEOUT) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return payload.get("results", {}).get("bindings", [])


def hotword_string(ranked: list[tuple[int, str]], max_chars: int = HOTWORD_CHARS) -> str:
    """The distinct words of `ranked` names as a space-separated hotword string,
    each rank spending at most its share of `max_chars` (see HOTWORD_SHARES)."""
    words: list[str] = []
    seen: set[str] = set()
    used = 0
    budget = 0
    for rank in sorted({rank for rank, _ in ranked}):
        share = HOTWORD_SHARES.get(rank, 0.0)
        budget = min(max_chars, budget + round(share * max_chars))
        for word in (w for r, name in ranked if r == rank
                     for w in _WORD_RE.findall(name)):
            key = word.casefold()
            if key in seen:
                continue
            cost = len(word) + (1 if words else 0)
            if used + cost > budget:
                break  # this rank is full; the next one starts on its own share
            seen.add(key)
            used += cost
            words.append(word)
    return " ".join(words)


def vocabulary(log_tag: str = "dictation") -> tuple[list[str], str]:
    """(name hints, hotword string) from the life store, cached with a TTL.

    Fail-open: an unreachable or empty store yields `([], "")`, which only costs
    the hints — dictation itself keeps working.
    """
    global _cache
    with _lock:
        now = time.monotonic()
        if _cache and now < _cache[0]:
            return _cache[1], _cache[2]
        ranked: list[tuple[int, str]] = []
        try:
            seen: set[str] = set()
            for row in _bindings(NAME_QUERY):
                name = " ".join((row.get("name", {}).get("value") or "").split())
                key = name.casefold()
                if not name or key in seen:
                    continue
                seen.add(key)
                rank = (row.get("rank", {}).get("value") or "").strip()
                # The query always binds ?rank; a shape that doesn't is treated as
                # top rank rather than dropped out of the hotword budget.
                ranked.append((int(rank) if rank.isdigit() else 1, name))
                if len(ranked) >= NAME_LIMIT:
                    break
        except Exception as exc:  # noqa: BLE001 - hints are optional, never fatal
            print(f"[{log_tag}] vocabulary unavailable: {exc}", flush=True)
            ranked = []
        names = [name for _rank, name in ranked]
        hotwords = hotword_string(ranked, HOTWORD_CHARS)
        _cache = (now + (CACHE_TTL if names else CACHE_TTL_EMPTY), names, hotwords)
        return names, hotwords


if __name__ == "__main__":  # a quick look at what the store yields right now
    started = time.monotonic()
    _names, _hotwords = vocabulary()
    print(f"{len(_names)} names in {time.monotonic() - started:.3f}s")
    print(f"{len(_hotwords.split())} hotwords, {len(_hotwords)} chars")
