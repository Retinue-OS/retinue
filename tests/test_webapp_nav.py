#!/usr/bin/env python3
"""Behavioural tests for the navigation row (webapp/components/nav.js) and its
back button on the drill-downs.

The row heads every page, and on a drill-down — a chat, a project, an open
thread — it starts with a back button. The row never decides where back goes:
it dispatches `retinue-back`, and the page listening on the row does the
going (a chat opened cold goes home, a thread on the home closes in place).
These checks pin that contract under Node with a scripted DOM:

- `back` renders the button, and taking it away removes it again (the
  conversations card toggles it as a thread opens and closes);
- a plain press dispatches `retinue-back` and navigates nowhere; a modified
  press (new tab) is left to the link;
- a drill-down's path underlines its list (Chats on a chat);
- a tab press on a page holding unsent input is held, as a Home press of
  the Android shell is;
- the conversations card shows the button only while a thread or the
  composer is open, and a press on the row unwinds exactly one entry — the
  element's own `retinue-back` (its minimize) still reaches the host once,
  not twice.

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

// ── A scripted DOM ───────────────────────────────────────────────────────────
// The shadow root hands out one stub per selector, so the test can press
// what the row wired up; whether a selector exists is read off innerHTML.
const registry = new Map();
class Stub {
  constructor() { this._l = new Map(); }
  addEventListener(t, f) { if (!this._l.has(t)) this._l.set(t, []); this._l.get(t).push(f); }
  fire(t, ev) { for (const f of this._l.get(t) || []) f(ev); }
}
class FakeShadow {
  constructor() { this.innerHTML = ''; this._stubs = new Map(); }
  querySelector(sel) {
    const present = sel === 'nav' ? this.innerHTML.includes('<nav')
      : sel === '[data-back]' ? this.innerHTML.includes('data-back') : false;
    if (!present) return null;
    if (!this._stubs.has(sel)) this._stubs.set(sel, new Stub());
    return this._stubs.get(sel);
  }
  querySelectorAll() { return []; }
}
class FakeElement {
  constructor() { this._attrs = new Map(); this._l = new Map(); this.shadowRoot = null; this.isConnected = true; this.style = {}; this.id = ''; }
  attachShadow() { this.shadowRoot = new FakeShadow(); return this.shadowRoot; }
  addEventListener(t, f) { if (!this._l.has(t)) this._l.set(t, []); this._l.get(t).push(f); }
  removeEventListener(t, f) { this._l.set(t, (this._l.get(t) || []).filter((g) => g !== f)); }
  dispatchEvent(ev) { for (const f of this._l.get(ev.type) || []) f(ev); return true; }
  getAttribute(n) { return this._attrs.has(n) ? this._attrs.get(n) : null; }
  setAttribute(n, v) {
    const before = this.getAttribute(n);
    this._attrs.set(n, String(v));
    if (this.attributeChangedCallback) this.attributeChangedCallback(n, before, String(v));
  }
  hasAttribute(n) { return this._attrs.has(n); }
  removeAttribute(n) {
    const before = this.getAttribute(n);
    this._attrs.delete(n);
    if (this.attributeChangedCallback) this.attributeChangedCallback(n, before, null);
  }
  toggleAttribute(n, on) {
    const want = on === undefined ? !this.hasAttribute(n) : !!on;
    if (want && !this.hasAttribute(n)) this.setAttribute(n, '');
    else if (!want && this.hasAttribute(n)) this.removeAttribute(n);
    return want;
  }
  querySelector() { return null; }
  querySelectorAll() { return []; }
}
const winListeners = new Map();
let navOnPage = null;
let note = null;
globalThis.HTMLElement = FakeElement;
globalThis.customElements = { define: (n, c) => registry.set(n, c), get: (n) => registry.get(n) };
globalThis.document = {
  addEventListener() {}, removeEventListener() {}, createElement: () => new FakeElement(),
  querySelector(sel) { return sel === 'retinue-nav' ? navOnPage : null; },
  getElementById() { return note; },
  hidden: false, visibilityState: 'visible',
  body: { appendChild(el) { note = el; } },
};
globalThis.window = globalThis;
globalThis.location = { hash: '', pathname: '/', search: '', href: 'http://dash/' };
globalThis.addEventListener = (t, f) => { if (!winListeners.has(t)) winListeners.set(t, []); winListeners.get(t).push(f); };
globalThis.removeEventListener = () => {};
const fire = (type) => { for (const f of winListeners.get(type) || []) f({ type }); };
let backs = 0;
// The address moves as a browser's does: no URL (null or '', as the card's
// state-only stamps pass) keeps it, a fragment replaces the hash, and a path
// replaces the address and drops the hash. A stub that cleared the hash on a
// state-only stamp would close a deep link before any press could.
const moveTo = (url) => {
  if (url === null || url === undefined || url === '') return;
  const s = String(url);
  if (s.startsWith('#')) { location.hash = s; return; }
  const u = new URL(s, 'http://dash' + location.pathname);
  location.pathname = u.pathname;
  location.search = u.search;
  location.hash = u.hash;
};
globalThis.history = {
  length: 2,
  pushState(_s, _t, url) { moveTo(url); },
  replaceState(_s, _t, url) { moveTo(url); },
  back() { backs++; location.hash = ''; fire('popstate'); },
};
globalThis.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} });
globalThis.localStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };
globalThis.sessionStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };
Object.defineProperty(globalThis, 'navigator', { value: { userAgent: 'node', mediaDevices: null }, configurable: true });
globalThis.CustomEvent = class { constructor(t, i = {}) { this.type = t; this.detail = i.detail; this.bubbles = !!i.bubbles; this.composed = !!i.composed; } };
globalThis.fetch = () => new Promise(() => {});   // the card's poll never answers here
globalThis.setInterval = () => 0;
globalThis.clearInterval = () => {};

const { registerUnsavedCheck } = await import('./shell.js');
await import('./nav.js');
await import('./conversations.js');
const Nav = registry.get('retinue-nav');
const Card = registry.get('retinue-conversations');
assert.ok(Nav && Card, 'both elements registered themselves');

const press = (extra = {}) => {
  const ev = { button: 0, metaKey: false, ctrlKey: false, shiftKey: false, altKey: false,
    prevented: false, preventDefault() { this.prevented = true; }, ...extra };
  return ev;
};
const mountNav = (attrs = {}) => {
  const nav = new Nav();
  for (const [k, v] of Object.entries(attrs)) nav._attrs.set(k, v);
  nav.connectedCallback();
  return nav;
};
const html = (nav) => nav.shadowRoot.innerHTML;

let passed = 0;
async function ok(name, fn) { await fn(); passed++; console.log('  ok  ' + name); }

await ok('a top-level page has no back button', () => {
  const nav = mountNav({ current: 'chats' });
  assert.ok(!html(nav).includes('data-back'));
  assert.match(html(nav), /href="\/chats\.html" aria-current="page"/);
});

await ok('`back` renders the button first in the row, and it comes and goes', () => {
  const nav = mountNav({ current: 'home' });
  assert.ok(!html(nav).includes('data-back'));
  nav.toggleAttribute('back', true);
  assert.ok(html(nav).includes('data-back'), 'shown once set after upgrade');
  assert.ok(html(nav).indexOf('data-back') < html(nav).indexOf('class="tabs"'), 'before the tabs');
  nav.toggleAttribute('back', false);
  assert.ok(!html(nav).includes('data-back'), 'gone once taken away');
});

await ok('a plain press dispatches retinue-back and navigates nowhere', () => {
  const nav = mountNav({ back: '', current: 'chats' });
  let got = null;
  nav.addEventListener('retinue-back', (e) => { got = e; });
  const before = location.href;
  const ev = press();
  nav.shadowRoot.querySelector('[data-back]').fire('click', ev);
  assert.ok(got, 'the page hears it');
  assert.ok(got.bubbles && got.composed, 'bubbling and composed');
  assert.ok(ev.prevented, 'the link does not follow');
  assert.equal(location.href, before);
});

await ok('a modified press is left to the link', () => {
  const nav = mountNav({ back: '' });
  let got = 0;
  nav.addEventListener('retinue-back', () => { got++; });
  for (const mod of [{ metaKey: true }, { ctrlKey: true }, { button: 1 }]) {
    const ev = press(mod);
    nav.shadowRoot.querySelector('[data-back]').fire('click', ev);
    assert.ok(!ev.prevented);
  }
  assert.equal(got, 0);
});

await ok("a drill-down's path underlines its list", () => {
  location.pathname = '/chat.html';
  assert.match(html(mountNav({ back: '' })), /href="\/chats\.html" aria-current="page"/);
  location.pathname = '/project.html';
  assert.match(html(mountNav({ back: '' })), /href="\/projects\.html" aria-current="page"/);
  location.pathname = '/';
});

await ok('a tab press with unsent input on the page is held, without it it goes', () => {
  const nav = mountNav({ back: '', current: 'chats' });
  const row = nav.shadowRoot.querySelector('nav');
  const tab = { closest: () => ({ hasAttribute: () => false }) };
  const free = press({ target: tab });
  row.fire('click', free);
  assert.ok(!free.prevented, 'nothing held: the link follows');
  const off = registerUnsavedCheck(() => true);
  const held = press({ target: tab });
  row.fire('click', held);
  assert.ok(held.prevented, 'unsent input: the tab stays put');
  assert.ok(note && /Unsent input/.test(note.textContent), 'and says why');
  const back = press({ target: { closest: () => ({ hasAttribute: (n) => n === 'data-back' }) } });
  row.fire('click', back);
  assert.ok(!back.prevented, 'the back button is the page\'s to decide, not the guard\'s');
  off();
});

await ok('the conversations card shows back only while a thread or the composer is open', () => {
  navOnPage = new FakeElement();
  location.hash = '';
  const card = new Card();
  card.render = function () { this._reflectView(this._active ? 'thread' : (this._composing ? 'composer' : 'list')); };
  card.connectedCallback();
  assert.ok(!navOnPage.hasAttribute('back'), 'the list: no back');
  card._showThread('a'.repeat(32));
  assert.ok(navOnPage.hasAttribute('back'), 'a thread: back');
  card._showList();
  assert.ok(!navOnPage.hasAttribute('back'), 'the list again: no back');
  card._showComposer();
  assert.ok(navOnPage.hasAttribute('back'), 'the composer: back');
  card.disconnectedCallback();
  navOnPage = null;
});

await ok("a press on the row unwinds one entry; the element's own back still reaches the host once", () => {
  navOnPage = new FakeElement();
  location.hash = '';
  const card = new Card();
  card.render = function () { this._reflectView(this._active ? 'thread' : (this._composing ? 'composer' : 'list')); };
  card.connectedCallback();
  card._openThread('a'.repeat(32));
  assert.ok(card._pushDepth > 0, 'opening pushed an entry');
  backs = 0;
  navOnPage.dispatchEvent(new CustomEvent('retinue-back', { bubbles: true, composed: true }));
  assert.equal(backs, 1, 'the row: one step back');
  card._openThread('b'.repeat(32));
  backs = 0;
  card.dispatchEvent(new CustomEvent('retinue-back', { bubbles: true, composed: true }));
  assert.equal(backs, 1, "the element's minimize: one step back, not two");
  card.disconnectedCallback();
  navOnPage = null;
});

await ok('a deep-linked thread closes in place, without leaving the page', () => {
  navOnPage = new FakeElement();
  location.hash = '#conversation-' + 'c'.repeat(32);
  const card = new Card();
  card.render = function () { this._reflectView(this._active ? 'thread' : (this._composing ? 'composer' : 'list')); };
  card.connectedCallback();
  assert.ok(navOnPage.hasAttribute('back'), 'opened on the thread: back');
  assert.equal(location.hash, '#conversation-' + 'c'.repeat(32), 'the deep link stays until the press');
  backs = 0;
  navOnPage.dispatchEvent(new CustomEvent('retinue-back', {}));
  assert.equal(backs, 0, 'no history step out of the app');
  assert.equal(location.hash, '', 'the hash is dropped in place');
  assert.ok(!navOnPage.hasAttribute('back'), 'the list: no back');
  card.disconnectedCallback();
  navOnPage = null;
});

console.log(`${passed} checks passed`);
process.exit(0);
"""


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("SKIP: node is not installed; the nav.js behaviour test needs it")
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
            print("FAIL: nav.js behaviour test")
            return 1
    print("PASS: nav.js behaviour test")
    return 0


if __name__ == "__main__":
    sys.exit(main())
