#!/usr/bin/env python3
"""Reference Markdown -> Turtle converter for qlever-dir: project frontmatter.

qlever-dir indexes a non-RDF file when a chamber declares a converter for its
extension in `.qlever/converters.json` (e.g. `{"md": "md2ttl.py"}`, as shown in
docs/triple-stores.md). The contract is minimal: invoked as `<converter>
<input-file>`, emit Turtle on stdout; the source file keeps its own
path-derived named graph. See scripts/jsonld2ttl.py for the same contract
applied to JSON-LD.

This converter turns a project's YAML frontmatter into the vocabulary the
framework's own consumers already query: `scripts/web-gateway.py` (the
dashboard's projects card), `scripts/agent-self-review.py` (the daily sweep of
projects pending on an AI agent) and `scripts/recurring-projects.py` (wakes
resting projects). All three were written against
`https://w3id.org/retinue/kb#` and `urn:retinue:actor:<slug>` — see
`scripts/discover-agents.py`, which assigns that actor-URI shape to every AI
agent — so this converter targets the same vocabulary rather than choosing its
own; the two disagreeing was retinue-os/retinue#1.

Only project frontmatter is in scope: a file is emitted as a `kb:Project` only
when its frontmatter declares `type: project` (see docs/triple-stores.md for
the worked example). Anything else yields no triples, deliberately — this is
one converter for one frontmatter shape, not a generic YAML-to-RDF mapper;
widening its scope on a guess would just add a second, uncoordinated vocabulary
of the same kind this file exists to settle.

Frontmatter is parsed without a YAML library: nothing in this tree currently
depends on one (`scripts/recurring-projects.py` reads the same kind of
frontmatter the same way, scalars only, for the same reason), and pulling one
in for a single optional field is not worth a new runtime dependency. Only
plain `key: value` scalar lines are understood; block lists and block scalars
are silently ignored, exactly like recurring-projects.py's reader. The one
list-valued field, `tags`, is therefore read from an inline value
(LIST_FIELDS).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

KB = "https://w3id.org/retinue/kb#"
XSD = "http://www.w3.org/2001/XMLSchema#"
PROJECT_PREFIX = "urn:retinue:project:"
# Same shape scripts/discover-agents.py assigns every AI agent
# (<urn:retinue:actor:NAME>, derived from the agent definition's basename) —
# `current_actor` in a project's frontmatter is expected to name one of those,
# or a human/external actor slug in the same shape (e.g. `reto`).
ACTOR_PREFIX = "urn:retinue:actor:"

# frontmatter key -> (predicate local name, literal datatype).
# Datatype is None for a plain string, "boolean" or "date" for an xsd literal.
# This is the field map the whole converter mechanism turns on: a key missing
# here is silently dropped, which is exactly how retinue-os/retinue#23 (the
# documented `resolved: true` escape hatch emitting no triple) happened.
SCALAR_FIELDS: dict[str, tuple[str, str | None]] = {
    "title": ("title", None),
    "goal": ("goal", None),
    "current_next_action": ("currentNextAction", None),
    "waiting_since": ("waitingSince", "date"),
    "expected_by": ("expectedBy", "date"),
    "paused": ("paused", "boolean"),
    "recurring": ("recurring", None),
    "next_due": ("nextDue", "date"),
    "status": ("status", None),
    "resolved": ("resolved", "boolean"),
    # The attention model's project properties (docs/attention-model.md): how
    # much it matters, where it belongs, what kind of thing it is, and the
    # lead time before its deadline. Plain strings; the dashboard parses them.
    "importance": ("importance", None),
    "sphere": ("sphere", None),
    "kind": ("kind", None),
    "remind_before": ("remindBefore", None),
}

# frontmatter key -> predicate local name, for the one list-valued field. The
# scalar-only reader cannot see a block list, so a list is written inline —
# `tags: [finance, tax]` or `tags: finance, tax` — and each entry becomes its
# own triple.
LIST_FIELDS: dict[str, str] = {
    "tags": "tag",
}

_FM_RE = re.compile(r"^---\n(.*?)\n---\s*(?:\n|$)", re.DOTALL)
_LINE_RE = re.compile(r"^([A-Za-z0-9_]+):\s*(.*)$")


def parse_frontmatter(text: str) -> dict[str, str]:
    """Minimal YAML-frontmatter reader (scalars only, stdlib-only).

    Same shape as scripts/recurring-projects.py's reader: a plain top-level
    `key: value` line per field, quotes stripped, everything else (lists,
    nested maps, folded/literal block scalars) ignored rather than
    misinterpreted.
    """
    m = _FM_RE.match(text)
    if not m:
        return {}
    fm: dict[str, str] = {}
    for line in m.group(1).splitlines():
        lm = _LINE_RE.match(line)
        if not lm:
            continue
        key, val = lm.group(1), lm.group(2).strip()
        if val.startswith(("'", '"')) and val.endswith(("'", '"')) and len(val) >= 2:
            val = val[1:-1]
        fm[key] = val
    return fm


def as_bool(raw: str) -> bool:
    """Same truthiness rule as recurring-projects.py's `as_bool`, for the
    same fields, so a file reads the same way whichever side is looking."""
    return raw.strip().lower() in ("true", "yes", "1")


def as_list(raw: str) -> list[str]:
    """An inline list value -> its entries: `[a, b]` or `a, b`, each entry
    stripped of whitespace and quotes, empties dropped."""
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    out = []
    for part in raw.split(","):
        part = part.strip().strip("'\"").strip()
        if part:
            out.append(part)
    return out


def actor_iri(raw: str) -> str:
    """`current_actor` frontmatter value -> its actor IRI.

    A bare slug (the common case: `current_actor: coach`) gets the standard
    prefix, matching discover-agents.py exactly. A value that already looks
    like an absolute IRI (a scheme, or an existing `urn:...`) is passed
    through unchanged rather than double-wrapped, so a project file that
    already spells out the full URI keeps working.
    """
    if re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*:", raw):
        return raw
    return ACTOR_PREFIX + raw


def turtle_string(value: str) -> str:
    """Escape a Python string as a Turtle short string literal (RDF 1.1 §19.8,
    the same escaping N-Triples uses)."""
    out = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{out}"'


def turtle_iri(value: str) -> str:
    return f"<{value}>"


def render(fm: dict[str, str], fallback_id: str) -> str:
    """Render a project's frontmatter as Turtle. Returns "" when the
    frontmatter is not project frontmatter (no `type: project`) or carries no
    usable `id`."""
    if fm.get("type", "").strip().lower() != "project":
        return ""
    project_id = fm.get("id", "").strip() or fallback_id
    if not project_id:
        return ""

    subj = turtle_iri(PROJECT_PREFIX + project_id)
    predicates = ["a kb:Project"]

    current_actor = fm.get("current_actor", "").strip()
    if current_actor:
        predicates.append(f"kb:currentActor {turtle_iri(actor_iri(current_actor))}")

    for key, (name, datatype) in SCALAR_FIELDS.items():
        raw = fm.get(key)
        if raw is None or raw == "":
            continue
        if datatype == "boolean":
            literal = "true" if as_bool(raw) else "false"
            predicates.append(f'kb:{name} "{literal}"^^xsd:boolean')
        elif datatype == "date":
            predicates.append(f'kb:{name} "{raw}"^^xsd:date')
        else:
            predicates.append(f"kb:{name} {turtle_string(raw)}")

    for key, name in LIST_FIELDS.items():
        for entry in as_list(fm.get(key) or ""):
            predicates.append(f"kb:{name} {turtle_string(entry)}")

    body = " ;\n    ".join(predicates)
    return f"{subj}\n    {body} .\n"


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: md2ttl.py <input.md>", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"md2ttl.py: {exc}", file=sys.stderr)
        return 1

    fm = parse_frontmatter(text)
    triples = render(fm, fallback_id=path.stem)
    if not triples:
        return 0  # not project frontmatter (or no id) -> no triples, not an error

    sys.stdout.write(f"@prefix kb: <{KB}> .\n@prefix xsd: <{XSD}> .\n\n")
    sys.stdout.write(triples)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
