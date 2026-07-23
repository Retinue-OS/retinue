#!/usr/bin/env python3
"""Generic JSON-LD -> Turtle converter for qlever-dir.

qlever-dir indexes a non-RDF file when a chamber declares a converter for its
extension in `.qlever/converters.json` (e.g. `{"jsonld": "jsonld2ttl.py"}`).
The contract is minimal: invoked as `<converter> <input-file>`, emit Turtle on
stdout; the source file keeps its own path-derived named graph.

This makes a JSON-LD document — which the web-gateway also reads directly as
plain JSON, no RDF dependency on the serving path — simultaneously queryable in
the life store. One file, two access paths, no duplicated source of truth.

Language-agnostic and content-agnostic: it expands whatever JSON-LD it is given.
"""
import sys

from rdflib import Graph


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: jsonld2ttl.py <input.jsonld>", file=sys.stderr)
        return 2
    g = Graph()
    # `publicID` gives blank-node-free relative IRIs a stable base; qlever-dir
    # overrides the graph IRI from the file path regardless.
    g.parse(sys.argv[1], format="json-ld")
    sys.stdout.write(g.serialize(format="turtle"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
