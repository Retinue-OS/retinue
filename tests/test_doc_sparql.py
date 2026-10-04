#!/usr/bin/env python3
"""The documentation's SPARQL examples are runnable queries (issue #261).

Every ```sparql block in docs/*.md is a query the dashboard's /sparql page
offers to run in place, and that an agent may POST to the life store as it
stands. So each block must be exactly one complete, read-only query that
declares every prefix it uses — a block that leans on a prefix declared in a
neighbouring block, or holds two queries, reads fine and fails on Run.

docs/ontology.md also promises one example per kind of data in its defaults
table: every namespace the table names must appear in at least one of its
examples, so adding a row without an example fails here.

The structural checks need nothing installed. When rdflib is importable (CI
installs it) every block is also parsed and translated by a SPARQL 1.1 parser.
The examples were also run against a real QLever when they were written; that
is not repeated here, since no store runs in CI.

    python3 tests/test_doc_sparql.py
"""
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS = REPO_ROOT / "docs"

FENCE_RE = re.compile(r"^```sparql[^\n]*\n(.*?)^```", re.M | re.S)
PREFIX_DECL_RE = re.compile(r"^\s*PREFIX\s+([A-Za-z][\w.-]*)?:\s*<([^>]*)>", re.I | re.M)
# Strings, IRIs and comments, which may contain anything that looks like a
# prefixed name; they are blanked before prefixed names are looked for.
OPAQUE_RE = re.compile(r'"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\'|<[^<>"\s]*>|#[^\n]*')
PNAME_RE = re.compile(r"(?<![\w.:?$-])([A-Za-z][\w.-]*)?:(?=[\w%])")
QUERY_FORM_RE = re.compile(r"\b(SELECT|ASK|CONSTRUCT|DESCRIBE)\b", re.I)
UPDATE_RE = re.compile(r"\b(INSERT|DELETE|LOAD|CLEAR|CREATE|DROP|COPY|MOVE|ADD)\b", re.I)


def sparql_blocks(path: Path) -> list[str]:
    return [m.group(1) for m in FENCE_RE.finditer(path.read_text(encoding="utf-8"))]


def _top_level_forms(body: str) -> int:
    """Query forms outside any braces: a subquery's SELECT does not count."""
    depth, count = 0, 0
    for token in re.finditer(r"[{}]|\b(?:SELECT|ASK|CONSTRUCT|DESCRIBE)\b", body, re.I):
        t = token.group(0)
        if t == "{":
            depth += 1
        elif t == "}":
            depth -= 1
        elif depth == 0:
            count += 1
    return count


def structural_problems(query: str) -> list[str]:
    problems = []
    declared = {m.group(1) or "" for m in PREFIX_DECL_RE.finditer(query)}
    body = OPAQUE_RE.sub(" ", PREFIX_DECL_RE.sub(" ", query))
    used = {m.group(1) or "" for m in PNAME_RE.finditer(body)}
    for prefix in sorted(used - declared):
        problems.append(f"prefix {prefix!r}: used but not declared in this block")
    forms = _top_level_forms(body)
    if forms != 1:
        problems.append(f"{forms} top-level query forms; a block holds exactly one query")
    if UPDATE_RE.search(body):
        problems.append("an update keyword: the life store is read-only")
    if not QUERY_FORM_RE.search(body):
        problems.append("no query form at all")
    return problems


def test_blocks_are_self_contained_queries():
    seen = 0
    failures = []
    for path in sorted(DOCS.glob("*.md")):
        for i, query in enumerate(sparql_blocks(path)):
            seen += 1
            for problem in structural_problems(query):
                failures.append(f"{path.name} block {i + 1}: {problem}")
    assert seen, "no sparql blocks found in docs/ — has the fence syntax changed?"
    assert not failures, "\n".join(failures)
    print(f"ok - {seen} sparql blocks are self-contained single queries")


def test_blocks_parse_as_sparql():
    try:
        from rdflib.plugins.sparql.algebra import translateQuery
        from rdflib.plugins.sparql.parser import parseQuery
    except ImportError:
        print("skip - rdflib not installed: blocks not parsed (structural checks still ran)")
        return
    failures = []
    for path in sorted(DOCS.glob("*.md")):
        for i, query in enumerate(sparql_blocks(path)):
            try:
                # translateQuery resolves every prefixed name, so an undeclared
                # prefix fails here as well as in the structural check.
                translateQuery(parseQuery(query))
            except Exception as exc:  # noqa: BLE001 - report every block
                failures.append(f"{path.name} block {i + 1}: {exc}")
    assert not failures, "\n".join(failures)
    print("ok - every sparql block parses as a SPARQL 1.1 query")


def test_every_default_namespace_has_an_example():
    text = (DOCS / "ontology.md").read_text(encoding="utf-8")
    table = text.split("## Defaults", 1)[1].split("\n## ", 1)[0]
    namespaces = set()
    for line in table.splitlines():
        if line.startswith("|"):
            namespaces.update(re.findall(r"`(https?://[^`]+)`", line))
    assert len(namespaces) >= 9, f"the defaults table lost its namespaces? {namespaces}"
    examples = "\n".join(sparql_blocks(DOCS / "ontology.md"))
    missing = sorted(ns for ns in namespaces if f"<{ns}" not in examples)
    assert not missing, ("docs/ontology.md: no example query uses "
                         + ", ".join(missing) + " — add one per kind of data")
    print(f"ok - all {len(namespaces)} default namespaces have an example query")


def test_structural_check_catches_what_it_should():
    ok = "PREFIX kb: <https://w3id.org/retinue/kb#>\nSELECT ?p WHERE { ?p a kb:Project }"
    assert structural_problems(ok) == [], structural_problems(ok)
    # A subquery is still one query; prefixes inside IRIs, strings and
    # comments are not prefixed names.
    sub = ('SELECT ?g (SUM(?n) AS ?t) WHERE { { SELECT ?g (COUNT(*) AS ?n) WHERE '
           '{ GRAPH ?g { ?s ?p ?o } } GROUP BY ?g } BIND ("a:b" AS ?x) } # c:d\n'
           'GROUP BY ?g')
    assert structural_problems(sub) == [], structural_problems(sub)
    assert any("'xsd'" in p for p in structural_problems(
        'SELECT * WHERE { ?s ?p ?o FILTER (?o > "1"^^xsd:integer) }'))
    assert any("2 top-level" in p for p in structural_problems(
        "SELECT * WHERE { ?s ?p ?o }\nSELECT * WHERE { ?s ?p ?o }"))
    assert any("read-only" in p for p in structural_problems(
        "INSERT DATA { <a> <b> <c> }"))
    print("ok - the structural check flags undeclared prefixes, two queries and updates")


def main() -> int:
    tests = [test_structural_check_catches_what_it_should,
             test_blocks_are_self_contained_queries,
             test_blocks_parse_as_sparql,
             test_every_default_namespace_has_an_example]
    failed = 0
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            failed += 1
            print(f"FAIL - {test.__name__}:\n{exc}")
    print("PASS" if not failed else f"FAIL: {failed} test(s)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
