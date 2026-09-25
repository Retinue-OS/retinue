#!/usr/bin/env python3
"""Cross-check the project vocabulary: converter output vs. the shipped queries.

retinue-os/retinue#1 happened because two sides of the same contract — the
Markdown->Turtle converter and the SPARQL queries that read its output — were
tested (if at all) in isolation: each side's own unit tests assumed the other
side's shape and never checked it. retinue-os/retinue#23 happened the same
way, one field later (`resolved`).

This test closes that gap without a live SPARQL endpoint. It runs the real
`scripts/md2ttl.py` over project fixtures and the real `scripts/discover-
agents.py` over a fixture agent tree, parses the Turtle/N-Triples they emit,
and checks the result against the *actual* query text imported live from
`scripts/web-gateway.py`, `scripts/agent-self-review.py` and `scripts/
recurring-projects.py` — never a hand-copied restatement of what those
queries are assumed to say, so a future edit to either side is what this test
re-reads, not what it remembers.

    python3 tests/test_project_vocabulary.py
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import os
import re
import sys
import tempfile
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

failures: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
        failures.append(label)


# ── Loading the real scripts (never a copy of their logic) ───────────────────

def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_web_gateway(tmp: Path):
    """Same recipe as tests/test_web_gateway_projects.py: sandboxed state dirs
    and a markdown_it stub, since only `_KB`/`_PROJECTS_SPARQL` are needed
    here, not a running server."""
    os.environ["CONVERSATIONS_DIR"] = str(tmp / "convs")
    os.environ["CONVERSATION_DIR"] = str(tmp / "convlog")
    os.environ["CHAMBERS_DIR"] = str(tmp / "wg_chambers")
    os.environ["WEB_GATEWAY_STATE"] = str(tmp / "state.json")
    (tmp / "wg_chambers").mkdir(parents=True, exist_ok=True)
    if "markdown_it" not in sys.modules:
        try:
            import markdown_it  # noqa: F401
        except ImportError:
            stub = types.ModuleType("markdown_it")
            stub.MarkdownIt = object
            sys.modules["markdown_it"] = stub
    sys.path.insert(0, str(SCRIPTS_DIR))
    return load_module("web_gateway_under_test", "web-gateway.py")


def load_discover_agents(tmp: Path, agents: dict[str, str]):
    """Write a fixture agent tree and run the real discover-agents.py module
    over it (its own pure discover()/render(), no filesystem write)."""
    workspace = tmp / "da_workspace"
    subagents = workspace / ".claude" / "agents"
    subagents.mkdir(parents=True, exist_ok=True)
    for slug, description in agents.items():
        (subagents / f"{slug}.md").write_text(
            f"---\ndescription: {description}\n---\nBody.\n")
    chambers = tmp / "da_chambers"
    chambers.mkdir(parents=True, exist_ok=True)
    os.environ["RETINUE_WORKSPACE"] = str(workspace)
    os.environ["CHAMBERS_DIR"] = str(chambers)
    os.environ["AGENTS_TTL_PATH"] = str(tmp / "agents.nt")
    mod = load_module("discover_agents_under_test", "discover-agents.py")
    return mod, mod.render(mod.discover())


# ── A tiny Turtle/N-Triples reader (data only: no variables, no blank nodes,
# no collections -- everything scripts/md2ttl.py and scripts/discover-
# agents.py actually emit fits this) ──────────────────────────────────────────

_TOKEN_RE = re.compile(r"""
    @prefix
  | <[^<>"{}|^`\\\s]*>                                                     # IRIREF
  | "(?:[^"\\]|\\.)*"(?:\^\^(?:<[^>]+>|[A-Za-z_][\w-]*:[A-Za-z_][\w-]*))?  # STRING (+ optional datatype)
  | [A-Za-z_][A-Za-z0-9_-]*:[A-Za-z_][A-Za-z0-9_-]*                       # PNAME_LN
  | [A-Za-z_][A-Za-z0-9_-]*:                                              # PNAME_NS (bare, e.g. in @prefix)
  | [{}();,.]
  | [A-Za-z_][A-Za-z0-9_]*                                                # WORD ('a', etc.)
  | \#[^\n]*                                                              # comment
""", re.VERBOSE)

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"


def _tokenize(text: str) -> list[str]:
    return [m.group(0) for m in _TOKEN_RE.finditer(text) if not m.group(0).startswith("#")]


def _prefixes(text: str) -> dict[str, str]:
    return dict(re.findall(r"@prefix\s+([A-Za-z_][\w-]*):\s*<([^>]*)>\s*\.", text))


def _expand(tok: str, prefixes: dict[str, str]):
    """Turtle/N-Triples token -> a term: ('iri', iri) or ('lit', value, tag)."""
    if tok == "a":
        return ("iri", RDF_TYPE)
    if tok.startswith("<"):
        return ("iri", tok[1:-1])
    if tok.startswith('"'):
        m = re.match(r'"((?:[^"\\]|\\.)*)"(?:\^\^(.+))?$', tok)
        value, dt_tok = m.group(1), m.group(2)
        if dt_tok is None:
            return ("lit", value, None)
        if dt_tok.startswith("<"):
            dt_iri = dt_tok[1:-1]
        else:
            pfx, local = dt_tok.split(":", 1)
            dt_iri = prefixes[pfx] + local
        return ("lit", value, dt_iri.rsplit("#", 1)[-1])
    if tok in ("true", "false"):
        return ("lit", tok, "boolean")
    pfx, _, local = tok.partition(":")
    return ("iri", prefixes[pfx] + local)


def parse_data(text: str) -> list[tuple[str, str, tuple]]:
    """Parse a Turtle (md2ttl.py) or N-Triples (discover-agents.py) document
    into flat (subject_iri, predicate_iri, object_term) triples."""
    prefixes = _prefixes(text)
    # Strip prefix declarations before tokenizing statements ([^.]* would stop
    # at the first '.' inside the IRI itself, e.g. "w3id.org" -- match the IRI
    # by its closing '>' instead).
    body = re.sub(r"@prefix\s+[A-Za-z_][\w-]*:\s*<[^>]*>\s*\.", "", text)
    toks = _tokenize(body)
    triples: list[tuple[str, str, tuple]] = []
    i, n = 0, len(toks)
    while i < n:
        subj = _expand(toks[i], prefixes)
        assert subj[0] == "iri", f"non-IRI subject {toks[i]!r}"
        i += 1
        while True:
            pred = _expand(toks[i], prefixes)
            obj = _expand(toks[i + 1], prefixes)
            triples.append((subj[1], pred[1], obj))
            sep = toks[i + 2]
            i += 3
            if sep == ";":
                continue
            if sep == ".":
                break
            raise AssertionError(f"unexpected separator {sep!r} in {text[:80]!r}...")
    return triples


def has_literal(store, subj: str, pred: str, value: str, tag: str | None = None) -> bool:
    return any(s == subj and p == pred and o == ("lit", value, tag) for s, p, o in store)


def has_iri(store, subj: str, pred: str, obj_iri: str) -> bool:
    return any(s == subj and p == pred and o == ("iri", obj_iri) for s, p, o in store)


def subjects_with_type(store, type_iri: str) -> set[str]:
    return {s for s, p, o in store if p == RDF_TYPE and o == ("iri", type_iri)}


# ── Extracting what each shipped query actually requires, from its own text ──
# Regexes on the live, imported query strings -- not a hand-restated copy, so
# an edit to the real query is what these re-read, not what they assume.

def not_exists_clauses(query_text: str, subject_var: str) -> list[tuple[str, str]]:
    """[(predicate_local, literal_token), ...] for every
    `FILTER NOT EXISTS { ?subject_var kb:pred VALUE }` in the query."""
    return re.findall(
        rf"FILTER\s+NOT\s+EXISTS\s*\{{\s*\?{subject_var}\s+\w+:(\w+)\s+"
        r'(true|false|"[^"]*")\s*\}',
        query_text,
    )


def literal_check(store, subj: str, kb_ns: str, pred_local: str, token: str) -> bool:
    """Does `store` carry (subj, kb_ns+pred_local, token-as-literal)?"""
    if token in ("true", "false"):
        return has_literal(store, subj, kb_ns + pred_local, token, "boolean")
    return has_literal(store, subj, kb_ns + pred_local, token.strip('"'), None)


def optional_predicate_vars(query_text: str, subject_var: str) -> dict[str, str]:
    """{predicate_local: bound_var_name} for every
    `OPTIONAL { ?subject_var kb:pred ?var }` in the query."""
    return dict(re.findall(
        rf"OPTIONAL\s*\{{\s*\?{subject_var}\s+\w+:(\w+)\s+\?(\w+)\s*\}}",
        query_text,
    ))


def all_predicate_locals(query_text: str) -> set[str]:
    """Every `<prefix>:Local` token bound to the query's own kb# prefix
    (never `rdf:`/`xsd:`, which are structural, not project vocabulary),
    minus the class names it tests for with `a`/`rdf:type` -- i.e. every
    predicate it reads. The query's own prefix letter(s) come from whichever
    PREFIX line(s) declare the kb# namespace, so this adapts to `k:` or
    `kb:` (or any future spelling) rather than hardcoding one."""
    kb_ns = re.search(r"https://w3id\.org/retinue/kb#", query_text)
    if not kb_ns:
        return set()
    prefix_letters = re.findall(
        r"PREFIX\s+(\w+):\s*<https://w3id\.org/retinue/kb#>", query_text)
    tokens: set[str] = set()
    for letter in prefix_letters:
        tokens |= set(re.findall(rf"\b{re.escape(letter)}:(\w+)\b", query_text))
    return tokens - {"Project", "AiAgent"}


# ── Fixtures ───────────────────────────────────────────────────────────────

PROJECT_FIXTURE = """---
type: project
id: {id}
title: "Fixture project {id}"
goal: "Exercised by tests/test_project_vocabulary.py."
current_next_action: "Do the next thing."
current_actor: {actor}
waiting_since: 2026-06-20
{extra}---

Prose body, ignored by the converter.
"""


def write_fixture(tmp: Path, name: str, actor: str, extra: str) -> Path:
    path = tmp / f"{name}.md"
    path.write_text(PROJECT_FIXTURE.format(id=name, actor=actor, extra=extra))
    return path


def convert(md2ttl_path: Path, fixture: Path) -> str:
    import subprocess
    result = subprocess.run(
        [sys.executable, str(md2ttl_path), str(fixture)],
        capture_output=True, text=True, check=True,
    )
    return result.stdout


# ── The test ───────────────────────────────────────────────────────────────

def main() -> int:
    with tempfile.TemporaryDirectory() as tmp_s:
        tmp = Path(tmp_s)

        md2ttl_mod = load_module("md2ttl_under_test", "md2ttl.py")
        webgw = load_web_gateway(tmp)
        asr = load_module("agent_self_review_under_test", "agent-self-review.py")
        discover_mod, agents_nt = load_discover_agents(tmp, {
            "coach": "Fixture coaching agent.",
        })
        # recurring-projects.py's env-driven constants (ENDPOINT etc.) are
        # harmless to load after the others; only build_query/KB are used.
        rp = load_module("recurring_projects_under_test", "recurring-projects.py")

        print("vocabulary agreement")
        check("md2ttl.py KB == web-gateway._KB", md2ttl_mod.KB, webgw._KB)
        check("md2ttl.py KB == agent-self-review.KB", md2ttl_mod.KB, asr.KB)
        check("md2ttl.py KB == recurring-projects.KB", md2ttl_mod.KB, rp.KB)
        check("md2ttl.py ACTOR_PREFIX == discover-agents.ACTOR_PREFIX",
              md2ttl_mod.ACTOR_PREFIX, discover_mod.ACTOR_PREFIX)
        KB = md2ttl_mod.KB
        agents_store = parse_data(agents_nt)
        coach_iri = md2ttl_mod.ACTOR_PREFIX + "coach"
        check("discover-agents typed the fixture agent kb:AiAgent",
              coach_iri in subjects_with_type(agents_store, KB + "AiAgent"), True)

        print("\npredicate completeness (every query predicate is one md2ttl.py can emit)")
        converter_knows = {name for name, _ in md2ttl_mod.SCALAR_FIELDS.values()}
        converter_knows |= set(md2ttl_mod.LIST_FIELDS.values())
        converter_knows.add("currentActor")
        queried = set()
        for text in (webgw._PROJECTS_SPARQL, asr.QUERY, rp.build_query(dt.date(2026, 9, 13))):
            queried |= all_predicate_locals(text)
        queried.discard("name")  # actor-side (discover-agents.py), not project frontmatter
        missing = queried - converter_knows
        check("no predicate is read by a query but never emitted by md2ttl.py",
              missing, set())

        print("\nactive project pending on a discovered agent")
        fx_a = write_fixture(tmp, "fixture-active", "coach", "paused: false\n")
        store_a = parse_data(convert(SCRIPTS_DIR / "md2ttl.py", fx_a)) + agents_store
        subj_a = md2ttl_mod.PROJECT_PREFIX + "fixture-active"
        check("typed kb:Project", subj_a in subjects_with_type(store_a, KB + "Project"), True)
        check("kb:currentActor -> the agent's own IRI",
              has_iri(store_a, subj_a, KB + "currentActor", coach_iri), True)
        check("that IRI is the one discover-agents.py typed kb:AiAgent (the join "
              "agent-self-review.py's query performs)",
              coach_iri in subjects_with_type(store_a, KB + "AiAgent"), True)

        # web-gateway's paused/status guards, applied via the predicate names
        # its OWN OPTIONAL bindings name -- not hardcoded 'paused'/'status'.
        wg_optionals = optional_predicate_vars(webgw._PROJECTS_SPARQL, "p")
        check("web-gateway's projects card would keep this row (not paused)",
              literal_check(store_a, subj_a, KB, wg_optionals["paused"], "true"), False)
        check("web-gateway's projects card would keep this row (status not done)",
              literal_check(store_a, subj_a, KB, wg_optionals["status"], "done"), False)

        for pred, token in not_exists_clauses(asr.QUERY, "project"):
            check(f"agent-self-review's FILTER NOT EXISTS({pred}={token}) does not "
                  "exclude this row",
                  literal_check(store_a, subj_a, KB, pred, token), False)

        print("\nresting project due for its recurring wake")
        fx_b = write_fixture(tmp, "fixture-resting", "coach",
                              "paused: true\nrecurring: monthly\nnext_due: 2026-09-01\n"
                              "due_day: 8\n")
        store_b = parse_data(convert(SCRIPTS_DIR / "md2ttl.py", fx_b))
        subj_b = md2ttl_mod.PROJECT_PREFIX + "fixture-resting"
        rp_query = rp.build_query(dt.date(2026, 9, 13))
        check("recurring-projects' mandatory `kb:paused true` pattern matches",
              has_literal(store_b, subj_b, KB + "paused", "true", "boolean"), True)
        # And the paused=false fixture above must NOT satisfy that same mandatory
        # pattern -- the gate is for resting projects only.
        check("...and does not match the active (paused: false) fixture",
              has_literal(store_a, subj_a, KB + "paused", "true", "boolean"), False)
        for pred, token in not_exists_clauses(rp_query, "project"):
            check(f"recurring-projects' FILTER NOT EXISTS({pred}={token}) does not "
                  "exclude this row",
                  literal_check(store_b, subj_b, KB, pred, token), False)

        # `due_day` (PR #224 review, finding 2): docs/scheduling.md documents it
        # as frontmatter but is explicit that it need not reach the store, since
        # unlike `remind_before` no code -- store or file side -- ever reads it
        # back; recurring-projects.py's own SELECT does not name `dueDay` either.
        # Pin both halves of that contract so a converter that starts emitting
        # it, or a query that starts requiring it without the other, is caught.
        check("md2ttl.py does not emit kb:dueDay (informational-only field, "
              "docs/scheduling.md)",
              "dueDay" in {p.rsplit("#", 1)[-1] for _, p, _ in store_b}, False)
        check("recurring-projects' own query does not read kb:dueDay either "
              "(so the omission above loses no query-visible data)",
              "dueDay" in all_predicate_locals(rp_query), False)

        print("\nresolved project (retinue-os/retinue#23 regression)")
        fx_c = write_fixture(tmp, "fixture-resolved", "coach",
                              "paused: false\nresolved: true\nstatus: done\n")
        store_c = parse_data(convert(SCRIPTS_DIR / "md2ttl.py", fx_c)) + agents_store
        subj_c = md2ttl_mod.PROJECT_PREFIX + "fixture-resolved"
        check("md2ttl.py actually emits kb:resolved true for `resolved: true` "
              "(this is the exact bug in retinue-os/retinue#23)",
              has_literal(store_c, subj_c, KB + "resolved", "true", "boolean"), True)
        asr_excludes = any(
            literal_check(store_c, subj_c, KB, pred, token)
            for pred, token in not_exists_clauses(asr.QUERY, "project"))
        check("agent-self-review's gate now correctly excludes the resolved project",
              asr_excludes, True)
        check("web-gateway's card would hide it too (status: done)",
              literal_check(store_c, subj_c, KB, wg_optionals["status"], "done"), True)

    if failures:
        print(f"\n{len(failures)} check(s) FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nall project-vocabulary checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
