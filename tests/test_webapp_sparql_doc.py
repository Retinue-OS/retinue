#!/usr/bin/env python3
"""The SPARQL page's rendering of the docs (webapp/components/sparql.js and
the shared renderer, webapp/components/markdown.js), under Node.

The page (issue #261) renders docs/ontology.md itself and turns each ```sparql
block into a runnable example, so what it runs is exactly what the document
says. This pins the pieces that promise rests on:

- the renderer hands every sparql fence of the real ontology.md to the page's
  code hook, verbatim, as many as sparqlBlocks() finds;
- the prefix map the editor completes from is the one the examples declare;
- a doc name from the URL can only name a doc;
- the renderer fixes the docs needed, without changing messages: a code span
  is literal (a URL in backticks is code, not a link inside backticks), and
  hard-wrapped prose flows (`hardWrapped`) while a message keeps its breaks.

Standalone like the rest of the suite. Needs `node` on PATH; without it the
test reports a skip and passes.
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
COMPONENTS = os.path.join(HERE, "..", "webapp", "components")
ONTOLOGY = os.path.join(HERE, "..", "docs", "ontology.md")

HARNESS = r"""
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

// sparql.js defines its elements on import; give it just enough of a DOM.
globalThis.HTMLElement = class {};
globalThis.customElements = { define() {} };

const { renderMarkdown, renderInline } = await import('./markdown.js');
const { sparqlBlocks, prefixesOf, docUrl } = await import('./sparql.js');
const ontology = readFileSync('./ontology.md', 'utf8');

let passed = 0;
const ok = (name, fn) => { fn(); passed += 1; console.log(`ok - ${name}`); };

ok('every sparql fence reaches the code hook verbatim', () => {
  const seen = [];
  renderMarkdown(ontology, {
    hardWrapped: true,
    code: (raw, lang, inner) => { if (lang === 'sparql') seen.push(raw); return inner; },
  });
  const blocks = sparqlBlocks(ontology);
  assert.ok(blocks.length >= 10, `only ${blocks.length} examples`);
  assert.deepEqual(seen, blocks);
});

ok('the prefixes are the ones the examples declare', () => {
  const p = prefixesOf(ontology);
  for (const [prefix, ns] of Object.entries({
    sosa: 'http://www.w3.org/ns/sosa/', xsd: 'http://www.w3.org/2001/XMLSchema#',
    vcard: 'http://www.w3.org/2006/vcard/ns#', foaf: 'http://xmlns.com/foaf/0.1/',
    schema: 'http://schema.org/', dcterms: 'http://purl.org/dc/terms/',
    rdfs: 'http://www.w3.org/2000/01/rdf-schema#', skos: 'http://www.w3.org/2004/02/skos/core#',
    prov: 'http://www.w3.org/ns/prov#', kb: 'https://w3id.org/retinue/kb#',
  })) assert.equal(p[prefix], ns, prefix);
});

ok('a doc name from the URL can only name a doc', () => {
  assert.equal(docUrl('triple-stores'), '/docs/triple-stores.md');
  for (const bad of ['../CLAUDE', 'a/b', '', null, '.hidden', 'x.md?'])
    assert.equal(docUrl(bad), '/docs/ontology.md', String(bad));
});

ok('a code span is literal, links and all', () => {
  const html = renderInline('`http://schema.org/` and `**not bold**`');
  assert.equal(html, '<code>http://schema.org/</code> and <code>**not bold**</code>');
  assert.match(renderInline('[`kb:`](https://w3id.org/retinue/kb#)'),
    /^<a href="https:\/\/w3id.org\/retinue\/kb#"[^>]*><code>kb:<\/code><\/a>$/);
  // A URL right before a code span keeps its own href.
  assert.match(renderInline('https://example.org`x`'), /href="https:\/\/example.org"/);
  // Text that happens to hold the stash sentinel cannot hang the renderer.
  assert.equal(typeof renderInline('a \u0001 7 \u0001 b'), 'string');
});

ok('a document flows, a message keeps its line breaks', () => {
  const src = 'one\ntwo\n\n- item\n  continued\n- next\n\n> quoted\n> on';
  const doc = renderMarkdown(src, { hardWrapped: true });
  assert.match(doc, /<p>one two<\/p>/);
  assert.match(doc, /<li>item continued<\/li><li>next<\/li>/);
  assert.match(doc, /quoted on/);
  const msg = renderMarkdown(src);
  assert.match(msg, /<p>one<br>two<\/p>/);
  assert.match(msg, /<li>item<\/li><\/ul><p>\s*continued<\/p>/);
});

console.log(`${passed} checks passed`);
"""


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("SKIP: node is not installed; the SPARQL page test needs it")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        for name in ("base.js", "markdown.js", "sparql.js"):
            shutil.copy(os.path.join(COMPONENTS, name), os.path.join(tmp, name))
        shutil.copy(ONTOLOGY, os.path.join(tmp, "ontology.md"))
        with open(os.path.join(tmp, "package.json"), "w", encoding="utf-8") as fh:
            fh.write('{"type":"module"}')
        with open(os.path.join(tmp, "harness.mjs"), "w", encoding="utf-8") as fh:
            fh.write(HARNESS)
        proc = subprocess.run([node, "harness.mjs"], cwd=tmp, capture_output=True, text=True)
        sys.stdout.write(proc.stdout)
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr)
            print("FAIL: SPARQL page rendering test")
            return 1
    print("PASS: SPARQL page rendering test")
    return 0


if __name__ == "__main__":
    sys.exit(main())
