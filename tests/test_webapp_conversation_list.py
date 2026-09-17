#!/usr/bin/env python3
"""Behavioural tests for the conversation list (webapp/components/conversations.js).

The card polls `/conversations` every four seconds while the user is inside a
thread, so an archive always races an answer that is already on the wire. This
test drives the element under Node with a scripted DOM and a scripted fetch —
the test decides when each request is answered — and pins what the user sees:
an archived thread leaves the list at once and stays gone, even when the poll
that predates the archive answers afterwards. Before the epoch guard that
answer put the row back, and opening it showed an Unarchive button.

Standalone like the rest of the suite. Needs `node` on PATH (GitHub's runners
have it); without it the test reports a skip and passes, so the Python-only
suite is never blocked by it.
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

// ── A scripted DOM, small enough to see through ──────────────────────────────
const registry = new Map();
class FakeShadow {
  constructor() { this.innerHTML = ''; }
  querySelector() { return null; }
  querySelectorAll() { return []; }
}
class FakeElement {
  constructor() { this._attrs = new Map(); this._l = new Map(); this.shadowRoot = null; }
  attachShadow() { this.shadowRoot = new FakeShadow(); return this.shadowRoot; }
  addEventListener(t, f) { if (!this._l.has(t)) this._l.set(t, []); this._l.get(t).push(f); }
  removeEventListener() {}
  dispatchEvent(ev) { for (const f of this._l.get(ev.type) || []) f(ev); return true; }
  getAttribute(n) { return this._attrs.has(n) ? this._attrs.get(n) : null; }
  setAttribute(n, v) { this._attrs.set(n, String(v)); }
  hasAttribute(n) { return this._attrs.has(n); }
  removeAttribute(n) { this._attrs.delete(n); }
  querySelector() { return null; }
  querySelectorAll() { return []; }
}
const winListeners = new Map();
globalThis.HTMLElement = FakeElement;
globalThis.customElements = { define: (n, c) => registry.set(n, c), get: (n) => registry.get(n) };
globalThis.document = {
  addEventListener() {}, removeEventListener() {}, createElement: () => new FakeElement(),
  querySelector() { return null; }, hidden: false, visibilityState: 'visible', body: new FakeElement(),
};
globalThis.window = globalThis;
globalThis.location = { hash: '', pathname: '/', href: 'http://dash/' };
globalThis.addEventListener = (t, f) => { if (!winListeners.has(t)) winListeners.set(t, []); winListeners.get(t).push(f); };
globalThis.removeEventListener = () => {};
const fire = (type) => { for (const f of winListeners.get(type) || []) f({ type }); };
globalThis.history = {
  pushState(_s, _t, url) { location.hash = String(url).startsWith('#') ? String(url) : ''; },
  replaceState(_s, _t, url) { location.hash = String(url).startsWith('#') ? String(url) : ''; },
  // The browser moves the address bar, then tells the page — as the card expects.
  back() { location.hash = ''; fire('popstate'); },
};
globalThis.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} });
globalThis.localStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };
Object.defineProperty(globalThis, 'navigator', { value: { userAgent: 'node', mediaDevices: null }, configurable: true });
globalThis.CustomEvent = class { constructor(t, i = {}) { this.type = t; this.detail = i.detail; this.bubbles = !!i.bubbles; } };

// ── A scripted /conversations ────────────────────────────────────────────────
// Requests queue up unanswered; the test answers them, in whatever order the
// race it is describing would have them answered in.
const wire = [];
globalThis.fetch = (url) => new Promise((resolve) => {
  wire.push({ url, answer: (list) => resolve({ ok: true, json: async () => ({ conversations: list }) }) });
});
const pending = (n) => { assert.equal(wire.length, n, `expected ${n} request(s) on the wire, saw ${wire.length}`); };
const settle = () => new Promise((r) => setTimeout(r, 0));

await import('./conversations.js');
const Card = registry.get('retinue-conversations');
assert.ok(Card, 'the element registered itself');

const A = 'a'.repeat(32);
const B = 'b'.repeat(32);
const thread = (id, extra = {}) => ({ id, title: 'T' + id.slice(0, 1), updated: 1, unread: 0, ...extra });
const ids = (card) => card._threads.map((t) => t.id);

// A mounted card with the list already loaded. Renders are counted, not done:
// the shadow DOM is not what this test is about.
async function mount({ full = false, threads = [thread(A), thread(B)] } = {}) {
  location.hash = '';
  wire.length = 0;
  const card = new Card();
  card.render = function () { this._renders = (this._renders || 0) + 1; };
  if (full) card.setAttribute('full', '');
  card.connectedCallback();
  pending(1);
  wire.shift().answer(threads);
  await settle();
  assert.deepEqual(ids(card), threads.map((t) => t.id), 'the first answer lands');
  return card;
}

let passed = 0;
function ok(name, fn) { return Promise.resolve().then(fn).then(() => { passed++; console.log('  ok  ' + name); }); }

// ── The archive race ─────────────────────────────────────────────────────────
await ok('an archive drops the poll that was already on the wire', async () => {
  const card = await mount();
  card._openThread(A);
  assert.equal(card._active, A);

  card.refresh();                       // the four-second poll, issued pre-archive
  pending(1);
  const stale = wire.shift();

  card.dispatchEvent(new CustomEvent('retinue-archived', { detail: { id: A, archived: true } }));
  assert.deepEqual(ids(card), [B], 'the row is gone before any answer comes back');
  assert.equal(card._active, null, 'and the list is what is shown');

  stale.answer([thread(A, { archived: true }), thread(B)]);   // the list as it was
  await settle();
  assert.deepEqual(ids(card), [B], 'the stale answer does not put the archived thread back');

  pending(1);
  wire.shift().answer([thread(B)]);      // the list as it now is
  await settle();
  assert.deepEqual(ids(card), [B]);
  card.disconnectedCallback();
});

await ok('an unarchive takes the thread out of the archived scope', async () => {
  const card = await mount({ full: true });
  card._setScope('archived');
  pending(1);
  wire.shift().answer([thread(A, { archived: true })]);
  await settle();
  card._openThread(A);

  card.refresh();
  const stale = wire.shift();
  card.dispatchEvent(new CustomEvent('retinue-archived', { detail: { id: A, archived: false } }));
  assert.deepEqual(ids(card), [], 'restored: out of the archived list at once');
  stale.answer([thread(A, { archived: true })]);
  await settle();
  assert.deepEqual(ids(card), [], 'and the answer from before the restore is dropped');
  card.disconnectedCallback();
});

await ok('a scope switch drops the answer meant for the scope left behind', async () => {
  const card = await mount({ full: true });
  card.refresh();
  const stale = wire.shift();
  card._setScope('archived');
  assert.deepEqual(ids(card), [], 'the old scope is cleared on the spot');
  stale.answer([thread(A), thread(B)]);  // active threads, answering into the archived view
  await settle();
  assert.deepEqual(ids(card), [], 'the other scope\'s threads never show');
  pending(1);
  assert.ok(wire[0].url.includes('archived=1'), 'the new scope is what is being fetched');
  wire.shift().answer([thread(A, { archived: true })]);
  await settle();
  assert.deepEqual(ids(card), [A]);
  card.disconnectedCallback();
});

// ── And the ordinary case still works ────────────────────────────────────────
await ok('an uncontested refresh lands as before', async () => {
  const card = await mount();
  card.refresh();
  wire.shift().answer([thread(B), thread(A)]);
  await settle();
  assert.deepEqual(ids(card), [B, A], 'order and content follow the gateway');
  card.refresh();
  wire.shift().answer('not a list');
  await settle();
  assert.deepEqual(ids(card), [], 'a malformed answer empties rather than throws');
  card.disconnectedCallback();
});

await ok('an archive event without an id changes no rows but still shows the list', async () => {
  const card = await mount();
  card._openThread(A);
  card.dispatchEvent(new CustomEvent('retinue-archived', { detail: {} }));
  assert.deepEqual(ids(card), [A, B]);
  assert.equal(card._active, null);
  card.disconnectedCallback();
});

console.log(`${passed} checks passed`);
process.exit(0);
"""


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("SKIP: node is not installed; the conversations.js behaviour test needs it")
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
            print("FAIL: conversations.js behaviour test")
            return 1
    print("PASS: conversations.js behaviour test")
    return 0


if __name__ == "__main__":
    sys.exit(main())
