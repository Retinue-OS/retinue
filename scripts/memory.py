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

The actor URI is the same convention the agent registry types
(`discover-agents.py`), so memories join with the `kb:AiAgent` roster.

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

Environment:
  RETINUE_MEMORY           set to 0/false/off/no to disable: `store` becomes a
                           successful no-op (recall still works — old entries
                           may exist)
  RETINUE_MEMORY_DIR       where entries are written
                           (default $CHAMBERS_DIR/_generated/memory)
  RETINUE_MEMORY_SESSION   default session label for `store`
  RETINUE_MEMORY_ACTOR     default actor name (default: ara)
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

    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    entry_id = f"{stamp}-{secrets.token_hex(3)}"

    session = (args.session or os.environ.get("RETINUE_MEMORY_SESSION", "")).strip()
    file_stem = _slug(session) if session else entry_id
    path = MEMORY_DIR / f"{file_stem}.nt"

    subj = f"<{MEMORY_PREFIX}{entry_id}>"
    lines = [
        f"{subj} <{RDF_TYPE}> <{KB}Memory> .",
        f"{subj} <{KB}content> {_nt_string(content)} .",
        f"{subj} <{KB}recordedAt> {_nt_string(now.isoformat().replace('+00:00', 'Z'))}"
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

    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("".join(line + "\n" for line in lines))
    print(f"[memory] stored {entry_id} -> {path}", file=sys.stderr)
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

    sparql = f"""
PREFIX kb: <{KB}>
PREFIX xsd: <{XSD}>
SELECT ?m ?content ?t ?actor ?relevance
       (GROUP_CONCAT(DISTINCT ?tag; SEPARATOR=", ") AS ?tags) WHERE {{
  ?m a kb:Memory ;
     kb:content ?content ;
     kb:recordedAt ?t .
  OPTIONAL {{ ?m kb:tag ?tag }}
  OPTIONAL {{ ?m kb:actor ?actor }}
  OPTIONAL {{ ?m kb:relevance ?relevance }}
  {chr(10).join('  ' + p for p in patterns)}
}}
GROUP BY ?m ?content ?t ?actor ?relevance
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

    if args.json:
        out = [
            {
                "id": val(r, "m"),
                "content": val(r, "content"),
                "recorded_at": val(r, "t"),
                "actor": val(r, "actor").removeprefix(ACTOR_PREFIX),
                "relevance": val(r, "relevance") or None,
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
        print(f"- {when} ({'; '.join(meta)})")
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
    p_store.set_defaults(func=store)

    p_recall = sub.add_parser("recall", help="query memories from the life store")
    p_recall.add_argument("--tag", action="append",
                          help="match any of these tags (repeatable)")
    p_recall.add_argument("--actor", default="", help="filter by recording actor")
    p_recall.add_argument("--since", default="", help="date or dateTime lower bound")
    p_recall.add_argument("--until", default="", help="date or dateTime upper bound")
    p_recall.add_argument("--min-relevance", type=float, default=None,
                          help="only entries declaring at least this relevance")
    p_recall.add_argument("--limit", type=int, default=20)
    p_recall.add_argument("--json", action="store_true",
                          help="raw rows instead of prompt-ready text")
    p_recall.set_defaults(func=recall)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
