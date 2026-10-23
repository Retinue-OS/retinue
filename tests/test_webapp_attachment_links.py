#!/usr/bin/env python3
"""Behavioural tests for a thread's attachment links (webapp/components/conversation.js).

Both links of a file row end in the file's own name, a segment the gateway
ignores. The Android shell's WebView hands every file it cannot show to its
download listener, which named the saved file with URLUtil.guessFileName
until it read the header itself — and that takes a name only from a bare
`attachment; filename="…"`, so with the gateway's `inline` or trailing
`filename*` it fell back to the URL's last segment and saved `<att-id>.pdf`.
These checks pin, under Node with a scripted DOM:

- a viewable file (a PDF) gets a view link (`?inline=1`) and a save link,
  both ending in its name;
- a name outside ASCII is percent-encoded into that segment;
- a name that is a path step (`.`, `..`) falls back to `attachment`;
- a file the gateway does not serve inline gets the save link alone, with
  the name too;
- an image's preview loads from the same named URL.

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

// ── A scripted DOM: just enough for the element module to load ───────────────
const registry = new Map();
class FakeElement {
  constructor() { this._attrs = new Map(); this.shadowRoot = null; this.isConnected = true; this.style = {}; }
  attachShadow() { this.shadowRoot = { innerHTML: '', querySelector() { return null; }, querySelectorAll() { return []; } }; return this.shadowRoot; }
  addEventListener() {}
  removeEventListener() {}
  dispatchEvent() { return true; }
  getAttribute(n) { return this._attrs.has(n) ? this._attrs.get(n) : null; }
  setAttribute(n, v) { this._attrs.set(n, String(v)); }
  hasAttribute(n) { return this._attrs.has(n); }
  removeAttribute(n) { this._attrs.delete(n); }
  querySelector() { return null; }
  querySelectorAll() { return []; }
}
globalThis.HTMLElement = FakeElement;
globalThis.customElements = { define: (n, c) => registry.set(n, c), get: (n) => registry.get(n) };
globalThis.document = {
  addEventListener() {}, removeEventListener() {}, createElement: () => new FakeElement(),
  querySelector() { return null; }, getElementById() { return null; },
  hidden: false, visibilityState: 'visible', body: { appendChild() {} },
  documentElement: { dataset: {} },
};
globalThis.window = globalThis;
globalThis.innerHeight = 800;
globalThis.location = { hash: '', pathname: '/', search: '', href: 'http://dash/' };
globalThis.addEventListener = () => {};
globalThis.removeEventListener = () => {};
globalThis.history = { length: 1, pushState() {}, replaceState() {}, back() {} };
globalThis.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} });
globalThis.localStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };
globalThis.sessionStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };
Object.defineProperty(globalThis, 'navigator', { value: { userAgent: 'node', mediaDevices: null }, configurable: true });
globalThis.CustomEvent = class { constructor(t, i = {}) { this.type = t; this.detail = i.detail; } };
globalThis.fetch = () => new Promise(() => {});

await import('./conversation.js');
const Conv = registry.get('retinue-conversation');
assert.ok(Conv, 'the element registered itself');
const el = new Conv();

const CID = 'c'.repeat(32), ID = 'a'.repeat(32);
const BASE = `/conversations/${CID}/attachments/${ID}`;
const row = (att) => el._attachmentsHtml(CID, [{ id: ID, size: 2048, ...att }]);
const hrefs = (html) => [...html.matchAll(/href="([^"]*)"/g)].map((m) => m[1]);
const downloads = (html) => [...html.matchAll(/download="([^"]*)"/g)].map((m) => m[1]);

let passed = 0;
function ok(name, fn) { fn(); passed++; console.log('  ok  ' + name); }

ok('a PDF: view and save links both end in its name', () => {
  const html = row({ filename: 'invoice.pdf', content_type: 'application/pdf' });
  assert.deepEqual(hrefs(html), [`${BASE}/invoice.pdf?inline=1`, `${BASE}/invoice.pdf`]);
  assert.deepEqual(downloads(html), ['invoice.pdf']);
  assert.ok(html.includes('<span class="a-name">invoice.pdf</span>'), html);
});

ok('a name outside ASCII is percent-encoded into the segment', () => {
  const html = row({ filename: 'Rechnung März #3.pdf', content_type: 'application/pdf; charset=binary' });
  assert.deepEqual(hrefs(html), [
    `${BASE}/Rechnung%20M%C3%A4rz%20%233.pdf?inline=1`, `${BASE}/Rechnung%20M%C3%A4rz%20%233.pdf`,
  ]);
});

ok('a path-step name falls back to "attachment"', () => {
  for (const filename of ['.', '..']) {
    const html = row({ filename, content_type: 'application/pdf' });
    assert.deepEqual(hrefs(html), [`${BASE}/attachment?inline=1`, `${BASE}/attachment`], filename);
  }
  const html = row({ filename: '...', content_type: 'application/pdf' });
  assert.equal(hrefs(html)[1], `${BASE}/...`, 'three dots are a name, not a step');
});

ok('a file the gateway does not serve inline gets the save link alone', () => {
  const html = row({ filename: 'notes.docx', content_type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' });
  assert.deepEqual(hrefs(html), [`${BASE}/notes.docx`]);
  assert.deepEqual(downloads(html), ['notes.docx']);
});

ok("an image's preview loads from the named URL", () => {
  const html = row({ filename: 'chart.png', content_type: 'image/png', width: 400, height: 200 });
  assert.ok(html.includes(`src="${BASE}/chart.png?inline=1"`), html);
  assert.deepEqual(hrefs(html), [`${BASE}/chart.png?inline=1`, `${BASE}/chart.png?inline=1`, `${BASE}/chart.png`]);
});

console.log(`${passed} attachment link checks passed`);
process.exit(0);
"""


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("SKIP: node is not installed; the attachment link test needs it")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        # The components are ESM and import each other by relative path; Node
        # treats a bare .js as CommonJS unless the directory says otherwise.
        for name in os.listdir(COMPONENTS):
            if name.endswith(".js"):
                shutil.copy(os.path.join(COMPONENTS, name), os.path.join(tmp, name))
        with open(os.path.join(tmp, "package.json"), "w", encoding="utf-8") as fh:
            fh.write('{"type":"module"}')
        with open(os.path.join(tmp, "harness.mjs"), "w", encoding="utf-8") as fh:
            fh.write(HARNESS)
        proc = subprocess.run([node, "harness.mjs"], cwd=tmp, capture_output=True, text=True)
        sys.stdout.write(proc.stdout)
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr)
            print("FAIL: attachment link test")
            return 1
    print("PASS: attachment link test")
    return 0


if __name__ == "__main__":
    sys.exit(main())
