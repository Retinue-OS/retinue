#!/usr/bin/env python3
"""Behavioural tests for the Chats page list (webapp/components/chats.js):
Unread · Read · Archived, and what a swipe does on each tab.

The element is driven under Node with a scripted DOM and a scripted fetch,
and pins:

  * the three-way split follows each chat's `list_group` from GET /chats,
    starred first and newest first inside a tab, with the unread badge kept
    on the row and the number of unread chats on the Unread tab — the
    payload's `counts` while the list is as fetched, the rows themselves
    once a write from here moved one;
  * a payload without `list_group` is grouped by the same rule (archived →
    archived, unread > 0 → unread, else read);
  * the tabs are a tablist, the tab choice holds across a refresh, and each
    tab has its own empty line;
  * the swipe each row is wired for: on Unread right = mark read and left =
    archive, on Read nothing to the right and left = archive, on Archived
    right = restore and left = the tray (no committing left swipe), with the
    tray carrying what a swipe does for a keyboard;
  * a committed mark read posts `{}` to /chats/<id>/read, moves the row to
    Read and offers Undo, which posts `{restore, marked}` from the answer and
    puts the row back; a 409 on Undo refetches the list;
  * a committed archive posts the flag and moves the row to Archived;
  * a refresh that left before a write is dropped when it answers.

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
FIXTURE = os.path.join(HERE, "..", "webapp", "data", "chats.json")

HARNESS = r"""
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

// ── A scripted DOM, small enough to see through ──────────────────────────────
const registry = new Map();
class FakeShadow {
  constructor() { this.innerHTML = ''; }
  querySelector() { return null; }
  querySelectorAll() { return []; }
}
class FakeElement {
  constructor() { this._attrs = new Map(); this.shadowRoot = null; }
  attachShadow() { this.shadowRoot = new FakeShadow(); return this.shadowRoot; }
  addEventListener() {}
  removeEventListener() {}
  dispatchEvent() { return true; }
  getAttribute(n) { return this._attrs.has(n) ? this._attrs.get(n) : null; }
  setAttribute(n, v) { this._attrs.set(n, String(v)); }
  hasAttribute(n) { return this._attrs.has(n); }
  removeAttribute(n) { this._attrs.delete(n); }
  get isConnected() { return true; }
}
// The Undo toast (swipe-rows.js offerUndo) lives on document.body in a shadow
// root of its own; this one keeps its text and its button's click.
let toast = null;
class ToastHost extends FakeElement {
  attachShadow() {
    const self = this;
    this.shadowRoot = {
      set innerHTML(h) { this._h = h; if (!h) toast = null; },
      get innerHTML() { return this._h || ''; },
      querySelector() {
        return { addEventListener: (_t, f) => { toast = { text: self.shadowRoot._h, undo: f }; } };
      },
    };
    return this.shadowRoot;
  }
}
globalThis.HTMLElement = FakeElement;
globalThis.customElements = { define: (n, c) => registry.set(n, c), get: (n) => registry.get(n) };
globalThis.document = {
  addEventListener() {}, removeEventListener() {}, createElement: () => new ToastHost(),
  querySelector() { return null; }, hidden: false, visibilityState: 'visible',
  body: { appendChild() {} },
};
globalThis.window = globalThis;
globalThis.location = { hash: '', pathname: '/chats.html', href: 'http://dash/chats.html' };
globalThis.addEventListener = () => {};
globalThis.removeEventListener = () => {};
globalThis.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} });
globalThis.localStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };
Object.defineProperty(globalThis, 'navigator', { value: { userAgent: 'node' }, configurable: true });
globalThis.CustomEvent = class { constructor(t, i = {}) { this.type = t; this.detail = i.detail; } };

// ── A scripted gateway ───────────────────────────────────────────────────────
// Requests queue up unanswered; the test answers each one.
const wire = [];
globalThis.fetch = (url, init = {}) => new Promise((resolve) => {
  wire.push({
    url: String(url), method: init.method || 'GET', body: init.body ? JSON.parse(init.body) : null,
    answer: (doc, status = 200) => resolve({ ok: status < 400, status, json: async () => doc }),
  });
});
const settle = () => new Promise((r) => setTimeout(r, 0));
const take = (pred, what) => {
  const i = wire.findIndex(pred);
  assert.ok(i >= 0, `expected ${what} on the wire, saw ${JSON.stringify(wire.map((w) => [w.method, w.url]))}`);
  return wire.splice(i, 1)[0];
};
const takeList = () => take((w) => w.method === 'GET' && w.url === '/chats', 'GET /chats');

await import('./chats.js');
const List = registry.get('retinue-chats');
assert.ok(List, 'the element registered itself');
const { listGroup, deriveListGroup } = await import('./chats.js');

const chat = (id, extra = {}) => ({
  id, channel: 'signal', account: '+41000', key: id, name: 'N' + id, group: false,
  unread: 0, archived: false, muted: false, starred: false,
  last: { ts: '2026-11-20T10:00:00Z', direction: 'in', kind: 'text', text: 'hi' },
  draft: null, companion: null, messages: `/chats/${id}/messages`, ...extra,
});
// A fresh copy each time: the element writes into the rows it was given.
const payload = (chats, counts) => JSON.parse(JSON.stringify(
  { generated: '2026-11-20T10:00:00Z', ...(counts ? { counts } : {}), chats }));
const rowIds = (html) => [...html.matchAll(/<li class="swipe" data-id="([^"]*)"/g)].map((m) => m[1]);
const rowAttrs = (html, id) => {
  const m = new RegExp(`<li class="swipe" data-id="${id}"([^>]*)>`).exec(html);
  assert.ok(m, `row ${id} rendered`);
  return { quick: (/data-quick="([^"]*)"/.exec(m[1]) || [])[1] || '',
           left: (/data-quick-left="([^"]*)"/.exec(m[1]) || [])[1] || '' };
};
const trayOf = (html, id) => {
  const start = html.indexOf(`<li class="swipe" data-id="${id}"`);
  const end = html.indexOf('</li>', start);
  const li = html.slice(start, end);
  const tray = li.slice(li.indexOf('<div class="tray">'), li.indexOf('<a class="row'));
  return [...tray.matchAll(/data-set="([^"]*)"/g)].map((m) => m[1]);
};

async function mount(doc) {
  wire.length = 0;
  const el = new List();
  el.setAttribute('full', '');
  el.setAttribute('heading', 'Chats');
  el.connectedCallback();
  clearInterval(el._timer);  // the test drives every refresh itself
  takeList().answer(doc);
  await settle();
  return el;
}
const html = (el) => el.shadowRoot.innerHTML;
const tab = (el, scope) => el._setScope(scope);

let passed = 0;
async function ok(name, fn) { await fn(); passed += 1; console.log('  ok  ' + name); }

const LIST = [
  chat('u1', { unread: 3, list_group: 'unread', last: { ts: '2026-11-20T09:00:00Z', direction: 'in', text: 'a' } }),
  chat('u2', { unread: 1, list_group: 'unread', starred: true, last: { ts: '2026-11-19T09:00:00Z', direction: 'in', text: 'b' } }),
  chat('u3', { unread: 2, list_group: 'unread', last: { ts: '2026-11-21T09:00:00Z', direction: 'in', text: 'c' } }),
  chat('r1', { unread: 0, list_group: 'read' }),
  chat('a1', { unread: 4, list_group: 'archived', archived: true, muted: true }),
];
const COUNTS = { unread: 3, read: 1, archived: 1 };

await ok('the list splits three ways by list_group, starred first then newest first', async () => {
  const el = await mount(payload(LIST, COUNTS));
  assert.equal(el._scope, 'unread', 'a fresh page opens on Unread');
  assert.deepEqual(rowIds(html(el)), ['u2', 'u3', 'u1']);
  tab(el, 'read');
  assert.deepEqual(rowIds(html(el)), ['r1']);
  tab(el, 'archived');
  assert.deepEqual(rowIds(html(el)), ['a1']);
});

await ok('the unread badge stays on the row, in words as well as the pill', async () => {
  const el = await mount(payload(LIST, COUNTS));
  const h = html(el);
  assert.ok(h.includes('title="3 unread">3<span class="vh"> unread</span></span>'), h);
  tab(el, 'archived');
  assert.ok(html(el).includes('unread muted-chat'), 'a muted archived chat keeps a quiet badge');
});

await ok('the tabs are a tablist; the Unread tab carries the number of unread chats', async () => {
  const el = await mount(payload(LIST, { unread: 7, read: 1, archived: 1 }));
  const h = html(el);
  assert.ok(h.includes('role="tablist"'), h);
  const tabs = [...h.matchAll(/<button type="button" role="tab"[^>]*data-scope="([a-z]+)">([^<]*)(?:<span class="tab-count">(\d+)<\/span>)?/g)]
    .map((m) => [m[1], m[2].trim(), m[3] || '']);
  assert.deepEqual(tabs, [['unread', 'Unread', '7'], ['read', 'Read', ''], ['archived', 'Archived', '']],
    'the payload\'s counts while the list is as fetched');
  assert.ok(/aria-selected="true"[^>]*tabindex="0" data-scope="unread"/.test(h), 'the selected tab is the tab stop');
  assert.ok(/aria-selected="false"[^>]*tabindex="-1" data-scope="read"/.test(h));
  assert.ok(h.includes('role="tabpanel" id="chats-panel" aria-labelledby="chats-tab-unread"'), h);
});

await ok('without counts the tabs count the rows; no unread chat, no number', async () => {
  const el = await mount(payload(LIST));
  assert.ok(html(el).includes('<span class="tab-count">3</span>'));
  const none = await mount(payload([chat('r1'), chat('r2')]));
  assert.ok(!html(none).includes('<span class="tab-count">'), html(none));
});

await ok('a payload without list_group is grouped by archived and the unread count', async () => {
  assert.equal(deriveListGroup({ archived: true, unread: 5 }), 'archived');
  assert.equal(deriveListGroup({ unread: 2 }), 'unread');
  assert.equal(deriveListGroup({ unread: 0 }), 'read');
  assert.equal(deriveListGroup({}), 'read');
  assert.equal(listGroup({ list_group: 'read', unread: 3 }), 'read', 'the server\'s word wins');
  assert.equal(listGroup({ list_group: 'bogus', unread: 3 }), 'unread', 'an unknown group is derived');
  const bare = LIST.map(({ list_group, ...c }) => c);
  const el = await mount(payload(bare));
  assert.deepEqual(rowIds(html(el)), ['u2', 'u3', 'u1']);
  tab(el, 'read');
  assert.deepEqual(rowIds(html(el)), ['r1']);
  tab(el, 'archived');
  assert.deepEqual(rowIds(html(el)), ['a1']);
});

await ok('the reference document follows the contract', async () => {
  const doc = JSON.parse(readFileSync(process.env.CHATS_FIXTURE, 'utf-8'));
  const n = { unread: 0, read: 0, archived: 0 };
  for (const c of doc.chats) {
    assert.ok(['unread', 'read', 'archived'].includes(c.list_group), c.id);
    assert.equal(c.list_group, deriveListGroup(c), c.id);
    n[c.list_group] += 1;
  }
  assert.deepEqual(doc.counts, n);
});

await ok('each tab has its own empty line; an empty list says one thing', async () => {
  const el = await mount(payload([chat('r1')]));
  assert.ok(html(el).includes('<p class="muted">Nothing unread.</p>'), html(el));
  tab(el, 'archived');
  assert.ok(html(el).includes('<p class="muted">No archived chats.</p>'));
  const one = await mount(payload([chat('u1', { unread: 1 })]));
  tab(one, 'read');
  assert.ok(html(one).includes('<p class="muted">No read chats.</p>'));
  const none = await mount(payload([]));
  assert.ok(html(none).includes('<p class="muted">No chats yet.</p>'));
});

await ok('the tab choice holds across a refresh', async () => {
  const el = await mount(payload(LIST, COUNTS));
  tab(el, 'read');
  el.load();
  takeList().answer(payload([...LIST, chat('r2', { list_group: 'read', last: { ts: '2026-11-22T00:00:00Z', direction: 'out', text: 'x' } })]));
  await settle();
  assert.equal(el._scope, 'read');
  assert.deepEqual(rowIds(html(el)), ['r2', 'r1']);
});

await ok('swipes per tab: Unread right=read left=archive, Read left=archive only, Archived right=restore', async () => {
  const el = await mount(payload(LIST, COUNTS));
  assert.deepEqual(rowAttrs(html(el), 'u1'), { quick: 'read', left: 'archive' });
  assert.ok(html(el).includes('<div class="quick read" aria-hidden="true">Mark read</div>'));
  assert.ok(html(el).includes('<div class="quick-left archive" aria-hidden="true">Archive</div>'));
  assert.deepEqual(trayOf(html(el), 'u1'), ['star', 'read', 'archive', 'mute']);
  tab(el, 'read');
  assert.deepEqual(rowAttrs(html(el), 'r1'), { quick: '', left: 'archive' });
  assert.ok(!html(el).includes('<div class="quick '), 'no right-hand-swipe layer where nothing commits');
  assert.deepEqual(trayOf(html(el), 'r1'), ['star', 'archive', 'mute']);
  tab(el, 'archived');
  assert.deepEqual(rowAttrs(html(el), 'a1'), { quick: 'restore', left: '' });
  assert.ok(!html(el).includes('<div class="quick-left'), 'the left swipe opens the tray here');
  assert.deepEqual(trayOf(html(el), 'a1'), ['star', 'restore', 'unmute', 'delete']);
});

await ok('swipe right on Unread marks read: POST {} , the row moves to Read, Undo restores', async () => {
  const el = await mount(payload(LIST, COUNTS));
  el._swipe._onQuick('u1', 'read', null);
  const post = take((w) => w.method === 'POST', 'the read');
  assert.equal(post.url, '/chats/u1/read');
  assert.deepEqual(post.body, {}, 'the client names no instant');
  post.answer({ id: 'u1', last_read: '2026-11-20T09:00:00Z', previous: '2026-11-18T00:00:00Z', unread: 0, list_group: 'read' });
  await settle();
  assert.deepEqual(rowIds(html(el)), ['u2', 'u3'], 'gone from Unread at once');
  assert.ok(html(el).includes('<span class="tab-count">2</span>'), 'and the tab counts it');
  tab(el, 'read');
  assert.deepEqual(rowIds(html(el)), ['r1', 'u1'], 'newest first there too');
  assert.ok(toast && toast.text.includes('Marked read · Nu1'), 'Undo is offered');
  tab(el, 'unread');
  toast.undo();
  const undo = take((w) => w.method === 'POST', 'the undo');
  assert.equal(undo.url, '/chats/u1/read');
  assert.deepEqual(undo.body, { restore: '2026-11-18T00:00:00Z', marked: '2026-11-20T09:00:00Z' });
  undo.answer({ id: 'u1', last_read: '2026-11-18T00:00:00Z', unread: 3, list_group: 'unread' });
  await settle();
  assert.deepEqual(rowIds(html(el)), ['u2', 'u3', 'u1'], 'back in Unread');
  assert.ok(html(el).includes('title="3 unread"'), 'with its count');
});

await ok('a read never marked before undoes to no mark at all', async () => {
  const el = await mount(payload(LIST, COUNTS));
  el._markRead('u3');
  take((w) => w.method === 'POST', 'the read')
    .answer({ id: 'u3', last_read: '2026-11-21T09:00:00Z', previous: null, unread: 0, list_group: 'read' });
  await settle();
  toast.undo();
  const undo = take((w) => w.method === 'POST', 'the undo');
  assert.deepEqual(undo.body, { restore: null, marked: '2026-11-21T09:00:00Z' });
  undo.answer({ id: 'u3', last_read: null });  // no count in the answer: the row's own comes back
  await settle();
  assert.ok(rowIds(html(el)).includes('u3'));
  assert.ok(html(el).includes('title="2 unread"'));
});

await ok('an Undo the server refuses (409) refetches the list', async () => {
  const el = await mount(payload(LIST, COUNTS));
  el._markRead('u1');
  take((w) => w.method === 'POST', 'the read')
    .answer({ id: 'u1', last_read: 'L', previous: 'P', unread: 0, list_group: 'read' });
  await settle();
  toast.undo();
  take((w) => w.method === 'POST', 'the undo').answer({ error: 'moved' }, 409);
  await settle();
  takeList().answer(payload(LIST.map((c) => (c.id === 'u1' ? { ...c, unread: 0, list_group: 'read' } : c))));
  await settle();
  assert.deepEqual(rowIds(html(el)), ['u2', 'u3'], 'the list as the server has it');
});

await ok('a server without `previous` reads, and offers no Undo', async () => {
  const el = await mount(payload(LIST, COUNTS));
  toast = null;
  el._markRead('u2');
  take((w) => w.method === 'POST', 'the read').answer({ id: 'u2', last_read: 'L' });
  await settle();
  assert.deepEqual(rowIds(html(el)), ['u3', 'u1'], 'the group follows the count it set');
  assert.equal(toast, null);
});

await ok('a failed read leaves the row in Unread', async () => {
  const el = await mount(payload(LIST, COUNTS));
  el._markRead('u1');
  take((w) => w.method === 'POST', 'the read').answer({}, 502);
  await settle();
  assert.deepEqual(rowIds(html(el)), ['u2', 'u3', 'u1']);
});

await ok('swipe left archives: the flag is posted and the row moves to Archived', async () => {
  const el = await mount(payload(LIST, COUNTS));
  tab(el, 'read');
  el._swipe._onQuick('r1', 'archive', null);
  const post = take((w) => w.method === 'POST', 'the flags');
  assert.equal(post.url, '/chats/r1/flags');
  assert.deepEqual(post.body, { archived: true });
  post.answer({ archived: true, muted: false, starred: false });
  await settle();
  assert.deepEqual(rowIds(html(el)), [], 'gone from Read');
  assert.ok(html(el).includes('No read chats.'));
  assert.ok(toast && toast.text.includes('Archived · Nr1'));
  tab(el, 'archived');
  assert.deepEqual(rowIds(html(el)), ['r1', 'a1']);
  // Restore brings an unread chat back to Unread, a read one to Read.
  el._swipe._onQuick('a1', 'restore', null);
  take((w) => w.method === 'POST', 'the restore').answer({ archived: false, muted: false, starred: false });
  await settle();
  tab(el, 'unread');
  assert.ok(rowIds(html(el)).includes('a1'), 'unread messages: back under Unread');
});

await ok('a refresh that left before a write is dropped when it answers', async () => {
  const el = await mount(payload(LIST, COUNTS));
  el.load();
  const stale = takeList();
  el._markRead('u1');
  take((w) => w.method === 'POST', 'the read').answer({ id: 'u1', last_read: 'L', previous: 'P', unread: 0, list_group: 'read' });
  await settle();
  stale.answer(payload(LIST, COUNTS));
  await settle();
  assert.deepEqual(rowIds(html(el)), ['u2', 'u3'], 'the stale list does not put the row back');
});

console.log(`${passed} checks passed`);
process.exit(0);
"""


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("SKIP: node is not installed; the chat list test needs it")
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
        env = dict(os.environ, CHATS_FIXTURE=os.path.abspath(FIXTURE))
        proc = subprocess.run([node, "harness.mjs"], cwd=tmp, capture_output=True,
                              text=True, timeout=120, env=env)
        sys.stdout.write(proc.stdout)
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr)
            print("FAIL: chat list test")
            return 1
    print("PASS: chat list test")
    return 0


if __name__ == "__main__":
    sys.exit(main())
