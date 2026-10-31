#!/usr/bin/env python3
"""Behavioural tests for a thread's attachment links (webapp/components/conversation.js).

Both links of a file row end in the file's own name, a segment the gateway
ignores. The Android shell's WebView hands every file it cannot show to its
download listener, which named the saved file with URLUtil.guessFileName
until it read the header itself — and that takes a name only from a bare
`attachment; filename="…"`, so with the gateway's `inline` or trailing
`filename*` it fell back to the URL's last segment and saved `<att-id>.pdf`.
These checks pin, under Node with a scripted DOM:

- a viewable file (a PDF) gets a view link and a save link, both ending in
  its name; the view link opens the dashboard's file viewer (viewer.html),
  which keeps the navigation row — and so the way back — on screen, except
  inside the Android shell, which keeps the bare `?inline=1` link;
- the viewer accepts only this dashboard's attachment paths and the types the
  gateway serves inline;
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

const unesc = (h) => h.replace(/&amp;/g, '&');
const viewer = (h) => {
  const u = new URL(unesc(h), 'http://dash');
  assert.equal(u.pathname, '/viewer.html', h);
  return Object.fromEntries(u.searchParams);
};

ok('a PDF: the view link opens the viewer, the save link the file; both end in its name', () => {
  const html = row({ filename: 'invoice.pdf', content_type: 'application/pdf' });
  const [view, save] = hrefs(html);
  assert.deepEqual(viewer(view), { src: `${BASE}/invoice.pdf`, name: 'invoice.pdf', type: 'application/pdf' });
  assert.equal(save, `${BASE}/invoice.pdf`);
  assert.deepEqual(downloads(html), ['invoice.pdf']);
  assert.ok(html.includes('<span class="a-name">invoice.pdf</span>'), html);
});

ok('inside the Android shell the view link stays the bare inline file', () => {
  globalThis.RetinueShell = { platform: 'android' };
  try {
    const html = row({ filename: 'invoice.pdf', content_type: 'application/pdf' });
    assert.deepEqual(hrefs(html), [`${BASE}/invoice.pdf?inline=1`, `${BASE}/invoice.pdf`]);
  } finally { delete globalThis.RetinueShell; }
});

ok('a name outside ASCII is percent-encoded into the segment', () => {
  const html = row({ filename: 'Rechnung März #3.pdf', content_type: 'application/pdf; charset=binary' });
  const [view, save] = hrefs(html);
  assert.equal(viewer(view).src, `${BASE}/Rechnung%20M%C3%A4rz%20%233.pdf`);
  assert.equal(viewer(view).name, 'Rechnung März #3.pdf');
  assert.equal(save, `${BASE}/Rechnung%20M%C3%A4rz%20%233.pdf`);
});

ok('a path-step name falls back to "attachment"', () => {
  for (const filename of ['.', '..']) {
    const html = row({ filename, content_type: 'application/pdf' });
    const [view, save] = hrefs(html);
    assert.equal(viewer(view).src, `${BASE}/attachment`, filename);
    assert.equal(save, `${BASE}/attachment`, filename);
  }
  const html = row({ filename: '...', content_type: 'application/pdf' });
  assert.equal(hrefs(html)[1], `${BASE}/...`, 'three dots are a name, not a step');
});

ok('a file the gateway does not serve inline gets the save link alone', () => {
  const html = row({ filename: 'notes.docx', content_type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' });
  assert.deepEqual(hrefs(html), [`${BASE}/notes.docx`]);
  assert.deepEqual(downloads(html), ['notes.docx']);
});

ok("an image's preview loads from the named URL and opens in the viewer", () => {
  const html = row({ filename: 'chart.png', content_type: 'image/png', width: 400, height: 200 });
  assert.ok(html.includes(`src="${BASE}/chart.png?inline=1"`), html);
  const [preview, view, save] = hrefs(html);
  assert.equal(preview, view, 'the preview and the name open the same view');
  assert.deepEqual(viewer(view), { src: `${BASE}/chart.png`, name: 'chart.png', type: 'image/png' });
  assert.equal(save, `${BASE}/chart.png`);
});

const { parseViewerParams } = await import('./viewer-page.js');
const qs = (o) => '?' + new URLSearchParams(o);

ok('the viewer reads what a view link carries', () => {
  assert.deepEqual(parseViewerParams(qs({ src: `${BASE}/invoice.pdf`, name: 'invoice.pdf', type: 'application/pdf' })),
    { src: `${BASE}/invoice.pdf`, name: 'invoice.pdf', kind: 'pdf' });
  assert.equal(parseViewerParams(qs({ src: `${BASE}/a.png`, type: 'image/png' })).kind, 'image');
  assert.equal(parseViewerParams(qs({ src: `${BASE}/a.txt`, type: 'text/plain; charset=utf-8' })).kind, 'text');
  assert.equal(parseViewerParams(qs({ src: `${BASE}/M%C3%A4rz.pdf`, type: 'application/pdf' })).name, 'März.pdf',
    'without a name, the URL segment stands in');
});

ok('the viewer refuses anything but an inline-safe attachment of this dashboard', () => {
  for (const src of ['https://evil.example/x.pdf', '//evil.example/x.pdf', 'javascript:alert(1)',
    `${BASE}/x.pdf?inline=1`, '/conversations/../etc/passwd', `/data/${ID}`, '']) {
    assert.equal(parseViewerParams(qs({ src, type: 'application/pdf' })), null, src);
  }
  for (const type of ['image/svg+xml', 'text/html', '']) {
    assert.equal(parseViewerParams(qs({ src: `${BASE}/x`, type })), null, type);
  }
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
