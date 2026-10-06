#!/usr/bin/env python3
"""The documentation's SPARQL examples are runnable queries (issue #261).

Every ```sparql block in docs/*.md is a query the dashboard's /sparql page
offers to run in place, and that an agent may POST to the life store as it
stands. So each block must be exactly one complete, read-only query that
declares every prefix it uses — a block that leans on a prefix declared in a
neighbouring block, or holds two queries, reads fine and fails on Run.

docs/ontology.md also promises one example per kind of data in its defaults
table: each vocabulary the table names needs an example section of its own
('### … — <vocabulary>', the names after the dash found in the row's
vocabulary cell; rows naming the same vocabulary, like the Fallback row,
share it) whose queries use — not merely declare — its namespace, and no
section may answer for two vocabularies. Adding a row without an example, or
dropping the example a row had, fails here.

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
# Strings (long ones first), IRIs and comments: anything that looks like a
# prefixed name or a keyword inside them is neither.
OPAQUE_RE = re.compile(
    r'"""(?:[^"\\]|\\.|"(?!""))*"""' r"|'''(?:[^'\\]|\\.|'(?!''))*'''"
    r'|"(?:[^"\\\n]|\\.)*"' r"|'(?:[^'\\\n]|\\.)*'"
    r'|<[^<>"\s]*>|#[^\n]*')
# PREFIX and BASE declarations, wherever they stand on a line.
DECL_RE = re.compile(r"\b(?:PREFIX\s+([A-Za-z][\w.-]*)?:\s*<([^>]*)>|BASE\s*<[^>]*>)", re.I)
PNAME_RE = re.compile(r"(?<![\w.:?$-])([A-Za-z][\w.-]*)?:(?=[\w%])")
QUERY_FORM_RE = re.compile(r"\b(SELECT|ASK|CONSTRUCT|DESCRIBE)\b", re.I)
# An update keyword standing alone: not a variable (?load, $add), not part of
# a prefixed name (ex:add, add:x, add.x:y) or a language tag (@add).
UPDATE_RE = re.compile(r"(?<![\w?$:.@\\~-])(INSERT|DELETE|LOAD|CLEAR|CREATE|DROP|COPY|MOVE|ADD)(?![\w:.-])",
                       re.I)
IRI_RE = re.compile(r"<([^<>\"\s]*)>")


def sparql_blocks(path: Path) -> list[str]:
    return [m.group(1) for m in FENCE_RE.finditer(path.read_text(encoding="utf-8"))]


def _lex(query: str) -> tuple[dict[str, str], str, str]:
    """(declared prefixes, body with IRIs, body without): comments and strings
    blanked, PREFIX/BASE declarations taken out."""
    kept = OPAQUE_RE.sub(lambda m: m.group(0) if m.group(0).startswith("<") else " ", query)
    declared = {m.group(1) or "": m.group(2) for m in DECL_RE.finditer(kept) if m.group(2) is not None}
    body = DECL_RE.sub(" ", kept)
    return declared, body, IRI_RE.sub(" ", body)


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


def used_iris(query: str) -> set[str]:
    """What the query itself uses: every IRI written out in full, and the
    namespace of every prefixed name, resolved through the block's own PREFIX
    declarations. A prefix declared and never used, a BASE, and anything in a
    comment or a string, does not count."""
    declared, body, bare = _lex(query)
    named = {declared[m.group(1) or ""] for m in PNAME_RE.finditer(bare)
             if (m.group(1) or "") in declared}
    return set(IRI_RE.findall(body)) | named


def structural_problems(query: str) -> list[str]:
    problems = []
    declared, _body, bare = _lex(query)
    used = {m.group(1) or "" for m in PNAME_RE.finditer(bare)}
    for prefix in sorted(used - set(declared)):
        problems.append(f"prefix {prefix!r}: used but not declared in this block")
    forms = _top_level_forms(bare)
    if forms != 1:
        problems.append(f"{forms} top-level query forms; a block holds exactly one query")
    if UPDATE_RE.search(bare):
        problems.append("an update keyword: the life store is read-only")
    if not QUERY_FORM_RE.search(bare):
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


def example_sections(text: str) -> dict[str, list[str]]:
    """The '### …' sections under '## Example queries', with their blocks."""
    part = text.split("## Example queries", 1)[1].split("\n## ", 1)[0]
    sections = {}
    for chunk in re.split(r"^### ", part, flags=re.M)[1:]:
        heading, _, body = chunk.partition("\n")
        sections[heading.strip()] = [m.group(1) for m in FENCE_RE.finditer(body)]
    return sections


def _names_vocabulary(heading: str, vocabulary: str) -> bool:
    """'Labels — RDFS and SKOS' names the row whose vocabulary cell reads
    'RDFS / **SKOS**': every name after the dash appears in the cell."""
    if " — " not in heading:
        return False
    names = [n.strip().strip("`") for n in heading.split(" — ", 1)[1].split(" and ")]
    return all(n and n in vocabulary for n in names)


def test_every_default_namespace_has_an_example():
    text = (DOCS / "ontology.md").read_text(encoding="utf-8")
    table = text.split("## Defaults", 1)[1].split("\n## ", 1)[0]
    rows = []
    for line in table.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        namespaces = re.findall(r"`(https?://[^`]+)`", line)
        if line.startswith("|") and len(cells) >= 3 and namespaces:
            rows.append((cells[1].replace("**", ""), namespaces))
    assert sum(len(n) for _v, n in rows) >= 10, f"the defaults table lost its namespaces? {rows}"
    # Rows that name the same vocabulary (the Fallback row names schema.org
    # again) share its section; every distinct vocabulary needs its own.
    vocabularies: dict[str, set[str]] = {}
    for vocabulary, namespaces in rows:
        vocabularies.setdefault(vocabulary, set()).update(namespaces)
    sections = example_sections(text)
    failures, answers_for = [], {}
    for vocabulary, namespaces in vocabularies.items():
        own = [h for h in sections if _names_vocabulary(h, vocabulary)]
        if not own:
            failures.append(f"no '### … — <vocabulary>' example section for {vocabulary!r}")
            continue
        for heading in own:
            if answers_for.setdefault(heading, vocabulary) != vocabulary:
                failures.append(f"{heading!r} answers for both {answers_for[heading]!r} and "
                                f"{vocabulary!r}: each vocabulary needs a section of its own")
        used = set().union(*(used_iris(q) for h in own for q in sections[h]))
        for ns in sorted(namespaces):
            if not any(iri.startswith(ns) for iri in used):
                failures.append(f"the example for {vocabulary!r} ({', '.join(own)}) does not use {ns}")
    assert not failures, "docs/ontology.md, one example per kind of data:\n" + "\n".join(failures)
    print(f"ok - each of the {len(vocabularies)} vocabularies of the defaults table "
          f"({len(rows)} rows) has an example of its own")


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
    # A variable, a local name, a prefix or a language tag that spells an
    # update keyword is no update.
    named = ("PREFIX ex: <http://ex/>\nPREFIX add: <http://a/>\n"
             'SELECT ?load ?add WHERE { ?s ex:add ?load ; ex:drop $add ; add:x "y"@add }')
    assert structural_problems(named) == [], structural_problems(named)
    # Declarations count wherever they stand; a long string hides what it holds.
    tricky = ("BASE <http://b/> PREFIX a: <http://a/> PREFIX p: <http://p/>\n"
              'SELECT * { ?s a:x """p:y\nDROP ALL""" }')
    assert structural_problems(tricky) == [], structural_problems(tricky)
    assert used_iris(tricky) == {"http://a/"}, used_iris(tricky)
    # Coverage counts what a query uses, not what it declares or mentions.
    assert used_iris("PREFIX p: <http://p/>\nPREFIX q: <http://q/>\n"
                     "SELECT * WHERE { ?s p:x <http://r/y> } # q:z") == {"http://p/", "http://r/y"}
    print("ok - the structural check flags undeclared prefixes, two queries and updates, "
          "and nothing that merely spells one")


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
