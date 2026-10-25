#!/usr/bin/env python3
"""Where a rendered link opens (webapp/components/markdown.js), under Node.

The dashboard is installed as a standalone PWA. A target="_blank" link is
handed to a browsing context outside the app window, so a link Ara gives to
another thread used to open the browser instead of the app. This pins the
rule: a link to the dashboard's own origin navigates in place (no target),
every other web link opens in a new tab, mailto:/tel: never carry a target,
and without a `location` (rendering outside a page) every web link counts as
external. An in-place link may unload the page, which costs nothing unsent —
it waits in its field (tests/test_webapp_unsent.py) — so, like a tab of the
navigation row, no press on one is ever held: the renderer puts no click
handler on the document.

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

HARNESS = r"""
import assert from 'node:assert/strict';

// base.js (for esc) defines a custom-element base class on import.
globalThis.HTMLElement = class {};
// Anything the renderer would listen for on the document.
const listeners = [];
globalThis.document = {
  addEventListener: (t) => { listeners.push(t); },
  getElementById: () => null,
};

const { renderInline } = await import('./markdown.js');

let passed = 0;
const ok = (name, fn) => { fn(); passed += 1; console.log(`ok - ${name}`); };
const NEW_TAB = ' target="_blank" rel="noopener noreferrer"';

ok('without a page every web link opens in a new tab', () => {
  assert.equal(renderInline('[t](https://ara.example.com/#conversation-1)'),
    `<a href="https://ara.example.com/#conversation-1"${NEW_TAB}>t</a>`);
});

globalThis.location = new URL('https://ara.example.com/#conversation-aaaa');

ok('a link to another thread opens in place', () => {
  assert.equal(renderInline('[the other thread](https://ara.example.com/#conversation-bbbb)'),
    '<a href="https://ara.example.com/#conversation-bbbb">the other thread</a>');
});

ok('any dashboard page opens in place, bare URLs included', () => {
  assert.equal(renderInline('[p](https://ara.example.com/sends/signal/42)'),
    '<a href="https://ara.example.com/sends/signal/42">p</a>');
  assert.equal(renderInline('[p](https://ara.example.com/project.html?id=x&y=1)'),
    '<a href="https://ara.example.com/project.html?id=x&amp;y=1">p</a>');
  assert.equal(renderInline('see https://ara.example.com/gateways.'),
    'see <a href="https://ara.example.com/gateways">https://ara.example.com/gateways</a>.');
});

ok('another origin opens in a new tab', () => {
  for (const u of ['https://github.com/retinue-os/retinue/pull/1',
                   'http://ara.example.com/#conversation-bbbb',
                   'https://ara.example.com:8443/',
                   'https://evil.ara.example.com/']) {
    assert.equal(renderInline(`[x](${u})`), `<a href="${u}"${NEW_TAB}>x</a>`, u);
  }
  assert.equal(renderInline('www.example.org'),
    `<a href="https://www.example.org"${NEW_TAB}>www.example.org</a>`);
});

ok('mailto and tel never carry a target', () => {
  assert.equal(renderInline('[m](mailto:a@example.com)'), '<a href="mailto:a@example.com">m</a>');
  assert.equal(renderInline('[t](tel:+41441234567)'), '<a href="tel:+41441234567">t</a>');
});

ok('no press on a link is held: nothing listens on the document', () => {
  assert.deepEqual(listeners, []);
});

console.log(`${passed} checks passed`);
"""


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("SKIP: node is not installed; the Markdown link test needs it")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        for name in ("base.js", "markdown.js"):
            shutil.copy(os.path.join(COMPONENTS, name), os.path.join(tmp, name))
        with open(os.path.join(tmp, "package.json"), "w", encoding="utf-8") as fh:
            fh.write('{"type":"module"}')
        with open(os.path.join(tmp, "harness.mjs"), "w", encoding="utf-8") as fh:
            fh.write(HARNESS)
        proc = subprocess.run([node, "harness.mjs"], cwd=tmp, capture_output=True,
                              text=True, timeout=120)
        sys.stdout.write(proc.stdout)
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr)
            print("FAIL: Markdown link test")
            return 1
    print("PASS: Markdown link test")
    return 0


if __name__ == "__main__":
    sys.exit(main())
