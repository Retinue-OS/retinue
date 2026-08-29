#!/usr/bin/env python3
"""Session memory — store and recall durable log entries via the life store.

Every Claude session in Retinue is a fresh `claude -p`; whatever it learned
dies with it unless written down. This CLI is the writing-down: `store` appends
a memory entry as N-Triples into the framework-owned `_generated`
pseudo-chamber (`chambers/_generated/memory/`), which qlever-life indexes like
any chamber data, and `recall` queries the entries back by tag, time range,
actor, or minimum relevance — formatted for pasting straight into a dispatch
prompt, so an agent that cannot query the store itself still gets the memories
that matter (docs/model-routing.md, "memory as triples").

A memory entry is a resource, not a bare fact:

    <urn:retinue:memory:ID> a               kb:Memory .
    <urn:retinue:memory:ID> kb:content      "what to remember" .
    <urn:retinue:memory:ID> kb:tag          "insurance" .          # repeatable
    <urn:retinue:memory:ID> kb:recordedAt   "…"^^xsd:dateTime .
    <urn:retinue:memory:ID> kb:actor        <urn:retinue:actor:ara> .
    <urn:retinue:memory:ID> kb:relevance    "0.7"^^xsd:decimal .   # optional
    <urn:retinue:memory:ID> kb:session      "…" .                  # optional
    <urn:retinue:memory:ID> kb:model        "…" .                  # optional
    <urn:retinue:memory:ID> kb:reiteratedAt "…"^^xsd:dateTime .    # per reinforce
    <urn:retinue:memory:ID> kb:correctedBy  <urn:retinue:memory:NEW> .  # was false
    <urn:retinue:memory:ID> kb:supersededBy <urn:retinue:memory:NEW> .  # world changed
    <urn:retinue:memory:ID> kb:questionedBy <urn:retinue:memory:NEW> .  # in doubt

The actor URI is the same convention the agent registry types
(`discover-agents.py`), so memories join with the `kb:AiAgent` roster.

`kb:model` records which model wrote the memory — the judgement-attribution
stamp (docs/model-routing.md): the actor stays the household (`ara`), while
the model tells a reader how much to trust a recorded decision. A session
cannot introspect its own `--model` flag, so the spawner advertises it via
RETINUE_SESSION_MODEL (set by the scheduler, the gate scripts, and the
entrypoint alongside the flag they build); `--model` overrides, and with
neither the stamp is simply absent.

`reinforce` strengthens an existing memory instead of duplicating it: when the
user restates a rule or preference already on record, one `kb:reiteratedAt`
timestamp is appended to the *same subject* — written into the current
session's file, since RDF merges triples by subject across named graphs, so
the original file is never touched. Alongside the creation-time relevance,
recall then reports how often and how recently an entry was repeated
(`COUNT`/`MAX` over the reiterations), which is the query-side signal for
"the user keeps saying this".

The inverse is challenging: a memory can turn out plainly false, be overtaken
by events, or become doubtful on new evidence. The old entry is never edited
or deleted — the *new* memory carries what is known now (the correction, the
new state, the doubt and its reason), and `store --corrects/--supersedes/
--questions <id>` links old to new (`kb:correctedBy` / `kb:supersededBy` /
`kb:questionedBy`, object = the new entry) in the new session's file, again by
subject-merge. recall omits corrected and superseded entries by default
(`--include-superseded` shows them, labeled) and flags questioned ones — the
record of having-believed stays queryable, but no stale fact leaks into a
dispatch prompt.

File layout: one flat directory. Entries from the same session share a file
when a session label is known (`--session` or RETINUE_MEMORY_SESSION —
appending to an `.nt` is an ordinary incremental store update); without a
label each entry gets its own file. The directory is deliberately flat: a
subdirectory created at runtime is invisible to the store's inotify watches
until a rebuild (qlever-dir#10), so per-month folders would silently delay a
whole month of memories.

Usage:

    memory.py store --tag insurance --tag deadline --relevance 0.7 \
        "IV filing for August submitted; response expected mid-September."
    memory.py recall --tag insurance --since 2026-06-01 --limit 10
    memory.py recall --tag health --json
    memory.py reinforce 20260829T193141Z-575748
    memory.py store --tag insurance --supersedes 20260829T193141Z-575748 \
        "The filing deadline moved to the 15th; the August arrangement is void."

Environment:
  RETINUE_MEMORY           set to 0/false/off/no to disable: `store` becomes a
                           successful no-op (recall still works — old entries
                           may exist)
  RETINUE_MEMORY_DIR       where entries are written
                           (default $CHAMBERS_DIR/_generated/memory)
  RETINUE_MEMORY_SESSION   default session label for `store`
  RETINUE_MEMORY_ACTOR     default actor name (default: ara)
  RETINUE_SESSION_MODEL    the model this session runs on, advertised by the
                           spawner; stamped as kb:model on stored entries
  SPARQL_ENDPOINT_LIFE     the life store endpoint for `recall`
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import secrets
import sys
import urllib.parse
import urllib.request
from pathlib import Path

KB = "https://w3id.org/retinue/kb#"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
XSD = "http://www.w3.org/2001/XMLSchema#"
ACTOR_PREFIX = "urn:retinue:actor:"
MEMORY_PREFIX = "urn:retinue:memory:"

CHAMBERS_DIR = Path(os.environ.get("CHAMBERS_DIR") or "/workspace/chambers")
MEMORY_DIR = Path(
    os.environ.get("RETINUE_MEMORY_DIR") or (CHAMBERS_DIR / "_generated" / "memory")
)
ENDPOINT = os.environ.get("SPARQL_ENDPOINT_LIFE", "http://qlever-life:7001")

SLUG_RE = re.compile(r"[^a-z0-9-]+")


def memory_enabled() -> bool:
    return os.environ.get("RETINUE_MEMORY", "").strip().lower() not in {
        "0", "false", "off", "no",
    }


def _slug(value: str) -> str:
    return SLUG_RE.sub("-", value.strip().lower()).strip("-")


def _nt_string(value: str) -> str:
    """Escape a Python string as an N-Triples literal (RDF 1.1 §7.2)."""
    out = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{out}"'


def _sparql_string(value: str) -> str:
    """Escape a string for embedding in a SPARQL query (same escapes work)."""
    return _nt_string(value)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


def _xsd_datetime(dt: datetime.datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _target_file(fallback_stem: str, session: str) -> Path:
    """Entries sharing a session label share a file; otherwise one per entry."""
    return MEMORY_DIR / f"{_slug(session) if session else fallback_stem}.nt"


def _append(path: Path, lines: list[str]) -> None:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("".join(line + "\n" for line in lines))


ENTRY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")

# Challenge links: store-flag -> predicate localname. Subject is the OLD
# memory, object the NEW one, so the link reads as history on the old
# resource — same subject-merge trick as kb:reiteratedAt.
CHALLENGE_PREDICATES = {
    "corrects": "correctedBy",       # the old memory was plainly false
    "supersedes": "supersededBy",    # the world changed; old no longer holds
    "questions": "questionedBy",     # new evidence puts the old one in doubt
}


def _verify_exists(entry_id: str, force: bool) -> bool:
    """May entry_id be referenced? Guards against a typo silently attaching
    history to a subject recall never surfaces. Store lag (seconds) or
    downtime must not block a legitimate write: --force skips the check, an
    unreachable store warns and proceeds."""
    if force:
        return True
    uri = f"{MEMORY_PREFIX}{entry_id}"
    try:
        if not _ask(f"ASK {{ <{uri}> a <{KB}Memory> }}"):
            print(f"[memory] no such memory in the life store: {entry_id} "
                  "(just stored? the index lags a few seconds — use --force)",
                  file=sys.stderr)
            return False
    except Exception as exc:  # noqa: BLE001 — one endpoint, one failure mode
        print(f"[memory] store unreachable ({exc}); proceeding unverified",
              file=sys.stderr)
    return True


# ---------------------------------------------------------------- store


def store(args: argparse.Namespace) -> int:
    if not memory_enabled():
        print("[memory] disabled (RETINUE_MEMORY=0) — nothing stored", file=sys.stderr)
        return 0

    content = args.content.strip()
    if not content:
        print("[memory] refusing to store an empty memory", file=sys.stderr)
        return 1

    actor = _slug(args.actor or os.environ.get("RETINUE_MEMORY_ACTOR", "") or "ara")
    if not actor:
        print(f"[memory] invalid actor name: {args.actor!r}", file=sys.stderr)
        return 1

    tags = sorted({_slug(t) for t in (args.tag or []) if _slug(t)})
    if not tags:
        print("[memory] at least one --tag is required (recall is tag-driven)",
              file=sys.stderr)
        return 1

    if args.relevance is not None and not (0.0 <= args.relevance <= 1.0):
        print("[memory] --relevance must be between 0 and 1", file=sys.stderr)
        return 1

    # Validate every challenged reference before anything is written.
    challenges: list[tuple[str, str]] = []  # (predicate localname, old id)
    for flag, pred in CHALLENGE_PREDICATES.items():
        for ref in getattr(args, flag) or []:
            old_id = ref.strip().removeprefix(MEMORY_PREFIX)
            if not ENTRY_ID_RE.match(old_id):
                print(f"[memory] not a memory id: {ref!r}", file=sys.stderr)
                return 1
            if not _verify_exists(old_id, args.force):
                return 1
            challenges.append((pred, old_id))

    now = _now()
    entry_id = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"

    session = (args.session or os.environ.get("RETINUE_MEMORY_SESSION", "")).strip()
    path = _target_file(entry_id, session)

    subj = f"<{MEMORY_PREFIX}{entry_id}>"
    lines = [
        f"{subj} <{RDF_TYPE}> <{KB}Memory> .",
        f"{subj} <{KB}content> {_nt_string(content)} .",
        f"{subj} <{KB}recordedAt> {_nt_string(_xsd_datetime(now))}"
        f"^^<{XSD}dateTime> .",
        f"{subj} <{KB}actor> <{ACTOR_PREFIX}{actor}> .",
    ]
    lines += [f"{subj} <{KB}tag> {_nt_string(t)} ." for t in tags]
    if args.relevance is not None:
        lines.append(
            f"{subj} <{KB}relevance> \"{args.relevance:g}\"^^<{XSD}decimal> ."
        )
    if session:
        lines.append(f"{subj} <{KB}session> {_nt_string(session)} .")
    model = (args.model or os.environ.get("RETINUE_SESSION_MODEL", "")).strip()
    if model:
        lines.append(f"{subj} <{KB}model> {_nt_string(model)} .")
    lines += [f"<{MEMORY_PREFIX}{old_id}> <{KB}{pred}> {subj} ."
              for pred, old_id in challenges]

    _append(path, lines)
    print(f"[memory] stored {entry_id} -> {path}", file=sys.stderr)
    for pred, old_id in challenges:
        print(f"[memory] {old_id} {pred} {entry_id}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------- reinforce


def reinforce(args: argparse.Namespace) -> int:
    if not memory_enabled():
        print("[memory] disabled (RETINUE_MEMORY=0) — nothing reinforced",
              file=sys.stderr)
        return 0

    entry_id = args.id.strip().removeprefix(MEMORY_PREFIX)
    if not ENTRY_ID_RE.match(entry_id):
        print(f"[memory] not a memory id: {args.id!r}", file=sys.stderr)
        return 1
    if not _verify_exists(entry_id, args.force):
        return 1
    uri = f"{MEMORY_PREFIX}{entry_id}"

    now = _now()
    session = (args.session or os.environ.get("RETINUE_MEMORY_SESSION", "")).strip()
    path = _target_file(f"{now.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}",
                        session)
    _append(path, [
        f"<{uri}> <{KB}reiteratedAt> {_nt_string(_xsd_datetime(now))}"
        f"^^<{XSD}dateTime> ."
    ])
    print(f"[memory] reinforced {entry_id} -> {path}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------- recall


def _query(sparql: str) -> list[dict]:
    data = urllib.parse.urlencode({"query": sparql}).encode()
    req = urllib.request.Request(
        ENDPOINT, data=data,
        headers={"Accept": "application/sparql-results+json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.load(resp)
    return body.get("results", {}).get("bindings", [])


def _ask(sparql: str) -> bool:
    data = urllib.parse.urlencode({"query": sparql}).encode()
    req = urllib.request.Request(
        ENDPOINT, data=data,
        headers={"Accept": "application/sparql-results+json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return bool(json.load(resp).get("boolean"))


def _date_bound(value: str, end_of_day: bool) -> str:
    """Accept a date or a full dateTime; widen a bare date to the day's edge."""
    if "T" in value:
        return value if value.endswith("Z") or "+" in value else value + "Z"
    return f"{value}T23:59:59Z" if end_of_day else f"{value}T00:00:00Z"


def recall(args: argparse.Namespace) -> int:
    patterns: list[str] = []
    if args.tag:
        wanted = " ".join(
            _sparql_string(_slug(t)) for t in args.tag if _slug(t)
        )
        patterns.append(f"?m kb:tag ?want . VALUES ?want {{ {wanted} }}")
    if args.actor:
        patterns.append(f"?m kb:actor <{ACTOR_PREFIX}{_slug(args.actor)}> .")
    if args.since:
        patterns.append(
            f'FILTER(?t >= "{_date_bound(args.since, False)}"^^xsd:dateTime)'
        )
    if args.until:
        patterns.append(
            f'FILTER(?t <= "{_date_bound(args.until, True)}"^^xsd:dateTime)'
        )
    if args.min_relevance is not None:
        # Filtering on the OPTIONAL drops entries that declare no relevance —
        # asking for a minimum means asking for entries that state one.
        patterns.append(f"FILTER(?relevance >= {args.min_relevance:g})")
    if not args.include_superseded:
        # A corrected or superseded memory is no longer true; keep it out of
        # anything that might land in a dispatch prompt. Questioned entries
        # stay in, flagged — doubt is a signal, not a verdict.
        patterns.append("FILTER NOT EXISTS { ?m kb:correctedBy ?x1 }")
        patterns.append("FILTER NOT EXISTS { ?m kb:supersededBy ?x2 }")

    sparql = f"""
PREFIX kb: <{KB}>
PREFIX xsd: <{XSD}>
SELECT ?m ?content ?t ?actor ?relevance ?model
       (GROUP_CONCAT(DISTINCT ?tag; SEPARATOR=", ") AS ?tags)
       (COUNT(DISTINCT ?r) AS ?reiterations) (MAX(?r) AS ?lastReiterated)
       (GROUP_CONCAT(DISTINCT ?cb; SEPARATOR=", ") AS ?correctedBy)
       (GROUP_CONCAT(DISTINCT ?sb; SEPARATOR=", ") AS ?supersededBy)
       (GROUP_CONCAT(DISTINCT ?qb; SEPARATOR=", ") AS ?questionedBy) WHERE {{
  ?m a kb:Memory ;
     kb:content ?content ;
     kb:recordedAt ?t .
  OPTIONAL {{ ?m kb:tag ?tag }}
  OPTIONAL {{ ?m kb:actor ?actor }}
  OPTIONAL {{ ?m kb:relevance ?relevance }}
  OPTIONAL {{ ?m kb:model ?model }}
  OPTIONAL {{ ?m kb:reiteratedAt ?r }}
  OPTIONAL {{ ?m kb:correctedBy ?cb }}
  OPTIONAL {{ ?m kb:supersededBy ?sb }}
  OPTIONAL {{ ?m kb:questionedBy ?qb }}
  {chr(10).join('  ' + p for p in patterns)}
}}
GROUP BY ?m ?content ?t ?actor ?relevance ?model
ORDER BY DESC(?t)
LIMIT {args.limit}
"""
    try:
        rows = _query(sparql)
    except Exception as exc:  # noqa: BLE001 — one endpoint, one failure mode
        print(f"[memory] life store query failed: {exc}", file=sys.stderr)
        return 1

    def val(row: dict, key: str) -> str:
        return row.get(key, {}).get("value", "")

    def ids(row: dict, key: str) -> list[str]:
        return [x.strip().removeprefix(MEMORY_PREFIX)
                for x in val(row, key).split(",") if x.strip()]

    if args.json:
        out = [
            {
                "id": val(r, "m"),
                "content": val(r, "content"),
                "recorded_at": val(r, "t"),
                "actor": val(r, "actor").removeprefix(ACTOR_PREFIX),
                "relevance": val(r, "relevance") or None,
                "model": val(r, "model") or None,
                "reiterations": int(val(r, "reiterations") or 0),
                "last_reiterated": val(r, "lastReiterated") or None,
                "corrected_by": ids(r, "correctedBy"),
                "superseded_by": ids(r, "supersededBy"),
                "questioned_by": ids(r, "questionedBy"),
                "tags": [t for t in val(r, "tags").split(", ") if t],
            }
            for r in rows
        ]
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if not rows:
        print("(no matching memories)")
        return 0
    for r in rows:
        when = val(r, "t").replace("T", " ").removesuffix("Z") + "Z"
        actor = val(r, "actor").removeprefix(ACTOR_PREFIX)
        meta = [m for m in (actor, val(r, "tags")) if m]
        rel = val(r, "relevance")
        if rel:
            meta.append(f"relevance {rel}")
        model = val(r, "model")
        if model:
            meta.append(f"via {model}")
        times = int(val(r, "reiterations") or 0)
        if times:
            last = val(r, "lastReiterated").split("T")[0]
            meta.append(f"reiterated {times}x, last {last}")
        for key, label in (("correctedBy", "CORRECTED by"),
                           ("supersededBy", "SUPERSEDED by"),
                           ("questionedBy", "questioned by")):
            challengers = ids(r, key)
            if challengers:
                meta.append(f"{label} {', '.join(challengers)}")
        entry_id = val(r, "m").removeprefix(MEMORY_PREFIX)
        print(f"- {when} ({'; '.join(meta)}) [{entry_id}]")
        print(f"  {val(r, 'content')}")
    return 0


# ---------------------------------------------------------------- main


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_store = sub.add_parser("store", help="store one memory entry")
    p_store.add_argument("content", help="the memory text")
    p_store.add_argument("--tag", action="append",
                         help="topic tag (repeatable, at least one)")
    p_store.add_argument("--actor", default="",
                         help="recording actor basename (default: ara)")
    p_store.add_argument("--relevance", type=float, default=None,
                         help="importance indicator, 0..1")
    p_store.add_argument("--session", default="",
                         help="session label — entries sharing it share a file")
    p_store.add_argument("--model", default="",
                         help="model that wrote this memory "
                              "(default: RETINUE_SESSION_MODEL)")
    p_store.add_argument("--corrects", action="append", metavar="ID",
                         help="this entry corrects that plainly false memory")
    p_store.add_argument("--supersedes", action="append", metavar="ID",
                         help="the world changed; this entry replaces that one")
    p_store.add_argument("--questions", action="append", metavar="ID",
                         help="this entry puts that memory's veracity in doubt")
    p_store.add_argument("--force", action="store_true",
                         help="skip the existence check for challenged ids")
    p_store.set_defaults(func=store)

    p_reinforce = sub.add_parser(
        "reinforce",
        help="strengthen an existing memory (the user restated it)")
    p_reinforce.add_argument("id", help="memory id or full urn:retinue:memory: URI")
    p_reinforce.add_argument("--session", default="",
                             help="session label for the file the "
                                  "reiteration is written to")
    p_reinforce.add_argument("--force", action="store_true",
                             help="skip the existence check in the life store")
    p_reinforce.set_defaults(func=reinforce)

    p_recall = sub.add_parser("recall", help="query memories from the life store")
    p_recall.add_argument("--tag", action="append",
                          help="match any of these tags (repeatable)")
    p_recall.add_argument("--actor", default="", help="filter by recording actor")
    p_recall.add_argument("--since", default="", help="date or dateTime lower bound")
    p_recall.add_argument("--until", default="", help="date or dateTime upper bound")
    p_recall.add_argument("--min-relevance", type=float, default=None,
                          help="only entries declaring at least this relevance")
    p_recall.add_argument("--limit", type=int, default=20)
    p_recall.add_argument("--include-superseded", action="store_true",
                          help="also return corrected/superseded entries "
                               "(labeled; hidden by default)")
    p_recall.add_argument("--json", action="store_true",
                          help="raw rows instead of prompt-ready text")
    p_recall.set_defaults(func=recall)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
